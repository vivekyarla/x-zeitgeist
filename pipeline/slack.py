"""Post the thesis, today's recap, and the page link to a Slack channel via an incoming webhook.

Set SLACK_WEBHOOK_URL (a repo secret) and turn Slack on in settings.json. How often it
posts is `slack.frequency`:
  "daily"        once a day, on the first update at or after `hour_pt` (optionally weekdays only)
  "on_change"    whenever the thesis changes
  "every_update" every run (every 3 hours)
The daily post is checked hourly by the deliver workflow (`run --deliver`), so it lands
within the hour instead of waiting for the next 3-hourly update.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import config, recap, thesis


def _mrkdwn(text: str) -> str:
    """Escape for Slack and turn the thesis's **emphasis** into Slack bold."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _thesis_mrkdwn(text: str, themes: list[dict]) -> str:
    """Key phrases in bold, linked to their theme on the page when we know the page URL."""
    out = []
    for chunk, key, idx in thesis.segments(text, themes):
        esc = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if key and idx is not None and config.PAGE_URL:
            out.append(f"*<{config.PAGE_URL}#theme-{idx + 1}|{esc.replace('|', '/')}>*")
        else:
            out.append(f"*{esc}*" if key else esc)
    return "".join(out)


def _plain(text: str, n: int) -> str:
    text = re.sub(r"https?://\S+", "", text).replace("\n", " ").strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def due(state: dict, now: datetime, thesis_changed: bool, deliver: bool = False) -> bool:
    """deliver: the hourly check between updates, which only handles the daily post."""
    s = config.SLACK
    if not s["enabled"] or not state.get("latest"):  # a new week with no read yet: wait for it
        return False
    freq = s["frequency"]
    if deliver and freq != "daily":
        return False
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
        # The latest read is at most 3 hours old, so posting it as soon as the hour comes is fine.
        return local.hour >= int(s["hour_pt"]) and not already
    return False


SECTION_MAX, BLOCKS_MAX = 3000, 50  # Slack's limits on section text and blocks per message


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn",
                                        "text": text if len(text) <= SECTION_MAX else text[:SECTION_MAX - 1] + "…"}}


def message(state: dict, now: datetime | None = None) -> dict:
    latest, tweets = state["latest"], state["tweets"]
    r = recap.recap(state, now)
    themes = [{**th, "tweets": [tweets[i] for i in th["tweet_ids"] if i in tweets]} for th in latest.get("themes", [])]
    today = r["today"][:int(config.SLACK["top_tweets"])]
    fresh = sum(t["new"] for t in today)
    label = ("*New today*" if fresh == len(today) else "*New today, plus the week's best*" if fresh
             else "*Top tweets this week*")
    lines = [f"• <{t['url']}|@{t['author']}>: {_mrkdwn(_plain(t['text'], 140))}  _{t['likes']:,} likes_"
             for t in today]
    blocks = [
        _section(f"*{config.SITE_TITLE}* · {r['day_label']}"),
        _section(f">{_thesis_mrkdwn(latest['thesis'], themes)}"),
    ]
    if latest.get("themes"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(
            _mrkdwn(th["name"]) for th in latest["themes"])[:SECTION_MAX]}]})
    if lines:
        blocks.append(_section(label + "\n" + "\n".join(lines)))
    blocks = blocks[:BLOCKS_MAX - 1]
    if config.PAGE_URL:
        blocks.append({"type": "actions", "elements": [{"type": "button", "url": config.PAGE_URL,
                                                        "text": {"type": "plain_text", "text": "Open the full site"}}]})
    return {"text": f"{config.SITE_TITLE} · {r['day_label']}: {thesis.plain(latest['thesis'])}",
            "blocks": blocks, "unfurl_links": False}


# What Slack's webhook error bodies mean, and how to fix each.
ERRORS = {
    "no_service": "The webhook was revoked or the app was removed. Create a new webhook and paste it in Settings → Daily recap.",
    "invalid_token": "The webhook URL isn't valid anymore. Create a new webhook and paste it in Settings → Daily recap.",
    "no_team": "The Slack workspace for this webhook is gone. Create a new webhook and paste it in Settings → Daily recap.",
    "team_disabled": "The Slack workspace for this webhook is gone. Create a new webhook and paste it in Settings → Daily recap.",
    "channel_not_found": "The webhook's channel no longer exists. Create a new webhook for the right channel and paste it in Settings → Daily recap.",
    "channel_is_archived": "The webhook's channel was archived. Unarchive it, or create a webhook for another channel and paste it in Settings → Daily recap.",
    "action_prohibited": "A Slack admin has restricted posting to that channel. Ask an admin to allow the app, or use another channel.",
    "posting_to_general_channel_denied": "Only admins can post to that channel. Pick another channel for the webhook.",
    "invalid_payload": "Slack rejected the message format. This is a bug on our side; the next post may work, and the log has details.",
    "too_many_attachments": "Slack rejected the message as too long. This is a bug on our side; the log has details.",
}


def _fail(state: dict, why: str) -> bool:
    state["slack_last_error"] = why
    print(f"slack: {why}")
    return False


def post(state: dict, now: datetime) -> bool:
    """Post the recap. Never raises: failures land in state["slack_last_error"] for the page to show."""
    url = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("slack: no SLACK_WEBHOOK_URL set, skipping")
        return False
    if not url.startswith("https://hooks.slack.com/"):
        return _fail(state, "The Slack webhook should start with https://hooks.slack.com/. "
                            "Copy it from your Slack app's Incoming Webhooks page into Settings → Daily recap.")
    try:
        body = message(state, now)
    except Exception as e:  # a bad state shouldn't take down the run
        return _fail(state, f"Couldn't build the Slack message ({e}).")
    why = "Slack didn't answer."
    for attempt in range(3):
        wait = 2 ** attempt * 5
        try:
            r = requests.post(url, json=body, timeout=30)
        except requests.RequestException as e:
            why = f"Couldn't reach Slack ({type(e).__name__}). It will try again on the next check."
        else:
            if r.status_code == 200:
                state["slack_last_post"] = now.isoformat()
                state.pop("slack_last_error", None)
                print("slack: posted")
                return True
            text = r.text.strip()[:200]
            print(f"slack: {r.status_code} {text}")
            if r.status_code == 429:
                why = "Slack is rate-limiting this webhook. It will try again on the next check."
                try:
                    wait = min(int(r.headers.get("Retry-After", wait)), 60)
                except ValueError:
                    pass
            elif r.status_code >= 500:
                why = f"Slack had a server error ({r.status_code}). It will try again on the next check."
            else:  # 4xx other than 429: retrying won't help
                return _fail(state, ERRORS.get(text, f"Slack refused the post ({r.status_code} {text or 'no reason given'})."))
        if attempt < 2:
            time.sleep(wait)
    return _fail(state, why)
