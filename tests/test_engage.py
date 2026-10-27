"""The daily engagement DM: filtering, ranking, output cleanup, Slack, and the schedule."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import engage

NOW = datetime(2026, 10, 12, 17, 50, tzinfo=timezone.utc)  # 10:50 PT


def tw(i, handle, followers, likes, bio="", hours=3, text=None, rts=0):
    posted = NOW - timedelta(hours=hours)
    return {"id": str(i), "url": f"https://x.com/{handle}/status/{i}", "text": text or f"post {i} by {handle}",
            "created_at": posted.strftime("%a %b %d %H:%M:%S %z %Y"), "likes": likes, "retweets": rts,
            "replies": 0, "quotes": 0, "views": 0, "is_retweet": False, "is_reply": False, "urls": [], "ref": None,
            "author": {"handle": handle, "name": handle.title(), "followers": followers, "bio": bio}}


@pytest.fixture
def cfg():
    return engage.load_config()


def test_config_has_all_six_people(cfg):
    assert [p["name"] for p in cfg["people"]] == ["Ishan", "Shriram", "Gopal", "Dan", "Taeuk", "Brian"]
    assert cfg["min_followers"] == 1500


def test_prominence_filter(cfg):
    team = {"ishanmkh"}
    big = tw(1, "bigacct", 40_000, 300)
    small = tw(2, "randomdude", 400, 300)
    ramp = tw(3, "rampeng", 600, 80, bio="building payments @tryramp")
    word = tw(4, "onramp", 600, 80, bio="the on-ramp to crypto")  # "ramp" as a word doesn't count
    oai = tw(5, "oaistaff", 900, 60, bio="Member of technical staff at OpenAI")
    tiny = tw(6, "freshoai", 50, 60, bio="intern @OpenAI")  # affiliated but too new/small
    own = tw(7, "ishanmkh", 20_000, 900)
    low = tw(8, "bigbutquiet", 90_000, 10)
    ok = {t["id"] for t in [big, small, ramp, word, oai, tiny, own, low] if engage.eligible(t, cfg, team)}
    assert ok == {"1", "3", "5"}
    assert ramp["org"] == "Ramp" and oai["org"] == "OpenAI"


def test_ranking_prefers_velocity_and_caps_authors(cfg):
    fast = tw(1, "a", 5000, 600, hours=2)
    slow = tw(2, "b", 5000, 900, hours=22)
    same = [tw(10 + i, "c", 5000, 400, hours=3) for i in range(4)]
    out = engage.candidates([slow, fast, *same], cfg, NOW)
    assert out[0]["id"] == "1"
    assert sum(1 for t in out if t["author"]["handle"] == "c") == 2


def test_clean_drops_made_up_ids_and_trims(cfg):
    cands = [tw(1, "a", 5000, 600), tw(2, "b", 5000, 500)]
    raw = {"headline": "Agents everywhere " + "word " * 30,
           "people": [{"name": "gopal", "picks": [{"id": "1", "why": "x " * 40}, {"id": "999", "why": "fake"}],
                       "topics": [{"topic": "RL scaling debate", "ids": ["2", "404"], "why": "his research area"}]},
                      {"name": "Nobody", "picks": [{"id": "2", "why": "?"}]}]}
    out = engage.clean(raw, cfg, cands)
    g = next(p for p in out["people"] if p["name"] == "Gopal")
    assert [x["id"] for x in g["picks"]] == ["1"] and len(g["picks"][0]["why"].split()) <= 14
    assert g["topics"][0]["ids"] == ["2"]
    assert [p["name"] for p in out["people"]] == ["Ishan", "Shriram", "Gopal", "Dan", "Taeuk", "Brian"]
    assert len(out["headline"].split()) <= 16


def test_message_is_short_and_linked(cfg):
    cands = [tw(1, "karpathy", 1_000_000, 9000, text="New paper on RL for agents <with> details " * 5)]
    picks = {"headline": "RL for agents is the story", "people": [
        {"name": "Gopal", "picks": [{"id": "1", "why": "his exact research area"}],
         "topics": [{"topic": "RL for agents", "ids": ["1"], "why": "lead the technical take"}]},
        *[{"name": n, "picks": [], "topics": []} for n in ("Ishan", "Shriram", "Dan", "Taeuk", "Brian")]]}
    msg = engage.message(picks, cands, cfg, NOW, "America/Los_Angeles")
    assert msg["blocks"][0]["text"]["text"] == "What to engage with · Mon Oct 12"
    gopal = next(b for b in msg["blocks"] if b.get("text", {}).get("text", "").startswith("*Gopal*"))["text"]["text"]
    assert "<https://x.com/karpathy/status/1|@karpathy>" in gopal and "&lt;with&gt;" in gopal
    assert "_his exact research area_" in gopal and "*Topic:* RL for agents" in gopal
    assert all(len(b.get("text", {}).get("text", "")) < 3000 for b in msg["blocks"])
    assert msg["unfurl_links"] is False


def test_send_opens_a_dm_and_posts(monkeypatch):
    calls = []

    class R:
        def __init__(self, body): self.body, self.status_code, self.headers = body, 200, {"content-type": "application/json"}
        def json(self): return self.body

    def fake(method, url, **kw):
        calls.append((method, url.rsplit("/", 1)[1], kw.get("params") or kw.get("json")))
        name = url.rsplit("/", 1)[1]
        return R({"ok": True, "user": {"id": "U1"}} if name == "users.lookupByEmail" else
                 {"ok": True, "channel": {"id": "D1"}} if name == "conversations.open" else {"ok": True})
    monkeypatch.setattr(engage.requests, "request", fake)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ENGAGE_DM_EMAIL", "vivek@rox.com")
    monkeypatch.delenv("ENGAGE_DM_USER", raising=False)
    engage.send({"text": "hi", "blocks": []})
    assert [c[1] for c in calls] == ["users.lookupByEmail", "conversations.open", "chat.postMessage"]
    assert calls[0][2] == {"email": "vivek@rox.com"} and calls[2][2]["channel"] == "D1"


def test_slack_errors_are_explained(monkeypatch):
    class R:
        status_code, headers = 200, {"content-type": "application/json"}
        def json(self): return {"ok": False, "error": "missing_scope"}
    monkeypatch.setattr(engage.requests, "request", lambda *a, **k: R())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ENGAGE_DM_USER", "U1")
    with pytest.raises(RuntimeError, match="chat:write, im:write"):
        engage.send({"text": "hi", "blocks": []})


def make_app(tmp_path, monkeypatch, **env):
    for k, v in {"DATA_DIR": str(tmp_path / "data"), "TIMELINE_DEMO": "1", "TIMELINE_WORKER": "0",
                 "SECRET_KEY": "test-secret-key-that-is-long-enough", "ENGAGE_SLACK_BOT_TOKEN": "xoxb-1",
                 "ENGAGE_DM_EMAIL": "Vivek@rox.com", "PREVIEW_TWITTERAPI_IO_KEY": "tw",
                 "PREVIEW_AI_GATEWAY_API_KEY": "gw", **env}.items():
        monkeypatch.setenv(k, v)
    from server import config
    from server.app import create_app
    return create_app(config.load())


def at(hour, minute=0, day=12):
    from zoneinfo import ZoneInfo
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()


def test_schedule(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    assert app.state.cfg.engage
    assert w.engage_due(at(21))  # never sent: send one right away
    db.set_meta("engage_sent_date", "first")
    assert not w.engage_due(at(10, 40))       # before 11:00 minus the lead
    assert w.engage_due(at(10, 55))           # starts a few minutes early so it lands at 11
    db.set_meta("engage_tried_at", str(at(10, 55)))
    assert not w.engage_due(at(11, 5))        # a failed try waits 15 minutes
    assert w.engage_due(at(11, 15))
    db.set_meta("engage_sent_date", "2026-10-12")
    assert not w.engage_due(at(11, 30))       # sent today
    assert w.engage_due(at(11, 0, day=13))    # tomorrow
    assert not w.engage_due(at(16, 0, day=13))  # gives up mid-afternoon
    db.set_meta("engage_force", "1")
    assert w.engage_due(at(20, 0, day=13))    # "send now"


def test_run_records_the_send(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    seen = {}

    def fake_exec(args, env, timeout, secrets_, module="pipeline.run"):
        seen.update(module=module, args=args, env=env)
        return 0, "fetched 300 tweets\n140 eligible, 120 sent to the model\nslack: sent\n"
    monkeypatch.setattr(w, "_exec", fake_exec)
    assert w.run_engage()
    assert seen["module"] == "pipeline.engage" and seen["args"] == ["--send"]
    env = seen["env"]
    assert env["TWITTERAPI_IO_KEY"] == "tw" and env["SLACK_BOT_TOKEN"] == "xoxb-1" and env["ENGAGE_DM_EMAIL"] == "vivek@rox.com"
    assert "SECRET_KEY" not in env
    assert db.meta("engage_sent_date") and db.meta("engage_last_error") == ""


def test_owner_only_routes(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    me, other = TestClient(app), TestClient(app)
    me.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    other.post("/api/signup", json={"email": "someone@rox.com", "password": "long enough pw"})
    assert other.post("/api/engage/run").status_code == 403
    st = me.get("/api/engage/status").json()
    assert st["enabled"] and st["time"] == "11:00" and not st["missing"]
    r = me.post("/api/engage/run")
    assert r.status_code == 200 and app.state.db.meta("engage_force") == "1"


def test_off_until_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGAGE_SLACK_BOT_TOKEN", raising=False)
    app = make_app(tmp_path, monkeypatch, ENGAGE_SLACK_BOT_TOKEN="")
    assert not app.state.cfg.engage
    me = TestClient(app)
    me.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    r = me.post("/api/engage/run")
    assert r.status_code == 400 and "ENGAGE_SLACK_BOT_TOKEN" in r.json()["error"]


def test_send_to_a_channel(monkeypatch):
    calls = []

    class R:
        status_code, headers = 200, {"content-type": "application/json"}
        def json(self): return {"ok": True}
    monkeypatch.setattr(engage.requests, "request", lambda m, url, **kw: calls.append((url.rsplit("/", 1)[1], kw["json"])) or R())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("ENGAGE_DM_EMAIL", "vivek@rox.com")
    monkeypatch.setenv("ENGAGE_SLACK_CHANNEL", "#C0ENGAGE")
    engage.send({"text": "hi", "blocks": []})
    assert calls == [("chat.postMessage", {"channel": "C0ENGAGE", "text": "hi", "blocks": []})]
    calls.clear()
    engage.send({"text": "hi", "blocks": []}, "C0FOLLOW")  # the follow recommendations' own channel
    assert calls[0][1]["channel"] == "C0FOLLOW"


def test_switching_to_a_channel_posts_right_away(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    db.set_meta("engage_sent_date", "2026-10-12"); db.set_meta("engage_tried_at", str(at(11)))
    assert not w.engage_due(at(14))  # first check only records the destination
    app2 = make_app(tmp_path, monkeypatch, ENGAGE_SLACK_CHANNEL="C0ENGAGE")
    assert app2.state.cfg.follow_slack_channel == "C0ENGAGE"
    assert app2.state.worker.engage_due(at(14, 5))
    seen = {}
    monkeypatch.setattr(app2.state.worker, "_exec", lambda args, env, timeout, secrets_, module="pipeline.run":
                        (seen.update(env=env) or (0, "slack: sent\n")))
    app2.state.worker.run_engage()
    assert seen["env"]["ENGAGE_SLACK_CHANNEL"] == "C0ENGAGE"


def test_one_off_for_one_person(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, ENGAGE_SLACK_CHANNEL="C0ENGAGE")
    w, db = app.state.worker, app.state.db
    me = TestClient(app)
    me.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    assert me.post("/api/engage/run", json={"person": "nobody"}).status_code == 400
    assert me.post("/api/engage/run", json={"person": "Shriram", "picks": 50}).status_code == 422
    r = me.post("/api/engage/run", json={"person": "@shriram_s", "picks": 12, "hours": 48})
    assert r.status_code == 200 and "Shriram" in r.json()["message"] and db.meta("engage_force") != "1"
    seen = {}
    monkeypatch.setattr(w, "_exec", lambda args, env, timeout, secrets_, module="pipeline.run":
                        (seen.update(env=env) or (0, "slack: sent\n")))
    db.set_meta("engage_sent_date", "2026-10-12")
    assert w.run_engage(oneoff=True)
    env = seen["env"]
    assert (env["ENGAGE_ONLY"], env["ENGAGE_PICKS"], env["ENGAGE_HOURS"]) == ("Shriram", "12", "48")
    assert env["ENGAGE_SLACK_CHANNEL"] == "" and env["ENGAGE_DM_EMAIL"] == "vivek@rox.com"  # DMed to you
    assert db.meta("engage_oneoff") is None and db.meta("engage_sent_date") == "2026-10-12"  # daily untouched
    assert me.get("/api/engage/status").json()["last_oneoff"]["ok"]


def test_one_person_config(cfg):
    one = engage.one_person(cfg, "shriram", 10)
    assert [p["name"] for p in one["people"]] == ["Shriram"] and one["picks_per_person"] == 10
    with pytest.raises(SystemExit):
        engage.one_person(cfg, "nobody", 10)
