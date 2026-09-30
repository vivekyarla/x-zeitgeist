import json

from fastapi.testclient import TestClient

from conftest import signup

TW_KEY = "tw-key-ABCDEF123456"
GW_KEY = "gw-key-ZYXWVU987654"
HOOK = "https://hooks.slack.com/services/T000/B000/abcdefSECRET"


def test_healthz_and_pages_without_session(client):
    assert client.get("/healthz").json() == {"ok": True}
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200
    assert client.get("/welcome", follow_redirects=False).headers["location"] == "/login"
    assert client.get("/app/", follow_redirects=False).headers["location"] == "/login"
    for path in ("/api/me", "/api/status", "/api/export"):
        r = client.get(path)
        assert r.status_code == 401 and "error" in r.json()
    assert client.put("/api/settings", json={"settings": {}}).status_code == 401
    assert client.get("/api/presets").status_code == 200


def test_signup_login_validation(client):
    assert client.post("/api/signup", json={"email": "nope", "password": "longenough"}).status_code == 422
    assert client.post("/api/signup", json={"email": "a@b.co", "password": "short"}).status_code == 422
    me = signup(client, "A@Example.com")
    assert me["email"] == "a@example.com" and me["demo"] is True and me["onboarded"] is False
    assert "preset_id" not in me["settings"]
    assert me["status"]["state"] == "new" and me["status"]["has_page"] is False
    assert me["recap_url"].startswith("http://localhost:8000/r/") and me["recap_url"].endswith("/recap.md")
    assert me["page_url"] == "http://localhost:8000/app/"
    assert client.post("/api/signup", json={"email": "a@example.com", "password": "whatever1"}).status_code == 409
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/login", json={"email": "a@example.com", "password": "wrong-pass"}).status_code == 401
    r = client.post("/api/login", json={"email": "a@example.com", "password": "correct horse"})
    assert r.status_code == 200 and r.json()["email"] == "a@example.com"


def test_rate_limit(client, app):
    for _ in range(app.state.cfg.rate_limit):
        client.post("/api/login", json={"email": "x@example.com", "password": "wrong-pass"})
    assert client.post("/api/login", json={"email": "x@example.com", "password": "wrong-pass"}).status_code == 429


