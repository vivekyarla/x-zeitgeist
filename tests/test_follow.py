"""Weekly follow recommendations."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from pipeline import engage, follow

NOW = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)  # a Monday, 11:00 PT


def tw(i, handle, followers, likes, bio=""):
    return {"id": str(i), "url": f"https://x.com/{handle}/status/{i}", "text": f"post {i}",
            "created_at": (NOW - timedelta(days=2)).strftime("%a %b %d %H:%M:%S %z %Y"), "likes": likes, "retweets": 5,
            "replies": 1, "quotes": 1, "views": 0, "is_retweet": False, "is_reply": False, "urls": [], "ref": None,
            "author": {"handle": handle, "name": handle.title(), "followers": followers, "bio": bio}}


def test_authors_are_prominent_active_and_not_the_team():
    cfg, f = engage.load_config(), follow.settings(engage.load_config())
    tweets = [tw(1, "bigvoice", 80_000, 900), tw(2, "bigvoice", 80_000, 400),
              tw(3, "oneshot", 200_000, 5000),                                      # one post: not "active"
              tw(4, "smallacct", 1200, 300), tw(5, "smallacct", 1200, 300),          # too small
              tw(6, "rampeng", 1500, 200, "eng @tryramp"), tw(7, "rampeng", 1500, 150, "eng @tryramp"),
              tw(8, "ishanmkh", 30_000, 800), tw(9, "ishanmkh", 30_000, 800),        # the team
              tw(10, "quiet", 50_000, 10), tw(11, "quiet", 50_000, 12)]              # no engagement
    got = follow.authors(tweets, cfg, f)
    assert [a["handle"] for a in got] == ["bigvoice", "rampeng"] and got[1]["org"] == "Ramp"


class Src:
    def followings(self, handle, max_pages):
        if handle == "dychen3":
            raise RuntimeError("402")
        return [{"handle": "BigVoice"}] if handle == "ishanmkh" else []


def test_already_following():
    cfg, f = engage.load_config(), follow.settings(engage.load_config())
    got = follow.already_following(cfg, f, Src())
    assert got["Ishan"] == {"bigvoice"} and got["Gopal"] == set()
    assert got["Dan"] is None and got["Taeuk"] is None  # failed fetch, no handle


def test_clean_blocks_follows_and_repeats():
    cfg, f = engage.load_config(), follow.settings(engage.load_config())
    cands = [{"handle": "BigVoice", "name": "Big", "followers": 80000, "org": None, "bio": "", "posts": []},
             {"handle": "rampeng", "name": "R", "followers": 1500, "org": "Ramp", "bio": "", "posts": []}]
    blocked = {"Ishan": {"bigvoice"}}
    raw = {"people": [{"name": "Ishan", "picks": [{"handle": "@BigVoice", "why": "x"}, {"handle": "rampeng", "why": "fintech ops agents"}]},
                      {"name": "Gopal", "picks": [{"handle": "madeup", "why": "?"}, {"handle": "bigvoice", "why": "y " * 30}]}]}
    recs = {r["name"]: r["picks"] for r in follow.clean(raw, cfg, cands, blocked, f)}
    assert [x["handle"] for x in recs["Ishan"]] == ["rampeng"]
    assert [x["handle"] for x in recs["Gopal"]] == ["BigVoice"] and len(recs["Gopal"][0]["why"].split()) <= 14
    assert recs["Brian"] == []
    msg = follow.message(follow.clean(raw, cfg, cands, blocked, f), cands, cfg, ["Taeuk", "Brian"], NOW, "America/Los_Angeles")
    assert msg["blocks"][0]["text"]["text"] == "Who to follow · week of Oct 19"
    ishan = msg["blocks"][1]["text"]["text"]
    assert "<https://x.com/rampeng|@rampeng> (R, 2K, Ramp)" in ishan and "_fintech ops agents_" in ishan
    assert "Taeuk, Brian" in msg["blocks"][-1]["elements"][0]["text"]


def test_history_window(tmp_path):
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"Ishan": {"old": "2026-07-01", "recent": "2026-10-12"}}))
    assert follow.load_history(p, NOW, 8) == {"Ishan": {"recent": "2026-10-12"}}


def make_app(tmp_path, monkeypatch, **env):
    base = {"DATA_DIR": str(tmp_path / "data"), "TIMELINE_DEMO": "1", "TIMELINE_WORKER": "0",
            "SECRET_KEY": "test-secret-key-that-is-long-enough", "ENGAGE_SLACK_BOT_TOKEN": "xoxb-1",
            "ENGAGE_DM_EMAIL": "vivek@rox.com", "PREVIEW_TWITTERAPI_IO_KEY": "tw", "PREVIEW_AI_GATEWAY_API_KEY": "gw"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    from server import config
    from server.app import create_app
    return create_app(config.load())


def test_weekly_schedule_and_run(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo
    app = make_app(tmp_path, monkeypatch)
    w, db = app.state.worker, app.state.db
    at = lambda d, h, m=0: datetime(2026, 10, d, h, m, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    assert w.follow_due(at(14, 15))                       # first time: right away, any day
    db.set_meta("follow_sent_date", "first")
    assert not w.follow_due(at(14, 11, 30))               # Wednesday
    assert not w.follow_due(at(19, 11, 0))                # Monday, before 11:15 minus the lead
    assert w.follow_due(at(19, 11, 10))                   # Monday 11:15 (starts a few minutes early)
    seen = {}
    monkeypatch.setattr(w, "_exec", lambda args, env, timeout, secrets_, module="pipeline.run":
                        (seen.update(module=module, env=env) or (0, "fetched 900 tweets\nslack: sent\n")))
    assert w.run_follow() and seen["module"] == "pipeline.follow"
    assert seen["env"]["ENGAGE_DM_EMAIL"] == "vivek@rox.com" and "SECRET_KEY" not in seen["env"]
    me, other = TestClient(app), TestClient(app)
    me.post("/api/signup", json={"email": "vivek@rox.com", "password": "long enough pw"})
    other.post("/api/signup", json={"email": "sam@rox.com", "password": "long enough pw"})
    assert other.post("/api/engage/follow/run").status_code == 403
    assert me.post("/api/engage/follow/run").status_code == 200 and db.meta("follow_force") == "1"


def test_can_be_turned_off(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, FOLLOW_ENABLED="0")
    assert not app.state.cfg.follow_enabled
