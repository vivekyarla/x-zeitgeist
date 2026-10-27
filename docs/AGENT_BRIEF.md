# Agent brief: Rox's market-intel and X-zeitgeist systems

Written 2026-10-27 for an agent picking up this work. It covers two separate systems that Vivek (vivek@rox.com, Rox marketing) runs. They are complementary: **comp-intel** tracks what competitors and the market *ship and announce* (news, press, product pages), and **X zeitgeist** tracks what people on X are *talking about* (tweets). One feeds into the other at a few points (§3).

Where a statement below is a measurement, it says so. Much of the "performance" is known only from the code's design and a few observed incidents, not from metrics; §4 and §5 say exactly what is and isn't measured.

---

## 1. comp-intel (the "Market Intel" site)

### What it is
- A static site on GitHub Pages: <https://vivekyarla.github.io/comp-intel/>. Repo: `vivekyarla/comp-intel`, branch `main`. No server, no build step.
- A daily executive brief for the Rox team on the **V1 watchlist**: Salesforce (Agentforce), Gong, Clay, Nooks, Outreach, Salesloft, Monaco. Rox itself is deliberately never tracked.
- Pages:
  - **Today** (`index.html` + `today.js`): renders `data/today.json`.
  - **Competitor pages** (`competitors/<slug>.html`, with `competitors/<slug>.json` for some): positioning plus a dated "recent" log, and a "Recent posts" block from `data/feed_recent.json`.
  - `dashboard/` holds a mirrored copy of the data (`dashboard/data/today.json`).

### How it's produced
- An AI agent runs a **morning routine** each weekday and commits the result. Vivek calls it the Grok bot; the commits are authored by **"Cursor Agent"** (`cursoragent@cursor.com`), so it runs as a Cursor background/scheduled agent. Its prompt and tooling are **not in the repo**; only its outputs are. Ask Vivek where the routine is configured before changing how it researches.
- **Timing (from commit history):** commits land around 15:45–16:15 UTC, i.e. **~8:45–9:15 AM PT**, Monday–Friday (11 dated briefs, Oct 12–27, no weekend runs). The brief's own `as_of` says "morning ~9:15 AM PT".
- **Each run:**
  1. Overwrites `data/today.json` (the structured brief) and `dashboard/data/today.json`.
  2. Writes `briefs/YYYY-MM-DD.md`, a longer Markdown version with the run's reasoning: what was deduped against yesterday, what was dropped and why ("outside window", "no primary source found"), and a `meaningful_signal` verdict.
  3. Updates competitor pages/JSON when there's news.
  4. Since Oct 21 it also writes a **posts feed**: `today.json.feed[]` (last 1–2 days) and `data/feed_recent.json` (rolling 14 days), produced by a `scripts/pull_feed.py` that lives in the agent's environment, not the repo.

### `today.json` shape
Documented in `README-ui.md`. Every section is optional and the UI adapts:
- `as_of`, `filter_note` (e.g. "V1 set (7) + market briefing + AI news; Today window = Mon Oct 26 – Tue Oct 27 PT").
- `meaningful_signal`: whether anything real happened.
- `market_briefing`: `{lede, bullets:[{text, cites[]}]}`, category trends rather than a competitor recap.
- `biggest_ai_news[]`, `what_changed[]`, `competitive_moves[]`, `watch[]`: each with `cites:[{label, url}]`.
- `market_direction[]` (`kind: up|flat|down|emerging`), `language_pulse[]` (`{term, gloss}`), `interpretation`, `quiet_note`.
- `feed[]`: `{company, author, kind: company|founder, source: X|LinkedIn, ts, category, summary, url, engagement}`.

### Sourcing rules the routine follows (visible in the briefs)
- **Window:** only the last 1–2 PT calendar days; older items go to competitor pages.
- **Dedup:** against the previous day's brief.
- **Primary sources preferred.** Items without one are dropped and listed as such, e.g. "an AI-written weekly digest claiming an Outreach MCP … (no primary source found)".
- **Cited:** every item carries at least one source link.

### Source mix (measured on the Oct 27 brief)
- **25 cited URLs.** salesforce.com 5, globenewswire.com 4, linkedin.com 4, x.com 4, clay.com 3, openai.com 2, plus superhuman.ai, nvidianews.nvidia.com and rappler.com 1 each. So it's mostly first-party newsrooms and press wires, with some social.
- **Oct 15 for comparison:** 17 URLs, led by a single survey report (growthunhinged.com 5).
- **Posts feed** (`feed_recent.json` on Oct 27): **12 posts in 14 days**.
  - By company: Salesforce 4, Monaco 4, Nooks 2, Clay 1, Gong 1. None from Outreach or Salesloft.
  - By source: 8 from X (5 company, 3 founder), 4 from LinkedIn (2 company, 2 founder).
  - Engagement is often tiny (e.g. 2 likes / 204 views), so the feed is **coverage of official channels, not a popularity filter**.

