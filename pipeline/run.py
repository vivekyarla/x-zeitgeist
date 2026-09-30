"""One update cycle. Run with:  python -m pipeline.run   (or --demo for sample data)"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from . import slack
from .build import build_site

ROOT = Path(__file__).resolve().parent.parent
DATA = config.DATA_DIR    # TIMELINE_DATA, default data/
OUT = config.OUT_DIR      # TIMELINE_OUT, default public/
STATE = DATA / "week.json"


def week_start(now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(config.TIMEZONE))
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.astimezone(timezone.utc)


def load_state(ws: datetime) -> dict:
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state["week_start"] == ws.isoformat():
            return state
        # New week: archive last week's state.
        (DATA / "archive").mkdir(parents=True, exist_ok=True)
        shutil.move(STATE, DATA / "archive" / f"{state['week_start'][:10]}.json")
    return {"week_start": ws.isoformat(), "tweets": {}, "thesis_history": [], "latest": None}


def _posted(t: dict) -> datetime | None:
    try:
        return datetime.strptime(t["created_at"], "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, KeyError):
        return None


def fetch(ws: datetime, state: dict, now: datetime, until: datetime | None = None,
          replay: bool = False) -> list[dict]:
    """Collect candidate tweets and record each story's reach across the panel.

    Sources: the timeline panel (primary: what the scene is posting, quoting, and
    retweeting), plus keyword searches, the watchlist, and tracked companies. Every quote
    or retweet is traced back to its original, so a story is found by who's talking about
    it, whatever words it uses.
    """
    from . import panel as panel_mod
    from .sources import TwitterApiIo
    src = TwitterApiIo()
    end = until or now
    since = f"since_time:{int(ws.timestamp())}" + (f" until_time:{int(until.timestamp())}" if until else "")
    pan = panel_mod.refresh(src, now)
    panel_set = {h.lower() for h in panel_mod.handles(pan)}
    reach: dict[str, set] = {k: set(v) for k, v in state.get("reach", {}).items()}
    seen: dict[str, dict] = {}
    counts = collections.Counter()

    def add(t: dict) -> None:
        posted = _posted(t)
        if not t["id"] or not t["text"] or (posted and not (ws - timedelta(days=2) <= posted <= end)):
            return
        old = seen.get(t["id"])
        if old:
            for k in ("likes", "retweets", "replies", "quotes", "views"):
                old[k] = max(old[k], t[k])
        else:
            seen[t["id"]] = t

    def ingest(t: dict, via: str) -> None:
        author = t["author"]["handle"].lower()
        in_panel = author in panel_set
        counts[via] += 1
        if t["ref"]:  # a quote or retweet: credit the original with this account's attention
            orig = t["ref"]
            orig.setdefault("story", orig["id"])
            add({k: v for k, v in orig.items() if k != "ref"})
            if in_panel:
                reach.setdefault(orig["id"], set()).add(author)
            if t["is_retweet"]:
                return
            t["story"] = orig["id"]
        else:
            t["story"] = t["id"]
        if in_panel:
            reach.setdefault(t["story"], set()).add(author)
            for u in t["urls"]:
                reach.setdefault("url:" + u, set()).add(author)
        add({k: v for k, v in t.items() if k != "ref"})

    def run_query(q: str, qt: str, pages: int, via: str) -> None:
        try:
            for t in src.search(q, qt, pages):
                ingest(t, via)
        except Exception as e:  # one bad query shouldn't kill the run
            print(f"  search failed ({e}): {q[:80]}")

    # 1. The panel: its most-engaged posts and quotes this week (re-fetched every run, so
    #    counts stay current), and its recent retweets (reach).
    hs = panel_mod.handles(pan)
    n = int(config.PANEL["handles_per_query"])
    rt_since = ws if replay else max(ws, now - timedelta(hours=8))
    rt_window = f"since_time:{int(rt_since.timestamp())}" + (f" until_time:{int(until.timestamp())}" if until else "")
    for i in range(0, len(hs), n):
        who = " OR ".join(f"from:{h}" for h in hs[i:i + n])
        run_query(f"({who}) -filter:replies {since}", "Top", int(config.PANEL["pages_per_batch"]), "panel")
        run_query(f"({who}) filter:nativeretweets {rt_window}", "Latest", 10 if replay else int(config.PANEL["retweet_pages"]), "panel retweets")

    # 2. Keyword searches, watchlist, and tracked companies (supplements).
    for q in config.SEARCH_QUERIES:
        run_query(f"{q} {since}", "Top", config.PAGES_PER_QUERY, "searches")
    for i in range(0, len(config.WATCHLIST), 6):
        who = " OR ".join(f"from:{h}" for h in config.WATCHLIST[i:i + 6])
        run_query(f"({who}) min_faves:{config.WATCHLIST_MIN_FAVES} -filter:replies {since}", "Latest",
                  config.PAGES_PER_QUERY, "watchlist")
    for i in range(0, len(config.TRACKED), 6):  # company accounts: every original post, judged vs their usual
        who = " OR ".join(f"from:{h}" for h in config.TRACKED[i:i + 6])
        run_query(f"({who}) -filter:replies -filter:retweets {since}", "Latest", config.PAGES_PER_QUERY, "tracked")
    for t in refresh_baselines(src, state, now):  # their recent posts double as candidates
        if (_posted(t) or ws) >= ws:
            ingest(t, "tracked")

    state["reach"] = {k: sorted(v) for k, v in reach.items()}
    print("  fetched by source:", dict(counts), f"| panel: {len(panel_set)} accounts")
    return list(seen.values())


def refresh_baselines(src, state: dict, now: datetime) -> list[dict]:
    """Once a day, record each tracked account's median likes over its ~20 latest posts."""
    baselines = state.setdefault("baselines", {})
    recent: list[dict] = []
    for h in config.TRACKED:
        b = baselines.get(h.lower())
        if b and now - datetime.fromisoformat(b["at"]) < timedelta(hours=20):
            continue
        try:
            posts = src.last_tweets(h)
        except Exception as e:
            print(f"  tracked @{h}: couldn't load recent posts ({e}); "
                  + ("keeping the last baseline" if b and b.get("n") else "no baseline yet"))
            if not (b and b.get("n")):
                baselines[h.lower()] = {"at": (now - timedelta(hours=19)).isoformat(), "n": 0,
                                        "error": str(e)[:120]}  # retry within the hour
            continue
        likes = [t["likes"] for t in posts]
        baselines[h.lower()] = {"at": now.isoformat(), "n": len(likes),
                                "median": statistics.median(likes) if likes else 0}
        print(f"  tracked @{h}: usual {baselines[h.lower()]['median']:,.0f} likes over {len(likes)} posts"
              if likes else f"  tracked @{h}: no recent posts found (check the handle)")
        recent += posts
    return recent


