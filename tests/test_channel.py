"""The marketing channel digest: the editable GTM archetype, market intel, and the post."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pipeline import channel

NOW = datetime(2026, 10, 13, 16, 30, tzinfo=timezone.utc)


def test_html_text_keeps_the_words():
    html = "<html><head><style>x{}</style></head><body><nav>Menu</nav><h1>Market</h1><script>bad()</script>" \
           "<p>Clay raised $100M.</p><ul><li>One</li><li>Two</li></ul></body></html>"
    t = channel.html_text(html)
    assert "Clay raised $100M." in t and "- One" in t and "bad()" not in t and "Menu" not in t


class Resp:
    def __init__(self, text, ctype="text/html", status=200):
        self.text, self.status_code, self.headers = text, status, {"content-type": ctype}

    def json(self):
        return json.loads(self.text)


def test_intel_prefers_a_fresh_pushed_copy(tmp_path, monkeypatch):
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"received_at": (NOW - timedelta(hours=2)).isoformat(), "text": "Pushed report " * 20, "url": ""}))
    monkeypatch.setenv("INTEL_FILE", str(f))
    monkeypatch.setenv("INTEL_URL", "https://grok.example/report")
    monkeypatch.setattr(channel.requests, "get", lambda *a, **k: pytest.fail("shouldn't fetch"))
    intel, note = channel.load_intel(NOW)
    assert intel["source"] == "pushed" and intel["url"] == "https://grok.example/report" and not note


def test_intel_falls_back_to_the_site(tmp_path, monkeypatch):
    f = tmp_path / "latest.json"
    f.write_text(json.dumps({"received_at": (NOW - timedelta(days=3)).isoformat(), "text": "old " * 50}))
    monkeypatch.setenv("INTEL_FILE", str(f))
    monkeypatch.setenv("INTEL_URL", "https://grok.example/report")
    page = "<html><body>" + "<p>HubSpot cut prices for AI seats by 30 percent this week.</p>" * 10 + "</body></html>"
    monkeypatch.setattr(channel.requests, "get", lambda *a, **k: Resp(page))
    intel, note = channel.load_intel(NOW)
    assert intel["source"] == "fetched" and "HubSpot cut prices" in intel["text"]


def test_javascript_only_site_is_explained(monkeypatch):
    monkeypatch.delenv("INTEL_FILE", raising=False)
    monkeypatch.setenv("INTEL_URL", "https://grok.example/report")
    monkeypatch.setattr(channel.requests, "get", lambda *a, **k: Resp("<html><body><div id=root></div><script src=a.js></script></body></html>"))
    intel, note = channel.load_intel(NOW)
    assert intel is None and "JavaScript" in note


REP = {"thesis": "Signal-based outbound is eating spray-and-pray.", "page_url": "https://x.example/gtm/",
       "themes": [], "writer_model": "m",
       "tweets": [{"id": "1", "url": "https://x.com/clay/status/1", "author": "clay", "text": "t", "likes": 900,
                   "theme": None, "hours_since_seen": 3},
                  {"id": "2", "url": "https://x.com/jasonlk/status/2", "author": "jasonlk", "text": "t", "likes": 400,
                   "theme": None, "hours_since_seen": 50}]}


def test_clean_and_message(monkeypatch):
    monkeypatch.setenv("INTEL_URL", "https://grok.example/report")
    raw = {"bullets": [{"text": "Clay ships signals " + "x " * 30, "ids": ["1", "999"]}, {"text": "made up", "ids": ["404"]},
                       {"text": "SDR teams shrink as agents land", "ids": ["2"]}],
           "market": [{"text": "HubSpot cuts AI seat pricing 30%"}, {"text": "Apollo raises"}],
           "trends": [{"text": "Buyers want usage pricing"}]}
    intel = {"text": "...", "url": "https://grok.example/report", "source": "fetched"}
    d = channel.clean(raw, REP, intel)
    assert [b["ids"] for b in d["bullets"]] == [["1"], ["2"]] and len(d["bullets"][0]["text"].split()) <= 18
    msg = channel.message(d, REP, intel, "", NOW, "America/Los_Angeles")
    top = msg["blocks"][0]["text"]["text"]
    assert top.startswith("*X zeitgeist · Tue Oct 13*  ·  <https://x.example/gtm/|Full report>")
    assert "<https://x.com/clay/status/1|@clay>" in top and "_Signal-based outbound" in top
    intel_block = msg["blocks"][2]["text"]["text"]
    assert "<https://grok.example/report|Full report>" in intel_block and "*Also trending*" in intel_block
    # without intel: no invented market section, just the link and why
    d2 = channel.clean(raw, REP, None)
    assert d2["market"] == [] and d2["trends"] == []
    msg2 = channel.message(d2, REP, None, "the market intel page has no readable text", NOW, "America/Los_Angeles")
    tail = msg2["blocks"][-1]["elements"][0]["text"]
    assert "<https://grok.example/report|Full report>" in tail and "couldn't summarize today: the market intel page" in tail


def test_post_explains_a_missing_invite(monkeypatch):
    class R:
        status_code, headers = 200, {"content-type": "application/json"}
        def json(self): return {"ok": False, "error": "not_in_channel"}
    from pipeline import engage
    monkeypatch.setattr(engage.requests, "request", lambda *a, **k: R())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("CHANNEL_SLACK_CHANNEL", "#marketing")
    with pytest.raises(RuntimeError, match="/invite"):
        channel.post({"text": "x", "blocks": []})


# ---------------------------------------------------------------------------
def make_app(tmp_path, monkeypatch, **env):
    base = {"DATA_DIR": str(tmp_path / "data"), "TIMELINE_DEMO": "1", "TIMELINE_WORKER": "0",
            "SECRET_KEY": "test-secret-key-that-is-long-enough", "ENGAGE_SLACK_BOT_TOKEN": "xoxb-1",
            "ENGAGE_DM_EMAIL": "vivek@rox.com", "PREVIEW_TWITTERAPI_IO_KEY": "tw", "PREVIEW_AI_GATEWAY_API_KEY": "gw",
            "CHANNEL_SLACK_CHANNEL": "C0123", "INTEL_URL": "https://grok.example/report", "INTEL_PUSH_TOKEN": "push-secret"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    from server import config
    from server.app import create_app
    return create_app(config.load())


def test_archetype_is_editable_by_admins_only_and_keeps_edits(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    db, w = app.state.db, app.state.worker
    uid = db.user_by_email("gtm@timeline.invalid")["id"]
    assert json.loads(db.user(uid)["settings"])["preset_id"] == "gtm_marketing"
    assert w.keys(uid) == {"twitterapi_io": "tw", "ai_gateway": "gw"}

    admin, other, anon = TestClient(app), TestClient(app), TestClient(app)
    admin.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    other.post("/api/signup", json={"email": "sam@rox.com", "password": "long enough pw"})
    H = {"X-Profile": "gtm"}
    me = admin.get("/api/me", headers=H).json()
    assert me["page_url"].endswith("/gtm/") and me["settings"]["preset_id"] == "gtm_marketing"
    assert other.get("/api/me", headers=H).status_code == 403
    assert anon.get("/api/me", headers=H).status_code == 401
    s = me["settings"]
    s["watchlist"] = ["clay", "lennysan", "newperson"]
    assert admin.put("/api/settings", json={"settings": s}, headers=H).status_code == 200
    assert "newperson" in json.loads(db.user(uid)["settings"])["watchlist"]
    assert "newperson" not in admin.get("/api/me").json()["settings"]["watchlist"]  # their own profile untouched
    # keys, export, deleting: never through the archetype
    assert admin.put("/api/keys", json={"twitterapi_io": "x"}, headers=H).status_code == 403
    assert admin.get("/api/export", headers=H).status_code == 403
    assert admin.request("DELETE", "/api/account", json={"password": "long enough pw"}, headers=H).status_code == 403
    # a restart keeps the edits (the preset isn't re-applied)
    app2 = make_app(tmp_path, monkeypatch)
    assert "newperson" in json.loads(app2.state.db.user(uid)["settings"])["watchlist"]


def test_gtm_page_is_public(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    c = TestClient(app)
    assert "being built" in c.get("/gtm/").text
    uid = app.state.db.user_by_email("gtm@timeline.invalid")["id"]
    assert app.state.worker.run_user(uid)
    assert 'class="thesis"' in c.get("/gtm/").text and c.get("/gtm/recap.md").status_code == 200


def test_off_until_a_channel_is_set(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, CHANNEL_SLACK_CHANNEL="")
    assert not app.state.cfg.channel and app.state.db.user_by_email("gtm@timeline.invalid") is None
    assert TestClient(app).get("/gtm/").status_code == 404


def test_intel_push(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    c = TestClient(app)
    assert c.post("/api/intel", json={"text": "x" * 100}).status_code == 401
    assert c.post("/api/intel", json={"text": "x" * 100}, headers={"Authorization": "Bearer nope"}).status_code == 401
    ok = {"Authorization": "Bearer push-secret"}
    assert c.post("/api/intel", json={"text": "short"}, headers=ok).status_code == 422
    r = c.post("/api/intel", json={"html": "<p>" + "Gong launched an AI forecasting agent. " * 5 + "</p>",
                                   "url": "https://grok.example/report"}, headers=ok)
    assert r.status_code == 200
    saved = json.loads((tmp_path / "data" / "intel" / "latest.json").read_text())
    assert saved["text"].startswith("Gong launched") and saved["url"] == "https://grok.example/report"
    r = c.post("/api/intel", content="Plain markdown report about pricing moves at Apollo and Outreach.",
               headers={**ok, "Content-Type": "text/markdown"})
    assert r.status_code == 200


def test_channel_schedule_and_run(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    at = lambda h, m=0, d=13: datetime(2026, 10, d, h, m, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert not w.channel_due(at(9, 30))  # no report yet: nothing to summarize
    (w.paths(w.channel_uid)["data"]).mkdir(parents=True, exist_ok=True)
    (w.paths(w.channel_uid)["data"] / "week.json").write_text(json.dumps({"latest": {"thesis": "x"}}))
    assert w.channel_due(at(20))  # first time: right away
    db.set_meta("channel_sent_date", "first")
    assert not w.channel_due(at(9, 20)) and w.channel_due(at(9, 29))
    assert not w.channel_due(at(9, 30, d=17))  # Saturday: weekdays only by default
    seen = {}
    monkeypatch.setattr(w, "_exec", lambda args, env, timeout, secrets_, module="pipeline.run":
                        (seen.update(module=module, env=env) or (0, "intel: fetched 5000 chars\nslack: posted\n")))
    assert w.run_channel()
    env = seen["env"]
    assert seen["module"] == "pipeline.channel" and env["CHANNEL_SLACK_CHANNEL"] == "C0123"
    assert env["PAGE_URL"].endswith("/gtm/") and env["INTEL_URL"] == "https://grok.example/report"
    assert env["SLACK_BOT_TOKEN"] == "xoxb-1" and "SECRET_KEY" not in env and "TWITTERAPI_IO_KEY" not in env
    assert db.meta("channel_sent_date")


def test_admin_channel_routes(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, ADMIN_EMAILS="vivek@rox.com, ana@rox.com")
    a, b = TestClient(app), TestClient(app)
    a.post("/api/signup", json={"email": "ana@rox.com", "password": "long enough pw"})
    b.post("/api/signup", json={"email": "sam@rox.com", "password": "long enough pw"})
    st = a.get("/api/channel/status").json()
    assert st["enabled"] and st["channel"] == "C0123" and st["time"] == "09:30" and st["weekdays_only"]
    assert b.get("/api/channel/status").status_code == 403
    assert a.post("/api/channel/run").status_code == 200 and app.state.db.meta("channel_force") == "1"


def test_comp_intel_today_json(monkeypatch):
    """The Grok bot's site (vivekyarla.github.io/comp-intel) renders in the browser from data/today.json:
    read that, use its market briefing and market direction, and link the page and each item's source."""
    from pathlib import Path
    raw = (Path(__file__).parent / "fixtures" / "comp_intel_today.json").read_text()
    monkeypatch.delenv("INTEL_FILE", raising=False)
    monkeypatch.setenv("INTEL_URL", "https://vivekyarla.github.io/comp-intel/data/today.json")
    monkeypatch.setenv("INTEL_PAGE_URL", "https://vivekyarla.github.io/comp-intel/")
    monkeypatch.setattr(channel.requests, "get", lambda *a, **k: Resp(raw, "application/json; charset=utf-8"))
    intel, note = channel.load_intel(NOW)
    assert intel and not note and intel["url"] == "https://vivekyarla.github.io/comp-intel/"
    assert intel["text"].startswith("As of: 2026-10-13") and "Market direction (trends):" in intel["text"]
    src = json.loads(raw)["market_briefing"]["bullets"][0]["cites"][0]["url"]
    assert src in intel["links"]
    d = channel.clean({"bullets": [{"text": "x", "ids": ["1"]}],
                       "market": [{"text": "Docusign agreements go native in Agentforce and Slack", "url": src},
                                  {"text": "Seismic opens content to assistants via MCP", "url": "https://made.up/x"}],
                       "trends": [{"text": "Accelerating: MCP as the GTM connector"}]}, REP, intel)
    assert d["market"][0]["url"] == src and d["market"][1]["url"] == ""
    msg = channel.message(d, REP, intel, "", NOW, "America/Los_Angeles")
    block = msg["blocks"][2]["text"]["text"]
    assert "<https://vivekyarla.github.io/comp-intel/|Full report>" in block and f"<{src}|source>" in block


