"""Server configuration from the environment."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SITE_TITLE = "This week on the timeline"


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
    base_url = (os.getenv("BASE_URL") or "http://localhost:8000").rstrip("/")
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
    )
