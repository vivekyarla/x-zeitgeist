"""Server configuration from the environment."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SITE_TITLE = "This week on the timeline"


def _env_value(name: str) -> str:
    """An env var pasted into a dashboard: drop whitespace and stray wrapping quotes or <brackets>."""
    v = os.getenv(name, "").strip()
    while len(v) >= 2 and (v[0], v[-1]) in (('"', '"'), ("'", "'"), ("<", ">")):
        v = v[1:-1].strip()
    return v


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path
    base_url: str
    secret_key: str
    encryption_key: str | None
    max_concurrent_runs: int
    refresh_hours: float
    demo: bool
    worker: bool
    rate_limit: int  # login/signup attempts per IP per 10 minutes
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_domains: tuple[str, ...] = ()  # Google Workspace domains allowed to sign in; empty = any
    password_login: bool = True
    preview_keys: dict | None = None       # the owner's keys for the public example page
    preview_preset: str = "ai_startup_marketing"

    @property
    def google(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def preview(self) -> bool:
        return self.demo or bool(self.preview_keys)

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "timeline.db"

    def user_dir(self, user_id: int) -> Path:
        # Integer ids only, so a path can never be steered by user input.
        return self.data_dir / "users" / str(int(user_id))

    @property
    def page_url(self) -> str:
        return self.base_url + "/app/"

    def recap_base(self, token: str) -> str:
        return f"{self.base_url}/r/{token}/"


def is_local(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def load() -> Config:
    data_dir = Path(os.getenv("DATA_DIR") or ROOT / "var").resolve()
    # On Railway, default to the public domain it assigns, so BASE_URL is only needed for a custom domain.
    railway = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    base_url = (os.getenv("BASE_URL") or (f"https://{railway}" if railway else "http://localhost:8000")).rstrip("/")
    demo = _truthy(os.getenv("TIMELINE_DEMO"))
    secret = os.getenv("SECRET_KEY", "").strip()
    if not secret:
        if not (demo or is_local(base_url)):
            raise RuntimeError("SECRET_KEY is required when BASE_URL isn't localhost. Set it to a long random "
                               "string, e.g. `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.")
        # Local or demo: keep a generated key in the data dir, so sessions and saved keys survive restarts.
        data_dir.mkdir(parents=True, exist_ok=True)
        f = data_dir / ".secret_key"
        if not f.exists():
            f.write_text(secrets.token_urlsafe(48))
            f.chmod(0o600)
        secret = f.read_text().strip()
    elif len(secret) < 16:
        raise RuntimeError("SECRET_KEY is too short; use at least 32 random characters.")
    google_id, google_secret = _env_value("GOOGLE_CLIENT_ID"), _env_value("GOOGLE_CLIENT_SECRET")
    domains = tuple(d.strip().lower().lstrip("@") for d in os.getenv("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip())
    pw = os.getenv("PASSWORD_LOGIN", "").strip()
    # With Google set up, email/password accounts are off unless PASSWORD_LOGIN=1 turns them back on.
    password_login = _truthy(pw) if pw else not (google_id and google_secret)
    pk = {"twitterapi_io": _env_value("PREVIEW_TWITTERAPI_IO_KEY"),
          "ai_gateway": _env_value("PREVIEW_AI_GATEWAY_API_KEY")}
    return Config(
        data_dir=data_dir,
        base_url=base_url,
        secret_key=secret,
        encryption_key=os.getenv("ENCRYPTION_KEY", "").strip() or None,
        max_concurrent_runs=max(1, int(os.getenv("MAX_CONCURRENT_RUNS", "3"))),
        refresh_hours=max(0.5, float(os.getenv("REFRESH_HOURS", "3"))),
        demo=demo,
        worker=os.getenv("TIMELINE_WORKER", "1") != "0",
        rate_limit=max(1, int(os.getenv("RATE_LIMIT", "20"))),
        google_client_id=google_id,
        google_client_secret=google_secret,
        allowed_domains=domains,
        password_login=password_login,
        preview_keys=pk if all(pk.values()) else None,
        preview_preset=os.getenv("PREVIEW_PRESET", "ai_startup_marketing").strip() or "ai_startup_marketing",
    )
