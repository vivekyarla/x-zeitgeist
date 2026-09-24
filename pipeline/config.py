"""Everything you'd want to tweak lives here."""
import os

# ---------------------------------------------------------------------------
# What to pull from X each run
# ---------------------------------------------------------------------------
# Standard X advanced-search syntax. Each query is run with queryType="Top"
# (most engaged first) and restricted to the current week automatically.
SEARCH_QUERIES = [
    # Model and AI product launches
    '(introducing OR "just launched" OR "just shipped" OR "now available" OR "new model" OR "open source model") '
    '(AI OR model OR agent OR Claude OR GPT OR Gemini OR Grok OR Llama) min_faves:300 lang:en -filter:replies',
    '("AI agent" OR "AI agents" OR "frontier model" OR OpenAI OR Anthropic OR Gemini) '
    'min_faves:500 lang:en -filter:replies',
    # Creative stuff going viral: demos, animations, vibe-coded toys
    '("made this with" OR "built this with" OR "vibe coded" OR "one prompt" OR "one-shot" OR animation '
    'OR shader OR "three.js" OR p5js OR "generative art" OR "look what") '
    '(Claude OR GPT OR Gemini OR Cursor OR AI) min_faves:300 lang:en -filter:replies',
    # What GTM and revenue people are doing: tools, playbooks, experiments
    '(Clay OR Monaco OR "GTM engineer" OR "GTM engineering" OR "AI SDR" OR "cold email" OR "outbound" '
    'OR "sales playbook" OR RevOps) min_faves:75 lang:en -filter:replies',
    '(GTM OR "go-to-market" OR "launch video" OR "launch day" OR billboard OR "brand campaign" '
    'OR "marketing stunt") (AI OR startup OR SaaS) min_faves:150 lang:en -filter:replies',
    '(ARR OR "usage-based pricing" OR "pricing page") (AI OR startup) min_faves:150 lang:en -filter:replies',
    # Startup and SF culture
    '("San Francisco" OR "SF tech" OR "in SF" OR "Bay Area" OR founders OR a16z OR YC) '
    '(AI OR startup OR school OR hackathon OR party OR launch) min_faves:200 lang:en -filter:replies',
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
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")  # direct TypeSafe; pin e.g. "jev-1.13.0" once tuned
JEV_GATEWAY_MODEL = os.getenv("JEV_GATEWAY_MODEL", "typesafe-ai/jev")  # via Vercel AI Gateway
JEV_CONCURRENCY = int(os.getenv("JEV_CONCURRENCY", "8"))

# Bump when the questions or topics below change, so saved tweets get re-judged.
JUDGE_VERSION = 2

# A tweet makes the page only if it clears all of these.
MIN_RELEVANCE = 0.60   # P(on-topic for tech / AI / SF / sales / revenue)
MAX_BAIT = 0.50        # P(engagement bait, spam, giveaway, shilling)
MIN_SIGNAL = 1.8       # 0-4 rubric score; ~2 = "notable"

TOPICS = {
    "launches": "New AI models and AI product launches, from big labs or startups (e.g. a GPT or Claude "
                "release, a new eval model like Jev, a new agent product)",
    "creative_viral": "Creative or delightful things people built with AI that are going viral: demos, "
                      "animations, games, art, vibe-coded apps, clever prompts",
    "gtm_playbooks": "What sales, GTM, and revenue teams and tools are doing: Clay, Monaco, AI SDRs, "
                     "outbound experiments, launch videos, brand stunts, pricing moves",
    "ai_at_work": "How people and teams are actually using AI at work, and takes on where it's going",
    "startup_culture": "Startup, founder, and VC culture moves: new programs, schools, hiring, notable "
                       "raises or acquisitions that people are talking about",
    "sf_scene": "San Francisco and Bay Area tech culture, events, parties, hackathons, and life",
    "industry_finance": "Infrastructure and finance: data centers, compute buildout, chips supply, power, "
                        "bonds, debt, earnings, stock moves, macro",
    "policy": "Politics, government, regulation, lawsuits, and AI safety incidents",
    "other": "Anything else",
}
# Topics that never make the page, however strong the signal.
EXCLUDED_TOPICS = {"industry_finance", "policy", "other"}

TOPIC_LABELS = {
    "launches": "Launches",
    "creative_viral": "Going viral",
    "gtm_playbooks": "GTM & revenue",
    "ai_at_work": "AI at work",
    "startup_culture": "Startup culture",
    "sf_scene": "SF scene",
    "industry_finance": "Infra & finance",
    "policy": "Policy",
    "other": "Other",
}

# ---------------------------------------------------------------------------
# Thesis writing (Vercel AI Gateway)
# ---------------------------------------------------------------------------
WRITER_MODEL = os.getenv("WRITER_MODEL", "anthropic/claude-sonnet-5")  # any AI Gateway model id
TWEETS_FOR_THESIS = 40     # top-ranked tweets sent to the writer model
TWEETS_PER_THEME_SHOWN = 6

# ---------------------------------------------------------------------------
# Week boundaries
# ---------------------------------------------------------------------------
TIMEZONE = "America/Los_Angeles"  # week starts Monday 00:00 in this zone
SITE_TITLE = "This week on the timeline"
