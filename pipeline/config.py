"""Everything you'd want to tweak lives here."""
import os

# ---------------------------------------------------------------------------
# What to pull from X each run
# ---------------------------------------------------------------------------
# Standard X advanced-search syntax. Each query is run with queryType="Top"
# (most engaged first) and restricted to the current week automatically.
SEARCH_QUERIES = [
    # AI: models, labs, agents
    '("AI agent" OR "AI agents" OR LLM OR "frontier model" OR OpenAI OR Anthropic '
    'OR Gemini OR "open source model") min_faves:300 lang:en -filter:replies',
    # Sales, GTM, revenue
    '(GTM OR "go-to-market" OR SDR OR BDR OR RevOps OR "sales team" OR quota '
    'OR pipeline OR "outbound") min_faves:75 lang:en -filter:replies',
    '(ARR OR "net revenue retention" OR churn OR "pricing page" OR "usage-based pricing") '
    '(SaaS OR AI OR startup) min_faves:100 lang:en -filter:replies',
    # Startups, funding, SF
    '("Series A" OR "Series B" OR "Series C" OR "just raised" OR "we raised") '
    '(AI OR startup) min_faves:200 lang:en -filter:replies',
    '("San Francisco" OR "SF tech" OR "in SF" OR "Bay Area") (AI OR startup OR founders OR tech) '
    'min_faves:150 lang:en -filter:replies',
    # Big tech / general tech news
    '(Apple OR Google OR Microsoft OR Meta OR Nvidia OR Salesforce) (AI OR launch OR announces) '
    'min_faves:500 lang:en -filter:replies',
]

# Accounts whose posts are always considered (still filtered by Jev).
# Starter list; edit to match who your team actually follows.
WATCHLIST = [
    "ycombinator", "a16z", "paulg", "levie", "jasonlk", "TechCrunch",
    "OpenAI", "AnthropicAI", "GoogleDeepMind", "sama", "karpathy", "benioff",
]
WATCHLIST_MIN_FAVES = 20

PAGES_PER_QUERY = int(os.getenv("PAGES_PER_QUERY", "2"))  # ~20 tweets per page

# ---------------------------------------------------------------------------
# Jev filtering
# ---------------------------------------------------------------------------
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")  # pin e.g. "jev-1.13.0" once tuned
JEV_CONCURRENCY = int(os.getenv("JEV_CONCURRENCY", "8"))

# A tweet makes the page only if it clears all of these.
MIN_RELEVANCE = 0.60   # P(on-topic for tech / AI / SF / sales / revenue)
MAX_BAIT = 0.50        # P(engagement bait, spam, giveaway, shilling)
MIN_SIGNAL = 1.8       # 0-4 rubric score; ~2 = "notable"

TOPICS = {
    "ai_models": "AI models, labs, research, benchmarks, open-source releases",
    "ai_products": "AI products, agents, and how people are using AI at work",
    "sales_gtm": "Sales, go-to-market, outbound, SDRs, RevOps, sales tools",
    "revenue_business": "Revenue, pricing, ARR, SaaS metrics, business models",
    "startups_funding": "Startups, fundraising, VCs, founders, acquisitions",
    "sf_scene": "San Francisco and Bay Area tech culture, events, and life",
    "big_tech": "Big tech companies and industry-wide news",
    "other": "Anything else",
}

TOPIC_LABELS = {
    "ai_models": "AI models",
    "ai_products": "AI at work",
    "sales_gtm": "Sales & GTM",
    "revenue_business": "Revenue & pricing",
    "startups_funding": "Startups & funding",
    "sf_scene": "SF scene",
    "big_tech": "Big tech",
    "other": "Other",
}

# ---------------------------------------------------------------------------
# Thesis writing (OpenRouter)
# ---------------------------------------------------------------------------
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5")
TWEETS_FOR_THESIS = 40     # top-ranked tweets sent to the writer model
TWEETS_PER_THEME_SHOWN = 6

# ---------------------------------------------------------------------------
# Week boundaries
# ---------------------------------------------------------------------------
TIMEZONE = "America/Los_Angeles"  # week starts Monday 00:00 in this zone
SITE_TITLE = "This week on the timeline"
