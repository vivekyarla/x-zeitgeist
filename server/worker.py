"""Background runs: one pipeline subprocess per user, on a schedule.

Every 20 seconds the scheduler queues users whose next update is due, starts up to
MAX_CONCURRENT_RUNS queued runs, and checks each Slack-enabled user's daily post once an
hour (`--deliver`, saved state only). Every run takes a lease on that user's row in the
database, so several server processes never work on one user's files at the same time.

Each subprocess gets a minimal environment: a few system variables plus that one user's
paths and keys. Nothing else from the server's environment (or any other user) leaks in.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ROOT, Config
from .db import DB
from .security import Box, redact
from .settings_schema import SettingsError, default_settings, validate

log = logging.getLogger("timeline.worker")

KEY_ENV = {"twitterapi_io": "TWITTERAPI_IO_KEY", "ai_gateway": "AI_GATEWAY_API_KEY",
           "typesafe": "TYPESAFE_API_KEY", "slack_webhook": "SLACK_WEBHOOK_URL"}
REQUIRED_KEYS = ("twitterapi_io", "ai_gateway")
RUN_TIMEOUT = 20 * 60
DELIVER_TIMEOUT = 5 * 60
RETRY_AFTER = 30 * 60
TICK = 20
LOG_MAX = 200_000
# System variables a subprocess may inherit. Proxy and CA settings are the operator's
# network setup, not anyone's credentials; everything else (other users' keys, SECRET_KEY)
# stays out.
ENV_ALLOW = ("PATH", "HOME", "LANG", "PYTHONPATH", "LC_ALL", "TZ", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")


class Worker:
    def __init__(self, cfg: Config, db: DB, box: Box):
        self.cfg, self.db, self.box = cfg, db, box
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=cfg.max_concurrent_runs + 1, thread_name_prefix="run")
        self._inflight: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    # -- per-user files and env -------------------------------------------
    def paths(self, user_id: int) -> dict[str, Path]:
        root = self.cfg.user_dir(user_id)
        return {"root": root, "settings": root / "settings.json", "data": root / "data", "out": root / "public"}

    def keys(self, user_id: int) -> dict[str, str]:
        """This user's keys, decrypted. Only ever handed to their own subprocess or check."""
        out = {}
        for name, sealed in self.db.keys(user_id).items():
            v = self.box.open(sealed)
            if v:
                out[name] = v
        return out

    def env_for(self, user_id: int, keys: dict[str, str], recap_token: str) -> dict[str, str]:
        p = self.paths(user_id)
        env = {k: os.environ[k] for k in ENV_ALLOW if os.environ.get(k)}
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.update({"TIMELINE_SETTINGS": str(p["settings"]), "TIMELINE_DATA": str(p["data"]),
                    "TIMELINE_OUT": str(p["out"]), "PAGE_URL": self.cfg.page_url,
                    "TIMELINE_RECAP_URL": self.cfg.recap_base(recap_token)})
        for name, var in KEY_ENV.items():
            if keys.get(name):
                env[var] = keys[name]
        return env

    def write_settings(self, user_id: int, raw: str) -> None:
        p = self.paths(user_id)
        for d in (p["data"], p["out"]):
            d.mkdir(parents=True, exist_ok=True)
        try:
            settings = validate(json.loads(raw))
        except (SettingsError, ValueError):
            settings = default_settings()
        settings.pop("preset_id", None)
        tmp = p["settings"].with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=1, ensure_ascii=False))
        tmp.replace(p["settings"])

    def _write_log(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text[-LOG_MAX:])

    def _exec(self, args: list[str], env: dict, timeout: int, secrets_: list[str]) -> tuple[int | None, str]:
        try:
            proc = subprocess.run([sys.executable, "-m", "pipeline.run", *args], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            code, out = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as e:
            code, out = None, e.stdout or b""
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return code, redact(text, secrets_)

    # -- one update --------------------------------------------------------
    def run_user(self, user_id: int) -> bool:
        """Run a queued update for this user now. Returns True on success, False on failure or if
        the run wasn't queued / someone else holds it."""
        if not self.db.claim(user_id, self.owner, RUN_TIMEOUT + 300, start_run=True):
            return False
        try:
            user = self.db.user(user_id)
            if not user:
                return False
            run = self.db.run(user_id)
            slack_mode = run["slack_mode"] if run and run["slack_mode"] in ("auto", "now", "skip") else "auto"
            self.write_settings(user_id, user["settings"])
            keys = {} if self.cfg.demo else self.keys(user_id)
            env = self.env_for(user_id, keys, user["recap_token"])
            args = ["--demo"] if self.cfg.demo else ["--slack", slack_mode]
            started = time.time()
            code, text = self._exec(args, env, RUN_TIMEOUT, list(keys.values()))
            self._write_log(self.paths(user_id)["data"] / "last_run.log", text)
            now = time.time()
            error = None
            if code is None:
                error = "The update took longer than 20 minutes and was stopped. It will try again in 30 minutes."
            elif code != 0:
                error = humanize(text)
            elif not self.cfg.demo and "fetched 0 tweets" in text and "search failed" in text:
                error = humanize(text)  # every search failed (e.g. a rejected key) but nothing crashed
            rerun = bool((self.db.run(user_id) or {"rerun": 0})["rerun"])  # settings saved while this ran
            if error:
                self.db.update_run(user_id, state="error", last_run_at=now, last_error=error, rerun=0,
                                   next_run_at=now if rerun else now + RETRY_AFTER, slack_mode="auto")
            else:
                self.db.update_run(user_id, state="ready", last_run_at=now, last_error=None, rerun=0,
                                   next_run_at=now if rerun else now + self.cfg.refresh_hours * 3600, slack_mode="auto")
            log.info("user %s run %s in %.0fs", user_id, "failed" if error else "ok", now - started)
            return error is None
        except Exception:
            log.exception("user %s run crashed", user_id)
            now = time.time()
            self.db.update_run(user_id, state="error", last_run_at=now, next_run_at=now + RETRY_AFTER,
                               last_error="The update couldn't start because of a server problem. "
                                          "It will try again in 30 minutes.")
            return False
        finally:
            self.db.release(user_id, self.owner)
            self._cleanup_if_deleted(user_id)

    def deliver_user(self, user_id: int) -> bool:
        """The hourly daily-post check: `pipeline.run --deliver` on saved state (no API calls)."""
        if self.cfg.demo or not self.db.claim(user_id, self.owner, DELIVER_TIMEOUT + 60, start_run=False):
            return False
        try:
            user = self.db.user(user_id)
            if not user:
                return False
            self.write_settings(user_id, user["settings"])
            keys = self.keys(user_id)
            code, text = self._exec(["--deliver"], self.env_for(user_id, keys, user["recap_token"]),
                                    DELIVER_TIMEOUT, list(keys.values()))
            self._write_log(self.paths(user_id)["data"] / "last_deliver.log", text)
            self.db.update_run(user_id, last_deliver_at=time.time())
            return code == 0
        finally:
            self.db.release(user_id, self.owner)
            self._cleanup_if_deleted(user_id)

    def _cleanup_if_deleted(self, user_id: int) -> None:
        if not self.db.user(user_id):
            shutil.rmtree(self.cfg.user_dir(user_id), ignore_errors=True)

    # -- scheduler ---------------------------------------------------------
    def _delivery_due(self, row, now: float) -> bool:
        try:
            s = json.loads(row["settings"])
        except ValueError:
            return False
        slack = s.get("slack") or {}
        if not slack.get("enabled") or slack.get("frequency", "daily") != "daily" or row["state"] == "running":
            return False
        if not (self.paths(row["id"])["data"] / "week.json").exists():
            return False
        last = row["last_deliver_at"]
        if not last:
            return True
        # Once per local clock hour, so a 9am post goes out within a tick of 9:00.
        try:
            tz = ZoneInfo(s.get("timezone") or "America/Los_Angeles")
        except Exception:
            tz = ZoneInfo("America/Los_Angeles")
        hour = lambda t: datetime.fromtimestamp(t, timezone.utc).astimezone(tz).strftime("%Y%m%d%H")
        return hour(now) != hour(last) or now - last >= 3600

    def _submit(self, kind: str, user_id: int) -> None:
        with self._lock:
            if (kind, user_id) in self._inflight:
                return
            self._inflight.add((kind, user_id))

        def job():
            try:
                (self.run_user if kind == "run" else self.deliver_user)(user_id)
            finally:
                with self._lock:
                    self._inflight.discard((kind, user_id))
        self._pool.submit(job)

    def tick(self) -> None:
        now = time.time()
        self.db.queue_due(now, () if self.cfg.demo else REQUIRED_KEYS)
        with self._lock:
            running = sum(1 for k, _ in self._inflight if k == "run")
        free = self.cfg.max_concurrent_runs - running
        if free > 0:
            for uid in self.db.queued(free):
                self._submit("run", uid)
        if not self.cfg.demo:
            for row in self.db.onboarded_users():
                if self._delivery_due(row, now) and self.db.keys(row["id"]).get("slack_webhook"):
                    self._submit("deliver", row["id"])

    def kick(self) -> None:
        self._kick.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._kick.wait(TICK)
            self._kick.clear()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()
        self._pool.shutdown(wait=False, cancel_futures=True)


def humanize(text: str) -> str:
    """Turn a run's output into one sentence a person can act on."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    low = text.lower()
    if "keyerror: 'twitterapi_io_key'" in low:
        return "Your twitterapi.io key is missing. Add it under API keys."
    if "keyerror: 'ai_gateway_api_key'" in low:
        return "Your AI Gateway key is missing. Add it under API keys."
    for ln in reversed(lines):
        l = ln.lower()
        if "twitterapi.io" in l and (" 401" in l or " 403" in l or "unauthorized" in l or "forbidden" in l):
            return ("twitterapi.io rejected your key. Copy it again from twitterapi.io/dashboard "
                    "and save it under API keys.")
        if "twitterapi.io" in l and " 402" in l:
            return "Your twitterapi.io account is out of credits. Top it up at twitterapi.io/dashboard."
        if "ai-gateway.vercel.sh" in l and (" 401" in l or " 403" in l or "unauthorized" in l):
            return ("Vercel AI Gateway rejected your key. Create a new one at vercel.com → AI Gateway → "
                    "API Keys and save it under API keys.")
        if "ai-gateway.vercel.sh" in l and " 402" in l:
            return "Your Vercel AI Gateway account is out of credits. Add credits in the Vercel dashboard."
    if "jev returned no verdicts" in low:
        return ("Jev couldn't judge any tweets. Check your AI Gateway key (or TypeSafe key) under API keys, "
                "and that your AI Gateway account has credits.")
    noise = ("traceback", "file \"", "^", "~", "during handling", "the above exception")
    meaningful = [ln for ln in lines if not ln.lower().startswith(noise)]
    last = meaningful[-1] if meaningful else "no output"
    return f"The last update failed: {last[:300]}"
