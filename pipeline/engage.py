"""Daily engagement picks: for each person in engage.json, today's tweets and topics worth
engaging with, sent as one Slack DM from a bot.

    python -m pipeline.engage            # build the picks and print the message (no Slack)
    python -m pipeline.engage --send     # build and DM it

Env: TWITTERAPI_IO_KEY, AI_GATEWAY_API_KEY, and for --send SLACK_BOT_TOKEN plus ENGAGE_DM_EMAIL
(or ENGAGE_DM_USER, a Slack member id). ENGAGE_CONFIG points at the config (default engage.json),
ENGAGE_OUT at a folder for the last run's candidates and picks.

1. Fetch: the config's searches and accounts, limited to the last `window_hours`. Quotes and
   retweets are traced to their originals.
2. Filter: originals only, not from the team, at least `min_likes`, and from a prominent-ish
   author: `min_followers`, or a bio tying them to a `prominent_orgs` company.
3. Rank by how fast each tweet is gaining engagement today; keep the top `candidates_for_model`.
4. One model call matches tweets and topics to each person, with a few words of why.
5. Format a short Slack message and DM it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://ai-gateway.vercel.sh/v1/chat/completions"
SLACK_API = "https://slack.com/api/"


def load_config(path: str | None = None) -> dict:
    return json.loads(Path(path or os.getenv("ENGAGE_CONFIG") or ROOT / "engage.json").read_text())


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def _posted(t: dict) -> datetime | None:
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(t.get("created_at", "").replace("Z", "+0000"), fmt)
        except ValueError:
            continue
    return None


def fetch(cfg: dict, now: datetime, src=None) -> list[dict]:
    """Originals from the window, deduped, with the highest engagement counts seen."""
    if src is None:
        from .sources import TwitterApiIo
        src = TwitterApiIo()
    since = now - timedelta(hours=float(cfg.get("window_hours", 24)))
    window = f"since_time:{int(since.timestamp())}"
    pages = int(cfg.get("pages_per_search", 2))
    seen: dict[str, dict] = {}

    def add(t: dict) -> None:
        if not t or not t.get("id") or not t.get("text") or t.get("is_retweet") or t.get("is_reply"):
            return
        posted = _posted(t)
        if posted and posted < since - timedelta(hours=6):  # quoted originals may be a bit older
            return
        old = seen.get(t["id"])
        if old:
            for k in ("likes", "retweets", "replies", "quotes", "views"):
                old[k] = max(old.get(k, 0), t.get(k, 0))
        else:
            seen[t["id"]] = {k: v for k, v in t.items() if k != "ref"}

    def run(q: str, qt: str) -> None:
        try:
            for t in src.search(q, qt, pages):
                add(t)
                if t.get("ref"):
                    add(t["ref"])
        except Exception as e:  # one bad query shouldn't sink the digest
            print(f"  search failed ({e}): {q[:80]}")

    for q in cfg.get("searches", []):
        run(f"{q} {window}", "Top")
    accounts = [a.lstrip("@") for a in cfg.get("accounts", []) if a.strip()]
    for i in range(0, len(accounts), 15):
        who = " OR ".join(f"from:{h}" for h in accounts[i:i + 15])
        run(f"({who}) -filter:replies {window}", "Latest")
    print(f"fetched {len(seen)} tweets")
    return list(seen.values())


# ---------------------------------------------------------------------------
# Filter and rank
# ---------------------------------------------------------------------------
def org_of(author: dict, orgs: list[dict]) -> str | None:
    """The prominent company an author's bio ties them to, if any."""
    bio = author.get("bio") or ""
    low = bio.lower()
    handle = (author.get("handle") or "").lower()
    for o in orgs:
        hs = [h.lower() for h in o.get("handles", [])]
        if handle in hs:
            return o["name"]
        if any(re.search(rf"@{re.escape(h)}\b", low) for h in hs):
            return o["name"]
        if o.get("match_name") and re.search(rf"\b{re.escape(o['name'].lower())}\b", low):
            return o["name"]
    return None


