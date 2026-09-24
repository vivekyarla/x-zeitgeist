"""Ask Jev which tweets are worth showing.

One POST per tweet to a System One endpoint, with all five questions answered
in a single pass. Goes through Vercel AI Gateway's TypeSafe-compatible API,
unless TYPESAFE_API_KEY is set, in which case it calls TypeSafe directly.
"""
from __future__ import annotations

import math
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config

TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
GATEWAY_ENDPOINT = "https://ai-gateway.vercel.sh/typesafe/v1/systemone"

SIGNAL_LEVELS = [
    "Noise: spam, low-effort, off-topic, or meaningless without context",
    "Minor: a personal update, generic take, or small niche announcement",
    "Notable: a launch, build, or GTM move a tech marketer might mention in Slack",
    "Significant: a launch, viral build, or culture moment much of tech Twitter is sharing this week",
    "Defining: the thing everyone in tech, AI, and startup culture is talking about this week",
]

_care = "; ".join(config.FOCUS["care_about"])
_skip = "; ".join(config.FOCUS["skip"])

QUESTIONS = {
    "relevant": {
        "type": "noul",
        "instructions": f"Is this tweet about something {config.FOCUS['audience']} cares about? "
                        f"They care about: {_care}.",
        "criteria": {
            "true": f"the substance is one of: {_care}",
            "false": f"{_skip}; or sports, entertainment, personal life, or a tech word used in passing",
        },
    },
    "topic": {
        "type": "choice",
        "instructions": "Which topic best fits this tweet?",
        "criteria": config.TOPICS,
    },
    "signal": {
        "type": "score",
        "instructions": "How much is this tweet part of what tech, AI, and startup culture is talking "
                        "about this week?",
        "criteria": SIGNAL_LEVELS,
    },
    "bait": {
        "type": "noul",
        "instructions": "Is this engagement bait, spam, a giveaway, a crypto or token shill, "
                        "or a 'like and reply for the link' growth hack?",
    },
    "marketing_useful": {
        "type": "noul",
        "instructions": f"Would {config.FOCUS['audience']} want to see this: something to reference, "
                        "react to, share, or borrow ideas from?",
    },
}


def _state(t: dict) -> dict:
    a = t["author"]
    return {
        "author": f"@{a['handle']} ({a['name']}, {a['followers']:,} followers)",
        "posted": t["created_at"],
        "text": t["text"],
        "engagement": f"{t['likes']:,} likes, {t['retweets']:,} reposts, "
                      f"{t['replies']:,} replies, {t['views']:,} views",
    }


class Jev:
    def __init__(self):
        if os.getenv("TYPESAFE_API_KEY"):
            key, self.endpoint = os.environ["TYPESAFE_API_KEY"], TYPESAFE_ENDPOINT
            self.model = config.JEV_MODEL
        else:
            key, self.endpoint = os.environ["AI_GATEWAY_API_KEY"], GATEWAY_ENDPOINT
            self.model = config.JEV_GATEWAY_MODEL
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {key.strip()}",
            "Content-Type": "application/json",
        })

    def judge(self, tweet: dict) -> dict | None:
        body = {"model": self.model, "state": _state(tweet), "questions": QUESTIONS}
        for attempt in range(5):
            try:
                r = self.session.post(self.endpoint, json=body, timeout=30)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 529) or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                print(f"  jev {r.status_code} on {tweet['id']}: {r.text[:200]}")
                return None
            data = r.json()
            a = data["answers"]
            return {
                "relevant": a["relevant"]["noul"],
                "topic": a["topic"]["choice"],
                "topic_confidence": a["topic"].get("confidence"),
                "signal": a["signal"]["score"],
                "bait": a["bait"]["noul"],
                "marketing_useful": a["marketing_useful"]["noul"],
                "model": data.get("model"),
                "v": config.JUDGE_VERSION,
            }
        return None

    def judge_many(self, tweets: list[dict]) -> dict[str, dict]:
        with ThreadPoolExecutor(config.JEV_CONCURRENCY) as pool:
            results = pool.map(self.judge, tweets)
        return {t["id"]: j for t, j in zip(tweets, results) if j}


def engagement(t: dict) -> float:
    return t["likes"] + 2 * t["retweets"] + 3 * t["quotes"] + t["replies"]


def passes(j: dict) -> bool:
    return (j["relevant"] >= config.MIN_RELEVANCE
            and j["bait"] <= config.MAX_BAIT
            and j["signal"] >= config.MIN_SIGNAL
            and j["topic"] not in config.EXCLUDED_TOPICS)


def rank_score(t: dict, max_log_eng: float) -> float:
    """Jev's judgment does most of the work; engagement breaks ties."""
    j = t["jev"]
    eng = math.log1p(engagement(t)) / max_log_eng if max_log_eng else 0
    return ((j["signal"] / 4) * j["relevant"] * (1 - j["bait"])
            * (0.7 + 0.3 * j["marketing_useful"]) * (0.6 + 0.4 * eng))
