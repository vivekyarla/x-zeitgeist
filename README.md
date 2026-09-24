# This week on the timeline

A page that shows the marketing team what tech, AI, SF, and sales Twitter is talking about this week, with a 1–2 sentence thesis that updates every three hours.

## How it works

Every 3 hours a GitHub Action runs `python -m pipeline.run`:

1. **Fetch** (`pipeline/sources.py`): runs the searches and watchlist in `pipeline/config.py` against twitterapi.io, limited to tweets since Monday 00:00 PT.
2. **Judge** (`pipeline/judge.py`): sends each *new* tweet to Jev once (through Vercel AI Gateway if `AI_GATEWAY_API_KEY` is set, otherwise TypeSafe directly), asking five questions in one call: is it on-topic, which topic, how much signal (0–4 rubric), is it bait, and would marketing care. Tweets that clear the thresholds are ranked, with Jev's scores weighted most and engagement breaking ties.
3. **Synthesize** (`pipeline/synthesize.py`): sends the top 40 to a model on OpenRouter, which writes the thesis and groups tweets into 3–5 themes. It sees its previous thesis, so the read evolves instead of starting over.
4. **Publish** (`pipeline/build.py`): renders `public/index.html` (plus `public/data.json` for a future Notion sync) and deploys to GitHub Pages. Weekly state is committed to `data/week.json`; on Monday the old week moves to `data/archive/`.

## Setup (about 15 minutes)

1. Push this folder to a new GitHub repo.
2. Add three repo secrets (Settings → Secrets and variables → Actions):
   - `TWITTERAPI_IO_KEY` from twitterapi.io/dashboard
   - `AI_GATEWAY_API_KEY` from vercel.com → AI Gateway → API Keys (or `TYPESAFE_API_KEY` from console.typesafe.ai to call Jev directly)
   - `OPENROUTER_API_KEY` from openrouter.ai/keys
3. Settings → Pages → Source: **GitHub Actions**.
4. Actions → "Update timeline page" → **Run workflow** to do the first run. After that it runs on its own.

## Preview locally

```bash
pip install -r requirements.txt
python -m pipeline.run --demo          # sample data, no keys needed → public/index.html
export TWITTERAPI_IO_KEY=... AI_GATEWAY_API_KEY=... OPENROUTER_API_KEY=...
python -m pipeline.run                 # a real update
python -m pipeline.run --build-only    # re-render from saved state (after template edits)
```

## Tuning

Everything is in `pipeline/config.py`:

- **Too much noise?** Raise `MIN_SIGNAL` (e.g. 2.2) or `MIN_RELEVANCE`, or raise `min_faves` in the queries.
- **Missing stuff?** Add queries or accounts to `WATCHLIST`, or lower `MIN_SIGNAL`.
- **Pin Jev** once thresholds feel right: set `JEV_MODEL=jev-1.13.0` (or whatever is current), so `jev-latest` updates don't shift scores under you.
- **Writer model**: set `OPENROUTER_MODEL` to any OpenRouter model id.

Every Jev verdict is saved in `data/week.json`, so you can look at what got filtered out and adjust thresholds from real data.

## Costs (rough)

- twitterapi.io: ~6 queries + watchlist × 2 pages × 8 runs/day ≈ 2,500 tweets/day ≈ **$10–15/month**.
- Jev: only new tweets are judged, a few hundred tokens each ≈ **well under $1/month**.
- OpenRouter: 8 calls/day with ~15K tokens in ≈ **$10–20/month** on a Sonnet-class model.
- GitHub Actions and Pages: free at this volume.

## Notes

- GitHub Pages sites are public unless your org is on GitHub Enterprise Cloud (which can restrict Pages to org members). The page only shows public tweets, but keep that in mind.
- Swapping to the official X API later: write a class with the same `search()` method in `sources.py` that returns the same normalized dicts, and use it in `run.fetch()`.
