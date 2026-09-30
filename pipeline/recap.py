"""The daily recap: the thesis, what's new today, and the themes, as a small feed.

build.py publishes it as recap.json and recap.md next to the page every update, for Slack
and for personal agents (Instinct, Meta Muse) that fetch a URL on a daily schedule.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, thesis

TODAY_MAX, TODAY_MIN = 5, 3
AGENT_NOTE = ("If you're an assistant delivering this: send the thesis, 2-3 of the 'New today' items, "
              "and the full-site link. Keep it short and keep the links.")


def _local(d: datetime) -> datetime:
    return d.astimezone(ZoneInfo(config.TIMEZONE))


def _day(d: datetime) -> str:
    return f"{d:%a %b} {d.day}"


def _plain(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", text)).strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _links() -> tuple[str, dict]:
    """Page URL (relative when PAGE_URL isn't set, e.g. locally) and the feed links.

    The feed lives at TIMELINE_RECAP_URL when set (a private, token URL that agents can
    read without signing in), else next to the page."""
    def slash(u: str) -> str:
        return u if not u or u.endswith("/") else u + "/"
    base, feed = slash(config.PAGE_URL), slash(config.RECAP_URL)
    page = base or "index.html"
    return page, {"page": page, "recap_json": feed + "recap.json", "recap_md": feed + "recap.md"}


def recap(state: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    local = _local(now)
    latest, tweets = state.get("latest"), state["tweets"]
    ws = _local(datetime.fromisoformat(state["week_start"]))
    page, links = _links()
    out = {
        "date": local.date().isoformat(),
        "day_label": _day(local),
        "week_label": f"{ws:%b} {ws.day}–{(ws + timedelta(days=6)):%b} {(ws + timedelta(days=6)).day}",
        "updated_at": latest["updated_at"] if latest else None,
        "page_url": config.PAGE_URL,
        "thesis": thesis.plain(latest["thesis"]) if latest else None,
        "thesis_changed_today": any(_local(datetime.fromisoformat(h["at"])).date() == local.date()
                                    for h in state.get("thesis_history", [])),
        "today": [],
        "themes": [],
        "links": links,
    }
    if not latest:
        return out

    themes = latest.get("themes", [])
    theme_of = {}
    for th in themes:
        for i in th["tweet_ids"]:
            theme_of.setdefault(i, th["name"])
    top = [tweets[i] for i in latest["top_ids"] if i in tweets]
    since = datetime.fromisoformat(latest["updated_at"]) - timedelta(hours=24)

    def is_new(t: dict) -> bool:
        try:
            return datetime.fromisoformat(t["first_seen"]) >= since
        except (KeyError, ValueError):
            return False

    picked = [t for t in top if is_new(t)][:TODAY_MAX]
    if len(picked) < TODAY_MIN:  # a quiet day: fill with the week's best, marked as not new
        picked += [t for t in top if t not in picked][:TODAY_MIN - len(picked)]
        picked.sort(key=top.index)
    out["today"] = [{
        "author": t["author"]["handle"],
        "name": t["author"].get("name", ""),
        "text": _plain(t["text"], 220),
        "url": t["url"],
        "likes": t["likes"],
        "theme": theme_of.get(t["id"]),
        "new": is_new(t),
    } for t in picked]
    out["themes"] = [{"name": th["name"], "summary": th.get("summary", ""), "url": f"{page}#theme-{n}"}
                     for n, th in enumerate(themes, 1)]
    return out


def markdown(r: dict, limit: int = 2500) -> str:
    """Short markdown version for people and agents. Stays under `limit` chars."""
    head = [f"# X thesis · {r['day_label']}", f"> {AGENT_NOTE}", "",
            f"_{config.SITE_TITLE}, week of {r['week_label']}_", ""]
    foot = ["", f"Full site: {r['links']['page']}"]
    if not r["thesis"]:
        return "\n".join(head + ["Nothing yet this week; the first read lands after the next update."] + foot) + "\n"
    body = [r["thesis"], ""]
    if r["today"]:
        body.append("## New today" if any(t["new"] for t in r["today"])
                    else "## Top this week (nothing new in the last 24 hours)")
    items = [f"- @{t['author']}: {t['text']} — {t['likes']:,} likes — {t['url']}"
             + ("" if t["new"] else " (earlier this week)") for t in r["today"]]
    themes = ["", "## Themes"] + [f"- **{th['name']}**: {_plain(th['summary'], 160)}" for th in r["themes"]]
    # Drop items from the end until it fits, then shorten the rest if it still doesn't.
    while True:
        text = "\n".join(head + body + items + (themes if r["themes"] else []) + foot) + "\n"
        if len(text) <= limit or not (items or len(themes) > 2):
            return text[:limit]
        if len(items) > 2:
            items.pop()
        elif len(themes) > 2:
            themes.pop()
        else:
            items.pop()