def eligible(t: dict, cfg: dict, team: set[str]) -> bool:
    a = t["author"]
    if a["handle"].lower() in team or t["likes"] < int(cfg.get("min_likes", 25)):
        return False
    org = org_of(a, cfg.get("prominent_orgs", []))
    t["org"] = org
    need = int(cfg.get("min_followers_if_affiliated", 200)) if org else int(cfg.get("min_followers", 1500))
    return a["followers"] >= need


def score(t: dict, now: datetime) -> float:
    """Engagement per hour since posting, softened, with a small nudge for bigger accounts."""
    posted = _posted(t) or now - timedelta(hours=12)
    hours = max(1.0, (now - posted).total_seconds() / 3600)
    eng = t["likes"] + 2 * t["retweets"] + 3 * t["quotes"] + 0.5 * t["replies"]
    return math.log1p(eng / hours ** 0.7) + 0.15 * math.log10(max(t["author"]["followers"], 10))


def candidates(tweets: list[dict], cfg: dict, now: datetime) -> list[dict]:
    team = {h.lower() for h in cfg.get("exclude_handles", [])} | \
           {p["x"].lower() for p in cfg.get("people", []) if p.get("x")}
    ok = [t for t in tweets if eligible(t, cfg, team)]
    for t in ok:
        t["score"] = score(t, now)
    ok.sort(key=lambda t: t["score"], reverse=True)
    out, per_author = [], {}
    for t in ok:
        h = t["author"]["handle"].lower()
        if per_author.get(h, 0) >= 2:
            continue
        per_author[h] = per_author.get(h, 0) + 1
        out.append(t)
        if len(out) >= int(cfg.get("candidates_for_model", 120)):
            break
    print(f"{len(ok)} eligible, {len(out)} sent to the model")
    return out


# ---------------------------------------------------------------------------
# Match tweets and topics to people
# ---------------------------------------------------------------------------
SYSTEM = """You pick what each person on a startup's team should engage with on X today.

You get the company, guidance, each person's profile, and today's candidate tweets: originals from the
last day by prominent-ish accounts, ranked by how fast they're gaining engagement (rank 1 is hottest).

For each person, choose:
- up to {picks} tweets (by id) that are dominating the conversation today AND where this person can add
  something credible given their role and expertise. A reply or quote from them should look natural.
- {topics} topic(s): a conversation many of today's tweets are part of that fits them, named in 2-6
  words, with the ids of 1-3 tweets that show it.

Rules:
- Fit beats virality, but prefer higher-ranked tweets when fit is similar.
- Each "why" is at most 12 words: why them, or the angle to take. No filler, no hashtags.
- Avoid giving everyone the same tweet; share one only if it's clearly the story of the day for both.
- Follow the guidance strictly. If nothing fits someone well, give them fewer picks.

Return only JSON:
{{"headline": "<at most 14 words: what's dominating today>",
  "people": [{{"name": "<name>", "picks": [{{"id": "<tweet id>", "why": "<=12 words"}}],
              "topics": [{{"topic": "<2-6 words>", "ids": ["<tweet id>"], "why": "<=12 words"}}]}}]}}"""


def _payload(cfg: dict, cands: list[dict], now: datetime) -> str:
    def hours(t):
        p = _posted(t)
        return round((now - p).total_seconds() / 3600, 1) if p else None
    return json.dumps({
        "company": cfg.get("company"),
        "guidance": cfg.get("guidance", []),
        "people": [{k: p.get(k) for k in ("name", "role", "profile", "engage_on")} for p in cfg["people"]],
        "tweets": [{"id": t["id"], "rank": i + 1, "author": "@" + t["author"]["handle"],
                    "author_name": t["author"]["name"], "followers": t["author"]["followers"],
                    "bio": (t["author"].get("bio") or "")[:160], "org": t.get("org"),
                    "text": re.sub(r"\s+", " ", t["text"])[:500], "likes": t["likes"], "reposts": t["retweets"],
                    "quotes": t["quotes"], "replies": t["replies"], "hours_old": hours(t)}
                   for i, t in enumerate(cands)],
    }, ensure_ascii=False)


