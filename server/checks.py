"""Live checks for a user's keys and Slack webhook, with messages a person can act on.

Key values are only sent to the service they belong to, and never included in a message.
"""
from __future__ import annotations

import os
from datetime import datetime

import requests

TWITTERAPI_SEARCH = "https://api.twitterapi.io/twitter/tweet/advanced_search"
GATEWAY_CREDITS = "https://ai-gateway.vercel.sh/v1/credits"
GATEWAY_CHAT = "https://ai-gateway.vercel.sh/v1/chat/completions"
CHECK_MODEL = os.getenv("KEY_CHECK_MODEL", "openai/gpt-4.1-nano")  # only used if /v1/credits is unavailable
TIMEOUT = 20


def _res(ok: bool, message: str) -> dict:
    return {"ok": ok, "message": message}


def check_twitterapi(key: str) -> dict:
    try:
        r = requests.get(TWITTERAPI_SEARCH, headers={"x-api-key": key.strip()},
                         params={"query": "from:twitterapi_io", "queryType": "Latest"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return _res(False, f"Couldn't reach twitterapi.io ({type(e).__name__}). Try again in a minute.")
    if r.status_code == 200:
        return _res(True, "twitterapi.io accepted the key.")
    if r.status_code in (401, 403):
        return _res(False, f"That key was rejected by twitterapi.io ({r.status_code}). "
                           "Copy it again from twitterapi.io/dashboard.")
    if r.status_code == 402:
        return _res(False, "The key works, but the twitterapi.io account is out of credits. "
                           "Top it up at twitterapi.io/dashboard.")
    if r.status_code == 429:
        return _res(True, "twitterapi.io is rate-limiting right now, but it recognized the key.")
    return _res(False, f"twitterapi.io answered {r.status_code}. Try again in a minute.")


def check_gateway(key: str) -> dict:
    headers = {"Authorization": f"Bearer {key.strip()}"}
    try:
        r = requests.get(GATEWAY_CREDITS, headers=headers, timeout=TIMEOUT)
        if r.status_code == 404:  # no credits endpoint: fall back to the smallest possible completion
            r = requests.post(GATEWAY_CHAT, headers=headers, timeout=TIMEOUT, json={
                "model": CHECK_MODEL, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]})
    except requests.RequestException as e:
        return _res(False, f"Couldn't reach Vercel AI Gateway ({type(e).__name__}). Try again in a minute.")
    if r.status_code == 200:
        msg = "Vercel AI Gateway accepted the key."
        try:
            bal = r.json().get("balance")
            if bal is not None:
                msg += f" Balance: ${float(bal):,.2f}."
        except (ValueError, AttributeError, TypeError):
            pass
        return _res(True, msg)
    if r.status_code in (401, 403):
        return _res(False, f"That key was rejected by Vercel AI Gateway ({r.status_code}). Create one at "
                           "vercel.com → AI Gateway → API Keys and paste it again.")
    if r.status_code == 402:
        return _res(False, "The key works, but the AI Gateway account has no credits. Add credits in Vercel.")
    if r.status_code == 429:
        return _res(True, "AI Gateway is rate-limiting right now, but it recognized the key.")
    return _res(False, f"Vercel AI Gateway answered {r.status_code}. Try again in a minute.")


def slack_test(url: str, page_url: str) -> dict:
    from pipeline.slack import ERRORS
    url = url.strip()
    if not url.startswith("https://hooks.slack.com/"):
        return _res(False, "The Slack webhook should start with https://hooks.slack.com/. Copy it from your "
                           "Slack app's Incoming Webhooks page.")
    text = (f"Test from This week on the timeline ({datetime.now():%b %-d, %-I:%M %p}). "
            f"Your daily recap will post here. <{page_url}|Open your page>")
    try:
        r = requests.post(url, json={"text": text, "unfurl_links": False}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return _res(False, f"Couldn't reach Slack ({type(e).__name__}). Try again in a minute.")
    if r.status_code == 200:
        return _res(True, "Sent. Check the channel for the test message.")
    body = r.text.strip()[:200]
    if r.status_code == 429:
        return _res(False, "Slack is rate-limiting this webhook. Try again in a minute.")
    if r.status_code >= 500:
        return _res(False, f"Slack had a server error ({r.status_code}). Try again in a minute.")
    return _res(False, ERRORS.get(body, f"Slack refused the post ({r.status_code} {body or 'no reason given'})."))
