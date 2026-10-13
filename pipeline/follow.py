"""Weekly follow recommendations: for each person in engage.json, accounts worth following that are
popular, match their interests, and are active enough to engage with. Sent as a Slack DM by the
same bot as the daily engagement picks.

    python -m pipeline.follow            # build the recommendations and print the message
    python -m pipeline.follow --send     # build and DM it

1. Fetch the last `window_days` from engage.json's searches and accounts (same sources as the daily picks).
2. Group by author. Keep authors with `min_followers` (or `min_followers_if_affiliated` and a bio tying them
   to a prominent company) who posted at least `min_posts` times with real engagement; drop the team.
3. Per person with an X handle: drop accounts they already follow. For everyone: drop accounts suggested
   in the last `repeat_after_weeks`.
4. One model call picks `per_person` accounts for each person, with a few words of why.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .engage import ENDPOINT, ROOT, _esc, fetch, load_config, org_of, send

DEFAULTS = {"per_person": 5, "window_days": 7, "min_followers": 5000, "min_followers_if_affiliated": 1000,
            "min_posts": 2, "min_likes": 50, "max_following_pages": 10, "repeat_after_weeks": 8,
            "candidates_for_model": 150}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("follow") or {})}


# ---------------------------------------------------------------------------
def authors(tweets: list[dict], cfg: dict, f: dict) -> list[dict]:
    """Prominent, active authors from the window, best first."""
    team = {h.lower() for h in cfg.get("exclude_handles", [])} | \
           {p["x"].lower() for p in cfg.get("people", []) if p.get("x")}
    by: dict[str, dict] = {}
    for t in tweets:
        a = t["author"]
        h = a["handle"]
        if not h or h.lower() in team or t["likes"] < int(f["min_likes"]):
            continue
        e = by.setdefault(h.lower(), {"handle": h, "name": a["name"], "followers": a["followers"],
                                      "bio": a.get("bio") or "", "posts": [], "engagement": 0.0})
        e["followers"] = max(e["followers"], a["followers"])
        e["posts"].append({"text": re.sub(r"\s+", " ", t["text"])[:240], "likes": t["likes"], "url": t["url"]})
        e["engagement"] += math.log1p(t["likes"] + 2 * t["retweets"] + 3 * t["quotes"])
    out = []
    for e in by.values():
        e["org"] = org_of({"handle": e["handle"], "bio": e["bio"]}, cfg.get("prominent_orgs", []))
        need = int(f["min_followers_if_affiliated"]) if e["org"] else int(f["min_followers"])
        if e["followers"] < need or len(e["posts"]) < int(f["min_posts"]):
            continue
        e["posts"].sort(key=lambda p: p["likes"], reverse=True)
        e["posts"] = e["posts"][:3]
        out.append(e)
    out.sort(key=lambda e: e["engagement"], reverse=True)
    print(f"{len(by)} authors in the window, {len(out)} prominent and active")
    return out


def already_following(cfg: dict, f: dict, src=None) -> dict[str, set[str] | None]:
    """Who each person follows now (lowercased handles), or None when unknown (no handle, or the fetch failed)."""
    if src is None:
        from .sources import TwitterApiIo
        src = TwitterApiIo()
    out = {}
    for p in cfg["people"]:
        h = (p.get("x") or "").lstrip("@")
        if not h:
            out[p["name"]] = None
            continue
        try:
            out[p["name"]] = {u["handle"].lower() for u in src.followings(h, int(f["max_following_pages"]))}
            print(f"followings: @{h} follows {len(out[p['name']])}")
        except Exception as e:
            print(f"followings: couldn't load @{h} ({type(e).__name__}); can't skip accounts they already follow")
            out[p["name"]] = None
    return out


def load_history(path: Path, now: datetime, weeks: int) -> dict[str, dict[str, str]]:
    """{person: {handle_lower: iso date}} for suggestions within the last `weeks`."""
    try:
        h = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = (now - timedelta(weeks=weeks)).date().isoformat()
    return {p: {k: d for k, d in v.items() if d >= cutoff} for p, v in h.items()}


SYSTEM = """You recommend X accounts for each person on a startup's team to follow this week.

You get the company, each person's profile, and candidate accounts: popular accounts (followers, bio,
company if known) that posted on these topics in the last week, with their best posts. Some candidates are
marked as not allowed for a given person (they already follow them, or were suggested recently).

For each person, pick up to {n} accounts that (1) match their interests and role, (2) are worth following
for a long time, not just one viral post, and (3) post things this person could credibly reply to or quote,
so following them helps their engagement. Prefer a mix: a couple of big names and a couple of rising,
highly relevant voices. Never pick an account marked not allowed for that person.

Each "why" is at most 12 words: what they post and why it fits this person.

