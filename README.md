# This week on the timeline

A page that shows you what your corner of Twitter is talking about this week, with a 1–2 sentence thesis that updates every three hours.

Everyone gets their own: sign up, pick a starting point (AI engineering, startups and SF, GTM and sales, marketing, AI business, or the original AI-startup-marketing mix), tune it, and paste in your own API keys. Your page, your daily recap, and your costs are yours alone. Nobody edits anyone else's settings.

## How it works

Every 3 hours the server runs `python -m pipeline.run` once for each person, in its own process, with only that person's settings and keys:

1. **Fetch** (`pipeline/sources.py`): reads the timeline panel (accounts your anchor accounts follow), plus your searches, watchlist, and tracked companies, from twitterapi.io, limited to tweets since Monday 00:00 in your time zone.
2. **Judge** (`pipeline/judge.py`): sends each *new* tweet to Jev once (through Vercel AI Gateway, or TypeSafe directly if you add a TypeSafe key), asking in one call: is it on-topic for you, which topic, how much signal (0–4 rubric), is it bait, and would you care. Tweets that clear your thresholds are ranked, with Jev's scores weighted most and engagement breaking ties.
3. **Synthesize** (`pipeline/synthesize.py`): sends the top 60 to Claude through Vercel AI Gateway, which writes the thesis and groups tweets into 3–5 themes. It sees its previous thesis, so the read evolves instead of starting over.
4. **Publish** (`pipeline/build.py`): renders your page (plus `data.json`, and `recap.json`/`recap.md`, the daily recap for Slack and agents). Your weekly state is kept in your data folder; on Monday the old week moves to `archive/`.

