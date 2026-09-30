"""The web app: accounts, each user's settings and keys, their page, and their recap feed.

Run locally:  TIMELINE_DEMO=1 uvicorn server.app:app --reload
"""
from __future__ import annotations

import collections
import json
import logging
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, TemplateNotFound, select_autoescape
from markupsafe import escape
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import checks
from . import google as google_mod
from . import config as config_mod
from .config import ROOT, SITE_TITLE, Config
from .db import DB
from .security import Box, fernet_key, hash_password, recap_token, verify_password
from .settings_schema import SettingsError, default_settings, load_presets, validate
from .worker import KEY_ENV, REQUIRED_KEYS, Worker

log = logging.getLogger("timeline")

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
APP_FILES = {"data.json": "application/json", "recap.json": "application/json",
             "recap.md": "text/markdown; charset=utf-8"}
RECAP_FILES = {"recap.json": "application/json", "recap.md": "text/markdown; charset=utf-8"}
RUN_COOLDOWN = 600
PREVIEW_EMAIL = "preview@timeline.invalid"  # the public example page's profile; can never sign in
NO_CACHE = {"Cache-Control": "no-cache"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Creds(_Body):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class SettingsIn(_Body):
    settings: dict


KeyVal = Optional[str]


class KeysIn(_Body):
    twitterapi_io: KeyVal = Field(default=None, max_length=500)
    ai_gateway: KeyVal = Field(default=None, max_length=500)
    typesafe: KeyVal = Field(default=None, max_length=500)
    slack_webhook: KeyVal = Field(default=None, max_length=500)


class KeysCheckIn(_Body):
    twitterapi_io: KeyVal = Field(default=None, max_length=500)
    ai_gateway: KeyVal = Field(default=None, max_length=500)


class SlackTestIn(_Body):
    webhook: KeyVal = Field(default=None, max_length=500)


class RunIn(_Body):
    slack: Literal["auto", "now"] = "auto"


class ImportIn(_Body):
    profile: dict


class DeleteIn(_Body):
    password: str = Field(default="", max_length=256)  # password accounts
    confirm: str = Field(default="", max_length=254)   # Google accounts type their email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


class _HideRecapTokens(logging.Filter):
    """Keep recap tokens (they're the only thing guarding a user's feed) out of access logs."""
    RX = re.compile(r"/r/[^/\s]+/")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self.RX.sub("/r/[token]/", a) if isinstance(a, str) else a for a in record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_HideRecapTokens())


class RateLimiter:
    def __init__(self, limit: int, window: float = 600):
        self.limit, self.window = limit, window
        self.hits: dict[str, collections.deque] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, collections.deque())
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self.hits) > 10_000:  # forget idle clients
                for k in [k for k, v in self.hits.items() if not v or v[-1] < now - self.window]:
                    del self.hits[k]
            return True


