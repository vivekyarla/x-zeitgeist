"""Validation for a user's settings, shared by PUT /api/settings, import, and the presets test.

One user's settings drive paid API calls on that user's own keys (searches, watchlist,
panel anchors), so everything is size-capped, and regexes are compiled with a length cap.
`validate()` returns a complete, normalized settings object (defaults filled in, unknown
keys dropped) or raises SettingsError with a message a person can act on.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

PRESETS_DIR = Path(__file__).resolve().parent.parent / "pipeline" / "presets"
DEFAULT_PRESET = "ai_startup_marketing"
PRESET_ORDER = ["sf_tech", "ai_startup_marketing", "ai_engineer", "startup_culture", "gtm_sales", "marketing_brand", "ai_business"]

MAX_JSON_BYTES = 200_000
MAX_STR = 2000
MAX_LIST_STR = 30          # care_about / skip lines
MAX_TOPICS = 30
MAX_SEARCHES = 40
MAX_QUERY = 512            # X's own limit on an advanced-search query is about this
MAX_HANDLES = 300          # per list
MAX_ANCHORS = 100          # panel anchors across all groups (each costs up to 10 API pages a week)
MAX_GROUPS = 10
MAX_ACCOUNT_GROUPS = 50
MAX_KNOWN = 30
MAX_PATTERN = 300

HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
TOPIC_KEY = re.compile(r"^[a-z0-9_]{1,40}$")
MODEL = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}/[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$")
WEEK = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FREQUENCIES = ("daily", "on_change", "every_update")


class SettingsError(ValueError):
    pass


def _fail(msg: str):
    raise SettingsError(msg)


def _obj(v, where: str) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        _fail(f"{where} should be an object.")
    return v


def _str(v, where: str, *, required=False, max_len=MAX_STR, default="") -> str:
    if v is None:
        v = default
    if not isinstance(v, str):
        _fail(f"{where} should be text.")
    v = v.strip()
    if required and not v:
        _fail(f"{where} can't be empty.")
    if len(v) > max_len:
        _fail(f"{where} is too long (at most {max_len:,} characters).")
    return v


def _num(v, where: str, lo: float, hi: float, default, *, integer=False):
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        if isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                _fail(f"{where} should be a number.")
        else:
            _fail(f"{where} should be a number.")
    if v != v or not (lo <= v <= hi):  # NaN or out of range
        _fail(f"{where} should be between {lo:g} and {hi:g}.")
    return int(round(v)) if integer else float(v)


def _bool(v, where: str, default: bool) -> bool:
    if v is None:
        return default
    if not isinstance(v, bool):
        _fail(f"{where} should be true or false.")
    return v


def _strings(v, where: str, max_items: int, max_len: int = MAX_STR) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        _fail(f"{where} should be a list.")
    out = [_str(x, f"Each line of {where}", max_len=max_len) for x in v]
    out = [x for x in out if x]
    if len(out) > max_items:
        _fail(f"{where} has {len(out)} entries; the limit is {max_items}.")
    return out


def _handles(v, where: str, max_items: int = MAX_HANDLES) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list):
        _fail(f"{where} should be a list of X handles.")
    out, seen = [], set()
    for x in v:
        if not isinstance(x, str):
            _fail(f"{where} should be a list of X handles.")
        h = x.strip().lstrip("@")
        if not h:
            continue
        if not HANDLE.match(h):
            _fail(f"\"{x.strip()[:40]}\" in {where} isn't a valid X handle (letters, numbers, and _; up to 15).")
        if h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    if len(out) > max_items:
        _fail(f"{where} has {len(out)} accounts; the limit is {max_items}.")
    return out


def _focus(v) -> dict:
    v = _obj(v, "Focus")
    if not v:
        _fail("Focus is missing: say who the page is for and what they care about.")
    care = _strings(v.get("care_about"), "Focus on", MAX_LIST_STR)
    if not care:
        _fail("Add at least one line under \"Focus on\".")
    return {"audience": _str(v.get("audience"), "Who it's for", required=True),
            "care_about": care,
            "skip": _strings(v.get("skip"), "Skip", MAX_LIST_STR)}


def _topics(v) -> list[dict]:
    if not isinstance(v, list) or not v:
        _fail("Add at least one topic.")
    if len(v) > MAX_TOPICS:
        _fail(f"There are {len(v)} topics; the limit is {MAX_TOPICS}.")
    out, keys = [], set()
    for i, t in enumerate(v, 1):
        t = _obj(t, f"Topic {i}")
        key = _str(t.get("key"), f"Topic {i}'s key", required=True, max_len=40)
        if not TOPIC_KEY.match(key):
            _fail(f"Topic key \"{key}\" should use only lowercase letters, numbers, and _.")
        if key in keys:
            _fail(f"Two topics use the key \"{key}\".")
        keys.add(key)
        out.append({"key": key,
                    "label": _str(t.get("label"), f"Topic \"{key}\"'s label", required=True, max_len=60),
                    "description": _str(t.get("description"), f"Topic \"{key}\"'s description", required=True),
                    "include": _bool(t.get("include"), f"Topic \"{key}\"'s include", True),
                    "share": _num(t.get("share"), f"Topic \"{key}\"'s share", 0, 1, 0)})
    inc = [t for t in out if t["include"] and t["key"] != "other"]
    if not inc:
        _fail("Turn on at least one topic.")
    total = sum(t["share"] for t in inc)
    if total == 0:  # nothing set yet: split evenly
        for t in inc:
            t["share"] = round(1 / len(inc), 3)
        total = sum(t["share"] for t in inc)
    if not 0.5 <= total <= 1.5:
        _fail(f"The topic shares add up to {total:.0%}; they should add up to about 100%.")
    if "other" not in keys:
        out.append({"key": "other", "label": "Other", "description": "Anything else", "include": False, "share": 0.0})
    return out


def _searches(v) -> list[str]:
    out = _strings(v, "Searches", MAX_SEARCHES, MAX_QUERY)
    for q in out:
        if "\n" in q or "\r" in q:
            _fail("Each search should be on one line.")
    return out


def _slack(v) -> dict:
    v = _obj(v, "Slack")
    hour = v.get("hour", v.get("hour_pt"))
    freq = v.get("frequency") or "daily"
    if freq not in FREQUENCIES:
        _fail("Slack frequency should be daily, on_change, or every_update.")
    return {"enabled": _bool(v.get("enabled"), "Slack on/off", False),
            "frequency": freq,
            "hour": _num(hour, "The Slack hour", 0, 23, 8, integer=True),
            "weekdays_only": _bool(v.get("weekdays_only"), "Weekdays only", True),
            "top_tweets": _num(v.get("top_tweets"), "Tweets in the Slack post", 0, 10, 5, integer=True)}


def _ranking(v) -> dict:
    v = _obj(v, "Ranking")
    groups = _obj(v.get("account_groups"), "Account groups")
    if len(groups) > MAX_ACCOUNT_GROUPS:
        _fail(f"There are {len(groups)} account groups; the limit is {MAX_ACCOUNT_GROUPS}.")
    ag = {}
    for name, hs in groups.items():
        name = _str(name, "An account group's name", required=True, max_len=60)
        ag[name] = _handles(hs, f"account group \"{name}\"")
    return {"per_author_max": _num(v.get("per_author_max"), "Max tweets per account", 1, 10, 2, integer=True),
            "breakout_weight": _num(v.get("breakout_weight"), "Breakout weight", 0, 1, 0.35),
            "mainstream_penalty": _num(v.get("mainstream_penalty"), "Big-lab penalty", 0, 1, 0.25),
            "breadth_weight": _num(v.get("breadth_weight"), "Reach weight", 0, 5, 1.0),
            "account_groups": ag}


def _tracked(v) -> dict:
    v = _obj(v, "Tracked companies")
    return {"accounts": _handles(v.get("accounts"), "Tracked companies"),
            "beat_baseline_by": _num(v.get("beat_baseline_by"), "Beat usual likes by", 1, 50, 3.0),
            "min_signal": _num(v.get("min_signal"), "Tracked min signal", 0, 4, 1.0),
            "min_likes": _num(v.get("min_likes"), "Tracked min likes", 0, 1_000_000, 25, integer=True)}


def _panel(v) -> dict:
    v = _obj(v, "Panel")
    groups = v.get("groups") or []
    if not isinstance(groups, list):
        _fail("Panel groups should be a list.")
    if len(groups) > MAX_GROUPS:
        _fail(f"There are {len(groups)} panel groups; the limit is {MAX_GROUPS}.")
    out = []
    for i, g in enumerate(groups, 1):
        g = _obj(g, f"Panel group {i}")
        name = _str(g.get("name"), f"Panel group {i}'s name", required=True, max_len=60)
        out.append({"name": name,
                    "share": _num(g.get("share"), f"Panel group \"{name}\"'s share", 0, 1, 0.5),
                    "accounts": _handles(g.get("accounts"), f"panel group \"{name}\"")})
    anchors = sum(len(g["accounts"]) for g in out)
    if anchors > MAX_ANCHORS:
        _fail(f"The panel has {anchors} anchor accounts; the limit is {MAX_ANCHORS}.")
    res = {"groups": out,
           "min_overlap": _num(v.get("min_overlap"), "Panel min overlap", 1, 20, 2, integer=True),
           "max_accounts": _num(v.get("max_accounts"), "Panel size", 20, 1000, 300, integer=True),
           "include": _handles(v.get("include"), "Panel: always include"),
           "exclude": _handles(v.get("exclude"), "Panel: never include"),
           "refresh_days": _num(v.get("refresh_days"), "Panel refresh days", 1, 60, 7, integer=True)}
    for k, lo, hi in (("max_pages_per_anchor", 1, 20), ("pages_per_batch", 1, 5), ("retweet_pages", 1, 10),
                      ("handles_per_query", 1, 25)):
        if v.get(k) is not None:
            res[k] = _num(v[k], f"Panel {k}", lo, hi, lo, integer=True)
    return res


def _known(v) -> list[dict]:
    if v is None:
        return []
    if not isinstance(v, list):
        _fail("Known stories should be a list.")
    if len(v) > MAX_KNOWN:
        _fail(f"There are {len(v)} known stories; the limit is {MAX_KNOWN}.")
    out = []
    for i, ks in enumerate(v, 1):
        ks = _obj(ks, f"Known story {i}")
        label = _str(ks.get("label"), f"Known story {i}'s label", required=True, max_len=200)
        pat = _str(ks.get("match"), f"Known story \"{label}\"'s pattern", required=True, max_len=MAX_PATTERN)
        try:
            re.compile(pat, re.I | re.S)
        except re.error as e:
            _fail(f"Known story \"{label}\"'s pattern isn't a valid regular expression ({e}).")
        item = {"label": label, "match": pat}
        week = _str(ks.get("week"), f"Known story \"{label}\"'s week", max_len=10)
        if week:
            if not WEEK.match(week):
                _fail(f"Known story \"{label}\"'s week should be a date like 2026-09-21.")
            item["week"] = week
        out.append(item)
    return out


def _timezone(v) -> str:
    tz = _str(v, "Time zone", max_len=64, default="America/Los_Angeles") or "America/Los_Angeles"
    try:
        ZoneInfo(tz)
    except Exception:
        _fail(f"\"{tz}\" isn't a time zone we know. Use a name like America/New_York or Europe/London.")
    return tz


def validate(settings) -> dict:
    """Return a complete, normalized copy of `settings`, or raise SettingsError."""
    if not isinstance(settings, dict):
        _fail("Settings should be an object.")
    try:
        size = len(json.dumps(settings))
    except (TypeError, ValueError):
        _fail("Settings couldn't be read as JSON.")
    if size > MAX_JSON_BYTES:
        _fail("Settings are too large.")
    th = _obj(settings.get("thresholds"), "Thresholds")
    out = {
        "focus": _focus(settings.get("focus")),
        "topics": _topics(settings.get("topics")),
        "searches": _searches(settings.get("searches")),
        "watchlist": _handles(settings.get("watchlist"), "Watchlist"),
        "watchlist_min_faves": _num(settings.get("watchlist_min_faves"), "Watchlist min likes", 0, 1_000_000, 20,
                                    integer=True),
        "thresholds": {"min_signal": _num(th.get("min_signal"), "Min signal", 0, 4, 1.8),
                       "min_relevance": _num(th.get("min_relevance"), "Min relevance", 0, 1, 0.6),
                       "max_bait": _num(th.get("max_bait"), "Max bait", 0, 1, 0.5),
                       "min_likes": _num(th.get("min_likes"), "Min likes", 0, 1_000_000, 300, integer=True)},
        "writer_model": _str(settings.get("writer_model"), "Writer model", max_len=120,
                             default="anthropic/claude-sonnet-5") or "anthropic/claude-sonnet-5",
        "timezone": _timezone(settings.get("timezone")),
        "slack": _slack(settings.get("slack")),
        "ranking": _ranking(settings.get("ranking")),
        "tracked": _tracked(settings.get("tracked")),
        "panel": _panel(settings.get("panel")),
        "known_stories": _known(settings.get("known_stories")),
    }
    if not MODEL.match(out["writer_model"]):
        _fail("The writer model should look like provider/model, e.g. anthropic/claude-sonnet-5.")
    pid = settings.get("preset_id")
    if pid is not None:
        pid = _str(pid, "Preset id", max_len=64)
        if pid:
            out["preset_id"] = pid
    if not (out["searches"] or out["watchlist"] or out["tracked"]["accounts"] or
            any(g["accounts"] for g in out["panel"]["groups"])):
        _fail("Add at least one search, watchlist account, tracked company, or panel anchor, "
              "or there's nothing to read.")
    return out


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def load_presets() -> list[dict]:
    """[{id, name, tagline, description, settings}] in display order, each validated."""
    out = []
    for path in PRESETS_DIR.glob("*.json"):
        raw = json.loads(path.read_text())
        meta = raw.pop("preset")
        out.append({"id": meta["id"], "name": meta["name"], "tagline": meta["tagline"],
                    "description": meta["description"], "settings": validate(raw)})
    rank = {k: i for i, k in enumerate(PRESET_ORDER)}
    return sorted(out, key=lambda p: (rank.get(p["id"], len(rank)), p["id"]))


def default_settings() -> dict:
    """What a brand-new user starts with: the original profile, with no preset_id (not picked yet)."""
    raw = json.loads((PRESETS_DIR / f"{DEFAULT_PRESET}.json").read_text())
    raw.pop("preset", None)
    raw.pop("preset_id", None)
    return validate(raw)
