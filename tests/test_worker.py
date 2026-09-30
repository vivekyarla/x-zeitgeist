import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, signup


def test_subprocess_env_is_minimal(client, app, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "server-secret-value")
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "someone-elses-key")
    monkeypatch.setenv("SOME_OTHER_VAR", "x")
    w = app.state.worker
    env = w.env_for(7, {"twitterapi_io": "mine-123456", "ai_gateway": "gw-123456"}, "tok")
    assert env["TWITTERAPI_IO_KEY"] == "mine-123456" and env["AI_GATEWAY_API_KEY"] == "gw-123456"
    assert "SECRET_KEY" not in env and "SOME_OTHER_VAR" not in env and "SLACK_WEBHOOK_URL" not in env
    assert env["TIMELINE_SETTINGS"].endswith(os.path.join("users", "7", "settings.json"))
    assert env["TIMELINE_RECAP_URL"] == "http://localhost:8000/r/tok/"
    assert env["PAGE_URL"] == "http://localhost:8000/app/"
    env2 = w.env_for(8, {}, "tok")
    assert "TWITTERAPI_IO_KEY" not in env2


def test_logs_are_redacted(client, app):
    signup(client)
    uid = app.state.db.user_by_email("ana@example.com")["id"]
    client.put("/api/keys", json={"twitterapi_io": "tw-secret-999999", "ai_gateway": "gw-secret-888888"})
    w = app.state.worker
    code, text = w._exec(["--help"], {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT),
                                      "X": "tw-secret-999999"}, 60, ["tw-secret-999999"])
    assert code == 0 and "tw-secret-999999" not in text


def test_user_dirs_use_integer_ids(app):
    with pytest.raises((ValueError, TypeError)):
        app.state.cfg.user_dir("../../etc")


def test_scheduler_queues_due_users(client, app):
    signup(client)
    db = app.state.db
    uid = db.user_by_email("ana@example.com")["id"]
    client.put("/api/keys", json={"twitterapi_io": "tw-123456", "ai_gateway": "gw-123456"})
    db.set_onboarded(uid)
    db.update_run(uid, state="ready", next_run_at=1.0)
    assert db.queue_due(10.0, ("twitterapi_io", "ai_gateway")) == 1
    assert db.run(uid)["state"] == "queued"
    # the lease: a second claimer loses
    assert db.claim(uid, "a", 60, start_run=True) is True
    assert db.claim(uid, "b", 60, start_run=True) is False
    db.release(uid, "a")


def test_pipeline_demo_honors_env(tmp_path):
    settings = json.loads((ROOT / "pipeline" / "presets" / "ai_engineer.json").read_text())
    settings.pop("preset")
    settings["timezone"] = "Europe/London"
    (tmp_path / "s.json").write_text(json.dumps(settings))
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "TIMELINE_SETTINGS": str(tmp_path / "s.json"),
           "TIMELINE_DATA": str(tmp_path / "data"), "TIMELINE_OUT": str(tmp_path / "out"),
           "PAGE_URL": "https://t.example/app/", "TIMELINE_RECAP_URL": "https://t.example/r/abc/"}
    subprocess.run([sys.executable, "-m", "pipeline.run", "--demo"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    for f in ("index.html", "data.json", "recap.json", "recap.md"):
        assert (tmp_path / "out" / f).exists()
    recap = json.loads((tmp_path / "out" / "recap.json").read_text())
    assert recap["links"] == {"page": "https://t.example/app/", "recap_json": "https://t.example/r/abc/recap.json",
                              "recap_md": "https://t.example/r/abc/recap.md"}
    code = ("from pipeline import config, run; from datetime import datetime, timezone;"
            "print(config.TIMEZONE, config.SLACK['hour'], config.DATA_DIR,"
            " run.week_start(datetime(2026, 9, 30, 12, tzinfo=timezone.utc)).isoformat())")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True,
                         text=True).stdout.split()
    assert out[0] == "Europe/London" and out[1] == "8" and out[2] == str(tmp_path / "data")
    assert out[3] == "2026-09-27T23:00:00+00:00"  # Monday 00:00 BST


def test_legacy_hour_pt(tmp_path):
    s = json.loads((ROOT / "settings.json").read_text())
    s["slack"] = {"enabled": True, "frequency": "daily", "hour_pt": 10}
    s.pop("timezone", None)
    (tmp_path / "s.json").write_text(json.dumps(s))
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(ROOT), "TIMELINE_SETTINGS": str(tmp_path / "s.json")}
    out = subprocess.run([sys.executable, "-c", "from pipeline import config; print(config.SLACK['hour'], config.TIMEZONE)"],
                         cwd=ROOT, env=env, check=True, capture_output=True, text=True).stdout.split()
    assert out == ["10", "America/Los_Angeles"]


def test_empty_run_explains_itself():
    from server.worker import empty_reason
    msg = empty_reason("fetched 812 tweets\njudged 812 tweets\nnothing passed the filter; leaving the page as it was")
    assert "812 tweets" in msg and "Wider net" in msg
    msg = empty_reason("  search failed (402 Payment Required): x\nfetched 0 tweets\nnothing passed the filter")
    assert "try again in 30 minutes" in msg


def test_small_accounts_need_the_likes_floor(monkeypatch):
    from pipeline import config, judge
    monkeypatch.setattr(config, "MIN_LIKES", 500)
    monkeypatch.setattr(config, "TRACKED_LOWER", {"clay"})
    jev = {"topic": "gtm_sales", "bait": 0.1, "relevant": 0.9, "signal": 3.0}
    dude = {"author": {"handle": "randomdude"}, "likes": 100, "jev": jev}
    assert judge.why_not(dude, {}) == "100 likes, under 500"
    assert judge.why_not({**dude, "likes": 800}, {}) is None
    # a tracked company that beat its usual still counts under the global floor
    clay = {"author": {"handle": "clay"}, "likes": 150, "jev": jev}
    assert judge.why_not(clay, {"clay": {"n": 20, "median": 30, "at": "2026-09-30T00:00:00+00:00"}}) is None


def test_tracked_company_needs_a_real_bar_too(monkeypatch):
    from pipeline import config, judge
    monkeypatch.setattr(config, "TRACKED_LOWER", {"clay"})
    monkeypatch.setattr(config, "TRACKED_MIN_LIKES", 150)
    monkeypatch.setattr(config, "TRACKED_MIN_SIGNAL", 1.6)
    base = {"clay": {"n": 20, "median": 15, "at": "2026-09-30T00:00:00+00:00"}}
    small = {"author": {"handle": "clay"}, "likes": 46,
             "jev": {"topic": "gtm_sales", "bait": 0.1, "relevant": 0.9, "signal": 1.2}}
    assert judge.why_not(small, base) == "46 likes, under 150 (3x its usual 15)"
    big = {**small, "likes": 400, "jev": {**small["jev"], "signal": 2.1}}
    assert judge.why_not(big, base) is None  # well past its usual, and notable: still counts
    assert judge.why_not({**big, "jev": small["jev"]}, base) == "signal 1.20"
