"""The Slack bot answering questions: when someone @mentions it in a channel or DMs it, it replies
with how to use or tweak things (mainly the GTM report behind the marketing channel digest).

Slack Events API: POST /slack/events, verified with SLACK_SIGNING_SECRET. Slack wants a 200 within
3 seconds, so the reply is written and sent from a background thread.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import time

import requests

log = logging.getLogger("timeline.slackbot")
SLACK_API = "https://slack.com/api/"
GATEWAY = "https://ai-gateway.vercel.sh/v1/chat/completions"


def verify(secret: str, timestamp: str, signature: str, body: bytes, now: float | None = None) -> bool:
    """Slack's v0 request signature, rejecting anything older than five minutes (replays)."""
    try:
        if abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    want = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature or "")


def help_doc(base_url: str, admins: tuple[str, ...], channel_time: str) -> str:
    owner = admins[0] if admins else "the site owner"
    return f"""How the X / GTM report works and how to change it.

THE GTM REPORT (what the marketing channel digest summarizes)
- Page: {base_url}/gtm/ . Anyone can read it. It refreshes twice on weekdays (about 8:55am and 8pm Pacific).
- The channel digest posts every weekday at {channel_time} Pacific: the report's thesis, 4-5 bullets with
  tweet links, then market intel from the comp-intel brief (https://vivekyarla.github.io/comp-intel/).
- Who can change it: admins only. Admins right now: {", ".join(admins) or "none set"}.
  To become one, ask {owner} to add your email to ADMIN_EMAILS (a setting on the server).

HOW TO TWEAK IT (admins)
1. Sign in at {base_url}/login with your @rox.com email (create an account the first time).
2. Open {base_url}/gtm/ . A Settings button appears top right (only for admins). Settings there change
   the GTM report, not your own page.
3. Tabs:
   - Feed: start from a preset; "Focus on" and "Skip" lines (plain-English briefs the AI judges tweets by);
     topics with on/off and share % (how much of the report each gets); Filter: min likes (tweets below it
     never count, except tracked companies), min signal, relevance, bait; ranking; writer model.
   - Sources: the timeline panel (anchor accounts whose follows define who's watched), X searches,
     watchlist (accounts always considered), tracked companies (count when they beat their own usual
     likes, with their own min likes / signal), and "stories we should have caught".
   - Filtered out: the best tweets that just missed, and why. Start here when something's missing or
     noisy, then loosen or tighten the matching setting.
4. Click "Save and refresh". The report rebuilds in about 10 minutes; the next channel post uses it.

COMMON TWEAKS
- Too much noise or small accounts: raise Filter > min likes, or min signal.
- Missing a company's posts: add it under Sources > Tracked companies (or the watchlist for people).
- A topic is too loud/quiet: change its share % in Feed > Topics, or uncheck it to drop it.
- Want a new angle: add a line to Feed > Focus on; add things to avoid under Skip.

YOUR OWN PAGE (anyone)
- {base_url}/ : sign in and set up your own feed on your own API keys (twitterapi.io and Vercel AI
  Gateway). Its Settings work the same way and only affect you.
"""


SYSTEM = """You're the Slack bot for an internal X (Twitter) report tool. Answer the user's question using
only the reference below. Be brief and practical: at most 120 words, Slack mrkdwn (*bold*, bullets with
•, links as <url|text>). Give exact steps and links. If they can't do it themselves (not an admin),
say so and who to ask. If the question isn't covered, say what you can help with in one line."""


def fallback(doc_parts: dict, is_admin: bool) -> str:
    base, owner = doc_parts["base"], doc_parts["owner"]
    steps = (f"• Sign in at <{base}/login|the site>\n• Open <{base}/gtm/|the GTM report> → *Settings* (top right)\n"
             "• *Feed*: focus, topics and shares, filter (min likes, signal) · *Sources*: accounts, searches, "
             "tracked companies · *Filtered out*: what just missed and why\n• *Save and refresh*: it rebuilds in "
             "about 10 min, and the next channel post uses it")
    if not is_admin:
        steps += f"\nYou'll need to be an admin first: ask {owner} to add your email to ADMIN_EMAILS."
    return "*To tweak the GTM report:*\n" + steps


def answer(question: str, email: str | None, cfg, api_key: str | None) -> str:
    is_admin = bool(email) and email.lower() in cfg.admin_emails
    doc = help_doc(cfg.base_url, cfg.admin_emails, cfg.channel_time)
    parts = {"base": cfg.base_url, "owner": cfg.admin_emails[0] if cfg.admin_emails else "the site owner"}
    if not api_key:
        return fallback(parts, is_admin)
    who = f"The person asking is {email or 'unknown'}; they are {'an admin' if is_admin else 'NOT an admin'}."
    try:
        r = requests.post(GATEWAY, timeout=40, headers={"Authorization": f"Bearer {api_key}"}, json={
            "model": "anthropic/claude-sonnet-5", "temperature": 0.2, "max_tokens": 1500,
            "messages": [{"role": "system", "content": SYSTEM + "\n\nREFERENCE:\n" + doc + "\n" + who},
                         {"role": "user", "content": question[:2000]}]})
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return text[:2900] or fallback(parts, is_admin)
    except Exception as e:  # never leave someone without an answer
        log.warning("slackbot: model failed (%s); sending the standard steps", type(e).__name__)
        return fallback(parts, is_admin)


def _api(method: str, token: str, **payload) -> dict:
    get = method == "users.info"
    r = requests.request("GET" if get else "POST", SLACK_API + method, timeout=20,
                         headers={"Authorization": f"Bearer {token}"},
                         params=payload if get else None, json=None if get else payload)
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}


def handle(event: dict, cfg, token: str, api_key: str | None) -> None:
    """Reply to one app_mention or DM. Runs in a background thread."""
    try:
        user = event.get("user")
        text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip() or "help"
        info = _api("users.info", token, user=user) if user else {}
        email = ((info.get("user") or {}).get("profile") or {}).get("email")
        reply = answer(text, email, cfg, api_key)
        payload = {"channel": event["channel"], "text": reply, "unfurl_links": False}
        if event.get("type") == "app_mention":
            payload["thread_ts"] = event.get("thread_ts") or event.get("ts")
        res = _api("chat.postMessage", token, **payload)
        if not res.get("ok"):
            log.warning("slackbot: couldn't reply (%s)", res.get("error"))
    except Exception:
        log.exception("slackbot: reply failed")


def should_answer(event: dict) -> bool:
    if event.get("bot_id") or event.get("subtype"):  # its own posts, edits, joins
        return False
    if event.get("type") == "app_mention":
        return True
    return event.get("type") == "message" and event.get("channel_type") == "im"


def dispatch(event: dict, cfg, token: str, api_key: str | None) -> None:
    if should_answer(event):
        threading.Thread(target=handle, args=(event, cfg, token, api_key), daemon=True).start()
