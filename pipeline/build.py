"""Render the weekly state into public/index.html (+ public/data.json)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from . import config

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _compact(n: int) -> str:
    for div, suf in ((1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return f"{v:.1f}".rstrip("0").rstrip(".") + suf
    return str(n)


def _emphasis(text: str) -> Markup:
    """Escape, then render the writer's **key phrases** as <strong>."""
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", str(escape(text))))


def _plain(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


def _tweet_date(raw: str) -> str:
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(raw.replace("Z", "+0000"), fmt)
            return f"{d:%b} {d.day}, {d.year}"
        except ValueError:
            continue
    return ""


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(ZoneInfo(config.TIMEZONE))


def build_site(state: dict, out_dir: Path, demo: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = state.get("latest")
    tweets = state["tweets"]

    ws = _local(state["week_start"])
    week_label = f"{ws:%b} {ws.day}–{(ws + timedelta(days=6)):%b} {(ws + timedelta(days=6)).day}"

    themes = []
    if latest:
        for th in latest["themes"]:
            items = [tweets[i] for i in th["tweet_ids"] if i in tweets][:config.TWEETS_PER_THEME_SHOWN]
            themes.append({**th, "tweets": items})

    history = [
        {"when": f"{_local(h['at']):%a} {_local(h['at']):%-I:%M %p}".replace(":00 ", " "), "thesis": h["thesis"]}
        for h in reversed(state.get("thesis_history", [])[:-1])
    ]

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    env.filters["compact"] = _compact
    env.filters["emphasis"] = _emphasis
    env.filters["plain"] = _plain
    env.filters["tweet_date"] = _tweet_date
    env.globals["topic_label"] = lambda k: config.TOPIC_LABELS.get(k, k)
    html = env.get_template("index.html").render(
        title=config.SITE_TITLE,
        week_label=week_label,
        latest=latest,
        updated_iso=latest["updated_at"] if latest else None,
        updated_label=f"{_local(latest['updated_at']):%a %-I:%M %p} PT" if latest else None,
        themes=themes,
        history=history,
        demo=demo,
        repo=config.REPO,
        settings=config.SETTINGS,
    )
    (out_dir / "index.html").write_text(html)
    # Machine-readable copy, handy for a later Notion sync.
    (out_dir / "data.json").write_text(json.dumps({
        "week_start": state["week_start"],
        "latest": latest,
        "thesis_history": state.get("thesis_history", []),
        "themes": [{**{k: v for k, v in th.items() if k != "tweets"},
                    "tweets": [{k: t[k] for k in ("id", "url", "text", "author", "likes", "retweets", "jev")}
                               for t in th["tweets"]]} for th in themes],
    }, ensure_ascii=False, indent=1))
