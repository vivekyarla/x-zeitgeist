"""Loads one profile's settings plus a few fixed knobs.

Each run works on one profile. The server points these env vars at that user's files;
unset, they fall back to the repo itself (settings.json, data/, public/), so
`python -m pipeline.run --demo` works on its own:

  TIMELINE_SETTINGS   the profile's settings.json
  TIMELINE_DATA       its state dir (week.json, archive/, panel.json)
  TIMELINE_OUT        where the page is built (index.html, data.json, recap.*)
  PAGE_URL            the page's public URL (links in the recap and Slack)
  TIMELINE_RECAP_URL  base URL of the recap feed (recap.md / recap.json); defaults to PAGE_URL
"""
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.getenv("TIMELINE_SETTINGS") or ROOT / "settings.json")
SETTINGS = json.loads(SETTINGS_PATH.read_text())
DATA_DIR = Path(os.getenv("TIMELINE_DATA") or ROOT / "data")
OUT_DIR = Path(os.getenv("TIMELINE_OUT") or ROOT / "public")

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

# The timeline panel (see panel.py): accounts followed by the anchor groups.
PANEL = {"groups": [], "min_overlap": 2, "max_accounts": 300, "include": [], "exclude": [],
         "refresh_days": 7, "max_pages_per_anchor": 10, "pages_per_batch": 2, "retweet_pages": 1, "handles_per_query": 18,
         **SETTINGS.get("panel", {})}
# How much a story's reach (distinct panel accounts posting, quoting, or retweeting it) counts.
BREADTH_WEIGHT = float(_rk.get("breadth_weight", 1.0))

# Stories a person noticed and expects the page to catch; every run reports where each landed.
KNOWN_STORIES = SETTINGS.get("known_stories", [])

# ---------------------------------------------------------------------------
# Thesis writing (Vercel AI Gateway)
# ---------------------------------------------------------------------------
WRITER_MODEL = os.getenv("WRITER_MODEL") or SETTINGS.get("writer_model", "anthropic/claude-sonnet-5")
TWEETS_FOR_THESIS = 60     # top-ranked tweets sent to the writer model
TWEETS_PER_THEME_SHOWN = 6

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
SLACK = {"enabled": False, "frequency": "daily", "weekdays_only": True, "top_tweets": 5,
         **SETTINGS.get("slack", {})}
# Local hour (in TIMEZONE) for the daily post; older settings called it hour_pt.
SLACK["hour"] = int(SLACK.get("hour", SLACK.get("hour_pt", 8)))

# ---------------------------------------------------------------------------
# Week boundaries and page
# ---------------------------------------------------------------------------
DEFAULT_TIMEZONE = "America/Los_Angeles"


def _timezone(name) -> str:
    try:
        ZoneInfo(str(name))
        return str(name)
    except Exception:
        return DEFAULT_TIMEZONE


# IANA zone for this profile: the week starts Monday 00:00 here, and times and the daily
# Slack hour are local to it.
TIMEZONE = _timezone(SETTINGS.get("timezone") or DEFAULT_TIMEZONE)
SITE_TITLE = "This week on the timeline"
PAGE_URL = os.getenv("PAGE_URL", "")
RECAP_URL = os.getenv("TIMELINE_RECAP_URL", "") or PAGE_URL
