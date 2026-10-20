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


def test_run_left_by_a_dead_process_is_picked_up_again(app, monkeypatch):
    import time
    from server import worker as wmod
    db, worker = app.state.db, app.state.worker
    uid = db.user_by_email("preview@timeline.invalid")["id"]
    # a redeploy killed the old container mid-run: its lease outlives it by at most LEASE seconds
    db.update_run(uid, state="running", lease_owner="old-container", lease_until=time.time() + wmod.LEASE)
    assert db.queue(uid) is False  # still "running" until the lease runs out
    db.update_run(uid, lease_until=time.time() - 1)
    db.queue_due(time.time(), ())
    assert db.run(uid)["state"] == "queued"
    assert worker.run_user(uid) is True


def test_lease_is_renewed_while_a_run_is_going(app, monkeypatch):
    import time
    from server import worker as wmod
    db, worker = app.state.db, app.state.worker
    uid = db.user_by_email("preview@timeline.invalid")["id"]
    monkeypatch.setattr(wmod, "RENEW_EVERY", 0.05)
    monkeypatch.setattr(wmod, "LEASE", 1000)
    db.update_run(uid, lease_owner=worker.owner, lease_until=time.time())
    with worker._holding(uid):
        time.sleep(0.2)
    assert db.run(uid)["lease_until"] > time.time() + 900


def test_deploy_rebuilds_pages_without_api_calls(app, monkeypatch):
    import json as _json
    from datetime import datetime, timedelta, timezone
    db, worker = app.state.db, app.state.worker
    uid = db.user_by_email("preview@timeline.invalid")["id"]
    p = worker.paths(uid)
    p["data"].mkdir(parents=True, exist_ok=True); p["out"].mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    # this week's Monday 00:00 PT, as the pipeline computes it
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo("America/Los_Angeles"))
    ws = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    (p["data"] / "week.json").write_text(_json.dumps({"week_start": ws.isoformat(), "tweets": {}, "thesis_history": [], "latest": None}))
    (p["out"] / "index.html").write_text("old page")
    calls = []
    real = worker._exec
    monkeypatch.setattr(worker, "_exec", lambda args, env, timeout, secrets_, module="pipeline.run":
                        calls.append((args, env)) or real(args, env, timeout, secrets_, module))
    assert worker.rebuild_pages() == 1
    args, env = calls[0]
    assert args == ["--build-only", "--slack", "skip"]
    assert "TWITTERAPI_IO_KEY" not in env and "AI_GATEWAY_API_KEY" not in env and "SLACK_WEBHOOK_URL" not in env
    assert "old page" not in (p["out"] / "index.html").read_text() and "data-get-daily" in (p["out"] / "index.html").read_text()


def test_new_week_carries_over_the_weekend(tmp_path, monkeypatch):
    import json as _json
    from datetime import datetime, timezone
    from pipeline import run
    monkeypatch.setattr(run, "STATE", tmp_path / "week.json")
    monkeypatch.setattr(run, "DATA", tmp_path)
    old_ws = datetime(2026, 10, 12, 7, tzinfo=timezone.utc)
    ws = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)
    tw = lambda i, when, judged=True: {"id": i, "created_at": when, "story": i, **({"jev": {"v": 1}} if judged else {})}
    (tmp_path / "week.json").write_text(_json.dumps({
        "week_start": old_ws.isoformat(), "thesis_history": [{"thesis": "old"}], "latest": {"thesis": "old"},
        "baselines": {"clay": {"median": 40}}, "reach": {"sat": ["a"], "tue": ["b"], "url:x": ["c"]},
        "tweets": {"tue": tw("tue", "Tue Oct 13 18:00:00 +0000 2026"), "sat": tw("sat", "Sat Oct 17 18:00:00 +0000 2026"),
                   "raw": tw("raw", "Sun Oct 18 18:00:00 +0000 2026", judged=False)}}))
    s = run.load_state(ws)
    assert s["week_start"] == ws.isoformat() and list(s["tweets"]) == ["sat"]
    assert s["latest"] is None and s["thesis_history"] == [] and s["baselines"] == {"clay": {"median": 40}}
    assert s["reach"] == {"sat": ["a"], "url:x": ["c"]}
    assert (tmp_path / "archive" / "2026-10-12.json").exists()


def test_a_week_already_under_way_is_backfilled_once(tmp_path, monkeypatch):
    import json as _json
    from datetime import datetime, timezone
    from pipeline import run
    monkeypatch.setattr(run, "STATE", tmp_path / "week.json")
    monkeypatch.setattr(run, "DATA", tmp_path)
    ws = datetime(2026, 10, 19, 7, tzinfo=timezone.utc)  # Monday 00:00 Pacific
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-10-12.json").write_text(_json.dumps({"week_start": "2026-10-12T07:00:00+00:00", "tweets": {
        "sat": {"id": "sat", "created_at": "Sat Oct 17 18:00:00 +0000 2026", "jev": {"v": 1}}}, "reach": {"sat": ["a"]}}))
    (tmp_path / "week.json").write_text(_json.dumps({"week_start": ws.isoformat(), "thesis_history": [], "latest": None,
        "tweets": {"mon": {"id": "mon", "created_at": "Mon Oct 19 18:00:00 +0000 2026", "jev": {"v": 1}}}}))
    s = run.load_state(ws)
    assert set(s["tweets"]) == {"sat", "mon"} and s["reach"] == {"sat": ["a"]} and s["carried"]
    del s["tweets"]["sat"]
    (tmp_path / "week.json").write_text(_json.dumps(s))
    assert set(run.load_state(ws)["tweets"]) == {"mon"}  # only once
