"""Post the thesis and top tweets to a Slack channel via an incoming webhook.

Set SLACK_WEBHOOK_URL (a repo secret) and turn Slack on in settings.json. How often it
posts is `slack.frequency`:
  "daily"        once a day, on the first update at or after `hour_pt` (optionally weekdays only)
  "on_change"    whenever the thesis changes
  "every_update" every run (every 3 hours)
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config


def _mrkdwn(text: str) -> str:
    """Escape for Slack and turn the thesis's **emphasis** into Slack bold."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _plain(text: str, n: int) -> str:
    text = re.sub(r"https?://\S+", "", text).replace("\n", " ").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def due(state: dict, now: datetime, thesis_changed: bool) -> bool:
    s = config.SLACK
    if not s["enabled"] or not state.get("latest"):
        return False
    freq = s["frequency"]
    if freq == "every_update":
        return True
    if freq == "on_change":
        return thesis_changed
    if freq == "daily":
        local = now.astimezone(ZoneInfo(config.TIMEZONE))
        if s["weekdays_only"] and local.weekday() >= 5:
            return False
        last = state.get("slack_last_post")
        already = last and datetime.fromisoformat(last).astimezone(ZoneInfo(config.TIMEZONE)).date() == local.date()
        return local.hour >= int(s["hour_pt"]) and not already
    return False


def message(state: dict) -> dict:
    latest, tweets = state["latest"], state["tweets"]
    top = [tweets[i] for i in latest["top_ids"] if i in tweets][:int(config.SLACK["top_tweets"])]
    lines = [f"• <{t['url']}|@{t['author']['handle']}>: {_mrkdwn(_plain(t['text'], 140))}  "
             f"_{t['likes']:,} likes_" for t in top]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{config.SITE_TITLE}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f">{_mrkdwn(latest['thesis'])}"}},
    ]
    if latest.get("themes"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(
            _mrkdwn(th["name"]) for th in latest["themes"])}]})
    if lines:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Top tweets*\n" + "\n".join(lines)}})
    if config.PAGE_URL:
        blocks.append({"type": "actions", "elements": [{"type": "button", "url": config.PAGE_URL,
                                                        "text": {"type": "plain_text", "text": "Open the page"}}]})
    return {"text": re.sub(r"\*\*(.+?)\*\*", r"\1", latest["thesis"]), "blocks": blocks, "unfurl_links": False}


def post(state: dict, now: datetime) -> bool:
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("slack: no SLACK_WEBHOOK_URL set, skipping")
        return False
    r = requests.post(url, json=message(state), timeout=30)
    if r.status_code != 200:
        print(f"slack: {r.status_code} {r.text[:200]}")
        return False
    state["slack_last_post"] = now.isoformat()
    print("slack: posted")
    return True
