"""Loads settings.json (what the page's Settings panel edits) plus a few fixed knobs.

Interests, searches, thresholds, and Slack schedule live in settings.json at the repo
root. Edit them there or from the Settings panel on the page; this file only reads them.
"""
import hashlib
import json
import os
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "settings.json"
SETTINGS = json.loads(SETTINGS_PATH.read_text())

# ---------------------------------------------------------------------------
# What to pull from X each run
# ---------------------------------------------------------------------------
# Standard X advanced-search syntax, restricted to the current week automatically.
SEARCH_QUERIES: list[str] = [q for q in SETTINGS["searches"] if q.strip()]
WATCHLIST: list[str] = [h.lstrip("@") for h in SETTINGS["watchlist"] if h.strip()]
WATCHLIST_MIN_FAVES = int(SETTINGS.get("watchlist_min_faves", 20))

PAGES_PER_QUERY = int(os.getenv("PAGES_PER_QUERY", "2"))  # ~20 tweets per page

# ---------------------------------------------------------------------------
# Jev filtering
# ---------------------------------------------------------------------------
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")  # direct TypeSafe; pin e.g. "jev-1.13.0" once tuned
JEV_GATEWAY_MODEL = os.getenv("JEV_GATEWAY_MODEL", "typesafe-ai/jev")  # via Vercel AI Gateway
JEV_CONCURRENCY = int(os.getenv("JEV_CONCURRENCY", "8"))

FOCUS = SETTINGS["focus"]
_topics = SETTINGS["topics"]
TOPICS = {t["key"]: t["description"] for t in _topics}
TOPIC_LABELS = {t["key"]: t["label"] for t in _topics}
# Topics that never make the page, however strong the signal. "other" is always excluded.
EXCLUDED_TOPICS = {t["key"] for t in _topics if not t.get("include", True)} | {"other"}
if "other" not in TOPICS:
    TOPICS["other"], TOPIC_LABELS["other"] = "Anything else", "Other"

# Saved tweets are re-judged whenever anything Jev is asked about changes.
_JUDGE_QUESTIONS_REV = 5  # bump when the question wording in judge.py changes
JUDGE_VERSION = hashlib.sha1(json.dumps(
    [_JUDGE_QUESTIONS_REV, FOCUS, TOPICS], sort_keys=True).encode()).hexdigest()[:10]

# A tweet makes the page only if it clears all of these.
_th = SETTINGS["thresholds"]
MIN_RELEVANCE = float(_th["min_relevance"])  # P(on-topic for the focus above)
MAX_BAIT = float(_th["max_bait"])            # P(engagement bait, spam, giveaway, shilling)
MIN_SIGNAL = float(_th["min_signal"])        # 0-4 rubric score; ~2 = "notable"

# Share of the tweets sent to the writer that each topic should get (they sum to ~1).
TOPIC_SHARES = {t["key"]: float(t.get("share", 0)) for t in _topics if t.get("include", True)}

_rk = {"per_author_max": 2, "breakout_weight": 0.5, "mainstream_penalty": 0.5, **SETTINGS.get("ranking", {})}
PER_AUTHOR_MAX = int(_rk["per_author_max"])          # max tweets per account sent to the writer
BREAKOUT_WEIGHT = float(_rk["breakout_weight"])      # 0 = raw engagement, 1 = engagement vs follower count
MAINSTREAM_PENALTY = float(_rk["mainstream_penalty"])  # how much big-lab news is ranked down (0-1)
# Accounts that count as one for the per-account cap (e.g. @OpenAI, @OpenAIDevs, @sama).
ACCOUNT_GROUP = {h.lower(): g for g, hs in _rk.get("account_groups", {}).items() for h in hs}

# Company accounts held to their own bar: a tweet counts if it beats that account's
# usual (median) likes by this factor, instead of needing to go broadly viral.
_tr = {"accounts": [], "beat_baseline_by": 3.0, "min_likes": 25, "min_signal": 1.0, **SETTINGS.get("tracked", {})}
TRACKED = [h.lstrip("@") for h in _tr["accounts"] if h.strip()]
TRACKED_LOWER = {h.lower() for h in TRACKED}
TRACKED_BEAT_BY = float(_tr["beat_baseline_by"])
TRACKED_MIN_LIKES = int(_tr["min_likes"])
TRACKED_MIN_SIGNAL = float(_tr["min_signal"])

# ---------------------------------------------------------------------------
# Thesis writing (Vercel AI Gateway)
# ---------------------------------------------------------------------------
WRITER_MODEL = os.getenv("WRITER_MODEL") or SETTINGS.get("writer_model", "anthropic/claude-sonnet-5")
TWEETS_FOR_THESIS = 60     # top-ranked tweets sent to the writer model
TWEETS_PER_THEME_SHOWN = 6

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
SLACK = {"enabled": False, "frequency": "daily", "hour_pt": 8, "weekdays_only": True, "top_tweets": 5,
         **SETTINGS.get("slack", {})}

# ---------------------------------------------------------------------------
# Week boundaries and page
# ---------------------------------------------------------------------------
TIMEZONE = "America/Los_Angeles"  # week starts Monday 00:00 in this zone
SITE_TITLE = "This week on the timeline"
PAGE_URL = os.getenv("PAGE_URL", "")
REPO = os.getenv("GITHUB_REPOSITORY", "")  # owner/name, set by GitHub Actions
