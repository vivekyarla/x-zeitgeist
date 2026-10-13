"""The marketing channel digest: the GTM archetype's report, condensed, plus market intel from
another bot's site, posted to a Slack channel.

    python -m pipeline.channel            # build the post and print it
    python -m pipeline.channel --send     # post it

Reads the archetype's saved week (TIMELINE_SETTINGS / TIMELINE_DATA, like a normal run) and:
- 4-5 bullets on what's moving today, each with tweet links, and a link to the full report (PAGE_URL)
- 2-3 market items and 1-2 other trends from the market-intel bot: a copy it pushed to /api/intel
  (INTEL_FILE) if one arrived in the last 36 hours, otherwise its site (INTEL_URL), fetched now

Env: AI_GATEWAY_API_KEY, SLACK_BOT_TOKEN, CHANNEL_SLACK_CHANNEL (an id like C0123, or a name),
optional INTEL_URL, INTEL_FILE, CHANNEL_TIMEZONE, CHANNEL_OUT.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .engage import ENDPOINT, SlackError, _esc, _slack

INTEL_FRESH = timedelta(hours=36)
INTEL_CHARS = 15_000


# ---------------------------------------------------------------------------
# Market intel
# ---------------------------------------------------------------------------
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "head"}
    BLOCK = {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def page_url() -> str:
    """The human-readable intel page to link to (INTEL_PAGE_URL, else INTEL_URL)."""
    return os.getenv("INTEL_PAGE_URL", "").strip() or os.getenv("INTEL_URL", "").strip()


def brief_text(d: dict) -> tuple[str, list[str]]:
    """A structured daily brief (like comp-intel's data/today.json) as compact text, plus every
    source URL it cites (the only links a market item may carry)."""
    urls: list[str] = []

    def cite(x: dict) -> str:
        cs = [c.get("url") for c in x.get("cites", []) if c.get("url")]
        urls.extend(cs)
        return f" [source: {cs[0]}]" if cs else ""
    mb = d.get("market_briefing") or {}
    parts = [f"As of: {d.get('as_of', '?')}"]
    if mb.get("lede"):
        parts.append("Market briefing: " + mb["lede"])
    parts += [f"- {b.get('text', '')}{cite(b)}" for b in mb.get("bullets", [])]
    if d.get("market_direction"):
        parts.append("Market direction (trends):")
        parts += [f"- {x.get('label', '')}: {x.get('text', '')}" for x in d["market_direction"]]
    if d.get("competitive_moves"):
        parts.append("Competitive moves:")
        parts += [f"- {x.get('company', '')}: {x.get('text', '')}{cite(x)}" for x in d["competitive_moves"]]
    for w in d.get("watch", [])[:1]:
        parts.append(f"One thing to watch: {w.get('title', '')}. {w.get('body', '')}{cite(w)}")
    if d.get("biggest_ai_news"):
        parts.append("Biggest AI news:")
        parts += [f"- {x.get('title', '')}: {x.get('body', '')}{cite(x)}" for x in d["biggest_ai_news"]]
    return "\n".join(parts), urls


def load_intel(now: datetime) -> tuple[dict | None, str]:
    """(intel, note). intel = {"text", "url", "source", "links"}; note explains a miss in a line."""
    url = os.getenv("INTEL_URL", "").strip()
    f = os.getenv("INTEL_FILE", "").strip()
    if f and Path(f).exists():
        try:
            pushed = json.loads(Path(f).read_text())
            at = datetime.fromisoformat(pushed["received_at"])
            if now - at <= INTEL_FRESH and len(pushed.get("text", "")) >= 40:
                print(f"intel: using the pushed copy from {pushed['received_at']}")
                return {"text": pushed["text"][:INTEL_CHARS], "url": pushed.get("url") or page_url(),
                        "source": "pushed", "links": re.findall(r"https?://[^\s)\]>\"']+", pushed["text"])}, ""
        except (OSError, ValueError, KeyError):
            pass
    if not url:
        return None, "no market intel source set up"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (timeline digest)",
                                                    "Accept": "text/html,application/json,text/markdown,text/plain"})
    except requests.RequestException as e:
        print(f"intel: couldn't fetch {url} ({type(e).__name__})")
        return None, "couldn't reach the market intel site"
    if r.status_code != 200:
        print(f"intel: {url} answered {r.status_code}")
        return None, f"the market intel site answered {r.status_code}"
    ctype = r.headers.get("content-type", "")
    links: list[str] = []
    if "json" in ctype or url.endswith(".json"):
        data = r.json()
        if isinstance(data, dict) and ("market_briefing" in data or "market_direction" in data):
            text, links = brief_text(data)  # a structured daily brief: use its sections directly
        else:
            text = json.dumps(data, ensure_ascii=False, indent=1)
    elif "html" in ctype or r.text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        text = html_text(r.text)
    else:
        text = r.text
    text = text.strip()
    if len(text) < 300:
        print(f"intel: {url} has almost no text ({len(text)} chars); it's probably built in the browser with JavaScript")
        return None, "the market intel page has no readable text (it's probably rendered with JavaScript)"
    print(f"intel: fetched {len(text)} chars from {url}")
    links = links or re.findall(r"https?://[^\s)\]>\"']+", text)
    return {"text": text[:INTEL_CHARS], "url": page_url(), "source": "fetched", "links": links}, ""


# ---------------------------------------------------------------------------
# The archetype's report
# ---------------------------------------------------------------------------
def report(now: datetime) -> dict:
    from . import config, recap
    state = json.loads((Path(os.environ["TIMELINE_DATA"]) / "week.json").read_text())
    latest = state.get("latest")
    if not latest:
        raise SystemExit("the GTM report has nothing yet this week, so there's nothing to post")
    tweets = state["tweets"]
    theme_of = {i: th["name"] for th in latest.get("themes", []) for i in th["tweet_ids"]}
    top = []
    for i in latest.get("top_ids", [])[:40]:
        t = tweets.get(i)
        if not t:
            continue
        seen = datetime.fromisoformat(t["first_seen"]) if t.get("first_seen") else now - timedelta(days=3)
        top.append({"id": t["id"], "url": t["url"], "author": t["author"]["handle"], "text": t["text"],
                    "likes": t["likes"], "theme": theme_of.get(t["id"]),
                    "hours_since_seen": round((now - seen).total_seconds() / 3600, 1)})
    # What's moving today first, then the rest of the week's best.
    top.sort(key=lambda t: (t["hours_since_seen"] > 30, -t["likes"]))
    r = recap.recap(state, now)
    return {"thesis": r["thesis"], "page_url": config.PAGE_URL or r["links"]["page"],
            "themes": [{"name": th["name"], "summary": th["summary"]} for th in latest.get("themes", [])],
            "tweets": top[:25], "writer_model": config.WRITER_MODEL}


SYSTEM = """You write a very short Slack digest for a B2B AI startup's marketing and GTM team.

Input: this week's GTM/marketing report from X (thesis, themes, top tweets; hours_since_seen tells you
what's new today), and optionally the text of a market-intelligence report from another bot.

Return only JSON:
{"bullets": [{"text": "<=16 words: what's happening and why it matters to GTM/marketing", "ids": ["<tweet id>", ...]}],
 "market": [{"text": "<=22 words", "url": "<the item's source URL from the intel, if it has one>"}],
 "trends": [{"text": "<=18 words"}]}

- bullets: 4-5, most important first, favoring what's new today; each cites 1-2 tweet ids from the input.
  Plain, specific, no hype or emoji; name companies and numbers when the tweets have them.
- market: the 2-3 most important market developments in the intel report (funding, launches, pricing,
  competitor moves, buyer behavior). If it has a "Market briefing", use those items first. Copy each
  item's source URL exactly from the intel. Only from the intel text; empty if there's no intel.
- trends: 1-2 other trends the intel report identifies that aren't already in market (its "Market
  direction" section if it has one), e.g. "Accelerating: MCP as the GTM connector, with Seismic and Nimble".
  Empty if none or no intel.
- Never invent facts, numbers, or tweet ids."""


def ask_model(rep: dict, intel: dict | None) -> dict:
    user = json.dumps({"thesis": rep["thesis"], "themes": rep["themes"],
                       "tweets": [{k: t[k] for k in ("id", "author", "text", "likes", "theme", "hours_since_seen")}
                                  for t in rep["tweets"]],
                       "intel": intel and intel["text"]}, ensure_ascii=False)
    for attempt in range(2):
        r = requests.post(ENDPOINT, timeout=180, headers={
            "Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY'].strip()}", "Content-Type": "application/json"},
            json={"model": rep["writer_model"], "temperature": 0.2, "max_tokens": 8000,
                  "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]})
        if r.status_code != 200:
            print(f"model {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        text = re.sub(r"^```(?:json)?|```$", "", (r.json()["choices"][0]["message"]["content"] or "").strip(), flags=re.M).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"model returned bad JSON ({e}): {text[:200]!r}")
    raise RuntimeError("the model didn't return valid JSON twice in a row")


def clean(out: dict, rep: dict, intel: dict | None) -> dict:
    ids = {t["id"] for t in rep["tweets"]}
    words = lambda s, n: " ".join(str(s or "").split()[:n])
    bullets = []
    for b in out.get("bullets", [])[:5]:
        cited = [str(i) for i in b.get("ids", []) if str(i) in ids][:2]
        if b.get("text") and cited:
            bullets.append({"text": words(b["text"], 18), "ids": cited})
    has = bool(intel)
    allowed = set((intel or {}).get("links") or [])
    return {"bullets": bullets,
            "market": [{"text": words(x.get("text"), 24), "url": x["url"] if x.get("url") in allowed else ""}
                       for x in out.get("market", [])[:3] if has and x.get("text")],
            "trends": [{"text": words(x.get("text"), 20)} for x in out.get("trends", [])[:2] if has and x.get("text")]}


def message(d: dict, rep: dict, intel: dict | None, intel_note: str, now: datetime, tz: str) -> dict:
    by_id = {t["id"]: t for t in rep["tweets"]}
    local = now.astimezone(ZoneInfo(tz))
    day = f"{local:%a %b} {local.day}"
    thesis = re.sub(r"\s+", " ", rep["thesis"] or "").strip()
    lines = [f"*GTM zeitgeist · {day}*  ·  <{rep['page_url']}|Full report>"]
    if thesis:
        lines.append(f"_{_esc(thesis if len(thesis) <= 240 else thesis[:239].rstrip() + '…')}_")
    for b in d["bullets"]:
        links = " ".join(f"<{by_id[i]['url']}|@{_esc(by_id[i]['author'])}>" for i in b["ids"])
        lines.append(f"• {_esc(b['text'])}  {links}")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2990]}}]
    intel_url = (intel or {}).get("url") or page_url()
    head = "*Market intel*" + (f"  ·  <{intel_url}|Full report>" if intel_url else "")
    if d["market"] or d["trends"]:
        mi = [head] + [f"• {_esc(x['text'])}" + (f"  <{x['url']}|source>" if x.get("url") else "") for x in d["market"]]
        if d["trends"]:
            mi.append("*Also trending*")
            mi += [f"• {_esc(x['text'])}" for x in d["trends"]]
        blocks += [{"type": "divider"}, {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(mi)[:2990]}}]
    elif intel_url:
        blocks += [{"type": "divider"}, {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"{head} (couldn't summarize today: {_esc(intel_note or 'no new report')})"}]}]
    return {"text": f"GTM zeitgeist · {day}: {thesis[:150]}", "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False}


CHANNEL_ERRORS = {
    "not_in_channel": "The bot isn't in that channel. In Slack, open the channel and type /invite @Engage digest.",
    "channel_not_found": "Slack can't find that channel. Use the channel ID (channel details → bottom, starts with C) "
                         "and invite the bot to it.",
    "is_archived": "That channel is archived.",
}


def post(msg: dict) -> None:
    """Post to CHANNEL_SLACK_CHANNEL: a channel id (C…) or name, or, for testing, an email address or
    user id (U…), which DMs that person from the bot instead."""
    token = os.environ["SLACK_BOT_TOKEN"].strip()
    channel = os.environ["CHANNEL_SLACK_CHANNEL"].strip().lstrip("#")
    try:
        if "@" in channel:
            channel = _slack("users.lookupByEmail", token, email=channel)["user"]["id"]
        if channel[:1] in ("U", "W"):
            channel = _slack("conversations.open", token, users=channel)["channel"]["id"]
        _slack("chat.postMessage", token, channel=channel, **msg)
    except SlackError as e:
        raise RuntimeError("slack: " + CHANNEL_ERRORS.get(e.code, str(e))) from None
    print("slack: posted")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="post it to the Slack channel")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    rep = report(now)
    intel, note = load_intel(now)
    d = clean(ask_model(rep, intel), rep, intel)
    if not d["bullets"]:
        raise SystemExit("the model didn't return any usable bullets")
    msg = message(d, rep, intel, note, now, os.getenv("CHANNEL_TIMEZONE", "America/Los_Angeles"))
    out = Path(os.getenv("CHANNEL_OUT") or Path(os.environ["TIMELINE_DATA"]) / "channel")
    out.mkdir(parents=True, exist_ok=True)
    (out / "last.json").write_text(json.dumps({"at": now.isoformat(), "digest": d, "intel_source": intel and intel["source"],
                                               "intel_note": note, "message": msg}, ensure_ascii=False, indent=1))
    if args.send:
        post(msg)
    else:
        print(json.dumps(msg, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