def score(state: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (kept, top, near_misses) from the saved verdicts."""
    from .judge import rank_all, select, why_not
    baselines = state.get("baselines", {})
    judged = [t for t in state["tweets"].values() if "jev" in t]
    rank_all(judged, baselines, state.get("reach", {}))
    reasons = {t["id"]: why_not(t, baselines) for t in judged}
    kept = [t for t in judged if reasons[t["id"]] is None]
    top = select(kept, config.TWEETS_FOR_THESIS)
    top_ids = {t["id"] for t in top}
    misses = sorted((t for t in judged if t["id"] not in top_ids), key=lambda t: t["rank"], reverse=True)[:40]
    near = [{"id": t["id"], "reason": reasons[t["id"]] or f"ranked below the top {config.TWEETS_FOR_THESIS}"} for t in misses]
    return kept, top, near


def update(state: dict, now: datetime, until: datetime | None = None, replay: bool = False) -> bool:
    """Run one cycle. Returns True if the thesis changed."""
    from .judge import Jev
    from .synthesize import synthesize

    ws = datetime.fromisoformat(state["week_start"])
    fetched = fetch(ws, state, now, until, replay)
    print(f"fetched {len(fetched)} tweets")

    tweets = state["tweets"]
    new = []
    for t in fetched:
        if t["id"] in tweets:  # refresh engagement counts, keep Jev's verdict
            old = tweets[t["id"]]
            for k in ("likes", "retweets", "replies", "quotes", "views"):
                old[k] = max(old[k], t[k])
            for k in ("story", "urls"):
                if t.get(k) and not old.get(k):
                    old[k] = t[k]
        else:
            t["first_seen"] = now.isoformat()
            new.append(t)
    # Saved tweets judged under older questions get another pass.
    stale = [t for t in tweets.values() if t["jev"].get("v") != config.JUDGE_VERSION]

    verdicts = Jev().judge_many(new + stale)
    for t in new + stale:
        if t["id"] in verdicts:
            t["jev"] = verdicts[t["id"]]
            tweets[t["id"]] = t
    print(f"judged {len(verdicts)} tweets ({len(new)} new, {len(stale)} re-judged)")
    if (new or stale) and not verdicts:
        raise SystemExit("Jev returned no verdicts at all; check the API key (see errors above)")

    kept, top, near = score(state)
    print("sent to writer by topic:", dict(collections.Counter(t["jev"]["topic"] for t in top).most_common()))
    if not top:
        print("nothing passed the filter; leaving the page as it was")
        return False

    prev = state["latest"]["thesis"] if state["latest"] else None
    out = synthesize(top, prev, state.get("baselines", {}))

    changed = not state["thesis_history"] or (out.get("thesis_changed", True) and out["thesis"] != prev)
    if changed:
        state["thesis_history"].append({"at": now.isoformat(), "thesis": out["thesis"]})
    state["latest"] = {
        "thesis": out["thesis"],
        "themes": out["themes"],
        "top_ids": [t["id"] for t in top],
        "updated_at": now.isoformat(),
        "stats": {"scanned": len(tweets), "kept": len(kept)},
        "near_misses": near,
    }
    from . import recall
    state["latest"]["known_stories"] = recall.report(state)
    return bool(changed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render sample data, no API calls")
    ap.add_argument("--build-only", action="store_true", help="re-render the page from saved state")
    ap.add_argument("--slack", choices=["auto", "now", "skip"], default="auto",
                    help="auto: post if the profile's Slack schedule says so; now: post regardless")
    ap.add_argument("--deliver", action="store_true",
                    help="only post the daily Slack read if it's due, from saved state (no fetching, no build)")
    ap.add_argument("--week", help="replay a past week (its Monday, YYYY-MM-DD) from scratch; implies --dry-run")
    ap.add_argument("--dry-run", action="store_true",
                    help="do a full update and build the page, but save nothing and post nothing; "
                         "writes public/compare.json (before vs after)")
    args = ap.parse_args()

    if args.week:  # replay: fresh state for that week, fetched as if at the week's end
        ws = datetime.fromisoformat(args.week).replace(tzinfo=ZoneInfo(config.TIMEZONE)).astimezone(timezone.utc)
        until = min(ws + timedelta(days=7), datetime.now(timezone.utc))
        state = {"week_start": ws.isoformat(), "tweets": {}, "thesis_history": [], "latest": None}
        update(state, until, until=until, replay=True)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "compare.json").write_text(json.dumps(
            compare({}, [], state), indent=1, ensure_ascii=False))
        build_site(state, OUT)
        print(f"replay of week {args.week}: built {OUT}/ and compare.json; nothing saved or posted")
        return
    if args.deliver:  # hourly, so the daily post lands on time between 3-hourly updates
        now = datetime.now(timezone.utc)
        state = load_state(week_start(now))
        if args.slack == "now" or (args.slack == "auto" and slack.due(state, now, False, deliver=True)):
            if state.get("latest"):
                slack.post(state, now)
        else:
            print("slack: nothing due")
        DATA.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
        return
    if args.demo:
        state = json.loads((ROOT / "pipeline" / "demo_state.json").read_text())
    else:
        now = datetime.now(timezone.utc)
        state = load_state(week_start(now))
        if args.dry_run:
            before = json.loads(json.dumps(state.get("latest") or {}))
            before_top = [state["tweets"][i] for i in before.get("top_ids", []) if i in state["tweets"]]
            update(state, now)
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "compare.json").write_text(json.dumps(
                compare(before, before_top, state), indent=1, ensure_ascii=False))
            build_site(state, OUT)
            print(f"dry run: built {OUT}/ and compare.json; nothing saved or posted")
            return
        changed = False
        try:
            if not args.build_only:
                changed = update(state, now)
            if args.slack == "now" or (args.slack == "auto" and slack.due(state, now, changed)):
                if state.get("latest"):
                    slack.post(state, now)
        finally:  # keep Jev verdicts even if a later step fails
            DATA.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    build_site(state, OUT, demo=args.demo)
    print(f"built {OUT / 'index.html'}")


def compare(before: dict, before_top: list[dict], state: dict) -> dict:
    """Before vs after, for reviewing a tuning change."""
    def side(latest: dict, top: list[dict]) -> dict:
        def band(f):
            return "<10K" if f < 1e4 else "10-100K" if f < 1e5 else "100K-1M" if f < 1e6 else "1M+"
        return {
            "thesis": latest.get("thesis"),
            "themes": [{"name": th["name"], "summary": th["summary"], "tweets": [
                f"@{state['tweets'][i]['author']['handle']}: {state['tweets'][i]['text'][:100]}"
                for i in th["tweet_ids"][:6] if i in state["tweets"]]} for th in latest.get("themes", [])],
            "top_by_topic": dict(collections.Counter(t["jev"]["topic"] for t in top).most_common()),
            "top_by_followers": dict(collections.Counter(band(t["author"]["followers"]) for t in top)),
            "top": [f"{t['jev']['topic']:<16} {t['likes']:>6} likes  @{t['author']['handle']}: "
                    f"{t['text'][:80]}" for t in top],
        }
    after = state["latest"] or {}
    after_top = [state["tweets"][i] for i in after.get("top_ids", []) if i in state["tweets"]]
    from .judge import why_not
    tracked = {h: state.get("baselines", {}).get(h.lower()) for h in config.TRACKED}
    company_posts = sorted((t for t in state["tweets"].values()
                            if "jev" in t and t["author"]["handle"].lower() in config.TRACKED_LOWER),
                           key=lambda t: -t["likes"])
    tracked_posts = [f"{'IN ' if t['id'] in after.get('top_ids', []) else 'ok ' if not why_not(t, state.get('baselines', {})) else 'no '}"
                     f"{t['likes']:>5} likes @{t['author']['handle']}: {(why_not(t, state.get('baselines', {})) or '')[:50]:<50} "
                     f"{t['text'][:60]}" for t in company_posts]
    return {"before": side(before, before_top), "after": side(after, after_top),
            "known_stories": after.get("known_stories", []),
            "tracked_baselines": tracked, "tracked_posts": tracked_posts,
            "near_misses": [f"{m['reason']:<44} @{state['tweets'][m['id']]['author']['handle']}: "
                            f"{state['tweets'][m['id']]['text'][:70]}" for m in after.get("near_misses", [])]}


if __name__ == "__main__":
    main()
