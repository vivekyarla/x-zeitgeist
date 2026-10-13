"""The Slack bot answering @mentions and DMs about how to tweak the report."""
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from server import slackbot

SECRET = "signing-secret"


def signed(body: dict, ts=None, secret=SECRET):
    raw = json.dumps(body).encode()
    ts = str(int(ts or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256).hexdigest()
    return raw, {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/json"}


def make_app(tmp_path, monkeypatch, **env):
    base = {"DATA_DIR": str(tmp_path / "data"), "TIMELINE_DEMO": "1", "TIMELINE_WORKER": "0",
            "SECRET_KEY": "test-secret-key-that-is-long-enough", "ENGAGE_SLACK_BOT_TOKEN": "xoxb-1",
            "ENGAGE_DM_EMAIL": "vivek@rox.com", "PREVIEW_TWITTERAPI_IO_KEY": "tw", "PREVIEW_AI_GATEWAY_API_KEY": "gw",
            "SLACK_SIGNING_SECRET": SECRET, "ADMIN_EMAILS": "vivek@rox.com,ana@rox.com"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    from server import config
    from server.app import create_app
    return create_app(config.load())


def test_signature():
    raw, h = signed({"a": 1})
    assert slackbot.verify(SECRET, h["X-Slack-Request-Timestamp"], h["X-Slack-Signature"], raw)
    assert not slackbot.verify("other", h["X-Slack-Request-Timestamp"], h["X-Slack-Signature"], raw)
    old_raw, old = signed({"a": 1}, ts=time.time() - 3600)
    assert not slackbot.verify(SECRET, old["X-Slack-Request-Timestamp"], old["X-Slack-Signature"], old_raw)


def test_events_endpoint(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    c = TestClient(app)
    raw, h = signed({"type": "url_verification", "challenge": "abc"})
    assert c.post("/slack/events", content=raw, headers=h).json() == {"challenge": "abc"}
    raw, h = signed({"type": "url_verification", "challenge": "abc"}, secret="forged")
    assert c.post("/slack/events", content=raw, headers=h).status_code == 401
    seen = []
    monkeypatch.setattr(slackbot, "dispatch", lambda ev, cfg, token, key: seen.append((ev, token, key)))
    ev = {"type": "app_mention", "user": "U1", "text": "<@UBOT> how do I tweak the X report?", "channel": "C1", "ts": "1.2"}
    raw, h = signed({"type": "event_callback", "event": ev})
    assert c.post("/slack/events", content=raw, headers=h).status_code == 200
    assert seen == [(ev, "xoxb-1", "gw")]
    raw, h = signed({"type": "event_callback", "event": ev})
    c.post("/slack/events", content=raw, headers={**h, "X-Slack-Retry-Num": "1"})
    assert len(seen) == 1  # Slack retries are ignored


def test_what_it_answers():
    assert slackbot.should_answer({"type": "app_mention", "text": "hi"})
    assert slackbot.should_answer({"type": "message", "channel_type": "im", "text": "hi"})
    assert not slackbot.should_answer({"type": "message", "channel_type": "channel", "text": "hi"})
    assert not slackbot.should_answer({"type": "message", "channel_type": "im", "bot_id": "B1"})
    assert not slackbot.should_answer({"type": "message", "channel_type": "im", "subtype": "message_changed"})


class Cfg:
    base_url = "https://t.example"
    admin_emails = ("vivek@rox.com", "ana@rox.com")
    channel_time = "09:30"


def test_answer_without_a_model_and_for_non_admins():
    a = slackbot.answer("how do I tweak the X report?", "sam@rox.com", Cfg, None)
    assert "<https://t.example/gtm/|the GTM report>" in a and "ask vivek@rox.com" in a
    b = slackbot.answer("how do I tweak the X report?", "ana@rox.com", Cfg, None)
    assert "ADMIN_EMAILS" not in b


def test_answer_uses_the_model_with_the_reference(monkeypatch):
    sent = {}

    class R:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "*Steps* • Open <https://t.example/gtm/|GTM report>"}}]}
    monkeypatch.setattr(slackbot.requests, "post", lambda url, **kw: sent.update(kw["json"]) or R())
    a = slackbot.answer("can I add Gong to tracked companies?", "sam@rox.com", Cfg, "gw")
    assert a.startswith("*Steps*")
    sys_msg = sent["messages"][0]["content"]
    assert "NOT an admin" in sys_msg and "Tracked companies" in sys_msg and "https://t.example/gtm/" in sys_msg


def test_reply_goes_in_the_thread(monkeypatch):
    calls = []

    class R:
        headers = {"content-type": "application/json"}
        def __init__(self, b): self.b = b
        def json(self): return self.b

    def fake(method, url, **kw):
        name = url.rsplit("/", 1)[1]
        calls.append((name, kw.get("params") or kw.get("json")))
        return R({"ok": True, "user": {"profile": {"email": "ana@rox.com"}}} if name == "users.info" else {"ok": True})
    monkeypatch.setattr(slackbot.requests, "request", fake)
    slackbot.handle({"type": "app_mention", "user": "U1", "text": "<@UB> settings?", "channel": "C1", "ts": "5.6"},
                    Cfg, "xoxb-1", None)
    post = calls[-1][1]
    assert calls[0][0] == "users.info" and post["channel"] == "C1" and post["thread_ts"] == "5.6"
