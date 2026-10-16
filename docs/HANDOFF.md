# Handoff: "This week on the timeline"

A multi-user web app. Each person signs in, picks a feed, and connects **their own** API keys. Every 3 hours the app reads what X (Twitter) is posting about their topics, filters it with an AI judge, has Claude write a 1–2 sentence thesis about the week grouped into themes, and serves it as their private page. It can also send them a daily recap through Slack, Instinct or Muse. A public example page shows a real feed before anyone signs up.

- **Repo:** `vivekyarla/x-zeitgeist`. All current work is on branch **`claude/relaxed-archimedes-pqcr5e`**, which isn't merged. `main` still has the old single-team GitHub Pages version and its scheduled workflow.
- **Owner:** vivek@rox.com. Users are the Rox team; sign-up is limited to `@rox.com`.
- **Hosting:** Railway, deployed from that branch. Every push redeploys automatically.

---

## 1. Current deployment state

| Item | State |
|---|---|
| Host | Railway, 1 replica, US West, Dockerfile build (`railway.json`), volume mounted at `/data` |
| Public URL | Railway-generated `*.up.railway.app` domain (the owner has it; it isn't in the repo). `BASE_URL` defaults to `https://$RAILWAY_PUBLIC_DOMAIN` |
| Port | App listens on `$PORT` (set to `8080` in Railway) |
| Sign-in | **Email + password for now**, limited to `@rox.com` by `ALLOWED_EMAIL_DOMAINS`. Google sign-in is built but **not configured**: the owner lacks permission to create a Google Cloud project in Rox's org and is waiting on a Workspace admin |
| Example page | Live at `/preview/`, running preset `sf_tech` on the owner's keys (`PREVIEW_*` env) |
| Railway variables | `SECRET_KEY`, `ENCRYPTION_KEY`, `ALLOWED_EMAIL_DOMAINS=rox.com`, `PREVIEW_TWITTERAPI_IO_KEY`, `PREVIEW_AI_GATEWAY_API_KEY`, `FORWARDED_ALLOW_IPS=*`, `PORT=8080` |
| To turn on Google later | Create an OAuth client (Web app, consent screen **Internal**, redirect `https://<domain>/auth/google/callback`), then add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in Railway. Password sign-up turns off automatically, and existing accounts link by email on first Google sign-in |

**Never change `ENCRYPTION_KEY`**: every saved API key becomes unreadable. Changing `SECRET_KEY` only signs everyone out, as long as `ENCRYPTION_KEY` is set.

---

## 2. Architecture

```
Browser ──► FastAPI (server/app.py) ── SQLite (DATA_DIR/timeline.db: users, encrypted keys, run state)
              │  serves templates (login / onboarding / building) and each user's built page
              │
              └─ Worker thread (server/worker.py), inside the same process
                   every ~20s: queue users whose next run is due; run up to MAX_CONCURRENT_RUNS
                   each run = subprocess `python -m pipeline.run` with ONLY that user's keys in env
                   hourly: `python -m pipeline.run --deliver` for users with Slack on
                        │
                        ▼
                 pipeline/ (writes DATA_DIR/users/<id>/{settings.json, data/, public/})
                   sources.py → twitterapi.io   judge.py → Jev (via Vercel AI Gateway)
                   synthesize.py → Claude (via Vercel AI Gateway)   build.py → index.html, recap.md/json
```

- **One container, one process.** The scheduler lives inside the web server, so the machine must stay up. Run exactly one instance; SQLite is local to the volume.
- **Per-user isolation:** each run is a separate subprocess. Its environment is built from an allowlist (PATH, HOME, LANG, PYTHONPATH, TZ, CA and proxy vars) plus that user's own keys, so a run can never see another user's keys or server secrets.
- **Deploys re-render every page:** pages are static files built by the pipeline, so on startup `Worker.rebuild_pages()` re-renders each built page from its saved `week.json` (`--build-only --slack skip`: no API keys, no Slack, under the user's lease). Template changes therefore show up on deploy, not at the next scheduled run.
- **Leases:** each run or delivery takes a lease on the user's `runs` row. Leases last 3 minutes and are renewed every 60 seconds while the subprocess runs. On startup, the server shortens any longer leftover lease, so a run cut off by a redeploy is picked up again within minutes.
- **Scheduling rules:**
  - A successful run schedules the next one `REFRESH_HOURS` (3h) later.
  - A failed or empty run retries in 30 minutes.
  - `POST /api/run` never refuses a request:
    - Already queued: the queued run picks up the latest settings.
    - Running: sets `rerun`, so another run starts right after.
    - Within 10 minutes of the last run: schedules a run for when the 10 minutes are up.

### Data on disk (the whole install is portable: this folder)
```
DATA_DIR/                      (/data on Railway)
  timeline.db                  SQLite (WAL)
  .secret_key                  only when running locally without SECRET_KEY
  users/<id>/settings.json     written from the DB before each run
  users/<id>/data/week.json    this week's tweets, Jev verdicts, thesis history, Slack status
  users/<id>/data/archive/     previous weeks
  users/<id>/data/panel.json   cached "timeline panel" of accounts
  users/<id>/data/last_run.log redacted output of the last run
  users/<id>/public/           index.html, data.json, recap.json, recap.md
```

### DB schema (server/db.py)
- `users(id, email UNIQUE NOCASE, pw_hash ("" for Google-only), created_at, onboarded, settings JSON, recap_token UNIQUE, google_sub UNIQUE)`
- `keys(user_id, name, value = Fernet ciphertext, updated_at)`. Names: `twitterapi_io`, `ai_gateway`, `typesafe`, `slack_webhook`
- `runs(user_id, state new|queued|running|ready|error, slack_mode, queued_at, started_at, last_run_at, next_run_at, last_error, last_deliver_at, lease_until, lease_owner, rerun)`
- New columns are added with guarded `ALTER TABLE` in `DB.__init__`; there's no migration framework.

---

## 3. The pipeline (per user, per run)

`pipeline/run.py` reads its paths and keys from env: `TIMELINE_SETTINGS`, `TIMELINE_DATA`, `TIMELINE_OUT`, `PAGE_URL`, `TIMELINE_RECAP_URL`, `TWITTERAPI_IO_KEY`, `AI_GATEWAY_API_KEY`, optional `TYPESAFE_API_KEY` and `SLACK_WEBHOOK_URL`. With no env it falls back to the repo's `settings.json`, `data/` and `public/`.

1. **Fetch** (`sources.py`, `panel.py`): twitterapi.io advanced search, limited to this week (Monday 00:00 in the user's `timezone`).
   - **Panel:** accounts followed by at least `min_overlap` of each group's anchor accounts. Rebuilt weekly or when the groups change. Its posts, quotes and retweets are fetched every run, and a story's "reach" is how many panel accounts are on it.
   - **Also fetched:** the settings' X `searches`, the `watchlist`, and `tracked` company accounts (with a daily baseline of each account's median likes).
2. **Judge** (`judge.py`): each *new* tweet goes to **Jev** once, through Vercel AI Gateway (`typesafe-ai/jev`, or TypeSafe directly when that key is set). Jev returns topic, relevance, signal (0–4), bait, and how useful it is for marketing.
   - Saved verdicts are re-judged when the focus or topics change (`JUDGE_VERSION` hash).
3. **Filter** (`judge.why_not`) — a tweet is dropped if any of these apply:
   - Its topic is excluded (`include: false`), or it's over `max_bait`, or under `min_relevance`.
   - **Tracked company accounts:** they must beat `beat_baseline_by` (3×) their median likes **and** reach `tracked.min_likes` **and** `tracked.min_signal`.
   - **Everyone else:** they must reach `thresholds.min_likes` (the floor that keeps small accounts' 100-like posts out) and `min_signal`.
4. **Rank and select:**
   - **Rank:** Jev's scores, times engagement (blending raw likes with likes relative to follower count by `breakout_weight`), times panel reach (`breadth_weight`), minus `mainstream_penalty` for big-lab news.
   - **Select** 60 tweets: one per story, at most `per_author_max` per account (`account_groups` treats @OpenAI, @sama and so on as one), with each topic filled toward its `share`.
5. **Write** (`synthesize.py`): Claude (`writer_model`, default `anthropic/claude-sonnet-5`, via AI Gateway) writes the thesis and 3–5 themes. It sees the previous thesis, so the read evolves through the week.
6. **Build** (`build.py`): Jinja renders `templates/index.html` plus `data.json`, `recap.json` and `recap.md`.
7. **Deliver:** see the Slack section under Features.

**Output that matters for debugging.** The pipeline prints lines like `fetched N tweets`, `judged N tweets (...)`, `search failed (401 ...)` and `nothing passed the filter`. The worker copies those lines into the server log as `user <id>: ...`, so Railway's Deploy Logs show them; the example page is usually user 1. The worker also turns failures into one plain sentence with `humanize()` / `empty_reason()`, such as "twitterapi.io rejected your key" or "out of credits". A run that finishes with no thesis counts as an error.

---

## 4. Features

### Accounts and sign-in (`server/app.py`, `server/google.py`, `server/security.py`)
- **Password accounts:** scrypt hashes, signed session cookie (Starlette `SessionMiddleware`, 30 days, httponly, samesite=lax, secure over https), and a per-IP rate limit on login and signup.
  - Sign-up is limited to `ALLOWED_EMAIL_DOMAINS`.
  - The `.invalid` domain is reserved and can't be used.
- **Google:**
  - **Flow:** OpenID Connect authorization-code flow with PKCE, state and nonce.
  - **Permissions:** scopes `openid email profile` only, `access_type=online` (no refresh token), and the access token is discarded.
  - **Checks on the ID token:** issuer, audience, nonce, expiry and `email_verified`. With `ALLOWED_EMAIL_DOMAINS` set, it also requires Google's `hd` claim to match, so only Workspace-managed accounts get in.
  - **Accounts:** an existing account is linked by email.
  - **Passwords:** once Google is configured, password sign-up turns off unless `PASSWORD_LOGIN=1`.
- **No password reset exists.**

### Example page (`/preview/`)
- **What it is:** a system profile (`preview@timeline.invalid`, which can't sign in), built from `PREVIEW_PRESET` (default `sf_tech`) on the owner's `PREVIEW_*` keys.
- **Access:** public and read-only, with a "Make your own" banner. Signed-out visitors to `/` land here.
- **Schedule:** it refreshes at `PREVIEW_REFRESH_TIMES` (08:30 and 20:00 Pacific, every day) via `preview_prep()` / `_fixed_refresh()`, not every 3h.
- **On every startup, `ensure_preview()`:**
  - Re-applies the preset and reseals the keys.
  - Rebuilds if the settings changed, the last run failed, or the page is empty.
  - Starts the week over if the topics changed.
  - Logs each key's length and last 4 characters, so you can compare against the provider dashboards.
- **Off switch:** leave either `PREVIEW_*` key empty to turn it off.

### Onboarding (`templates/onboarding.html`, `/welcome`)
1. Welcome, with how it works and the cost.
2. Pick a preset, then choose "use as is" or "fine-tune it".
3. *Optional fine-tune:*
   - **Focus:** written-for, focus-on and skip lists.
   - **Topic mix:** on/off switches and share sliders, with a live bar.
   - **Filter:** Sharper, Balanced or Wider presets, plus minimum likes and exact numbers.
   - **Accounts:** watchlist and tracked companies.
4. **API keys (required):** twitterapi.io and Vercel AI Gateway, each checked live; TypeSafe is optional.
5. **Daily recap:** time zone, Slack, Instinct and Muse.
6. **Build:** `POST /api/onboarding/complete` queues the first run, and `/app/` shows `building.html` (progress, and on failure the reason with inline key repair) until the page exists.

Each step saves the settings (`PUT /api/settings`), so a reload resumes where the person left off. They can also import a profile export on the welcome step.

### The page and its Settings (`templates/index.html`, served at `/app/`)
- **The page:** thesis with phrases linked to their themes, "Earlier this week" history, a "New today" list, then themes with tweet cards (X embeds with a fallback card, plus Jev's signal meter).
- **Settings tabs:**
  - **Feed:** apply a preset, focus, topics (label, description, include, share), filter (signal, relevance, bait, **min likes**), ranking, writer model.
  - **Sources:** panel groups and anchors, panel size and overlap, always/never lists, "stories we should have caught" (`label | regex | week`), X searches, watchlist, tracked companies and their bar.
  - **Filtered out:** the best near-misses and why, plus where each expected story ended up.
  - **Daily recap.**
  - **API keys:** replace or re-check keys; values are never shown.
  - **Profile:** appearance (light/dark, stored per device), export (keys only if ticked) and import, private recap link with copy and reset, sign out, delete account (a password, or for Google accounts, typing your email).
- **Save and refresh:** `PUT /api/settings`, then `POST /api/run`, then the page polls status and offers a reload when the new read is ready.

### Presets (`pipeline/presets/*.json`)
Each preset is a full settings object plus `"preset": {id, name, tagline, description}`. Display order comes from `PRESET_ORDER` in `server/settings_schema.py`. Applying a preset keeps the person's Slack settings and time zone, and sets `preset_id`.

| id | For | Non-tracked min likes | Tracked bar (likes / signal) |
|---|---|---|---|
| `sf_tech` *(example page, listed first)* | The most-shared tech, AI and startup posts; GTM, marketing and deep engineering are hidden topics; no tracked companies; mainstream penalty 0.1 | 1000 | n/a |
| `ai_startup_marketing` | The original team feed: tech, AI and SF plus GTM and marketing. Equals the root `settings.json`, which is also new users' starting settings before they pick | 500 | 150 / 1.6 |
| `ai_engineer` | Models, evals, agents, coding tools | 300 | 100 / 1.5 |
| `startup_culture` | Founders, VC, YC, SF | 500 | 100 / 1.5 |
| `gtm_sales` | GTM, outbound, sales tools; tracks Clay, Monaco, Gong and others | 250 | 75 / 1.4 |
| `marketing_brand` | Launch videos, campaigns, positioning | 300 | 100 / 1.5 |
| `ai_business` | Labs, money, compute deals (infra topic on) | 500 | 200 / 1.6 |

Profiles **copy** a preset when it's applied, so later preset edits don't reach existing users until they re-apply it. Only the example page re-applies its preset on startup.

### Settings validation (`server/settings_schema.py`)
`validate()` is used by `PUT /api/settings`, import and presets.
- **Cleanup:** drops unknown keys, fills defaults (for example `thresholds.min_likes` = 300 when missing), and accepts legacy `slack.hour_pt` as `hour`.
- **Caps** (one user's settings drive paid calls on their own keys): ≤ 40 searches, ≤ 300 handles per list, ≤ 100 panel anchors in total, strings ≤ 2,000 characters, and length-capped regexes.
- **Topic shares:** included topics must add up to 50–150%; if they're all zero, they're split evenly.

### Daily recap
- **`recap.md` / `recap.json`:** written every build. They contain the thesis, up to 5 "New today" tweets (first seen in the last 24h, topped up with the best of the week and flagged `new`), the themes and the page link.
- **Private recap URL:** `/r/<token>/recap.md|json`. It's public by unguessable token, can be reset in Profile, and the token is hidden from access logs.
- **Slack** (per user, their own incoming webhook, stored as the `slack_webhook` key):
  - **Setup:** "Create the Slack app" opens Slack with a prefilled manifest; the person pastes the webhook, and the server sends a real test post (`POST /api/slack/test`).
  - **Schedule:** daily after `slack.hour` in the user's time zone (optionally weekdays only), when the thesis changes, or every update.
  - **Delivery:** the worker runs `--deliver` hourly so daily posts land on time. Posts retry on 429 and 5xx, and failures are saved as a plain sentence (`slack_last_error`) shown in Settings.
- **Instinct / Muse:** these consumer AI agents have no API for other apps to push messages into. The UI gives a one-tap setup message ("every weekday at 8:00 AM, read `<recap url>` and send me …") via an `sms:` link for Instinct, or `muse.ai` / `wa.me` plus copy for Muse. Setup state is remembered per browser only (`localStorage` `tl_deliver`).

### Daily engagement DM (`pipeline/engage.py`, `engage.json`)
- **What it is:** a Slack bot DMs one person (`ENGAGE_DM_EMAIL`, vivek@rox.com) every day at `ENGAGE_TIME` (11:00 `America/Los_Angeles`). For each of Ishan (CEO), Shriram (CTO), Gopal (research lead), Dan (Daniel Chen: ex-founder, ex-Sequoia), Taeuk (founding engineer) and Brian (engineer), it lists 2–3 of today's tweets and one topic to engage with, each with a reason of 12 words or fewer.
- **How it picks:**
  1. Fetches the last 24h from `engage.json` searches and accounts.
  2. Keeps originals by authors with 1,500+ followers, or 200+ followers and a bio naming a `prominent_orgs` company (short names like Ramp only count as an @handle).
  3. Drops the team's own posts and anything under 25 likes.
  4. Ranks by engagement per hour (`score()`) and keeps the top 120, at most 2 per author.
  5. Makes one Claude call (via AI Gateway) that returns per-person picks and topics as JSON. `clean()` drops made-up ids and trims the reasons.
  6. Sends Slack Block Kit: `users.lookupByEmail`, then `conversations.open`, then `chat.postMessage`.
- **Scheduling** (`Worker.engage_due` / `run_engage`):
  - Starts 8 minutes before `ENGAGE_TIME` and gives up for the day at 3pm.
  - Retries a failure every 15 minutes.
  - Sends once right after it's first configured.
  - State lives in the `meta` table (`engage_sent_date`, `engage_tried_at`, `engage_last_error`, `engage_force`).
  - Logs go to `DATA_DIR/engage/last_run.log`, and the last picks to `DATA_DIR/engage/last.json`.
- **Env:** `ENGAGE_SLACK_BOT_TOKEN` (xoxb), `ENGAGE_DM_EMAIL` or `ENGAGE_DM_USER`, optional `ENGAGE_TIME`, `ENGAGE_TIMEZONE`, `ENGAGE_WEEKDAYS_ONLY`, and `ENGAGE_TWITTERAPI_IO_KEY` / `ENGAGE_AI_GATEWAY_API_KEY` (they fall back to the `PREVIEW_*` keys).
- **Owner-only routes:** `POST /api/engage/run` (send now) and `GET /api/engage/status`. "Owner" means the signed-in email equals `ENGAGE_DM_EMAIL`.
- **Channel instead of DM:** `ENGAGE_SLACK_CHANNEL` (channel ID, or an email / user ID to DM) overrides the DM; `FOLLOW_SLACK_CHANNEL` defaults to it. `engage.send(msg, dest)` resolves the destination. `engage_due()` stores `engage_target` and resets the sent date when it changes, so the new destination gets one right away (the first deploy with this check only records it).
- **Slack app manifest** (scopes `chat:write`, `im:write`, `users:read`, `users:read.email`): https://api.slack.com/apps?new_app=1&manifest_json=%7B%22display_information%22%3A%7B%22name%22%3A%22Engage%20digest%22%2C%22description%22%3A%22Daily%20picks%20of%20what%20the%20team%20should%20engage%20with%20on%20X%22%7D%2C%22features%22%3A%7B%22bot_user%22%3A%7B%22display_name%22%3A%22Engage%20digest%22%2C%22always_online%22%3Afalse%7D%2C%22app_home%22%3A%7B%22messages_tab_enabled%22%3Atrue%2C%22messages_tab_read_only_enabled%22%3Atrue%7D%7D%2C%22oauth_config%22%3A%7B%22scopes%22%3A%7B%22bot%22%3A%5B%22chat%3Awrite%22%2C%22im%3Awrite%22%2C%22users%3Aread%22%2C%22users%3Aread.email%22%5D%7D%7D%2C%22settings%22%3A%7B%22org_deploy_enabled%22%3Afalse%2C%22socket_mode_enabled%22%3Afalse%2C%22token_rotation_enabled%22%3Afalse%7D%7D
- **People profiles** are seeded from public info and confirmed by the owner. Brian is Brian Xu (Stanford; works on agents with Gopal). Dan is Daniel Chen.

### Weekly follow recommendations (`pipeline/follow.py`)
- **What it does:** Mondays at `FOLLOW_TIME` (11:15, `FOLLOW_DAY`), it DMs `ENGAGE_DM_EMAIL` 5 accounts per person in `engage.json` to follow, each with a reason of 12 words or fewer. It's sent once right after first setup, like the others.
- **How it picks:**
  1. Fetches 7 days of the engage sources (`fetch()` with a 168h window).
  2. `authors()` keeps accounts with 5,000+ followers (or 1,000+ and a `prominent_orgs` bio), at least 2 posts with 50+ likes, not the team, and individuals only (`is_company()` drops known company handles and brand-voiced bios; the prompt also says people only, though employees count). It ranks them by summed log engagement and sends the top 150 to the model.
  3. `already_following()` loads each person's followings via twitterapi.io (from `x` in engage.json; None means unknown).
  4. `follow_history.json` blocks repeats for 8 weeks.
  5. One Claude call picks the accounts; `clean()` drops unknown or blocked handles.
- **Settings:** under `follow` in engage.json. State keys `follow_*` live in `meta`, and logs go to `DATA_DIR/engage/follow_last_run.log`.
- **Route:** `POST /api/engage/follow/run` (owner) sends one now. `FOLLOW_ENABLED=0` turns it off.
- **Handles:** all six people have X handles in engage.json (confirmed by the owner), so everyone's existing follows are skipped.

### Marketing channel digest (`pipeline/channel.py`)
- **The archetype:** a system profile `gtm@timeline.invalid`, created once from `CHANNEL_PRESET` (`gtm_marketing`, a merge of `gtm_sales` and `marketing_brand`) and never re-applied, so admin edits persist.
  - It runs every 3h on `CHANNEL_*` keys, falling back to `ENGAGE_*` then `PREVIEW_*`.
  - The page at `/gtm/` is public and read-only.
  - **Admin editing:** admins (`ADMIN_EMAILS`, default `ENGAGE_DM_EMAIL`) edit it from that page. The page's JS sets `TL.profile = "gtm"`, which sends `X-Profile: gtm`, and `acting()` in `server/app.py` maps the request onto the archetype for `/api/me`, `PUT /api/settings`, `POST /api/run` and `/api/status`. Every other route rejects that header.
- **Twice a day:** the archetype doesn't refresh every 3h. After each run its `next_run_at` is left empty, and `channel_prep()` queues one run 35 minutes before each post and one at `CHANNEL_EVENING_REFRESH` (20:00; empty turns it off). Both happen on posting days only, and are skipped if it ran in the last hour. The post waits for that run to finish. Admins can still force a run with Save and refresh on `/gtm/`. The comp-intel brief is produced once a day by the Grok bot (about 9:00 PT) and read once per post.
- **Destination:** `CHANNEL_SLACK_CHANNEL` is a channel ID, or an email or user ID (resolved to a DM from the bot; used for testing). When it changes, `channel_due()` resets the sent date so the next post goes out right away.
- **The post:** each weekday at `CHANNEL_TIME` (09:30, starting 2 minutes early), it posts to `CHANNEL_SLACK_CHANNEL` with the engagement bot's token (or `CHANNEL_SLACK_BOT_TOKEN`). The bot must be invited to the channel.
- **Split:** with `INTEL_SLACK_CHANNEL` set, `main()` sends two posts: `message(..., with_intel=False)` to `CHANNEL_SLACK_CHANNEL` and `intel_message()` to `INTEL_SLACK_CHANNEL` (with `INTEL_SLACK_BOT_TOKEN` if set). `CHANNEL_OUT/posted.json` records each post by day, part, and destination so a retry skips what went out; `CHANNEL_FORCE=1` (set by an admin's "run now") bypasses it. The destination-change key is `channel|intel_channel`.
  - **Contents:** the thesis, 4–5 bullets with tweet links and a "Full report" link, then a "Market intel" section (2–3 items, and 1–2 "Also trending" items).
  - **How it's made:** one Claude call over the archetype's saved week (top tweets, with today's first) plus the intel text. `clean()` keeps only real tweet ids and drops the intel sections when there's no intel.
- **Intel:** a pushed copy (`POST /api/intel`, Bearer `INTEL_PUSH_TOKEN`, stored at `DATA_DIR/intel/latest.json`) newer than 36h wins; otherwise `INTEL_URL` is fetched and turned into text (`html_text`).
  - A page that's only built in the browser yields almost no text. The post then links to it with a short reason.
  - **comp-intel** (vivekyarla.github.io/comp-intel, the Grok bot's site) is that kind of page. It's configured as `INTEL_URL=.../comp-intel/data/today.json` plus `INTEL_PAGE_URL=.../comp-intel/`. `brief_text()` turns its `market_briefing` / `market_direction` / `competitive_moves` / `watch` / `biggest_ai_news` sections into compact text. Market items may carry a source URL, but only one cited in the JSON (`clean()` checks). A fixture lives in `tests/fixtures/comp_intel_today.json`.
- **State** lives in `meta` (`channel_sent_date`, `channel_tried_at`, `channel_last_error`, `channel_force`). Logs go to `DATA_DIR/channel/last_run.log`, and the last post to `DATA_DIR/channel/last.json`.
- **Admin routes:** `GET /api/channel/status` and `POST /api/channel/run`.
- **Instinct/Muse for shared reports:** `/gtm/` and `/preview/` show a "Get it daily" button. It opens `TL.agentCards()` pointed at that report's public `recap.md`, with no account needed, and setup state is stored per report in `localStorage`.
- **Competitive intel for agents** (`server/intel_feed.py`): `IntelFeed` renders comp-intel's `today.json` as Markdown (`brief_markdown()`), cached 15 min, with a fresh pushed copy taking precedence. Routes: `GET|HEAD /intel/recap.md` (full brief) and `/intel/` (redirect to the page). Any `recap.md` (`/gtm/`, `/preview/`, `/r/<token>/`) with `?intel=1` gets the short version appended (`recap_response()`). `/api/me` returns `intel_url`; shared pages detect it with a HEAD request. `agentCard()` shows a "What to send" select (x / both / intel) that changes the URL and the setup message.
- **Tests:** `tests/test_channel.py`.

### Asking the bot (`server/slackbot.py`)
- `POST /slack/events` (Slack Events API) verifies `SLACK_SIGNING_SECRET`, answers Slack's `url_verification` handshake, ignores retries, and handles `app_mention` and DM `message.im` events in a background thread, since Slack wants a 200 within 3 seconds.
- **The reply:** it looks up the asker's email (`users.info`) and makes one Claude call grounded in `help_doc()` (how to tweak the GTM report, Settings tabs, common tweaks, admins), which knows whether the asker is an admin. It falls back to fixed steps if the call fails. Mentions are answered in a thread.
- **Slack app needs:** scopes `app_mentions:read` and `im:history` (on top of `chat:write`, `im:write`, `users:read`, `users:read.email`), the messages tab writable, and event subscriptions to `<BASE_URL>/slack/events` for `app_mention` and `message.im`.

---

## 5. HTTP surface (`server/app.py`)

| Route | Auth | Notes |
|---|---|---|
| `GET /healthz` (also HEAD) | – | `{"ok": true}` |
| `GET /` | – | Signed out: `/preview/` (or `/login` if no preview). Signed in: `/welcome` or `/app/` |
| `GET /preview/`, `/preview/{data.json,recap.json,recap.md}` | – | Example page |
| `GET /login`, `GET /welcome`, `GET /app/`, `GET /app/{file}` | session for welcome/app | `/app/` shows `building.html` until the page exists |
| `GET /r/{token}/{recap.md,recap.json}` | token | Private recap feed |
| `GET /auth/google`, `GET /auth/google/callback` | – | OIDC; errors redirect to `/login?error=` |
| `POST /api/signup`, `/api/login`, `/api/logout` | – | 403 when Google-only |
| `GET /api/me` | session | `{email, auth, demo, onboarded, settings, keys: {name: bool}, status, recap_url, page_url, slack}` |
| `GET /api/presets` | – | `[{id, name, tagline, description, settings}]` |
| `PUT /api/settings {settings}` | session | Validated; 422 `{error}` |
| `PUT /api/keys {...}` | session | `""` deletes; values are never returned |
| `POST /api/keys/check {...}` | session | Live checks: twitterapi.io search, AI Gateway `/v1/credits` (falling back to a 1-token completion) |
| `POST /api/slack/test {webhook?}` | session | |
| `POST /api/onboarding/complete` | session | Requires both keys; queues the first run |
| `POST /api/run {slack: auto\|now}` | session | Never refused (see scheduling rules); returns status plus `message` |
| `GET /api/status` | session | `{state, last_run_at, next_run_at, last_error, has_page}` |
| `GET /api/export[?keys=1]`, `POST /api/import {profile}` | session | `{"format":"timeline-profile","version":1,...}` |
| `POST /api/recap/rotate`, `DELETE /api/account {password \| confirm}` | session | |
| `POST /api/engage/run`, `GET /api/engage/status` | session, `ENGAGE_DM_EMAIL` only | Daily engagement DM |
| `GET /gtm/`, `/gtm/{file}` | – | The GTM archetype's page (admins edit it via `X-Profile: gtm`) |
| `GET /api/channel/status`, `POST /api/channel/run` | admin (`ADMIN_EMAILS`) | Marketing channel digest |
| `POST /api/intel` | Bearer `INTEL_PUSH_TOKEN` | Another bot pushes its market-intel report |
| `POST /slack/events` | Slack signature | The bot answering @mentions and DMs |

All errors are JSON `{"error": "<a sentence for a person>"}`.

---

## 6. Frontend

- **No build step and no framework.** Server-rendered Jinja templates plus inline JS.
- **Shared pieces** are pulled in with `{% include %}`:
  - `_base.css`: the design tokens and all styles, with light and dark themes.
  - `_theme.js`: applies the theme before first paint.
  - `_shared.js`: the `TL` component library, covering the API client, list, tag, mix and filter editors, the preset picker, key cards and the delivery cards.
- **Templates:** `login.html`, `onboarding.html`, `building.html`, and `index.html` (rendered by the **pipeline**, not the server, and served as a static file per user).
- **Look:** serif (Newsreader) and sans (Inter), warm neutral palette, hairline rules with corner dots.
- **Watch out:** a literal `{#` inside template JS starts a Jinja comment.

---

## 7. Running, testing, deploying

```bash
pip install -r requirements.txt -r requirements-dev.txt
TIMELINE_DEMO=1 uvicorn server.app:app --reload     # no keys needed: checks skipped, pages built from pipeline/demo_state.json
pytest -q                                            # 88 tests; CI: .github/workflows/test.yml
python -m pipeline.run --demo                        # render one page from sample data into ./public
```

- **Docker:**
  - Base image `python:3.12-slim`.
  - `docker-entrypoint.sh` starts as root only to `chown` the mounted `/data`, then drops to uid 10001 with `setpriv`.
  - The start command listens on `${PORT:-8000}`.
  - **No `VOLUME` line:** Railway rejects Dockerfiles that have one.
- **Railway:** `railway.json` sets the Dockerfile build, `numReplicas: 1`, the `/healthz` health check and restart on failure.
- **Fly.io:** an alternative config is in `fly.toml`.
- **docker-compose:** works for self-hosting.
- **Env reference:** `.env.example`. Notable variables: `MAX_CONCURRENT_RUNS` (3), `REFRESH_HOURS` (3), `TIMELINE_WORKER=0` (turns the scheduler off; the tests use it), `RATE_LIMIT`, `KEY_CHECK_MODEL`.

---

## 8. Known gaps and unverified pieces

- **Google sign-in** is untested against real Google, because the owner can't create the OAuth client yet. The flow is covered by tests with a mocked token exchange.
- **The AI Gateway key check** uses `GET https://ai-gateway.vercel.sh/v1/credits` (with a fallback completion on `openai/gpt-4.1-nano`). Neither has been confirmed against Vercel. If valid keys get rejected at onboarding, look here first.
- **Accounts:** no password reset and no email verification. The domain limit on password sign-up keeps outsiders out but doesn't prove someone owns the address.
- **Preset edits don't reach existing profiles** (profiles copy a preset).
- **Instinct / Muse** setup isn't confirmed server-side, since the app can't see whether the agent scheduled the task.
- **Single instance only:** SQLite plus an in-process scheduler. Scaling past one box would mean moving to Postgres and a separate worker.
- **`main` still runs the old GitHub Pages workflow** (`update.yml` commits `data/week.json` every 3h). The branch deletes those workflows; merging the branch stops it. The branch also still has the old team-mode `data/` folder, which the new app doesn't use.
- **Costs:**
  - Railway: about $5–8 a month.
  - Each user, on their own keys: about $20–35 a month (twitterapi.io about $10–15, AI Gateway about $10–20).
  - The example page costs the owner about the same as one user.

## 9. Where to look for common tasks

| Task | Files |
|---|---|
| Change what makes a page (filters/ranking) | `pipeline/judge.py` (`why_not`, `rank_all`, `select`), `pipeline/config.py` |
| Add or tune a preset | `pipeline/presets/*.json`, `PRESET_ORDER` in `server/settings_schema.py`, `tests/test_presets.py` |
| Add a settings field | `server/settings_schema.py` (validate + default), `pipeline/config.py` (read it), `templates/index.html` (`fill`/`collect`), `templates/_shared.js` if it's in onboarding |
| Scheduler, runs, error messages | `server/worker.py` (`run_user`, `humanize`, `empty_reason`, leases), `server/db.py` |
| Routes and auth | `server/app.py`, `server/google.py`, `server/security.py`, `server/config.py` |
| Page design | `templates/_base.css`, `templates/index.html` |
| Slack message / recap format | `pipeline/slack.py`, `pipeline/recap.py` |
