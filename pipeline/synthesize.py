"""Write the thesis and group tweets into themes, via OpenRouter."""
from __future__ import annotations

import json
import os
import re

import requests

from . import config

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = """You write a weekly briefing for a B2B tech marketing team in San Francisco.
You get the most important tweets of the week so far (already filtered for relevance) and
the thesis you wrote on the previous update, if any.

Return ONLY a JSON object, no markdown fences, with this shape:
{
  "thesis": "1-2 sentences, max 45 words. The single most useful read on what the tech/AI/sales
             timeline is about this week and why it matters. Specific: name companies, launches,
             or arguments. No hype words, no 'buzzing', no 'abuzz'.",
  "thesis_changed": true or false,   // false if the story is essentially the same as last time
  "themes": [
    {
      "name": "3-6 word theme name",
      "summary": "One sentence on what people are saying and where the disagreement is.",
      "tweet_ids": ["ids of the tweets that belong to this theme, most important first"]
    }
  ]
}
Use 3-5 themes. Only use tweet ids from the input. If last week's thesis still holds,
keep its core but update the specifics."""


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
    r = requests.post(
        ENDPOINT,
        headers={
            "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
            "Content-Type": "application/json",
            "X-Title": "Timeline zeitgeist",
        },
        json={
            "model": config.OPENROUTER_MODEL,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user}],
            "temperature": 0.4,
            "max_tokens": 1500,
        },
        timeout=120,
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    out = json.loads(text)
    valid = {t["id"] for t in tweets}
    for theme in out.get("themes", []):
        theme["tweet_ids"] = [i for i in theme.get("tweet_ids", []) if i in valid]
    out["themes"] = [th for th in out.get("themes", []) if th["tweet_ids"]]
    return out