def test_report_is_refreshed_before_the_post(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    uid = w.channel_uid
    at = lambda h, m=0, d=13: datetime(2026, 10, d, h, m, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    db.update_run(uid, state="ready", last_run_at=at(6, 40))
    w.channel_prep(at(8, 40))
    assert db.run(uid)["state"] == "ready"           # too early
    w.channel_prep(at(8, 56))
    assert db.run(uid)["state"] == "queued"          # 9:30 minus 35 min: refresh now
    db.update_run(uid, state="ready", last_run_at=at(9, 5))
    w.channel_prep(at(8, 58))
    assert db.run(uid)["state"] == "ready"           # once per day
    db.set_meta("channel_prep_date", None)
    w.channel_prep(at(8, 57, d=14))                  # next day, but the report is only an hour old
    db.update_run(uid, state="ready", last_run_at=at(8, 30, d=14))
    db.set_meta("channel_prep_date", None)
    w.channel_prep(at(8, 57, d=14))
    assert db.run(uid)["state"] == "ready"           # fresh enough: no extra run
    db.set_meta("channel_prep_date", None)
    w.channel_prep(at(8, 57, d=17))                  # Saturday: no post, no refresh
    assert db.meta("channel_prep_date") is None


def test_archetype_runs_once_a_day(tmp_path, monkeypatch):
    import time
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    uid = w.channel_uid
    assert w.run_user(uid)
    assert db.run(uid)["next_run_at"] is None          # no 3-hourly refreshes
    db.queue_due(time.time() + 10 * 3600, ())
    assert db.run(uid)["state"] == "ready"             # the scheduler never picks it up on its own
    me = TestClient(app)
    me.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    uid2 = db.user_by_email("vivek@rox.com")["id"]
    db.set_onboarded(uid2); db.queue(uid2)
    assert w.run_user(uid2) and db.run(uid2)["next_run_at"]  # personal pages keep refreshing


def test_evening_refresh(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    uid = w.channel_uid
    at = lambda h, m=0, d=13: datetime(2026, 10, d, h, m, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    db.update_run(uid, state="ready", last_run_at=at(9))
    w.channel_prep(at(19, 50))
    assert db.run(uid)["state"] == "ready"
    w.channel_prep(at(20, 5))
    assert db.run(uid)["state"] == "queued"
    app2 = make_app(tmp_path / "b", monkeypatch, CHANNEL_EVENING_REFRESH="")
    w2, db2 = app2.state.worker, app2.state.db
    db2.update_run(w2.channel_uid, state="ready", last_run_at=at(9))
    w2.channel_prep(at(20, 5))
    assert db2.run(w2.channel_uid)["state"] == "ready"   # turned off


def test_post_can_dm_a_person_for_testing(monkeypatch):
    from pipeline import engage
    calls = []

    class R:
        status_code, headers = 200, {"content-type": "application/json"}
        def __init__(self, b): self.b = b
        def json(self): return self.b

    def fake(method, url, **kw):
        name = url.rsplit("/", 1)[1]
        calls.append((name, kw.get("params") or kw.get("json")))
        return R({"ok": True, "user": {"id": "U9"}} if name == "users.lookupByEmail" else
                 {"ok": True, "channel": {"id": "D9"}} if name == "conversations.open" else {"ok": True})
    monkeypatch.setattr(engage.requests, "request", fake)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("CHANNEL_SLACK_CHANNEL", "vivek@rox.com")
    channel.post({"text": "x", "blocks": []})
    assert [c[0] for c in calls] == ["users.lookupByEmail", "conversations.open", "chat.postMessage"]
    assert calls[-1][1]["channel"] == "D9"
    calls.clear()
    monkeypatch.setenv("CHANNEL_SLACK_CHANNEL", "C0123")
    channel.post({"text": "x", "blocks": []})
    assert [c[0] for c in calls] == ["chat.postMessage"] and calls[0][1]["channel"] == "C0123"


def test_switching_destination_posts_right_away(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = make_app(tmp_path, monkeypatch, CHANNEL_SLACK_CHANNEL="vivek@rox.com")
    w, db = app.state.worker, app.state.db
    d = w.paths(w.channel_uid)["data"]; d.mkdir(parents=True, exist_ok=True)
    (d / "week.json").write_text(json.dumps({"latest": {"thesis": "x"}}))
    at = lambda h, m=0: datetime(2026, 10, 13, h, m, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert w.channel_due(at(14))                      # first time: right away (the test DM)
    db.set_meta("channel_sent_date", "2026-10-13"); db.set_meta("channel_tried_at", str(at(14)))
    assert not w.channel_due(at(15))                  # done for today
    app2 = make_app(tmp_path, monkeypatch, CHANNEL_SLACK_CHANNEL="C0123")   # switch to the team channel
    assert app2.state.worker.channel_due(at(15, 5))   # posts there right away, once
    assert db.meta("channel_target") == "C0123"
