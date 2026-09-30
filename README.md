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

Once Google is set up, email/password sign-up turns off (`PASSWORD_LOGIN=1` turns it back on). An existing password account is linked the first time someone signs in with Google using the same address.

## The example page

Signed-out visitors land on `/preview/`: a real page built from one preset (`PREVIEW_PRESET`, default `ai_startup_marketing`) on **your** keys (`PREVIEW_TWITTERAPI_IO_KEY` and `PREVIEW_AI_GATEWAY_API_KEY`), refreshed on the same schedule as everyone else. It has no settings and no sign-in, and a "Make your own" button. It costs about what one person's page does ($20–35/month). Leave either key empty to turn it off.

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

## Presets

Setup starts from a preset in `pipeline/presets/`. Each is a complete settings file plus a `"preset"` block (id, name, tagline, description):

| id | For |
|---|---|
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
