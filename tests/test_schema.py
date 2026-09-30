import copy

import pytest

from server.settings_schema import SettingsError, default_settings, validate


@pytest.fixture
def base():
    return default_settings()


def bad(s, fragment):
    with pytest.raises(SettingsError) as e:
        validate(s)
    assert fragment.lower() in str(e.value).lower(), str(e.value)


def test_defaults_have_no_preset_id(base):
    assert "preset_id" not in base
    assert base["slack"]["hour"] == 8 and "hour_pt" not in base["slack"]


def test_legacy_hour_pt_and_preset_id(base):
    s = copy.deepcopy(base)
    s["slack"] = {"enabled": True, "frequency": "daily", "hour_pt": 9}
    s["preset_id"] = "ai_engineer"
    s["preset"] = {"id": "x"}          # preset metadata is stripped
    s["something_else"] = 1           # unknown keys are dropped
    out = validate(s)
    assert out["slack"]["hour"] == 9 and out["preset_id"] == "ai_engineer"
    assert "preset" not in out and "something_else" not in out


def test_limits(base):
    s = copy.deepcopy(base); s["searches"] = ["AI min_faves:100"] * 41
    bad(s, "limit is 40")
    s = copy.deepcopy(base); s["watchlist"] = [f"user{i}" for i in range(301)]
    bad(s, "limit is 300")
    s = copy.deepcopy(base); s["watchlist"] = ["not a handle!"]
    bad(s, "valid X handle")
    s = copy.deepcopy(base); s["focus"]["audience"] = "x" * 2001
    bad(s, "too long")
    s = copy.deepcopy(base); s["searches"] = ["x" * 513]
    bad(s, "too long")
    s = copy.deepcopy(base); s["known_stories"] = [{"label": "a", "match": "(unclosed"}]
    bad(s, "regular expression")
    s = copy.deepcopy(base); s["known_stories"] = [{"label": "a", "match": "a" * 301}]
    bad(s, "too long")
    s = copy.deepcopy(base); s["timezone"] = "Mars/Olympus"
    bad(s, "time zone")
    s = copy.deepcopy(base); s["thresholds"]["min_signal"] = 9
    bad(s, "between")
    s = copy.deepcopy(base)
    for t in s["topics"]:
        t["share"] = 0.5
    bad(s, "add up")
    s = copy.deepcopy(base); s["writer_model"] = "not a model"
    bad(s, "provider/model")
    s = copy.deepcopy(base); s["panel"]["groups"][0]["accounts"] = [f"a{i}" for i in range(120)]
    bad(s, "anchor")
    bad("nope", "object")


def test_min_likes_default_and_presets():
    from server.settings_schema import load_presets, validate
    s = validate({**load_presets()[0]["settings"], "thresholds": {"min_signal": 1.8, "min_relevance": 0.6, "max_bait": 0.5}})
    assert s["thresholds"]["min_likes"] == 300  # older profiles get a floor
    by_id = {p["id"]: p["settings"]["thresholds"]["min_likes"] for p in load_presets()}
    assert by_id["ai_startup_marketing"] == 500 and by_id["gtm_sales"] == 250
