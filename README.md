# This week on the timeline

A page that shows the marketing team what tech, AI, SF, and sales Twitter is talking about this week, with a 1–2 sentence thesis that updates every three hours.

## How it works

Every 3 hours a GitHub Action runs `python -m pipeline.run`:

1. **Fetch** (`pipeline/sources.py`): runs the searches and watchlist in `pipeline/config.py` against twitterapi.io, limited to tweets since Monday 00:00 PT.
2. **Judge** (`pipeline/judge.py`): sends each *new* tweet to Jev once (through Vercel AI Gateway, or TypeSafe directly if `TYPESAFE_API_KEY` is set), asking five questions in one call: is it on-topic, which topic, how much signal (0–4 rubric), is it bait, and would marketing care. Tweets that clear the thresholds are ranked, with Jev's scores weighted most and engagement breaking ties.
3. **Synthesize** (`pipeline/synthesize.py`): sends the top 40 to Claude through Vercel AI Gateway, which writes the thesis and groups tweets into 3–5 themes. It sees its previous thesis, so the read evolves instead of starting over.
4. **Publish** (`pipeline/build.py`): renders `public/index.html` (plus `public/data.json` for a future Notion sync) and deploys to GitHub Pages. Weekly state is committed to `data/week.json`; on Monday the old week moves to `data/archive/`.

## Setup (about 15 minutes)

1. Push this folder to a new GitHub repo.
2. Add two repo secrets (or add them later from Settings → API keys on the page) (Settings → Secrets and variables → Actions):
   - `TWITTERAPI_IO_KEY` from twitterapi.io/dashboard
   - `AI_GATEWAY_API_KEY` from vercel.com → AI Gateway → API Keys. Used for both Jev and Claude. (Optional: add `TYPESAFE_API_KEY` from console.typesafe.ai to send Jev calls to TypeSafe directly.)
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "Update timeline page" → **Run workflow** to do the first run. After that it runs on its own.

## Preview locally

```bash
pip install -r requirements.txt
python -m pipeline.run --demo          # sample data, no keys needed → public/index.html
export TWITTERAPI_IO_KEY=... AI_GATEWAY_API_KEY=...
python -m pipeline.run                 # a real update
python -m pipeline.run --build-only    # re-render from saved state (after template edits)
```

## Tuning

Interests, topics, searches, thresholds, the writer model, and the Slack schedule live in `settings.json`. Edit them from the **Settings** button on the page (connect a fine-grained GitHub token for this repo with read/write on Contents, Secrets, and Actions), or edit the file directly. Saving from the page commits `settings.json` and starts an update; any change to the interests or topics re-judges the week's saved tweets automatically.

- **Too much noise?** Raise the min signal (e.g. 2.2) or min relevance, or raise `min_faves` in the searches.
- **Missing stuff?** Add searches or watchlist accounts, or lower the min signal.
- **Change the focus:** edit "Focus on" and "Skip", and the topics (unchecked topics never make the page).
- **Pin Jev** once thresholds feel right: set `JEV_MODEL=jev-1.13.0` (or whatever is current), so `jev-latest` updates don't shift scores under you.

## Slack

1. Create a Slack app (api.slack.com/apps → From scratch), turn on **Incoming Webhooks**, and add one for your channel.
2. On the page: Settings → API keys → paste the webhook URL as the Slack webhook.
3. Settings → Slack → turn it on and pick how often: once a day after a set hour (PT), when the thesis changes, or every update. "Send to Slack now" posts after one immediate update.

Every Jev verdict is saved in `data/week.json`, so you can look at what got filtered out and adjust thresholds from real data.

## Costs (rough)

- twitterapi.io: ~6 queries + watchlist × 2 pages × 8 runs/day ≈ 2,500 tweets/day ≈ **$10–15/month**.
- Jev: only new tweets are judged, a few hundred tokens each ≈ **well under $1/month**.
- Claude via AI Gateway: 8 calls/day with ~15K tokens in ≈ **$10–20/month** on a Sonnet-class model.
- GitHub Actions and Pages: free at this volume.

## Notes

- GitHub Pages sites are public unless your org is on GitHub Enterprise Cloud (which can restrict Pages to org members). The page only shows public tweets, but keep that in mind.
- Swapping to the official X API later: write a class with the same `search()` method in `sources.py` that returns the same normalized dicts, and use it in `run.fetch()`.