def ask_model(cfg: dict, cands: list[dict], now: datetime) -> dict:
    system = SYSTEM.format(picks=int(cfg.get("picks_per_person", 3)), topics=int(cfg.get("topics_per_person", 1)))
    user = _payload(cfg, cands, now)
    for attempt in range(2):
        r = requests.post(ENDPOINT, timeout=180, headers={
            "Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY'].strip()}", "Content-Type": "application/json"},
            json={"model": cfg.get("model", "anthropic/claude-sonnet-5"), "temperature": 0.3, "max_tokens": 12000,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        if r.status_code != 200:
            print(f"  model {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        body = r.json()
        print(f"  model usage: {body.get('usage')}")
        text = re.sub(r"^```(?:json)?|```$", "", (body["choices"][0]["message"]["content"] or "").strip(), flags=re.M).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  model returned bad JSON ({e}): {text[:200]!r}")
    raise RuntimeError("the model didn't return valid JSON twice in a row")


def clean(out: dict, cfg: dict, cands: list[dict]) -> dict:
    """Keep only real tweet ids, known people, the configured counts, and short reasons."""
    by_id = {t["id"]: t for t in cands}
    want = {p["name"].lower(): p["name"] for p in cfg["people"]}
    short = lambda s, n: " ".join(str(s or "").split()[:n])
    people = {}
    for p in out.get("people", []):
        name = want.get(str(p.get("name", "")).lower())
        if not name:
            continue
        picks = [{"id": str(x.get("id")), "why": short(x.get("why"), 14)} for x in p.get("picks", [])
                 if str(x.get("id")) in by_id][: int(cfg.get("picks_per_person", 3))]
        topics = []
        for x in p.get("topics", [])[: int(cfg.get("topics_per_person", 1))]:
            ids = [str(i) for i in x.get("ids", []) if str(i) in by_id]
            if x.get("topic"):
                topics.append({"topic": short(x["topic"], 7), "ids": ids[:3], "why": short(x.get("why"), 14)})
        people[name] = {"picks": picks, "topics": topics}
    return {"headline": short(out.get("headline"), 16),
            "people": [{"name": p["name"], **people.get(p["name"], {"picks": [], "topics": []})} for p in cfg["people"]]}


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _snippet(t: dict, n: int = 80) -> str:
    s = re.sub(r"https?://\S+", "", t["text"]).replace("\n", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def message(picks: dict, cands: list[dict], cfg: dict, now: datetime, tz: str) -> dict:
    by_id = {t["id"]: t for t in cands}
    local = now.astimezone(ZoneInfo(tz))
    day = f"{local:%a %b} {local.day}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": f"What to engage with · {day}"}}]
    if picks.get("headline"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(picks["headline"])}]})
    labels = {p["name"]: p.get("label") or p.get("role", "") for p in cfg["people"]}
    for p in picks["people"]:
        lines = []
        for x in p["picks"]:
            t = by_id[x["id"]]
            lines.append(f"• <{t['url']}|@{_esc(t['author']['handle'])}> {_esc(_snippet(t))}  _{_esc(x['why'])}_")
        for x in p["topics"]:
            link = f" (<{by_id[x['ids'][0]]['url']}|example>)" if x["ids"] else ""
            lines.append(f"◦ *Topic:* {_esc(x['topic'])}{link}  _{_esc(x['why'])}_")
        if not lines:
            lines.append("_Nothing strong for you today._")
        label = labels.get(p["name"], "")
        text = f"*{_esc(p['name'])}*" + (f" · {_esc(label)}" if label else "") + "\n" + "\n".join(lines)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:2990]}})
    fallback = f"What to engage with · {day}" + (f": {picks['headline']}" if picks.get("headline") else "")
    return {"text": fallback, "blocks": blocks[:50], "unfurl_links": False, "unfurl_media": False}


SLACK_ERRORS = {
    "invalid_auth": "The Slack bot token was rejected. Copy the Bot User OAuth Token (xoxb-…) again.",
    "not_authed": "No Slack bot token was sent. Set ENGAGE_SLACK_BOT_TOKEN.",
    "token_revoked": "The Slack app was uninstalled or its token revoked. Reinstall it and copy the new token.",
    "missing_scope": "The Slack app is missing a scope. It needs chat:write, im:write, and users:read.email; reinstall after adding them.",
    "users_not_found": "No Slack user has that email. Check ENGAGE_DM_EMAIL.",
    "channel_not_found": "Slack can't find that channel. Use the channel ID (channel details → bottom, starts with C) "
                         "and invite the bot to it.",
    "not_in_channel": "The bot isn't in that channel. In Slack, open the channel and type /invite @Engage digest.",
    "is_archived": "That channel is archived.",
}