def _validate_keys(values: dict[str, str | None]) -> dict[str, str | None]:
    """Only the fields that were sent. "" means delete."""
    out = {}
    for name, v in values.items():
        if v is None:
            continue
        if not isinstance(v, str):
            raise ApiError(422, f"{name} should be text.")
        v = v.strip()
        if v and (len(v) > 500 or any(c.isspace() for c in v)):
            raise ApiError(422, "That key doesn't look right (it has spaces or is too long). Copy it again.")
        if name == "slack_webhook" and v and not v.startswith("https://hooks.slack.com/"):
            raise ApiError(422, "The Slack webhook should start with https://hooks.slack.com/. Copy it from "
                                "your Slack app's Incoming Webhooks page.")
        out[name] = v
    return out


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or config_mod.load()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(cfg.db_path)
    box = Box(fernet_key(cfg.secret_key, cfg.encryption_key))
    worker = Worker(cfg, db, box)
    limiter = RateLimiter(cfg.rate_limit)
    presets = load_presets()
    templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
    json_cache: dict[str, tuple[float, dict]] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if cfg.worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title=SITE_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg, app.state.db, app.state.worker, app.state.box = cfg, db, worker, box
    app.add_middleware(SessionMiddleware, secret_key=cfg.secret_key, session_cookie="timeline_session",
                       max_age=30 * 24 * 3600, same_site="lax", https_only=cfg.secure_cookies)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith(("/api/", "/app/", "/r/")):
            resp.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/") else "no-cache")
        if request.url.path.startswith("/r/"):
            resp.headers["X-Robots-Tag"] = "noindex"
        return resp

    # -- errors: always {"error": "<a sentence>"} ----------------------------
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        errs = exc.errors()
        where = ".".join(str(p) for p in errs[0].get("loc", []) if p != "body") if errs else ""
        msg = "The request was missing something or had the wrong type" + (f" ({where})." if where else ".")
        return JSONResponse({"error": msg}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        msg = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, str(exc.detail))
        if request.url.path.startswith(("/api/", "/r/", "/app/")) or exc.status_code != 404:
            return JSONResponse({"error": msg}, status_code=exc.status_code)
        return HTMLResponse("<!doctype html><meta charset='utf-8'><title>Not found</title><p>Not found.</p>",
                            status_code=404)

    # -- sessions --------------------------------------------------------------
    def session_user(request: Request):
        uid = request.session.get("uid")
        if not isinstance(uid, int):
            return None
        user = db.user(uid)
        if not user:
            request.session.clear()
        return user

    def require_user(request: Request):
        user = session_user(request)
        if not user:
            raise ApiError(401, "Sign in first.")
        return user

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # -- views of a user -------------------------------------------------------
    def key_flags(uid: int) -> dict[str, bool]:
        sealed = db.keys(uid)
        return {n: bool(sealed.get(n) and box.open(sealed[n])) for n in KEY_ENV}

    def status(uid: int) -> dict:
        r = db.run(uid)
        return {"state": r["state"], "last_run_at": _iso(r["last_run_at"]), "next_run_at": _iso(r["next_run_at"]),
                "last_error": r["last_error"],
                "has_page": (worker.paths(uid)["out"] / "index.html").exists()}

    def week_state(uid: int) -> dict:
        p = worker.paths(uid)["data"] / "week.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return {}
        hit = json_cache.get(str(p))
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            d = json.loads(p.read_text())
            small = {"slack_last_post": d.get("slack_last_post"), "slack_last_error": d.get("slack_last_error")}
        except (OSError, ValueError):
            small = {}
        json_cache[str(p)] = (mtime, small)
        return small

    def recap_url(user) -> str:
        return cfg.recap_base(user["recap_token"]) + "recap.md"

    def me(user) -> dict:
        uid = user["id"]
        ws = week_state(uid)
        return {"email": user["email"], "demo": cfg.demo, "onboarded": bool(user["onboarded"]),
                "auth": "password" if user["pw_hash"] else "google",
                "settings": json.loads(user["settings"]), "keys": key_flags(uid), "status": status(uid),
                "recap_url": recap_url(user), "page_url": cfg.page_url,
                "slack": {"last_post": ws.get("slack_last_post"), "last_error": ws.get("slack_last_error")}}

    def has_required_keys(uid: int) -> bool:
        flags = key_flags(uid)
        return all(flags[k] for k in REQUIRED_KEYS)

    def queue_run(uid: int, slack_mode: str = "auto") -> None:
        db.queue(uid, slack_mode)
        worker.kick()

    def save_keys(uid: int, values: dict[str, str | None]) -> None:
        for name, v in _validate_keys(values).items():
            db.set_key(uid, name, box.seal(v) if v else None)

    # -- the public example page ------------------------------------------------
    # One shared profile, built from a preset on the owner's keys (PREVIEW_* env), so people can
    # see a real page before signing in or bringing keys. It has no login and no settings.
    def ensure_preview() -> int | None:
        user = db.user_by_email(PREVIEW_EMAIL)
        if not cfg.preview:
            if user:  # turned off: drop its keys so the scheduler stops running it
                for n in KEY_ENV:
                    db.set_key(user["id"], n, None)
            return None
        p = next((x for x in presets if x["id"] == cfg.preview_preset), None) or \
            next(x for x in presets if x["id"] == "ai_startup_marketing")
        settings = validate(p["settings"])
        settings["slack"]["enabled"] = False
        uid = user["id"] if user else db.create_user(PREVIEW_EMAIL, "", settings, recap_token())
        db.set_settings(uid, settings)
        db.set_onboarded(uid)
        for n, v in (cfg.preview_keys or {}).items():
            db.set_key(uid, n, box.seal(v))
        run = db.run(uid)
        if run["state"] in ("error", "new") or not worker._has_read(uid):
            db.queue(uid)  # e.g. after fixing a preview key: redeploying retries right away
        return uid

    preview_uid = ensure_preview()
    preview_name = next((x["name"] for x in presets if x["id"] == cfg.preview_preset), "AI startup marketer")

    # -- pages -----------------------------------------------------------------
    def render(name: str, user, fallback: str) -> str:
        ctx = {"title": SITE_TITLE, "email": user["email"] if user else None, "demo": cfg.demo,
               "google": cfg.google, "password_login": cfg.password_login, "preview": preview_uid is not None,
               "preview_name": preview_name, "domains": cfg.allowed_domains}
        try:
            return templates.get_template(name).render(**ctx)
        except TemplateNotFound:  # templates/ is maintained separately; keep the server usable without it
            return (f"<!doctype html><meta charset='utf-8'><title>{escape(SITE_TITLE)}</title>"
                    f"<p>{escape(fallback)}</p>")

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthz():
        return {"ok": True}

    @app.get("/")
    def home(request: Request):
        user = session_user(request)
        if not user:
            return RedirectResponse("/preview/" if preview_uid is not None else "/login", 303)
        return RedirectResponse("/app/" if user["onboarded"] else "/welcome", 303)

    @app.get("/preview")
    def preview_no_slash():
        return RedirectResponse("/preview/", 308)

    @app.get("/preview/")
    def preview_page():
        if preview_uid is None:
            return RedirectResponse("/login", 303)
        page = worker.paths(preview_uid)["out"] / "index.html"
        if page.exists():
            return FileResponse(page, media_type="text/html; charset=utf-8", headers=NO_CACHE)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<meta http-equiv='refresh' content='20'><title>Example page</title>"
            "<body style='font:16px/1.5 system-ui;max-width:36em;margin:15vh auto;padding:0 20px'>"
            "<p>The example page is being built for the first time. This takes a few minutes; "
            "this page refreshes on its own.</p><p><a href='/login'>Sign in to make your own</a></p>",
            headers=NO_CACHE)

    @app.get("/preview/{name}")
    def preview_file(name: str):
        if preview_uid is None or name not in APP_FILES:
            raise ApiError(404, "Not found.")
        f = worker.paths(preview_uid)["out"] / name
        if not f.exists():
            raise ApiError(404, "Not built yet.")
        return FileResponse(f, media_type=APP_FILES[name], headers=NO_CACHE)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return HTMLResponse(render("login.html", session_user(request), "Sign in"))

    @app.get("/welcome", response_class=HTMLResponse)
    def welcome(request: Request):
        user = session_user(request)
        if not user:
            return RedirectResponse("/login", 303)
        return HTMLResponse(render("onboarding.html", user, "Set up your page"))

    @app.get("/app")
    def app_no_slash():
        return RedirectResponse("/app/", 308)

    @app.get("/app/")
    def app_page(request: Request):
        user = session_user(request)
        if not user:
            return RedirectResponse("/login", 303)
        page = worker.paths(user["id"])["out"] / "index.html"
        if page.exists():
            return FileResponse(page, media_type="text/html; charset=utf-8", headers=NO_CACHE)
        return HTMLResponse(render("building.html", user, "Your page is being built. This takes a few minutes."),
                            headers=NO_CACHE)

    @app.get("/app/{name}")
    def app_file(name: str, request: Request):
        user = session_user(request)
        if not user:
            raise ApiError(401, "Sign in first.")
        if name == "index.html":
            return RedirectResponse("/app/", 303)
        if name not in APP_FILES:
            raise ApiError(404, "Not found.")
        f = worker.paths(user["id"])["out"] / name
        if not f.exists():
            raise ApiError(404, "Not built yet.")
        return FileResponse(f, media_type=APP_FILES[name], headers=NO_CACHE)

    @app.get("/r/{token}/{name}")
    def recap_file(token: str, name: str):
        if name not in RECAP_FILES or not TOKEN.match(token):
            raise ApiError(404, "Not found.")
        user = db.user_by_token(token)
        if not user:
            raise ApiError(404, "Not found.")
        f = worker.paths(user["id"])["out"] / name
        if not f.exists():
            raise ApiError(404, "The first recap lands after the first update.")
        return Response(f.read_bytes(), media_type=RECAP_FILES[name], headers=NO_CACHE)

    # -- auth ------------------------------------------------------------------
    def start_session(request: Request, uid: int) -> None:
        request.session.clear()
        request.session["uid"] = uid

    def login_error(msg: str):
        return RedirectResponse("/login?" + urlencode({"error": msg}), 303)

    @app.get("/auth/google")
    def google_start(request: Request):
        if not cfg.google:
            return login_error("Google sign-in isn't set up on this server.")
        url, pending = google_mod.start(cfg.google_client_id, cfg.base_url + "/auth/google/callback",
                                        cfg.allowed_domains)
        request.session["google"] = pending
        return RedirectResponse(url, 303)

    @app.get("/auth/google/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        pending = request.session.pop("google", None)
        if error:
            return login_error("Google sign-in was cancelled." if error == "access_denied" else
                               "Google couldn't sign you in. Try again.")
        if not cfg.google or not pending or not code or state != pending.get("state"):
            return login_error("That sign-in link expired. Try again.")
        if not limiter.allow("auth:" + client_ip(request)):
            return login_error("Too many attempts. Wait a few minutes and try again.")
        try:
            c = google_mod.exchange(cfg.google_client_id, cfg.google_client_secret,
                                    cfg.base_url + "/auth/google/callback", code, pending["verifier"])
            sub, email, _name = google_mod.claims(c, cfg.google_client_id, pending["nonce"], cfg.allowed_domains)
        except google_mod.GoogleError as e:
            return login_error(str(e))
        user = db.user_by_google(sub)
        if not user:
            user = db.user_by_email(email)
            if user and user["email"] == PREVIEW_EMAIL:
                return login_error("Google couldn't sign you in. Try again.")
            if user:  # Google verified this address: link it to the existing account
                db.set_google_sub(user["id"], sub)
            else:
                uid = db.create_user(email, "", default_settings(), recap_token())
                if uid is None:
                    return login_error("Couldn't create your account. Try again.")
                db.set_google_sub(uid, sub)
                user = db.user(uid)
        start_session(request, user["id"])
        return RedirectResponse("/app/" if user["onboarded"] else "/welcome", 303)

    @app.post("/api/signup", status_code=201)
    def signup(request: Request, body: Creds):
        if not cfg.password_login:
            raise ApiError(403, "Sign in with Google instead.")
        if not limiter.allow("auth:" + client_ip(request)):
            raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
        email = body.email.strip().lower()
        if not EMAIL.match(email) or email.endswith(".invalid"):
            raise ApiError(422, "That doesn't look like an email address.")
        if cfg.allowed_domains and email.rsplit("@", 1)[1] not in cfg.allowed_domains:
            raise ApiError(403, f"Sign up with your {' or '.join('@' + d for d in cfg.allowed_domains)} email.")
        if len(body.password) < 8:
            raise ApiError(422, "Use a password with at least 8 characters.")
        uid = db.create_user(email, hash_password(body.password), default_settings(), recap_token())
        if uid is None:
            raise ApiError(409, "There's already an account for that email. Sign in instead.")
        request.session.clear()
        request.session["uid"] = uid
        return me(db.user(uid))

    @app.post("/api/login")
    def login(request: Request, body: Creds):
        if not cfg.password_login:
            raise ApiError(403, "Sign in with Google instead.")
        if not limiter.allow("auth:" + client_ip(request)):
            raise ApiError(429, "Too many attempts. Wait a few minutes and try again.")
        user = db.user_by_email(body.email.strip().lower())
        if not verify_password(body.password, user["pw_hash"] if user else None):
            raise ApiError(401, "That email and password don't match.")
        request.session.clear()
        request.session["uid"] = user["id"]
        return me(user)

    @app.post("/api/logout")
    def logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.get("/api/me")
    def get_me(request: Request):
        return me(require_user(request))

    @app.get("/api/presets")
    def get_presets():
        return presets

    # -- settings and keys -----------------------------------------------------
    @app.put("/api/settings")
    def put_settings(request: Request, body: SettingsIn):
        user = require_user(request)
        try:
            settings = validate(body.settings)
        except SettingsError as e:
            raise ApiError(422, str(e))
        db.set_settings(user["id"], settings)  # used from the next run; POST /api/run to rebuild now
        return {"settings": settings}

    @app.put("/api/keys")
    def put_keys(request: Request, body: KeysIn):
        user = require_user(request)
        save_keys(user["id"], body.model_dump())
        return {"keys": key_flags(user["id"])}

    @app.post("/api/keys/check")
    def keys_check(request: Request, body: KeysCheckIn = Body(default=KeysCheckIn())):
        user = require_user(request)
        saved = worker.keys(user["id"])
        out = {}
        for name, fn in (("twitterapi_io", checks.check_twitterapi), ("ai_gateway", checks.check_gateway)):
            value = (getattr(body, name) or "").strip() or saved.get(name)
            if not value:
                out[name] = {"ok": False, "message": "No key yet."}
            elif cfg.demo:
                out[name] = {"ok": True, "message": "Demo mode: keys aren't checked."}
            else:
                out[name] = fn(value)
        return out

    @app.post("/api/slack/test")
    def slack_test(request: Request, body: SlackTestIn = Body(default=SlackTestIn())):
        user = require_user(request)
        url = (body.webhook or "").strip() or worker.keys(user["id"]).get("slack_webhook")
        if not url:
            return {"ok": False, "message": "Paste your Slack webhook URL first."}
        if cfg.demo:
            ok = url.startswith("https://hooks.slack.com/")
            return {"ok": ok, "message": "Demo mode: nothing was sent." if ok else
                    "The Slack webhook should start with https://hooks.slack.com/."}
        return checks.slack_test(url, cfg.page_url)

    # -- runs ------------------------------------------------------------------
    @app.post("/api/onboarding/complete")
    def onboarding_complete(request: Request):
        user = require_user(request)
        if not has_required_keys(user["id"]):
            raise ApiError(400, "Add your twitterapi.io and Vercel AI Gateway keys first.")
        db.set_onboarded(user["id"])
        queue_run(user["id"])
        return me(db.user(user["id"]))

    @app.post("/api/run")
    def run_now(request: Request, body: RunIn = Body(default=RunIn())):
        user = require_user(request)
        uid = user["id"]
        if not user["onboarded"]:
            raise ApiError(400, "Finish setting up your page first.")
        if not cfg.demo and not has_required_keys(uid):
            raise ApiError(400, "Add your twitterapi.io and Vercel AI Gateway keys first.")
        # Never refuse: a person who just changed their settings should get a rebuild. Runs stay
        # at least RUN_COOLDOWN apart, so a request that comes too soon is scheduled instead.
        r = db.run(uid)
        if r["state"] == "queued":  # settings are read when the run starts, so it picks up the change
            if body.slack == "now":
                db.update_run(uid, slack_mode="now")
            return {**status(uid), "message": "An update is already queued and will use your latest settings."}
        if r["state"] == "running":
            db.update_run(uid, rerun=1, **({"slack_mode": "now"} if body.slack == "now" else {}))
            return {**status(uid), "message": "Your page is updating now. It rebuilds with your changes right after."}
        if r["state"] not in ("new", "error"):
            last = max(r["last_run_at"] or 0, r["queued_at"] or 0)
            wait = RUN_COOLDOWN - (time.time() - last)
            if wait > 0:
                db.update_run(uid, next_run_at=min(r["next_run_at"] or float("inf"), last + RUN_COOLDOWN),
                              **({"slack_mode": "now"} if body.slack == "now" else {}))
                return {**status(uid), "message": f"Your page updated a few minutes ago, so it rebuilds with your changes "
                                                  f"in about {max(1, round(wait / 60))} min."}
        queue_run(uid, body.slack)
        return {**status(uid), "message": "Your page rebuilds in a few minutes."}

    @app.get("/api/status")
    def get_status(request: Request):
        return status(require_user(request)["id"])

    # -- portability -----------------------------------------------------------
    @app.get("/api/export")
    def export(request: Request, keys: int = 0):
        user = require_user(request)
        out = {"format": "timeline-profile", "version": 1,
               "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "settings": json.loads(user["settings"])}
        if keys:
            out["keys"] = worker.keys(user["id"])
        return JSONResponse(out, headers={"Content-Disposition": 'attachment; filename="timeline-profile.json"',
                                          "Cache-Control": "no-store"})

    @app.post("/api/import")
    def import_profile(request: Request, body: ImportIn):
        user = require_user(request)
        p = body.profile
        if p.get("format") != "timeline-profile" or p.get("version") != 1:
            raise ApiError(422, "That file isn't a timeline profile export (format timeline-profile, version 1).")
        try:
            settings = validate(p.get("settings"))
        except SettingsError as e:
            raise ApiError(422, f"The profile's settings aren't valid: {e}")
        keys = p.get("keys")
        if keys is not None:
            if not isinstance(keys, dict):
                raise ApiError(422, "The profile's keys should be an object.")
            keys = _validate_keys({k: keys.get(k) for k in KEY_ENV})
        db.set_settings(user["id"], settings)
        if keys:
            save_keys(user["id"], keys)
        return me(db.user(user["id"]))

    @app.post("/api/recap/rotate")
    def rotate(request: Request):
        user = require_user(request)
        db.set_recap_token(user["id"], recap_token())
        return {"recap_url": recap_url(db.user(user["id"]))}

    @app.delete("/api/account")
    def delete_account(request: Request, body: DeleteIn):
        user = require_user(request)
        if user["pw_hash"]:
            if not verify_password(body.password, user["pw_hash"]):
                raise ApiError(403, "That password isn't right.")
        elif body.confirm.strip().lower() != user["email"].lower():
            raise ApiError(403, "Type your email address to confirm.")
        db.delete_user(user["id"])
        shutil.rmtree(cfg.user_dir(user["id"]), ignore_errors=True)
        request.session.clear()
        return {"ok": True}

    return app


app = create_app()
