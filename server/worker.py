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
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
# Leases are short and renewed while a job runs, so one left by a process that died (e.g. a
# redeploy mid-run) expires in minutes and the run is picked up again, instead of blocking it.
LEASE = 180
RENEW_EVERY = 60
# The daily engagement DM: start a little before the send time so it lands on time, retry a
# failed attempt every 15 minutes, and give up for the day mid-afternoon.
ENGAGE_TIMEOUT = 15 * 60
ENGAGE_LEAD = 8 * 60
ENGAGE_RETRY = 15 * 60
ENGAGE_GIVE_UP_HOUR = 15
CHANNEL_LEAD = 2 * 60  # the channel digest only summarizes saved reports, so it's quick
CHANNEL_PREP = 35 * 60  # refresh the archetype's report this long before the post, so it's current
CHANNEL_FRESH = 60 * 60  # a report newer than this doesn't need the extra refresh
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
        self._engage_lock = threading.Lock()
        self.channel_uid: int | None = None  # set by the app once the GTM archetype exists
        self.preview_uid: int | None = None  # set by the app once the example page exists
        self.cfg, self.db, self.box = cfg, db, box
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool = ThreadPoolExecutor(max_workers=cfg.max_concurrent_runs + 3, thread_name_prefix="run")
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

    def _exec(self, args: list[str], env: dict, timeout: int, secrets_: list[str],
              module: str = "pipeline.run") -> tuple[int | None, str]:
        try:
            proc = subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            code, out = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as e:
            code, out = None, e.stdout or b""
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return code, redact(text, secrets_)

    @contextmanager
    def _holding(self, user_id: int):
        """Keep renewing this user's lease while the block runs."""
        done = threading.Event()

        def beat():
            while not done.wait(RENEW_EVERY):
                self.db.renew(user_id, self.owner, LEASE)
        t = threading.Thread(target=beat, daemon=True)
        t.start()
        try:
            yield
        finally:
            done.set()
            t.join(timeout=5)

    def _has_read(self, user_id: int) -> bool:
        try:
            return bool(json.loads((self.paths(user_id)["data"] / "week.json").read_text()).get("latest"))
        except (OSError, ValueError):
            return False

    # -- one update --------------------------------------------------------
    def run_user(self, user_id: int) -> bool:
        """Run a queued update for this user now. Returns True on success, False on failure or if
        the run wasn't queued / someone else holds it."""
        if not self.db.claim(user_id, self.owner, LEASE, start_run=True):
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
            with self._holding(user_id):
                code, text = self._exec(args, env, RUN_TIMEOUT, list(keys.values()))
            self._write_log(self.paths(user_id)["data"] / "last_run.log", text)
            # A few progress lines (already redacted) in the server log, so a host's log view shows
            # what each run fetched and kept without opening the per-user log file.
            for ln in text.splitlines():
                if ln.strip().startswith(SUMMARY_PREFIXES):
                    log.info("user %s: %s", user_id, ln.strip()[:300])
            now = time.time()
            error = None
            if code is None:
                error = "The update took longer than 20 minutes and was stopped. It will try again in 30 minutes."
            elif code != 0:
                error = humanize(text)
            elif not self.cfg.demo and "nothing passed the filter" in text and not self._has_read(user_id):
                error = empty_reason(text)  # it ran, but there's still no thesis to show
            elif not self.cfg.demo and "fetched 0 tweets" in text and "search failed" in text:
                error = humanize(text)  # every search failed (e.g. a rejected key) but nothing crashed
            rerun = bool((self.db.run(user_id) or {"rerun": 0})["rerun"])  # settings saved while this ran
            if error:
                self.db.update_run(user_id, state="error", last_run_at=now, last_error=error, rerun=0,
                                   next_run_at=now if rerun else now + RETRY_AFTER, slack_mode="auto")
            else:
                # The GTM archetype runs once a day, just before its channel post (channel_prep), not every few hours.
                # The GTM archetype and the example page refresh at set times (fixed_time_prep), not every few hours.
                daily = user_id == self.channel_uid or (user_id == self.preview_uid and bool(self.cfg.preview_times))
                self.db.update_run(user_id, state="ready", last_run_at=now, last_error=None, rerun=0,
                                   next_run_at=now if rerun else None if daily else now + self.cfg.refresh_hours * 3600,
                                   slack_mode="auto")
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
        if self.cfg.demo or not self.db.claim(user_id, self.owner, LEASE, start_run=False):
            return False
        try:
            user = self.db.user(user_id)
            if not user:
                return False
            self.write_settings(user_id, user["settings"])
            keys = self.keys(user_id)
            with self._holding(user_id):
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
                {"run": self.run_user, "deliver": self.deliver_user, "engage": lambda _: self.run_engage(),
                 "channel": lambda _: self.run_channel()}[kind](user_id)
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
        if self.cfg.engage and self.engage_due(now):
            self._submit("engage", 0)
        if self.preview_uid is not None and self.cfg.preview_times:
            self.preview_prep(now)
        if self.cfg.channel:
            self.channel_prep(now)
            if self.channel_due(now) and self.db.run(self.channel_uid)["state"] not in ("queued", "running"):
                self._submit("channel", 0)  # posts after a pre-post refresh finishes, not mid-run
        if not self.cfg.demo:
            for row in self.db.onboarded_users():
                if self._delivery_due(row, now) and self.db.keys(row["id"]).get("slack_webhook"):
                    self._submit("deliver", row["id"])

    # -- the daily engagement DM ---------------------------------------------
    # Daily Slack jobs share one schedule: start `lead` seconds before the time, retry a failure
    # every ENGAGE_RETRY, give up at ENGAGE_GIVE_UP_HOUR, and send once right after first setup.
    def _window(self, now: float, at: str, tz: str, lead: int) -> tuple[datetime, datetime]:
        local = datetime.fromtimestamp(now, ZoneInfo(tz))
        h, m = (int(x) for x in at.split(":"))
        return local, local.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(seconds=lead)

    def _daily_due(self, prefix: str, now: float, at: str, tz: str, weekdays_only: bool, lead: int) -> bool:
        if self.db.meta(prefix + "_force") == "1":
            return True
        if now - float(self.db.meta(prefix + "_tried_at") or 0) < ENGAGE_RETRY:
            return False
        sent = self.db.meta(prefix + "_sent_date")
        if sent is None:
            return True  # first time it's set up: send one right away, so it can be checked
        local, start = self._window(now, at, tz, lead)
        if sent == local.date().isoformat() or (weekdays_only and local.weekday() >= 5):
            return False
        return start <= local and local.hour < ENGAGE_GIVE_UP_HOUR

    def _daily_done(self, prefix: str, at: str, tz: str, lead: int) -> None:
        local, start = self._window(time.time(), at, tz, lead)
        # A first send before today's window doesn't count as today's, so the scheduled one still goes out.
        prev = self.db.meta(prefix + "_sent_date")
        self.db.set_meta(prefix + "_sent_date", local.date().isoformat() if local >= start else (prev or "first"))
        self.db.set_meta(prefix + "_last_error", "")

    def engage_due(self, now: float) -> bool:
        return self._daily_due("engage", now, self.cfg.engage_time, self.cfg.engage_tz,
                               self.cfg.engage_weekdays_only, ENGAGE_LEAD)

    def channel_due(self, now: float) -> bool:
        if self.channel_uid is None or not self._has_read(self.channel_uid):
            return False  # nothing to summarize until the archetype's first report exists
        if self.db.meta("channel_target") != self.cfg.channel_slack_channel:
            # A new destination (e.g. from a test DM to the team channel): post there right away once.
            self.db.set_meta("channel_target", self.cfg.channel_slack_channel)
            self.db.set_meta("channel_sent_date", None)
            self.db.set_meta("channel_tried_at", None)
        return self._daily_due("channel", now, self.cfg.channel_time, self.cfg.channel_tz,
                               self.cfg.channel_weekdays_only, CHANNEL_LEAD)

    def channel_prep(self, now: float) -> None:
        """The archetype refreshes twice on posting days: shortly before the morning post, and in the
        evening (CHANNEL_EVENING_REFRESH), instead of every few hours."""
        slots = [("channel_prep_date", self.cfg.channel_time, CHANNEL_PREP)]
        if self.cfg.channel_evening:
            slots.append(("channel_evening_date", self.cfg.channel_evening, 0))
        self._fixed_refresh(self.channel_uid, slots, self.cfg.channel_tz, self.cfg.channel_weekdays_only, "GTM report", now)

    def preview_prep(self, now: float) -> None:
        """The example page refreshes at PREVIEW_REFRESH_TIMES (08:30 and 20:00 PT), every day."""
        slots = [(f"preview_refresh_{t}", t, 0) for t in self.cfg.preview_times]
        self._fixed_refresh(self.preview_uid, slots, "America/Los_Angeles", False, "example page", now)

    def _fixed_refresh(self, uid, slots, tz: str, weekdays_only: bool, what: str, now: float | None = None) -> None:
        """Queue one run of `uid` in each slot (meta key, HH:MM, lead seconds) once a day, within 20 minutes
        of its time, unless it ran in the last hour."""
        if uid is None:
            return
        now = time.time() if now is None else now
        for key, at, lead in slots:
            local, start = self._window(now, at, tz, lead)
            today = local.date().isoformat()
            if (self.db.meta(key) == today or local < start or local >= start + timedelta(minutes=20)
                    or (weekdays_only and local.weekday() >= 5)):
                continue
            self.db.set_meta(key, today)
            run = self.db.run(uid)
            if run["state"] in ("queued", "running") or now - (run["last_run_at"] or 0) < CHANNEL_FRESH:
                continue
            self.db.queue(uid)
            log.info("refreshing the %s (%s)", what, at)

    def _job_env(self, extra: dict) -> dict:
        env = {k: os.environ[k] for k in ENV_ALLOW if os.environ.get(k)}
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONUNBUFFERED"] = "1"
        env.update({k: v for k, v in extra.items() if v})
        return env

    def run_channel(self) -> bool:
        self.db.set_meta("channel_tried_at", str(time.time()))
        self.db.set_meta("channel_force", "0")
        uid = self.channel_uid
        p = self.paths(uid)
        out = self.cfg.data_dir / "channel"
        out.mkdir(parents=True, exist_ok=True)
        keys = self.cfg.channel_keys or {}
        env = self._job_env({"TIMELINE_SETTINGS": str(p["settings"]), "TIMELINE_DATA": str(p["data"]),
                             "TIMELINE_OUT": str(p["out"]), "PAGE_URL": self.cfg.base_url + "/gtm/",
                             "AI_GATEWAY_API_KEY": keys.get("ai_gateway", ""), "SLACK_BOT_TOKEN": self.cfg.channel_slack_token,
                             "CHANNEL_SLACK_CHANNEL": self.cfg.channel_slack_channel, "CHANNEL_TIMEZONE": self.cfg.channel_tz,
                             "INTEL_URL": self.cfg.intel_url, "INTEL_PAGE_URL": self.cfg.intel_page_url, "INTEL_FILE": str(self.cfg.data_dir / "intel" / "latest.json"),
                             "CHANNEL_OUT": str(out)})
        code, text = self._exec(["--send"], env, ENGAGE_TIMEOUT, [*keys.values(), self.cfg.channel_slack_token],
                                module="pipeline.channel")
        self._write_log(out / "last_run.log", text)
        for ln in text.splitlines():
            if ln.strip().startswith(("intel", "slack", "Traceback", "RuntimeError", "SystemExit", "model")):
                log.info("channel: %s", ln.strip()[:300])
        if code == 0:
            self._daily_done("channel", self.cfg.channel_time, self.cfg.channel_tz, CHANNEL_LEAD)
            log.info("channel: digest posted")
            return True
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        err = "timed out" if code is None else (lines[-1][:300] if lines else "no output")
        self.db.set_meta("channel_last_error", err)
        log.warning("channel: failed: %s", err)
        return False

    def run_engage(self) -> bool:
        now = time.time()
        self.db.set_meta("engage_tried_at", str(now))
        self.db.set_meta("engage_force", "0")
        out = self.cfg.data_dir / "engage"
        out.mkdir(parents=True, exist_ok=True)
        env = {k: os.environ[k] for k in ENV_ALLOW if os.environ.get(k)}
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        keys = self.cfg.engage_keys or {}
        env.update({"PYTHONUNBUFFERED": "1", "TWITTERAPI_IO_KEY": keys.get("twitterapi_io", ""),
                    "AI_GATEWAY_API_KEY": keys.get("ai_gateway", ""), "SLACK_BOT_TOKEN": self.cfg.engage_slack_token,
                    "ENGAGE_DM_EMAIL": self.cfg.engage_dm_email, "ENGAGE_DM_USER": self.cfg.engage_dm_user,
                    "ENGAGE_TIMEZONE": self.cfg.engage_tz, "ENGAGE_OUT": str(out)})
        with self._engage_lock:
            code, text = self._exec(["--send"], env, ENGAGE_TIMEOUT,
                                    [*keys.values(), self.cfg.engage_slack_token], module="pipeline.engage")
        self._write_log(out / "last_run.log", text)
        for ln in text.splitlines():
            if ln.strip().startswith(("fetched", "search failed", "  search failed", "slack", "Traceback",
                                      "RuntimeError", "SystemExit")) or "eligible" in ln:
                log.info("engage: %s", ln.strip()[:300])
        if code == 0:
            self._daily_done("engage", self.cfg.engage_time, self.cfg.engage_tz, ENGAGE_LEAD)
            log.info("engage: DM sent")
            return True
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        err = "timed out" if code is None else (lines[-1][:300] if lines else "no output")
        self.db.set_meta("engage_last_error", err)
        log.warning("engage: failed: %s", err)
        return False

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


