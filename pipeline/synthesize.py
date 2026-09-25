"""Write the thesis and group tweets into themes, via Vercel AI Gateway."""
from __future__ import annotations

import json
import os
import re

import requests

from . import config

ENDPOINT = "https://ai-gateway.vercel.sh/v1/chat/completions"

def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


SYSTEM = f"""You write a weekly briefing for {config.FOCUS['audience']}.
They want the tech culture read on the week: what people in tech, AI, and GTM are sharing,
building, and copying, so they can reference it, react to it, or borrow the idea.

What they care about, most important first:
{_bullets(config.FOCUS['care_about'])}

What they do NOT care about, even if it shows up in the input. Leave these out of the thesis
and the themes entirely:
{_bullets(config.FOCUS['skip'])}

You get the most important tweets of the week so far (already filtered) and the thesis you wrote on
the previous update, if any.

Return ONLY a JSON object, no markdown fences, with this shape:
{{
  "thesis": "1-2 sentences, max 45 words. The single most useful read on what tech culture is about
             this week and why a marketer should care. Specific: name the launches, builds, companies,
             or people. Wrap the 2-4 key phrases (each a few words, e.g. a launch or a company's move)
             in **double asterisks** and follow each with {{n}}, the 1-based number of the theme
             below it belongs to, e.g. **reply-based outbound**{{1}}; no other markdown. No hype words, no 'buzzing', no 'abuzz'.",
  "thesis_changed": true or false,   // false if the story is essentially the same as last time
  "themes": [
    {{
      "name": "3-6 word theme name",
      "summary": "One sentence on what people are making, launching, or saying, and why it's catching on.",
      "tweet_ids": ["ids of the tweets that belong to this theme, most important first"]
    }}
  ]
}}
Use 3-5 themes. Only use tweet ids from the input. At most ONE theme may be about AI model or
product launches from big labs; fold the rest of the launch news into it or leave it out. If the
input has GTM, sales, or marketing tweets, at least one theme must be about them, and lead with
what's working (a playbook that's paying off, a campaign that took off, a company post that
outperformed). Tweets marked "company_post_outperforming" are posts from GTM companies (Clay,
Monaco, Gong, ...) that did much better than that company usually does; treat them as signs of
what's working in GTM, not as ads. If the previous thesis still holds and fits the
focus above, keep its core but update the specifics; if it's about things they don't care about,
replace it."""


def _payload(tweets: list[dict], baselines: dict) -> list[dict]:
    def perf(t: dict) -> dict:
        b = baselines.get(t["author"]["handle"].lower())
        if b and b.get("median"):
            return {"company_post_outperforming": f"{t['likes'] / b['median']:.1f}x its usual likes"}
        return {}
    return [{
        "id": t["id"],
        "author": "@" + t["author"]["handle"],
        "followers": t["author"]["followers"],
        "text": t["text"][:600],
        "likes": t["likes"],
        "reposts": t["retweets"],
        "topic": t["jev"]["topic"],
        "jev_signal_0_to_4": round(t["jev"]["signal"], 2),
        **perf(t),
    } for t in tweets]


def synthesize(tweets: list[dict], previous_thesis: str | None, baselines: dict | None = None) -> dict:
    user = json.dumps({
        "previous_thesis": previous_thesis,
        "tweets": _payload(tweets, baselines or {}),
    }, ensure_ascii=False)
    for attempt in range(2):
        out = _ask(user)
        if out is not None:
            break
    else:
        raise RuntimeError("writer model didn't return valid JSON twice in a row (see above)")
    valid = {t["id"] for t in tweets}
    for theme in out.get("themes", []):
        theme["tweet_ids"] = [i for i in theme.get("tweet_ids", []) if i in valid]
    out["themes"] = [th for th in out.get("themes", []) if th["tweet_ids"]]
    return out


def _ask(user: str) -> dict | None:
    r = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY'].strip()}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.WRITER_MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_tokens": 16000,  # reasoning tokens count toward this; you pay only for what is used
        },
        timeout=120,
    )
    if r.status_code != 200:
        print(f"  writer {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    body = r.json()
    choice = body["choices"][0]
    print(f"  writer usage: {body.get('usage')}")
    text = re.sub(r"^```(?:json)?|```$", "", (choice["message"]["content"] or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  writer returned bad JSON ({e}; finish_reason={choice.get('finish_reason')}): {text[:300]!r}")
        return None
