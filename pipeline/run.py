"""One update cycle. Run with:  python -m pipeline.run   (or --demo for sample data)"""
from __future__ import annotations

import argparse
import json
import math
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .build import build_site

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
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
        (DATA / "archive").mkdir(exist_ok=True)
        shutil.move(STATE, DATA / "archive" / f"{state['week_start'][:10]}.json")
    return {"week_start": ws.isoformat(), "tweets": {}, "thesis_history": [], "latest": None}


def fetch(ws: datetime) -> list[dict]:
    from .sources import TwitterApiIo
    src = TwitterApiIo()
    since = f"since_time:{int(ws.timestamp())}"
    queries = [(f"{q} {since}", "Top") for q in config.SEARCH_QUERIES]
    for i in range(0, len(config.WATCHLIST), 6):  # keep queries short
        handles = " OR ".join(f"from:{h}" for h in config.WATCHLIST[i:i + 6])
        queries.append((f"({handles}) min_faves:{config.WATCHLIST_MIN_FAVES} -filter:replies {since}", "Latest"))
    seen: dict[str, dict] = {}
    for q, qt in queries:
        try:
            for t in src.search(q, qt, config.PAGES_PER_QUERY):
                seen[t["id"]] = t
        except Exception as e:  # one bad query shouldn't kill the run
            print(f"  search failed ({e}): {q[:80]}")
    return list(seen.values())


def update(state: dict, now: datetime) -> None:
    from .judge import Jev, engagement, passes, rank_score
    from .synthesize import synthesize

    ws = datetime.fromisoformat(state["week_start"])
    fetched = fetch(ws)
    print(f"fetched {len(fetched)} tweets")

    tweets = state["tweets"]
    new = []
    for t in fetched:
        if t["id"] in tweets:  # refresh engagement counts, keep Jev's verdict
            old = tweets[t["id"]]
            for k in ("likes", "retweets", "replies", "quotes", "views"):
                old[k] = max(old[k], t[k])
        else:
            t["first_seen"] = now.isoformat()
            new.append(t)

    verdicts = Jev().judge_many(new)
    for t in new:
        if t["id"] in verdicts:
            t["jev"] = verdicts[t["id"]]
            tweets[t["id"]] = t
    print(f"judged {len(verdicts)} new tweets")

    kept = [t for t in tweets.values() if passes(t["jev"])]
    max_log = max((math.log1p(engagement(t)) for t in kept), default=0)
    for t in kept:
        t["rank"] = rank_score(t, max_log)
    kept.sort(key=lambda t: t["rank"], reverse=True)
    top = kept[:config.TWEETS_FOR_THESIS]
    if not top:
        print("nothing passed the filter; leaving the page as it was")
        return

    prev = state["latest"]["thesis"] if state["latest"] else None
    out = synthesize(top, prev)

    if not state["thesis_history"] or (out.get("thesis_changed", True) and out["thesis"] != prev):
        state["thesis_history"].append({"at": now.isoformat(), "thesis": out["thesis"]})
    state["latest"] = {
        "thesis": out["thesis"],
        "themes": out["themes"],
        "top_ids": [t["id"] for t in top],
        "updated_at": now.isoformat(),
        "stats": {"scanned": len(tweets), "kept": len(kept)},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="render sample data, no API calls")
    ap.add_argument("--build-only", action="store_true", help="re-render the page from saved state")
    args = ap.parse_args()

    if args.demo:
        state = json.loads((ROOT / "pipeline" / "demo_state.json").read_text())
    else:
        now = datetime.now(timezone.utc)
        state = load_state(week_start(now))
        if not args.build_only:
            update(state, now)
            DATA.mkdir(exist_ok=True)
            STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False))

    build_site(state, ROOT / "public", demo=args.demo)
    print("built public/index.html")


if __name__ == "__main__":
    main()