The web app (`server/`) handles accounts, settings, keys, and scheduling. The pipeline doesn't know about any of that: the server points it at one person's files with environment variables (see [Running the pipeline by hand](#running-the-pipeline-by-hand)).

## Run it locally

```bash
pip install -r requirements.txt
TIMELINE_DEMO=1 uvicorn server.app:app --reload
```

Open http://localhost:8000, sign up, and go through setup. With `TIMELINE_DEMO=1` nothing calls an API: keys aren't checked, and your page is built from sample data (`pipeline/demo_state.json`) with your settings. Drop `TIMELINE_DEMO=1` and add real keys to do real runs.

Everything lands in `./var` (set `DATA_DIR` to move it).

## Deploy

It's one small container: a web server with a background scheduler, one SQLite file, and one data folder. Any host that runs a Docker image with a persistent disk works: Fly.io, Railway, Render, or your own box.

```bash
cp .env.example .env      # set BASE_URL and SECRET_KEY
docker compose up -d --build
```

### Railway (about $5–8 a month)

`railway.json` sets the build (this repo's `Dockerfile`), one replica, and the `/healthz` check. The server listens on Railway's `$PORT` and uses the domain Railway assigns as `BASE_URL`, so you only set `BASE_URL` for a custom domain.

1. New project → **Deploy from GitHub repo** → this repo, and pick the branch to deploy.
2. On the service: **Settings → Networking → Generate Domain** (e.g. `timeline-production.up.railway.app`).
3. Right-click the service → **Attach volume**, mount path `/data`.
4. **Variables** → Raw editor, paste `SECRET_KEY`, `ENCRYPTION_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `ALLOWED_EMAIL_DOMAINS`, `PREVIEW_TWITTERAPI_IO_KEY`, `PREVIEW_AI_GATEWAY_API_KEY`, and `FORWARDED_ALLOW_IPS=*`, then deploy.

It redeploys on every push to that branch. Keep it at one replica: the scheduler runs inside the server.

### Fly.io (about $4 a month)

`fly.toml` is ready: one always-on `shared-cpu-1x` machine with 512 MB (about $3.30/month) and a 1 GB volume (about $0.15/month). API costs are separate and land on each person's own keys, plus the example page on yours.

```bash
fly launch --copy-config --no-deploy          # pick an app name, then set BASE_URL in fly.toml to match
fly volumes create timeline_data --size 1
fly secrets set SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))') \
    GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... \
    PREVIEW_TWITTERAPI_IO_KEY=... PREVIEW_AI_GATEWAY_API_KEY=...
fly deploy
```

The machine has to stay up (`auto_stop_machines = "off"`), because the scheduler runs inside the server.

### Other platforms

1. Deploy from this repo's `Dockerfile`. The server runs as a non-root user; the entrypoint starts as root only to hand a root-owned volume to that user.
2. Attach a **persistent volume at `/data`**. Every account, key, and page lives there; without a volume, a redeploy wipes everyone.
3. Set the env vars:
   - `BASE_URL`: the public URL, e.g. `https://timeline.example.com`. Links in recaps and Slack come from it, and cookies are marked secure when it's https.
   - `SECRET_KEY`: a long random string (`python -c 'import secrets; print(secrets.token_urlsafe(48))'`). The server refuses to start without it unless `BASE_URL` is localhost or `TIMELINE_DEMO=1`. Changing it signs everyone out and makes saved keys unreadable, so people would re-enter them.
   - Optional: `ENCRYPTION_KEY` (a Fernet key, so key encryption doesn't depend on `SECRET_KEY`), `MAX_CONCURRENT_RUNS` (default 3), `REFRESH_HOURS` (default 3), `FORWARDED_ALLOW_IPS=*` behind the platform's proxy so the login rate limit sees real client IPs.
   - Sign-in and the example page: see the next two sections.
4. Health check: `GET /healthz`.

## Sign in with Google

1. In [Google Cloud Console](https://console.cloud.google.com/apis/credentials), create a project, then **OAuth consent screen**: pick **Internal** if everyone is in your Google Workspace (only your org can sign in, and there's no review), or External otherwise. Add only the `openid`, `email`, and `profile` scopes.
2. **Credentials → Create credentials → OAuth client ID**, type **Web application**, with the authorized redirect URI `<BASE_URL>/auth/google/callback`.
3. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `ALLOWED_EMAIL_DOMAINS` (e.g. `rox.com`; comma-separated; empty allows any Google account).

What it can see: those three scopes are Google's basic sign-in scopes, marked non-sensitive, so the app needs no Google verification. They share the account's name, email address, and profile picture, and nothing else: no Gmail, Drive, Calendar, or contacts. The app asks for no offline access (no refresh token), throws away the access token Google returns, and stores only the account's id and email. With `ALLOWED_EMAIL_DOMAINS` set, it also checks Google's `hd` claim, so only accounts managed by your Workspace get in, not personal Google accounts that happen to use a work address.

If your Workspace admin restricts third-party apps, they may need to mark the OAuth client as trusted (Admin console → Security → API controls → App access control). An Internal app in your own org is usually allowed by default.

Without Google set up, people sign up with email and password instead, and `ALLOWED_EMAIL_DOMAINS` limits sign-up to those domains (the address isn't verified, so this keeps outsiders out rather than proving ownership). Once Google is set up, email/password sign-up turns off (`PASSWORD_LOGIN=1` turns it back on). An existing password account is linked the first time someone signs in with Google using the same address.

## The example page

Signed-out visitors land on `/preview/`: a real page built from one preset (`PREVIEW_PRESET`, default `sf_tech`) on **your** keys (`PREVIEW_TWITTERAPI_IO_KEY` and `PREVIEW_AI_GATEWAY_API_KEY`), refreshed twice a day at `PREVIEW_REFRESH_TIMES` (08:30 and 20:00 Pacific). It has no settings and no sign-in, and a "Make your own" button. It costs about what one person's page does ($20–35/month). Leave either key empty to turn it off.

Run one instance. Several processes against the same volume won't double-run anyone (each run takes a lease in the database), but one is plenty: runs are mostly waiting on APIs.

## Your keys, your costs

Each person adds their own keys under **API keys** (during setup, or later from the page). They're encrypted at rest, only ever handed to that person's own runs, and never shown again after saving (the page just says whether each one is set).

- **twitterapi.io** (required): from twitterapi.io/dashboard.
- **Vercel AI Gateway** (required): vercel.com → AI Gateway → API Keys. Used for both Jev and Claude.
- **TypeSafe** (optional): from console.typesafe.ai, to send Jev calls to TypeSafe directly.
- **Slack webhook** (optional): for the daily post.

**Check and save** tests a key with one tiny call before you save it.

Rough cost per person, with the default 3-hour refresh:

- twitterapi.io: ~6 queries + watchlist + panel × 2 pages × 8 runs/day ≈ 2,500 tweets/day ≈ **$10–15/month**.
- Jev: only new tweets are judged, a few hundred tokens each ≈ **well under $1/month**.
- Claude via AI Gateway: 8 calls/day with ~15K tokens in ≈ **$10–20/month** on a Sonnet-class model.

More searches, a bigger watchlist, or more panel anchors cost more. Settings are capped (40 searches, 300 accounts per list, 100 panel anchors) so a typo can't run up a bill.

## Daily engagement DM

A Slack bot DMs one person (`ENGAGE_DM_EMAIL`) every day at `ENGAGE_TIME` (11:00 Pacific by default): for each teammate in `engage.json`, two or three tweets and a topic from today that they'd credibly engage with, each with a few words of why.

How it picks (`pipeline/engage.py`):
1. Fetches the last 24 hours from `engage.json`'s searches and accounts (research, agents and dev tools, data infrastructure, startups and VC, AI for sales, SF builders).
2. Keeps originals from prominent-ish authors: at least `min_followers` (1,500), or a bio tying them to a company in `prominent_orgs` (Ramp, Cognition, OpenAI, Anthropic, …; short names like Ramp only count as an @handle). Drops the team's own posts and anything under `min_likes`.
3. Ranks by engagement per hour today, not total likes, so it catches what's climbing now.
4. One Claude call (`model`) matches tweets and topics to each person's profile in `engage.json` and writes the reasons. The guidance there rules out politics and anything that would look bad for the company.

Setup:
1. Create the Slack app from this manifest (opens Slack with the scopes `chat:write`, `im:write`, `users:read`, `users:read.email`): see the link in `docs/HANDOFF.md`, or create an app with those bot scopes. Install it to the workspace (an admin may need to approve it), then copy the **Bot User OAuth Token** (`xoxb-…`).
2. Set `ENGAGE_SLACK_BOT_TOKEN` and `ENGAGE_DM_EMAIL`. It uses the example page's `PREVIEW_*` keys unless `ENGAGE_*` keys are set.
3. The first DM goes out right after the deploy, so you can check it; after that, daily. Signed in as `ENGAGE_DM_EMAIL`, `POST /api/engage/run` sends one now and `GET /api/engage/status` shows the last send or error. Locally: `python -m pipeline.engage` prints the message without sending.

**Who to follow (weekly).** Mondays at `FOLLOW_TIME` (11:15 Pacific), the same bot DMs 5 accounts per person to follow (`pipeline/follow.py`). It gathers authors who posted on these topics over the last 7 days and keeps the ones with 5,000+ followers (or 1,000+ with a bio tying them to a prominent company) who posted at least twice with real engagement. It skips accounts each person already follows (fetched from their X handle in `engage.json`) and anything suggested in the last 8 weeks, and one Claude call matches the rest to each profile. Tune it under `follow` in `engage.json`. `FOLLOW_ENABLED=0` turns it off, and `POST /api/engage/follow/run` sends one now.

Edit `engage.json` to change people, their profiles, sources, thresholds, or how many picks each gets. Cost: about 15 searches and one Claude call a day, a few dollars a month.

## Marketing channel digest

Every weekday at `CHANNEL_TIME` (9:30 Pacific by default), the bot posts to `CHANNEL_SLACK_CHANNEL`. That's a channel ID, or, to test it first, an email address, which sends it as a DM from the bot. Changing the destination posts once right away.
- **X zeitgeist:** the thesis in one line, 4–5 bullets on what's moving today with tweet links, and a link to the full report.
- **Market intel:** 2–3 market developments and 1–2 other trends from a market-intelligence bot's latest report, with a link to it.

**The archetype** is its own profile, created once from `CHANNEL_PRESET` (`gtm_marketing`: GTM & sales plus marketing & brand) and served at `/gtm/`. Anyone can read the page. Admins (`ADMIN_EMAILS`) who are signed in see Settings there, and those Settings edit the archetype: Feed, Sources, Filtered out. Edits stick across deploys, and the digest always summarizes the archetype's latest report. To save API costs it refreshes twice a day instead of every 3 hours: about 35 minutes before the post, and in the evening (`CHANNEL_EVENING_REFRESH`, 20:00 by default).

**Market intel** comes from whichever of these is available:
1. **Pushed:** the other bot POSTs its report to `/api/intel` with `Authorization: Bearer $INTEL_PUSH_TOKEN`. The body is JSON `{"markdown" | "text" | "html": ..., "url": "<its page>", "title": ...}`, or plain text. A copy pushed in the last 36 hours wins.
2. **Fetched:** otherwise `INTEL_URL` is fetched at post time. This works when the page's HTML (or a JSON or Markdown URL) contains the report text. A page that only renders in the browser can't be read this way; the post then links to it and says so. In that case, point `INTEL_URL` at the data file the page loads, and set `INTEL_PAGE_URL` to the page itself for the link.

**comp-intel (the Grok market-intel bot)** is a GitHub Pages site that renders in the browser from `data/today.json`, committed each morning around 9:00 PT. Set `INTEL_URL=https://vivekyarla.github.io/comp-intel/data/today.json` and `INTEL_PAGE_URL=https://vivekyarla.github.io/comp-intel/`. The digest recognizes that structure: market items come from `market_briefing.bullets` (each linked to its cited source) and trends from `market_direction`. No push or token is needed.

Signed in as an admin, `POST /api/channel/run` posts now and `GET /api/channel/status` shows the setup and the last post or error. The first post goes out as soon as the archetype's first report exists.

## Asking the bot

When someone @mentions the bot in a channel, or DMs it, it replies (in the thread) with how to do what they asked, mainly how to tweak the GTM report: sign in, open `/gtm/`, which Settings tab does what, and the common tweaks. It knows whether the person is an admin (`ADMIN_EMAILS`) and, if not, who to ask. Answers come from a fixed reference in `server/slackbot.py` (one short Claude call, with a plain fallback if that fails).

Setup, on the same Slack app:
1. **App Manifest:** add the bot scopes `app_mentions:read` and `im:history`. Under `features.app_home`, set `messages_tab_read_only_enabled` to false. Add `event_subscriptions` with `request_url` `https://<your-domain>/slack/events` and bot events `app_mention` and `message.im`. Save, then reinstall the app.
2. Copy **Basic Information → Signing Secret** into `SLACK_SIGNING_SECRET`.

## Presets

Setup starts from a preset in `pipeline/presets/`. Each is a complete settings file plus a `"preset"` block (id, name, tagline, description):

| id | For |
|---|---|
| `sf_tech` | SF tech culture: the most-talked-about tech, AI, and startup posts (launches, raises, viral builds, founder and VC culture, the SF scene), without deep engineering or GTM and marketing. The example page uses it. |
| `ai_startup_marketing` | The original page: tech and AI conversations, startup and SF moments, and GTM and marketing, for the marketing team at an AI startup. Used as the default until someone picks a preset. |
| `ai_engineer` | Model and tool launches, evals, agents, coding tools, open models, and papers people discuss. |
| `startup_culture` | Founders, VC, YC, big raises and ARR milestones, the SF scene, and culture debates. |
| `gtm_sales` | Signal-based selling, outbound, GTM engineering, AI SDRs, and sales tools (Clay, Monaco, Gong, …). |
| `marketing_brand` | Launch videos, campaigns, positioning, content, and brand. |
| `ai_business` | Labs, launches, revenue and valuations, M&A, and compute deals. |

To add one, drop a new JSON file in that folder; `tests/test_presets.py` checks it against the same validator the settings API uses. `settings.json` at the repo root is the default profile the pipeline uses when run on its own.

## Tuning

Everything is in **Settings** on your page: who it's for, the topic mix, how picky the filter is, sources (searches, watchlist, tracked companies, panel anchors), and your time zone. Saving rebuilds your page; any change to your interests or topics re-judges the week's saved tweets.

- **Too much noise?** Raise the min signal (e.g. 2.2) or min relevance, or raise `min_faves` in the searches.
- **Missing stuff?** Add searches or watchlist accounts, or lower the min signal.
- **Change the focus:** edit "Focus on" and "Skip", and the topics (unchecked topics never make the page).
- **Company accounts** (Clay, Monaco, Gong, …): add them as tracked companies. Their posts count when they beat that account's usual likes (3× its median and at least 25 likes by default), instead of needing to go broadly viral.
- **Topic mix:** each topic's share sets roughly how much of the read it gets; the Filtered out tab shows the best tweets that just missed, and why.
- **Pin Jev** once thresholds feel right: set `JEV_MODEL=jev-1.13.0` (or whatever is current) on the server, so `jev-latest` updates don't shift scores under you.

## Daily recap

Every update also publishes `recap.md` and `recap.json`: the thesis, up to five tweets new in the last 24 hours, the themes, and the link to your page.

**Slack.** Under **Daily recap**, create a Slack app (the page fills in the manifest for you), install it to a channel, paste the webhook URL, and click **Send test**. Then turn posting on and pick how often: once a day after an hour you choose (in your time zone), when the thesis changes, or every update. The daily post is checked every hour from saved state, without fetching anything, so a 9am post lands just after 9. If Slack says no (a revoked webhook, an archived channel), the reason and how to fix it shows on your page.

**Instinct and Muse.** Personal agents can't be pushed to, but they can read a URL on a daily schedule. Your recap has a private link, `https://<your server>/r/<token>/recap.md`, that works without signing in. The **Daily recap** tab gives you a one-tap setup message that asks your agent to read it every morning and send you the thesis, today's recap, and the link. Anyone with the link can read your recap, so treat it like a password; **Profile → Reset** (next to the recap link) replaces it.

## Moving your profile

- **One profile:** Profile → **Export profile** downloads `timeline-profile.json` (your settings, and your keys only if you ask for them). Import it on any other install, or into another account.
- **The whole install:** stop the server and copy `DATA_DIR`: `timeline.db` (accounts, settings, encrypted keys, run state) and `users/<id>/` (each person's page and weekly state). Keep the same `SECRET_KEY` (or `ENCRYPTION_KEY`) on the new host, or saved keys can't be decrypted.

## Security notes

- Passwords are hashed with scrypt. Sessions are signed, httponly, same-site cookies. Login and signup are rate-limited per IP.
- API keys are encrypted with Fernet (derived from `SECRET_KEY` with HKDF, or `ENCRYPTION_KEY`). The API never returns them (except in an export you ask to include keys in), and they're scrubbed from run logs.
- Each run gets a fresh environment: a few system variables plus that one person's paths and keys. Nothing from the server's environment or other accounts leaks in.
- Per-user files live under `users/<numeric id>/`; no path is ever built from user input.
- Settings are validated and size-capped before they're saved, since they drive paid API calls.
- Pages only show public tweets, but your page and settings are visible only to you once signed in. The recap link is the one thing that's readable without an account.

## Running the pipeline by hand

```bash
python -m pipeline.run --demo          # sample data, no keys → public/index.html
export TWITTERAPI_IO_KEY=... AI_GATEWAY_API_KEY=...
python -m pipeline.run                 # a real update of the default profile (settings.json, data/)
python -m pipeline.run --build-only    # re-render from saved state (after template edits)
python -m pipeline.run --dry-run       # full update, saves nothing, writes compare.json (before vs after)
```

Point it at any profile with `TIMELINE_SETTINGS` (settings file), `TIMELINE_DATA` (state folder), `TIMELINE_OUT` (where the page goes), `PAGE_URL`, and `TIMELINE_RECAP_URL`. That's exactly what the server does for each person.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The tests run in `TIMELINE_DEMO` mode and need no keys. CI runs them on every push (`.github/workflows/test.yml`).

Swapping to the official X API later: write a class with the same `search()` method in `sources.py` that returns the same normalized dicts, and use it in `run.fetch()`.