class SlackError(RuntimeError):
    def __init__(self, method: str, code: str, explained: str):
        super().__init__(f"slack {method}: {explained}")
        self.code = code


def _slack(method: str, token: str, **payload) -> dict:
    get = method == "users.lookupByEmail"
    r = requests.request("GET" if get else "POST", SLACK_API + method, timeout=30,
                         headers={"Authorization": f"Bearer {token}"},
                         params=payload if get else None, json=None if get else payload)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("ok"):
        err = body.get("error") or f"HTTP {r.status_code}"
        raise SlackError(method, err, SLACK_ERRORS.get(err, err))
    return body


def send(msg: dict, dest: str | None = None) -> None:
    """Post to `dest` or ENGAGE_SLACK_CHANNEL: a channel ID (C…), or an email / user ID (U…) to DM.
    Neither set: DM ENGAGE_DM_USER or ENGAGE_DM_EMAIL."""
    token = os.environ["SLACK_BOT_TOKEN"].strip()
    dest = (dest or os.getenv("ENGAGE_SLACK_CHANNEL", "")).strip().lstrip("#") \
        or os.getenv("ENGAGE_DM_USER", "").strip() or os.environ["ENGAGE_DM_EMAIL"].strip()
    if "@" in dest:
        dest = _slack("users.lookupByEmail", token, email=dest)["user"]["id"]
    if dest[:1] in ("U", "W"):
        dest = _slack("conversations.open", token, users=dest)["channel"]["id"]
    _slack("chat.postMessage", token, channel=dest, **msg)
    print("slack: sent")


# ---------------------------------------------------------------------------
def one_person(cfg: dict, name: str, picks: int) -> dict:
    """The config narrowed to one person, asking for `picks` tweets and a couple of topics."""
    p = [x for x in cfg["people"] if name.lower() in (x["name"].lower(), (x.get("x") or "").lower())]
    if not p:
        raise SystemExit(f"no one called {name} in engage.json")
    return {**cfg, "people": p, "picks_per_person": picks, "topics_per_person": 2}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="DM the result on Slack")
    ap.add_argument("--config", help="path to the config (default engage.json)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    tz = os.getenv("ENGAGE_TIMEZONE", "America/Los_Angeles")
    now = datetime.now(timezone.utc)
    only = os.getenv("ENGAGE_ONLY", "").strip()  # a one-off: more picks for one person
    if only:
        cfg["window_hours"] = float(os.getenv("ENGAGE_HOURS") or cfg.get("window_hours", 24))
        cfg["candidates_for_model"] = max(int(cfg.get("candidates_for_model", 120)), 200)
    cands = candidates(fetch(cfg, now), cfg, now)  # the whole team is still excluded as authors
    if not cands:
        raise SystemExit("no candidate tweets today (every search failed, or nothing passed the filters)")
    if only:
        cfg = one_person(cfg, only, int(os.getenv("ENGAGE_PICKS") or 10))
    picks = clean(ask_model(cfg, cands, now), cfg, cands)
    msg = message(picks, cands, cfg, now, tz)
    if only:
        msg["blocks"][0]["text"]["text"] = msg["blocks"][0]["text"]["text"].replace(
            "What to engage with", f"More for {cfg['people'][0]['name']}", 1)
        msg["text"] = msg["text"].replace("What to engage with", f"More for {cfg['people'][0]['name']}", 1)
    out = Path(os.getenv("ENGAGE_OUT") or ROOT / "public" / "engage")
    out.mkdir(parents=True, exist_ok=True)
    (out / "last.json").write_text(json.dumps({"at": now.isoformat(), "picks": picks, "message": msg,
                                               "candidates": cands}, ensure_ascii=False, indent=1))
    if args.send:
        send(msg)
    else:
        print(json.dumps(msg, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
