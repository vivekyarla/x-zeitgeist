import json

from server.settings_schema import PRESETS_DIR, load_presets, validate
from conftest import ROOT

REQUIRED = {"ai_engineer", "startup_culture", "gtm_sales", "marketing_brand", "ai_business", "ai_startup_marketing"}


def test_every_preset_validates_and_is_coherent():
    files = sorted(PRESETS_DIR.glob("*.json"))
    assert REQUIRED <= {f.stem for f in files}
    for f in files:
        raw = json.loads(f.read_text())
        meta = raw.pop("preset")
        assert meta["id"] == f.stem
        assert meta["name"] and meta["description"]
        assert 0 < len(meta["tagline"]) <= 60, meta
        s = validate(raw)  # the same validator PUT /api/settings and import use
        inc = [t for t in s["topics"] if t["include"] and t["key"] != "other"]
        assert abs(sum(t["share"] for t in inc) - 1) < 0.02, f.stem
        assert any(t["key"] == "other" and not t["include"] for t in s["topics"]), f.stem
        assert s["searches"] and all("min_faves:" in q for q in s["searches"]), f.stem
        assert all(len(g["accounts"]) >= 2 for g in s["panel"]["groups"]), f.stem
        assert s["watchlist"] and s["focus"]["care_about"] and s["focus"]["skip"]
        assert s["slack"]["enabled"] is False
        assert s["timezone"] == "America/Los_Angeles"
        assert s["writer_model"]


def test_ai_business_includes_infra():
    s = {p["id"]: p for p in load_presets()}["ai_business"]["settings"]
    assert any(t["key"] == "industry_finance" and t["include"] and t["share"] > 0 for t in s["topics"])


def test_original_profile_is_the_default_preset():
    presets = {p["id"]: p for p in load_presets()}
    assert list(presets)[:2] == ["sf_tech", "ai_startup_marketing"]  # the general feed is offered first
    assert presets["ai_startup_marketing"]["settings"] == validate(json.loads((ROOT / "settings.json").read_text()))


def test_gtm_tracks_the_original_companies():
    orig = json.loads((ROOT / "settings.json").read_text())["tracked"]["accounts"]
    s = {p["id"]: p for p in load_presets()}["gtm_sales"]["settings"]
    assert s["tracked"]["accounts"] == orig
    anchors = {h for g in s["panel"]["groups"] for h in g["accounts"]}
    assert set(orig) | {"jasonlk", "samdblond", "RobHoffman_"} <= anchors


def test_example_page_feed_is_general():
    from server import config
    s = {p["id"]: p for p in load_presets()}["sf_tech"]["settings"]
    shown = {t["key"] for t in s["topics"] if t["include"]}
    assert {"gtm_marketing", "engineering"}.isdisjoint(shown) and "launches" in shown
    assert not s["tracked"]["accounts"] and s["thresholds"]["min_likes"] >= 1000
    assert config.Config.__dataclass_fields__["preview_preset"].default == "sf_tech"
