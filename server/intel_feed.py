"""The competitive intel brief (comp-intel's data/today.json, or a copy another bot pushed) as Markdown,
for Instinct and Muse: on its own at /intel/recap.md, or appended to any recap.md with ?intel=1."""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline.channel import INTEL_FRESH

CACHE_SECONDS = 900


def _cite(x: dict) -> str:
    cs = [c for c in x.get("cites", []) if c.get("url")]
    return f" ([{cs[0].get('label') or 'source'}]({cs[0]['url']}))" if cs else ""


def brief_markdown(d: dict, page_url: str, full: bool = True) -> str:
    """comp-intel's daily brief as Markdown. full=False keeps the briefing and the trends only (for
    appending to the X recap)."""
    mb = d.get("market_briefing") or {}
    out = ["## Competitive intel" + (f" ({d['as_of']})" if d.get("as_of") else "")]
    if page_url:
        out.append(f"Full report: {page_url}")
    if mb.get("lede"):
        out += ["", mb["lede"]]
    if mb.get("bullets"):
        out += ["", "### Market briefing"] + [f"- {b.get('text', '')}{_cite(b)}" for b in mb["bullets"]]
    if d.get("competitive_moves"):
        out += ["", "### Competitive moves"] + [f"- **{x.get('company', '')}:** {x.get('text', '')}{_cite(x)}"
                                               for x in d["competitive_moves"]]
    if d.get("market_direction"):
        out += ["", "### Market direction"] + [f"- **{x.get('label', '')}:** {x.get('text', '')}"
                                               for x in d["market_direction"]]
    if full:
        for w in d.get("watch", [])[:1]:
            out += ["", "### One thing to watch", f"**{w.get('title', '')}.** {w.get('body', '')}{_cite(w)}"]
        if d.get("biggest_ai_news"):
            out += ["", "### Biggest AI news"] + [f"- **{x.get('title', '')}:** {x.get('body', '')}{_cite(x)}"
                                                  for x in d["biggest_ai_news"]]
        if d.get("quiet_note"):
            out += ["", f"_{d['quiet_note']}_"]
    return "\n".join(out).strip() + "\n"


class IntelFeed:
    """Fetches INTEL_URL at most every 15 minutes; a fresh pushed copy (POST /api/intel) wins."""

    def __init__(self, url: str, page_url: str, pushed_file: Path):
        self.url, self.page_url, self.pushed_file = url, page_url or url, pushed_file
        self._lock = threading.Lock()
        self._at, self._data = 0.0, None

    @property
    def enabled(self) -> bool:
        return bool(self.url) or self.pushed_file.exists()

    def _pushed(self) -> str | None:
        try:
            p = json.loads(self.pushed_file.read_text())
            if datetime.now(timezone.utc) - datetime.fromisoformat(p["received_at"]) <= INTEL_FRESH and p.get("text"):
                head = "## Competitive intel" + (f"\nFull report: {p.get('url') or self.page_url}" if (p.get("url") or self.page_url) else "")
                return head + "\n\n" + p["text"].strip() + "\n"
        except (OSError, ValueError, KeyError):
            pass
        return None

    def _brief(self) -> dict | None:
        with self._lock:
            if self.url and time.time() - self._at > CACHE_SECONDS:
                self._at = time.time()
                try:
                    r = requests.get(self.url, timeout=10, headers={"Accept": "application/json"})
                    d = r.json() if r.status_code == 200 else None
                    if isinstance(d, dict) and ("market_briefing" in d or "market_direction" in d):
                        self._data = d
                except (requests.RequestException, ValueError):
                    pass  # keep the last good copy
            return self._data

    def markdown(self, full: bool = True) -> str | None:
        pushed = self._pushed()
        if pushed:
            return pushed
        d = self._brief()
        return brief_markdown(d, self.page_url, full) if d else None