def test_full_flow(client, app):
    cfg, worker = app.state.cfg, app.state.worker
    signup(client)
    uid = client.app.state.db.user_by_email("ana@example.com")["id"]
    assert client.get("/", follow_redirects=False).headers["location"] == "/welcome"
    assert client.get("/welcome").status_code == 200

    presets = client.get("/api/presets").json()
    ids = [p["id"] for p in presets]
    assert {"ai_engineer", "startup_culture", "gtm_sales", "marketing_brand", "ai_business",
            "ai_startup_marketing"} <= set(ids)
    eng = next(p for p in presets if p["id"] == "ai_engineer")
    assert set(eng) == {"id", "name", "tagline", "description", "settings"} and "preset" not in eng["settings"]

    # settings: valid, then invalid
    s = dict(eng["settings"], preset_id="ai_engineer", timezone="America/New_York")
    r = client.put("/api/settings", json={"settings": s})
    assert r.status_code == 200 and r.json()["settings"]["preset_id"] == "ai_engineer"
    bad = dict(s, searches=["q min_faves:10"] * 50)
    r = client.put("/api/settings", json={"settings": bad})
    assert r.status_code == 422 and "40" in r.json()["error"]
    assert client.put("/api/settings", json={"nope": 1}).status_code == 422
    assert client.get("/api/me").json()["settings"]["timezone"] == "America/New_York"

    # onboarding needs keys
    r = client.post("/api/onboarding/complete")
    assert r.status_code == 400 and "key" in r.json()["error"]
    r = client.put("/api/keys", json={"slack_webhook": "https://example.com/hook"})
    assert r.status_code == 422
    r = client.put("/api/keys", json={"twitterapi_io": TW_KEY, "ai_gateway": GW_KEY, "slack_webhook": HOOK})
    assert r.status_code == 200
    assert r.json() == {"keys": {"twitterapi_io": True, "ai_gateway": True, "typesafe": False, "slack_webhook": True}}
    # keys are never echoed, and never stored in plain text
    for body in (r.text, client.get("/api/me").text, client.get("/api/export").text):
        assert TW_KEY not in body and GW_KEY not in body and "abcdefSECRET" not in body
    raw_db = cfg.db_path.read_bytes()
    assert TW_KEY.encode() not in raw_db and GW_KEY.encode() not in raw_db
    chk = client.post("/api/keys/check", json={}).json()
    assert chk["twitterapi_io"]["ok"] and chk["ai_gateway"]["ok"]
    assert TW_KEY not in json.dumps(chk)

    r = client.post("/api/onboarding/complete")
    assert r.status_code == 200 and r.json()["onboarded"] is True
    assert r.json()["status"]["state"] == "queued"
    assert client.get("/app/").status_code == 200  # the building page until the first run
    r = client.post("/api/run", json={})  # already queued: fine, it reads settings when it starts
    assert r.status_code == 200 and r.json()["state"] == "queued" and "already queued" in r.json()["message"]

    # the worker builds this user's page (TIMELINE_DEMO: sample data, their settings)
    assert worker.run_user(uid) is True, (cfg.user_dir(uid) / "data" / "last_run.log").read_text()
    st = client.get("/api/status").json()
    assert st["state"] == "ready" and st["has_page"] and st["last_error"] is None and st["next_run_at"]
    udir = cfg.user_dir(uid)
    assert (udir / "public" / "index.html").exists()
    written = json.loads((udir / "settings.json").read_text())
    assert written["timezone"] == "America/New_York" and "preset_id" not in written
    page = client.get("/app/")
    assert page.status_code == 200 and page.text == (udir / "public" / "index.html").read_text()
    assert client.get("/app/data.json").status_code == 200
    assert client.get("/app/../settings.json").status_code in (401, 404)
    assert client.get("/app/settings.json").status_code == 404
    assert client.get("/", follow_redirects=False).headers["location"] == "/app/"

    # right after a run, a new request is scheduled for the end of the cooldown instead of refused
    r = client.post("/api/run", json={"slack": "now"})
    assert r.status_code == 200 and r.json()["state"] == "ready" and "rebuilds with your changes" in r.json()["message"]
    run = worker.db.run(uid)
    assert run["next_run_at"] - run["last_run_at"] <= 601 and run["slack_mode"] == "now"

    # saving mid-run: the run finishes, then another starts right away with the new settings
    worker.db.update_run(uid, state="running")
    r = client.post("/api/run", json={})
    assert r.status_code == 200 and "right after" in r.json()["message"] and worker.db.run(uid)["rerun"] == 1
    worker.db.update_run(uid, state="queued", lease_until=0)
    assert worker.run_user(uid) is True
    run = worker.db.run(uid)
    assert run["rerun"] == 0 and run["next_run_at"] <= run["last_run_at"] + 1

    # the recap feed: public by token, links point at the token URL
    me = client.get("/api/me").json()
    token_path = me["recap_url"].replace("http://localhost:8000", "")
    anon = TestClient(app)
    r = anon.get(token_path)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    assert "Full site: http://localhost:8000/app/" in r.text
    recap = anon.get(token_path.replace("recap.md", "recap.json")).json()
    assert recap["links"]["recap_md"] == me["recap_url"]
    assert recap["links"]["page"] == "http://localhost:8000/app/"
    assert anon.get("/r/" + "x" * 43 + "/recap.md").status_code == 404
    assert anon.get("/r/short/recap.md").status_code == 404
    assert anon.get(token_path.replace("recap.md", "index.html")).status_code == 404
    new_url = client.post("/api/recap/rotate").json()["recap_url"]
    assert new_url != me["recap_url"]
    assert anon.get(token_path).status_code == 404
    assert anon.get(new_url.replace("http://localhost:8000", "")).status_code == 200

    # export / import roundtrip
    exp = client.get("/api/export")
    assert exp.headers["content-disposition"].startswith("attachment")
    prof = exp.json()
    assert prof["format"] == "timeline-profile" and prof["version"] == 1 and "keys" not in prof
    with_keys = client.get("/api/export?keys=1").json()
    assert with_keys["keys"]["twitterapi_io"] == TW_KEY

    other = TestClient(app)
    signup(other, "ben@example.com")
    r = other.post("/api/import", json={"profile": with_keys})
    assert r.status_code == 200
    assert r.json()["settings"] == prof["settings"]
    assert r.json()["keys"]["twitterapi_io"] and r.json()["keys"]["slack_webhook"]
    assert other.post("/api/import", json={"profile": {"format": "x", "version": 1}}).status_code == 422
    broken = dict(prof, settings=dict(prof["settings"], timezone="Nowhere/Nope"))
    assert other.post("/api/import", json={"profile": broken}).status_code == 422

    # another user can't see the first user's page or files
    r = other.get("/app/", follow_redirects=False)
    assert r.status_code == 200 and r.text != page.text  # ben sees his building page, not ana's
    assert other.get("/app/data.json").status_code == 404
    assert other.get("/api/me").json()["email"] == "ben@example.com"

    # delete account
    assert client.request("DELETE", "/api/account", json={"password": "wrong-pass"}).status_code == 403
    r = client.request("DELETE", "/api/account", json={"password": "correct horse"})
    assert r.status_code == 200
    assert not udir.exists()
    assert client.get("/api/me").status_code == 401
    assert anon.get(new_url.replace("http://localhost:8000", "")).status_code == 404
    assert client.post("/api/login", json={"email": "ana@example.com", "password": "correct horse"}).status_code == 401


def test_error_run_is_explained(client, app, monkeypatch):
    signup(client)
    uid = app.state.db.user_by_email("ana@example.com")["id"]
    client.put("/api/keys", json={"twitterapi_io": TW_KEY, "ai_gateway": GW_KEY})
    client.post("/api/onboarding/complete")
    w = app.state.worker
    monkeypatch.setattr(w, "_exec", lambda *a, **k: (1, "Traceback (most recent call last):\n  File \"x\"\n"
                        "requests.exceptions.HTTPError: 401 Client Error: Unauthorized for url: "
                        "https://api.twitterapi.io/twitter/user/last_tweets\n"))
    assert w.run_user(uid) is False
    st = client.get("/api/status").json()
    assert st["state"] == "error" and "twitterapi.io rejected your key" in st["last_error"]
    # after an error, "run now" isn't rate-limited
    assert client.post("/api/run", json={}).status_code == 200


def test_head_healthz_and_token_log_filter(client):
    import logging
    from server.app import _HideRecapTokens
    assert client.head("/healthz").status_code == 200
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET", "/r/SECRETTOKEN123/recap.md"), None)
    _HideRecapTokens().filter(rec)
    assert "SECRETTOKEN123" not in rec.getMessage()