Return only JSON:
{{"people": [{{"name": "<name>", "picks": [{{"handle": "<handle, no @>", "why": "<=12 words"}}]}}]}}"""


def ask_model(cfg: dict, cands: list[dict], blocked: dict[str, set[str]], f: dict) -> dict:
    payload = json.dumps({
        "company": cfg.get("company"),
        "people": [{k: p.get(k) for k in ("name", "role", "profile", "engage_on")} for p in cfg["people"]],
        "candidates": [{"handle": c["handle"], "name": c["name"], "followers": c["followers"], "org": c["org"],
                        "bio": c["bio"][:200], "posts": [{"text": p["text"], "likes": p["likes"]} for p in c["posts"]],
                        "not_allowed_for": sorted(n for n, hs in blocked.items() if c["handle"].lower() in hs)}
                       for c in cands],
    }, ensure_ascii=False)
    system = SYSTEM.format(n=int(f["per_person"]))
    for attempt in range(2):
        r = requests.post(ENDPOINT, timeout=180, headers={
            "Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY'].strip()}", "Content-Type": "application/json"},
            json={"model": cfg.get("model", "anthropic/claude-sonnet-5"), "temperature": 0.3, "max_tokens": 10000,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": payload}]})
        if r.status_code != 200:
            print(f"model {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        text = re.sub(r"^```(?:json)?|```$", "", (r.json()["choices"][0]["message"]["content"] or "").strip(), flags=re.M).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"model returned bad JSON ({e}): {text[:200]!r}")
    raise RuntimeError("the model didn't return valid JSON twice in a row")


def clean(out: dict, cfg: dict, cands: list[dict], blocked: dict[str, set[str]], f: dict) -> list[dict]:
    """Only real candidates, nothing a person already follows or was just sent, the configured count."""
    by = {c["handle"].lower(): c for c in cands}
    want = {p["name"].lower(): p["name"] for p in cfg["people"]}
    got = {}
    for p in out.get("people", []):
        name = want.get(str(p.get("name", "")).lower())
        if not name:
            continue
        picks, seen = [], set()
        for x in p.get("picks", []):
            h = str(x.get("handle", "")).lstrip("@").lower()
            if h in by and h not in seen and h not in blocked.get(name, set()):
                seen.add(h)
                picks.append({"handle": by[h]["handle"], "why": " ".join(str(x.get("why") or "").split()[:14])})
        got[name] = picks[: int(f["per_person"])]
    return [{"name": p["name"], "picks": got.get(p["name"], [])} for p in cfg["people"]]


def _compact(n: int) -> str:
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M") if n >= 1_000_000 else f"{round(n / 1000)}K" if n >= 1000 else str(n)


def message(recs: list[dict], cands: list[dict], cfg: dict, unknown: list[str], now: datetime, tz: str) -> dict:
    by = {c["handle"].lower(): c for c in cands}
    local = now.astimezone(ZoneInfo(tz))
    monday = local - timedelta(days=local.weekday())
    title = f"Who to follow · week of {monday:%b} {monday.day}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}}]
    labels = {p["name"]: p.get("label") or "" for p in cfg["people"]}
    for p in recs:
        lines = []
        for x in p["picks"]:
            c = by[x["handle"].lower()]
            who = f"{_esc(c['name'])}, {_compact(c['followers'])}" + (f", {_esc(c['org'])}" if c.get("org") else "")
            lines.append(f"• <https://x.com/{c['handle']}|@{_esc(c['handle'])}> ({who})  _{_esc(x['why'])}_")
        if not lines:
            lines.append("_No strong new suggestions this week._")
        head = f"*{_esc(p['name'])}*" + (f" · {_esc(labels[p['name']])}" if labels.get(p["name"]) else "")
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (head + "\n" + "\n".join(lines))[:2990]}})
    if unknown:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
            "Couldn't check who " + ", ".join(unknown) + " already follow (no X handle in engage.json), so a few may be repeats."}]})
    return {"text": title, "blocks": blocks[:50], "unfurl_links": False, "unfurl_media": False}


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="DM it on Slack")
    ap.add_argument("--config", help="path to the config (default engage.json)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    f = settings(cfg)
    tz = os.getenv("ENGAGE_TIMEZONE", "America/Los_Angeles")
    now = datetime.now(timezone.utc)
    out = Path(os.getenv("ENGAGE_OUT") or ROOT / "public" / "engage")
    out.mkdir(parents=True, exist_ok=True)

    tweets = fetch({**cfg, "window_hours": 24 * int(f["window_days"]), "pages_per_search": 3}, now)
    cands = authors(tweets, cfg, f)[: int(f["candidates_for_model"])]
    if not cands:
        raise SystemExit("no candidate accounts this week (every search failed, or nothing passed the filters)")
    following = already_following(cfg, f)
    history = load_history(out / "follow_history.json", now, int(f["repeat_after_weeks"]))
    blocked = {p["name"]: (following.get(p["name"]) or set()) | set(history.get(p["name"], {})) for p in cfg["people"]}
    recs = clean(ask_model(cfg, cands, blocked, f), cfg, cands, blocked, f)

    today = now.date().isoformat()
    for p in recs:
        history.setdefault(p["name"], {}).update({x["handle"].lower(): today for x in p["picks"]})
    unknown = [p["name"] for p in cfg["people"] if following.get(p["name"]) is None]
    msg = message(recs, cands, cfg, unknown, now, tz)
    (out / "follow_last.json").write_text(json.dumps({"at": now.isoformat(), "recs": recs, "message": msg},
                                                     ensure_ascii=False, indent=1))
    if args.send:
        send(msg)
        (out / "follow_history.json").write_text(json.dumps(history, indent=1))
    else:
        print(json.dumps(msg, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
