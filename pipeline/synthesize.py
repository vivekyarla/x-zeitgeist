"""Write the thesis and group tweets into themes, via Vercel AI Gateway."""
from __future__ import annotations

import json
import os
import re

import requests

from . import config

ENDPOINT = "https://ai-gateway.vercel.sh/v1/chat/completions"

SYSTEM = """You write a weekly briefing for the marketing team at an AI startup in San Francisco.
They want the tech culture read on the week: what people in tech, AI, and GTM are sharing,
building, and copying, so they can reference it, react to it, or borrow the idea.

What they care about, most important first:
- AI model and product launches (a new GPT or Claude release, a new model like Jev, a new agent product)
- Creative things people built with AI that are going viral (JavaScript animations made in Claude,
  vibe-coded games, clever demos)
- What other revenue and GTM teams and tools are doing (Clay, Monaco, AI SDRs, outbound experiments,
  launch videos, brand stunts, pricing moves)
- Startup, VC, and SF culture moments (a16z starting a school, a hackathon everyone went to, a
  notable raise people are talking about)

What they do NOT care about, even if it shows up in the input: infrastructure and finance stories
(data centers, compute deals, chips, power, bonds, debt, earnings, stock moves), macro, politics,
regulation, and lawsuits. Leave these out of the thesis and the themes entirely.

You get the most important tweets of the week so far (already filtered) and the thesis you wrote on
the previous update, if any.

Return ONLY a JSON object, no markdown fences, with this shape:
{
  "thesis": "1-2 sentences, max 45 words. The single most useful read on what tech culture is about
             this week and why a marketer should care. Specific: name the launches, builds, companies,
             or people. No hype words, no 'buzzing', no 'abuzz'.",
  "thesis_changed": true or false,   // false if the story is essentially the same as last time
  "themes": [
    {
      "name": "3-6 word theme name",
      "summary": "One sentence on what people are making, launching, or saying, and why it's catching on.",
      "tweet_ids": ["ids of the tweets that belong to this theme, most important first"]
    }
  ]
}
Use 3-5 themes. Only use tweet ids from the input. If the previous thesis still holds and fits the
focus above, keep its core but update the specifics; if it's about things they don't care about,
replace it."""


def _payload(tweets: list[dict]) -> list[dict]:
    return [{
        "id": t["id"],
        "author": "@" + t["author"]["handle"],
        "followers": t["author"]["followers"],
        "text": t["text"][:600],
        "likes": t["likes"],
        "reposts": t["retweets"],
        "topic": t["jev"]["topic"],
        "jev_signal_0_to_4": round(t["jev"]["signal"], 2),
    } for t in tweets]


def synthesize(tweets: list[dict], previous_thesis: str | None) -> dict:
    user = json.dumps({
        "previous_thesis": previous_thesis,
        "tweets": _payload(tweets),
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
            "max_tokens": 3000,
        },
        timeout=120,
    )
    if r.status_code != 200:
        print(f"  writer {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    choice = r.json()["choices"][0]
    text = re.sub(r"^```(?:json)?|```$", "", (choice["message"]["content"] or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"  writer returned bad JSON ({e}; finish_reason={choice.get('finish_reason')}): {text[:300]!r}")
        return None
