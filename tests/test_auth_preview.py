"""Sign in with Google and the public example page."""
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from conftest import signup


def make_app(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TIMELINE_DEMO", env.pop("TIMELINE_DEMO", "1"))
    monkeypatch.setenv("TIMELINE_WORKER", "0")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-that-is-long-enough")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from server import config
    from server.app import create_app
    return create_app(config.load())


def test_preview_is_public_and_read_only(client, app):
    worker, db = app.state.worker, app.state.db
    prev = db.user_by_email("preview@timeline.invalid")
    assert prev and prev["onboarded"] and db.run(prev["id"])["state"] == "queued"
    r = client.get("/preview/")
    assert r.status_code == 200 and "being built" in r.text  # before the first run
    assert worker.run_user(prev["id"]) is True
    r = client.get("/preview/")
    assert r.status_code == 200 and 'class="thesis"' in r.text
    assert client.get("/preview/recap.md").status_code == 200
    assert client.get("/preview/settings.json").status_code == 404
    # the example profile can't be signed into or claimed
    assert client.post("/api/login", json={"email": "preview@timeline.invalid", "password": ""}).status_code in (401, 422)
    assert client.post("/api/signup", json={"email": "x@timeline.invalid", "password": "long enough pw"}).status_code == 422
    assert client.get("/api/me").status_code == 401


def test_preview_off_without_owner_keys(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, TIMELINE_DEMO="0", BASE_URL="http://localhost:8000")
    c = TestClient(app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/preview/", follow_redirects=False).headers["location"] == "/login"


def test_preview_runs_on_owner_keys(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, TIMELINE_DEMO="0", BASE_URL="http://localhost:8000",
                   PREVIEW_TWITTERAPI_IO_KEY="owner-tw", PREVIEW_AI_GATEWAY_API_KEY="owner-gw")
    uid = app.state.db.user_by_email("preview@timeline.invalid")["id"]
    assert app.state.worker.keys(uid) == {"twitterapi_io": "owner-tw", "ai_gateway": "owner-gw"}


def google_app(tmp_path, monkeypatch, **env):
    return make_app(tmp_path, monkeypatch, GOOGLE_CLIENT_ID="cid.apps.googleusercontent.com",
                    GOOGLE_CLIENT_SECRET="csecret", **env)


def sign_in(c, monkeypatch, **claims):
    r = c.get("/auth/google", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == ["openid email profile"] and q["access_type"] == ["online"]
    assert q["code_challenge_method"] == ["S256"] and "code_challenge" in q
    base = {"iss": "https://accounts.google.com", "aud": "cid.apps.googleusercontent.com", "sub": "g-123",
            "email": "ana@rox.com", "email_verified": True, "hd": "rox.com", "nonce": q["nonce"][0],
            "exp": time.time() + 600}
    base.update(claims)
    from server import google
    monkeypatch.setattr(google, "exchange", lambda *a, **k: base)
    return c.get("/auth/google/callback", params={"code": "x", "state": q["state"][0]}, follow_redirects=False)


def test_google_sign_in_creates_and_reuses_account(tmp_path, monkeypatch):
    app = google_app(tmp_path, monkeypatch)
    c = TestClient(app)
    r = sign_in(c, monkeypatch)
    assert r.headers["location"] == "/welcome"
    me = c.get("/api/me").json()
    assert me["email"] == "ana@rox.com" and me["auth"] == "google" and not me["onboarded"]
    # password accounts are off once Google is set up
    assert c.post("/api/signup", json={"email": "b@rox.com", "password": "long enough pw"}).status_code == 403
    assert c.post("/api/login", json={"email": "ana@rox.com", "password": "x"}).status_code == 403
    c.post("/api/logout")
    c2 = TestClient(app)
    sign_in(c2, monkeypatch, email="ana@rox.com")
    assert c2.get("/api/me").json()["email"] == "ana@rox.com"
    assert len([u for u in app.state.db.user_ids()]) == 2  # ana + the example profile
    # deleting a Google account asks for the email, not a password
    assert c2.request("DELETE", "/api/account", json={"confirm": "nope"}).status_code == 403
    assert c2.request("DELETE", "/api/account", json={"confirm": "ana@rox.com"}).status_code == 200


@pytest.mark.parametrize("claims,msg", [
    ({"state_mismatch": True}, "expired"),
    ({"aud": "someone-else"}, "wasn't meant"),
    ({"nonce": "wrong"}, "wasn't meant"),
    ({"exp": 1}, "expired"),
    ({"email_verified": False}, "verified email"),
    ({"hd": "", "email": "ana@rox.com"}, "@rox.com"),  # a personal Google account using a work address
    ({"hd": "gmail.com", "email": "ana@gmail.com"}, "@rox.com"),
])
def test_google_rejections(tmp_path, monkeypatch, claims, msg):
    app = google_app(tmp_path, monkeypatch, ALLOWED_EMAIL_DOMAINS="rox.com")
    c = TestClient(app)
    if claims.pop("state_mismatch", False):
        c.get("/auth/google", follow_redirects=False)
        r = c.get("/auth/google/callback", params={"code": "x", "state": "forged"}, follow_redirects=False)
    else:
        r = sign_in(c, monkeypatch, **claims)
    loc = r.headers["location"]
    assert loc.startswith("/login?error="), loc
    assert msg in parse_qs(urlparse(loc).query)["error"][0]
    assert c.get("/api/me").status_code == 401


def test_google_links_existing_password_account(tmp_path, monkeypatch):
    app = google_app(tmp_path, monkeypatch, PASSWORD_LOGIN="1")
    c = TestClient(app)
    signup(c, email="ana@rox.com")
    c.post("/api/logout")
    sign_in(c, monkeypatch)
    me = c.get("/api/me").json()
    assert me["email"] == "ana@rox.com" and me["auth"] == "password"  # same account, now also Google


def test_password_signup_limited_to_allowed_domains(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, ALLOWED_EMAIL_DOMAINS="rox.com")
    c = TestClient(app)
    r = c.post("/api/signup", json={"email": "ana@gmail.com", "password": "long enough pw"})
    assert r.status_code == 403 and "@rox.com" in r.json()["error"]
    assert c.post("/api/signup", json={"email": "Ana@Rox.com", "password": "long enough pw"}).status_code == 201
    assert "@rox.com" in c.get("/login").text


def test_switching_the_example_feed_starts_its_week_over(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, PREVIEW_PRESET="ai_startup_marketing")
    uid = app.state.db.user_by_email("preview@timeline.invalid")["id"]
    week = app.state.worker.paths(uid)["data"] / "week.json"
    week.parent.mkdir(parents=True, exist_ok=True)
    week.write_text('{"latest": {"thesis": "GTM teams are copying Clay"}}')  # what a real run leaves
    app2 = make_app(tmp_path, monkeypatch, PREVIEW_PRESET="sf_tech")
    assert not week.exists() and app2.state.db.run(uid)["state"] == "queued"