SUMMARY_PREFIXES = ("fetched", "judged", "sent to writer", "nothing passed", "search failed", "  search failed",
                    "  fetched by source", "slack:", "Jev", "jev:", "Traceback", "SystemExit", "requests.exceptions")


def empty_reason(text: str) -> str:
    """Why a run that finished still has nothing to show."""
    m = re.search(r"fetched (\d+) tweets", text)
    n = int(m.group(1)) if m else 0
    if n == 0:
        failed = "search failed" in text
        return (humanize(text) if failed and humanize(text) != "The last update failed: no output" else
                "The update found no tweets this week. " + ("Every search failed, so check your twitterapi.io key "
                "and credits." if failed else "Check the searches and panel accounts under Sources.")) + \
            " It will try again in 30 minutes."
    return (f"The update read {n} tweets, but none passed the filter yet. Early in the week there's less to go on; "
            "try the Wider net filter under Feed. It will try again in 30 minutes.")


def humanize(text: str) -> str:
    """Turn a run's output into one sentence a person can act on."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    low = text.lower()
    if "keyerror: 'twitterapi_io_key'" in low:
        return "Your twitterapi.io key is missing. Add it under API keys."
    if "keyerror: 'ai_gateway_api_key'" in low:
        return "Your AI Gateway key is missing. Add it under API keys."
    for ln in reversed(lines):
        l = " " + ln.lower().replace("(", " ")  # "search failed (402 Client Error…" → " 402"
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
