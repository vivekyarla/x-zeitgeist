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
_JUDGE_QUESTIONS_REV = 3  # bump when the question wording in judge.py changes
JUDGE_VERSION = hashlib.sha1(json.dumps(
    [_JUDGE_QUESTIONS_REV, FOCUS, TOPICS], sort_keys=True).encode()).hexdigest()[:10]

# A tweet makes the page only if it clears all of these.
_th = SETTINGS["thresholds"]
MIN_RELEVANCE = float(_th["min_relevance"])  # P(on-topic for the focus above)
MAX_BAIT = float(_th["max_bait"])            # P(engagement bait, spam, giveaway, shilling)
MIN_SIGNAL = float(_th["min_signal"])        # 0-4 rubric score; ~2 = "notable"

# ---------------------------------------------------------------------------
# Thesis writing (Vercel AI Gateway)
# ---------------------------------------------------------------------------
WRITER_MODEL = os.getenv("WRITER_MODEL") or SETTINGS.get("writer_model", "anthropic/claude-sonnet-5")
TWEETS_FOR_THESIS = 40     # top-ranked tweets sent to the writer model
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