### Delivery
- **The site itself.**
- **"Get it daily" button** (`daily.js`, merged in PR #1): builds a setup message for **Instinct** (SMS) or **Muse** (WhatsApp / muse.ai). The message creates a recurring task that reads `data/today.json` and sends the lede plus 2–3 cited items. Default 9:30 AM weekdays. No server is involved.
- **Slack and the X-zeitgeist app** (see §3).

### Known weak spots
- The research step is a black box from the repo's point of view: there's no log of what was searched, so **recall is unmeasured** (we can't say what it missed).
- Coverage is 7 named competitors. Adjacent players (HubSpot, Apollo, Attio, Unify, Common Room, Demandbase …) appear only when they make "market" news.
- No weekend runs. Monday's brief covers Sat–Mon only through the 1–2-day window rule, so Friday-evening news can be thin.
- `as_of` is a free-text string, not a timestamp. Consumers check freshness loosely; the Instinct message asks the agent to compare `as_of` with today's date.

---

## 2. X zeitgeist (repo `vivekyarla/x-zeitgeist`)

### What it is
A multi-user web app (FastAPI + SQLite + an in-process scheduler) deployed on **Railway** at `https://x-today.up.railway.app`, from branch `claude/relaxed-archimedes-pqcr5e`. That branch is what Railway runs; `main` is stale and still has an old GitHub Pages workflow. The full engineering handoff is `docs/HANDOFF.md`; this brief summarizes it.

It has five products:

| Product | Who it's for | Schedule (Pacific) | Output |
|---|---|---|---|
| **Personal pages** (`/app/`) | Any signed-in @rox.com user, on **their own** API keys | Every 3 h | A weekly page "This week on the timeline": a thesis, 3–5 themes, tweet cards, "New today". Plus a daily recap to Slack / Instinct / Muse |
| **GTM report** (`/gtm/`) | The marketing team; admins (`ADMIN_EMAILS`) edit it | Weekdays ~8:55 am and 8 pm | Same page format, built from the `gtm_marketing` archetype on the owner's keys |
| **Slack channel digest** | Marketing channel(s) | Weekdays 9:30 | "X zeitgeist" post (GTM report summary). With `INTEL_SLACK_CHANNEL` set, a separate "Competitive intel" post from comp-intel goes to a second channel |
| **Engage bot** | Vivek, for six execs/engineers | Daily 11:00; follow recs Mondays 11:15 | "What to engage with": 2–3 tweets + 1 topic each for Ishan (CEO), Shriram (CTO), Gopal (research), Dan Chen (ex-founder/ex-Sequoia), Taeuk and Brian (engineers). Weekly: 5 individual accounts each to follow. Sent as a DM, or to `ENGAGE_SLACK_CHANNEL` |
| **Example page** (`/preview/`) | Public / signed-out visitors | 08:30 and 20:00 daily | The `sf_tech` preset on the owner's keys |

There's also a **Slack bot** (`POST /slack/events`) that answers @mentions and DMs with how-to help about the reports.

### Where tweets come from
Everything goes through **twitterapi.io** advanced search (`pipeline/sources.py`), normalized so another provider can be swapped in with one class. Each run of a page (`pipeline/run.py`) collects candidates from:

1. **The panel** (`pipeline/panel.py`), the primary source.
   - **Who's in it:** accounts that at least `min_overlap` (2) of a group's *anchor* accounts follow, up to 300, rebuilt weekly.
   - **Why:** stories are found by **who in the scene is posting, quoting and retweeting**, not by guessing keywords.
   - **Reach:** each story's reach is the number of distinct panel accounts on it, and it boosts ranking.
   - **GTM report's anchor groups:**
     - GTM companies (30%): clay, MonacoGTM, gong_io, attio, unifygtm, CommonRoomHQ, useapolloio, heyreach_io, InstantlyAI, outreach_io.
     - GTM operators (20%): jasonlk, samdblond, RobHoffman_.
     - Marketers (35%): codyschneider, antinertia, fivosaresti, kristaletz, lennysan, harrydry.
     - Brands (15%): figma, linear, vercel, NotionHQ, stripe.
   - These are the preset's values; the live profile may have been edited by admins. Check `/gtm/` Settings → Sources.
2. **Keyword searches**: 12 for GTM, each with a `min_faves` floor (150–300).
3. **Watchlist accounts**: low floor, 20 likes.
4. **Tracked company accounts**: every original post. Each is judged against that account's own median likes (a baseline refreshed daily).

Quotes and retweets are traced back to the original, so a story is credited once.

### How tweets are chosen
1. **Judge.** Each new tweet goes once to **Jev**, a TypeSafe "System One" classifier reached through the Vercel AI Gateway (shown as "Jev 2.5" on the page). It returns:
   - topic
   - relevance (0–1, against the profile's "care about" / "skip" lists)
   - **signal** (0–4, judged *within the tweet's own scene*)
   - bait probability
2. **Filter** (`judge.why_not`).
   - **Everyone:** excluded topic, bait > 0.5, or relevance < 0.6 fails.
   - **Non-company accounts:** need ≥ `min_likes` (**250** for GTM) and signal ≥ 1.7.
   - **Tracked companies:** need ≥ 3× their usual likes, ≥ 75 likes and signal ≥ 1.4.
   - **Thin-week rule** (added Oct 20): if fewer than 20 pass, the best-ranked tweets that only missed the like floor are let in, down to 25% of it (never under 20 likes).
3. **Rank.** Jev's scores × engagement (raw likes blended with likes-per-follower) × panel reach, minus a penalty for big-lab mainstream news.
4. **Select** the top 60 for the writer:
   - one per story
   - ≤ 2 per account (with groups such as all OpenAI accounts counting as one)
   - each topic filled toward its target share (GTM: outbound 12.5%, GTM engineering 10%, sales tools 10%, launch videos 10%, campaigns 10%, content/social 10% …)
5. **Write.** Claude (`anthropic/claude-sonnet-5` via the AI Gateway) writes the thesis and themes. It sees the previous thesis, so the read evolves through the week.
6. **Week boundary.** A week starts Monday 00:00 PT. Since Oct 20 a new week carries over the previous Sat–Sun judged tweets, so Monday isn't empty.

### The engage bot's own pipeline (`pipeline/engage.py`, config `engage.json`)
It's separate from the pages:
1. **Fetch:** 7 keyword searches (`min_faves` 75–300) and 37 accounts, over the **last 24 h**.
2. **Filter:** originals only, never from the team, ≥ 25 likes. The author needs ≥ 1,500 followers, or ≥ 200 if their bio ties them to one of ~33 prominent orgs (OpenAI, Anthropic, Sequoia, a16z, YC, Salesforce, Clay, Gong …).
3. **Rank:** by engagement velocity, roughly (likes + 2·RTs + 3·quotes + 0.5·replies) ÷ hours^0.7, with a small bump for follower count. Max 2 per author; the top 120 go to the model.
4. **Match:** one Claude call matches tweets and a topic to each person's profile, with a ≤ 12-word "why" for each. The guidance bans politics, tragedies, dunking on competitors, and Rox's own posts.

**One-off option:** `POST /api/engage/run {"person": "Shriram", "picks": 12, "hours": 48}` sends Vivek a longer list for one person without touching the daily send.

The weekly follow recommendations (`pipeline/follow.py`) look at 7 days of posts, need ≥ 5,000 followers (≥ 1,000 if affiliated), ≥ 2 posts and ≥ 50 likes. They suggest individuals only, never company accounts, and never repeat a suggestion within 8 weeks.

---

## 3. How the two connect

- **Slack:** the channel digest (`pipeline/channel.py`) reads comp-intel's `data/today.json`. `brief_text()` understands its sections, and market items may only link to URLs the JSON actually cites.
  - With `INTEL_SLACK_CHANNEL` set: two posts.
    - **"X zeitgeist"** goes to `CHANNEL_SLACK_CHANNEL`.
    - **"Competitive intel"** goes to `INTEL_SLACK_CHANNEL`, optionally from a second bot (`INTEL_SLACK_BOT_TOKEN`).
  - Without it: one combined post.
  - `posted.json` stops a retry from double-posting.
- **Instinct / Muse:** the app serves the brief as Markdown.
  - `/intel/recap.md` serves it on its own (cached 15 min).
  - Any `recap.md?intel=1` appends it to an X recap.
  - The setup cards offer "X recap", "X recap + competitive intel" (default on `/gtm/`) or "Competitive intel only".
- **Push alternative:** `POST /api/intel` with `INTEL_PUSH_TOKEN` lets a bot push a report instead of the app fetching it. It's unused today; the fetch works because the JSON is public.

---

## 4. Performance: what we actually know

### Measured or observed
- **comp-intel reliability:** 11 of 11 weekday briefs from Oct 12 to Oct 27 were committed on time (8:45–9:15 PT). Source counts per brief: 17–25 cited URLs, mostly primary.
- **comp-intel posts feed:** 12 posts in 14 days across 5 of 7 competitors. It's sparse, so either those channels post rarely or the scraper misses posts; this hasn't been checked.
- **GTM report, early in the week:** it was **thin**. Oct 20 (a Tuesday) showed 1 tweet, then 2.
  - **Diagnosis (from the code; live stats weren't visible to us):** the 250-like floor for non-company accounts is high for GTM/marketing content, plus the Monday reset, plus only two refreshes a day.
  - **Fixes shipped:** the thin-week relaxation and the weekend carry-over. **Whether they fixed it hasn't been confirmed.** Check `/gtm/` (footer: "Scanned N tweets this week; M passed", plus the "Filtered out" near-misses list) or `/gtm/data.json` → `latest.stats`.
- **Tracked-company bar:** an earlier lower bar let a 46-like Clay post through. The bar was raised to 3× baseline and ≥ 75 likes.
- **Engage bot:** in production daily; Vivek confirmed the DMs arrive. Pick quality hasn't been formally evaluated. Vivek asked for "more options for Shriram", which suggests 2–3 picks a day isn't always enough for the more technical profiles (Shriram's topics are infra / databases / distributed systems, where fewer posts cross the like floors).
- **Incidents:**
  - A preview twitterapi.io key returned 401. Key fingerprints are now logged.
  - A run lease was orphaned by a redeploy. Leases are now short and renewable, and capped on startup.
  - Railway rejected the Dockerfile's `VOLUME` line, which was removed.

### Not measured (open questions worth instrumenting)
- **Recall of X stories.** The app has a hook for it: Settings → Sources → "stories we should have caught" (`known_stories`: a label, a regex and a week). Each run reports where each expected story landed (kept, filtered and why, or never fetched). Nobody has populated it yet, and it's the best way to measure recall.
- **Precision** (would a human keep what's shown?) for both systems. No ratings are collected.
- **twitterapi.io coverage:** it's search-based, so a story that's only in images or replies, or below each query's `min_faves`, is never fetched. The panel's "Top" search returns a limited number of pages per batch of 18 handles (2 pages), so a busy panel member's mid-engagement posts can be missed.
- **Cost:** estimated, not metered. Per user on their own keys, about $20–35/month (twitterapi.io $10–15, AI Gateway $10–20). The owner pays for the GTM report, the example page, engage and the channel digest. Railway costs about $5–8/month.

---

## 5. How to inspect each system

| Question | Where |
|---|---|
| Today's brief, raw | `https://vivekyarla.github.io/comp-intel/data/today.json`; reasoning in `briefs/<date>.md` |
| GTM report contents and stats | `https://x-today.up.railway.app/gtm/` (footer + Settings → Filtered out), `/gtm/data.json` |
| Channel digest state | `GET /api/channel/status` (admin): `enabled`, `missing`, both channels, `last_error`, `last_tried_at` |
| Engage state | `GET /api/engage/status` (signed in as `ENGAGE_DM_EMAIL`): destination, `last_error`, `last_oneoff` |
| Run logs | Railway → service → Deploy Logs: `user <id>: fetched N tweets`, `search failed (…)`, `engage: …`, `channel: …` |
| Re-run now | `POST /api/run` (page), `POST /api/channel/run` (admin), `POST /api/engage/run` (owner), `POST /api/engage/follow/run` |

Network note for cloud agents: some sandboxes can't reach `*.up.railway.app` or `api.twitterapi.io`. If yours can't, ask Vivek to open the status URLs and paste the result.

---

## 6. Constraints Vivek has set (don't violate)
- Other users never run on his API keys. Each person's page uses their own keys; only the shared system profiles (GTM report, example page, engage, digest) use the owner's.
- No team edits of personal pages: profiles are per user and portable (export/import).
- Google sign-in, if enabled, may request only `openid email profile`. It's built but not configured, because there's no GCP project permission. Sign-in today is password-based, limited to @rox.com (`ALLOWED_EMAIL_DOMAINS`).
- Follow suggestions are individuals only, never company accounts.
- The channel post is titled "X zeitgeist", not "GTM zeitgeist". Reports refresh once or twice a day, not every 3 h (the shared ones).
- Never track Rox as a competitor in comp-intel.
