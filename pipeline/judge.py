"""Ask Jev which tweets are worth showing.

One POST per tweet to a System One endpoint, with all six questions answered
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

# Signal is judged within the tweet's own scene (GTM, marketing, AI builders, startups),
# so a sharp GTM playbook can score as high as a frontier-model launch.
SIGNAL_LEVELS = [
    "Noise: spam, low-effort, off-topic, or meaningless without context",
    "Minor: a routine update, generic advice, or small announcement its scene would scroll past",
    "Notable: a useful playbook, sharp take, clever move, or launch its scene would bookmark or mention",
    "Significant: something its scene is actively sharing and discussing this week",
    "Defining: the thing its scene will remember from this week",
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
        "instructions": "How notable is this tweet within its own scene this week? Judge it against "
                        "its own corner of Twitter (GTM and sales people, marketers, AI builders, startup "
                        "and SF people), not against all of tech, so a niche post that its scene is "
                        "sharing can score high.",
        "criteria": SIGNAL_LEVELS,
    },
    "bait": {
        "type": "noul",
        "instructions": "Is this engagement bait, spam, a giveaway, a crypto or token shill, a 'like "
                        "and reply for the link' growth hack, or a generic advice or listicle thread "
                        "written mainly to farm followers (e.g. 'the playbook to $100M ARR', '7 lessons "
                        "from advising startups') with no specific result, data, or real example?",
    },
    "mainstream": {
        "type": "noul",
        "instructions": "Is this a major announcement or news item from a big AI lab or big tech company "
                        "(OpenAI, Anthropic, Google, Meta, Microsoft, Apple, Nvidia, xAI) that most people in "
                        "tech will see anyway, or a reaction that mostly restates one?",
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
                "mainstream": a["mainstream"]["noul"],
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


def _tracked_baseline(t: dict, baselines: dict) -> float | None:
    """Median likes for a tracked company account, if we know it."""
    h = t["author"]["handle"].lower()
    if h not in config.TRACKED_LOWER:
        return None
    b = baselines.get(h) or {}
    return b.get("median") if b.get("n", 0) >= 5 else None


def why_not(t: dict, baselines: dict) -> str | None:
    """None if the tweet makes the cut, otherwise a short reason."""
    j = t["jev"]
    if j["topic"] in config.EXCLUDED_TOPICS:
        return f"topic: {config.TOPIC_LABELS.get(j['topic'], j['topic'])}"
    if j["bait"] > config.MAX_BAIT:
        return f"bait {j['bait']:.2f}"
    if j["relevant"] < config.MIN_RELEVANCE:
        return f"relevance {j['relevant']:.2f}"
    base = _tracked_baseline(t, baselines)
    tracked = t["author"]["handle"].lower() in config.TRACKED_LOWER
    if tracked and base is None and t["likes"] < config.TRACKED_MIN_LIKES:
        return f"{t['likes']:,} likes, under {config.TRACKED_MIN_LIKES} (no baseline yet)"
    if base is not None:  # company accounts: beat their own usual, not the global bar
        need = max(config.TRACKED_MIN_LIKES, config.TRACKED_BEAT_BY * base)
        if t["likes"] < need:
            return f"{t['likes']:,} likes, under {need:,.0f} ({config.TRACKED_BEAT_BY:g}x its usual {base:,.0f})"
        if j["signal"] < config.TRACKED_MIN_SIGNAL:
            return f"signal {j['signal']:.2f}"
        return None
    if j["signal"] < config.MIN_SIGNAL:
        return f"signal {j['signal']:.2f}"
    return None


def rank_all(tweets: list[dict], baselines: dict) -> None:
    """Set t["rank"]. Jev's judgment does most of the work; engagement (overall and
    relative to the account's size) breaks ties, and big-lab news is ranked down."""
    if not tweets:
        return
    ratio = lambda t: engagement(t) / max(t["author"]["followers"], 500)
    max_abs = max(math.log1p(engagement(t)) for t in tweets) or 1
    max_br = max(math.log1p(100 * ratio(t)) for t in tweets) or 1
    for t in tweets:
        j = t["jev"]
        base = _tracked_baseline(t, baselines)
        if base is not None:
            br = min(1.0, math.log1p(t["likes"] / max(base, 1)) / math.log1p(10))
        else:
            br = math.log1p(100 * ratio(t)) / max_br
        eng = (1 - config.BREAKOUT_WEIGHT) * math.log1p(engagement(t)) / max_abs + config.BREAKOUT_WEIGHT * br
        t["rank"] = ((j["signal"] / 4) * j["relevant"] * (1 - j["bait"])
                     * (0.7 + 0.3 * j["marketing_useful"]) * (0.6 + 0.4 * eng)
                     * (1 - config.MAINSTREAM_PENALTY * j.get("mainstream", 0)))


def select(kept: list[dict], n: int) -> list[dict]:
    """Pick the n tweets the writer sees: best-ranked first, at most PER_AUTHOR_MAX per
    account, each topic filled toward its share, and no topic far past its share."""
    ranked = sorted(kept, key=lambda t: t["rank"], reverse=True)
    quota = {k: round(s * n) for k, s in config.TOPIC_SHARES.items()}
    cap = {k: max(2, round(q * 1.5)) for k, q in quota.items()}
    per_author, per_topic, chosen, ids = {}, {}, [], set()

    def account(t: dict) -> str:
        h = t["author"]["handle"].lower()
        return config.ACCOUNT_GROUP.get(h, h)

    def take(t: dict) -> None:
        a, k = account(t), t["jev"]["topic"]
        per_author[a] = per_author.get(a, 0) + 1
        per_topic[k] = per_topic.get(k, 0) + 1
        chosen.append(t); ids.add(t["id"])

    ok_author = lambda t: per_author.get(account(t), 0) < config.PER_AUTHOR_MAX
    for k, q in quota.items():  # each topic's best, up to its share
        for t in ranked:
            if per_topic.get(k, 0) >= q or len(chosen) >= n:
                break
            if t["jev"]["topic"] == k and t["id"] not in ids and ok_author(t):
                take(t)
    for t in ranked:  # then the best of the rest, within each topic's cap
        if len(chosen) >= n:
            break
        k = t["jev"]["topic"]
        if t["id"] not in ids and ok_author(t) and per_topic.get(k, 0) < cap.get(k, 2):
            take(t)
    return sorted(chosen, key=lambda t: t["rank"], reverse=True)
