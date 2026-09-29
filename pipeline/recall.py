"""Where each known story landed this run.

settings.json → known_stories is a list of {label, match (regex), week (optional,
YYYY-MM-DD Monday)}: stories someone noticed and expects the page to catch. Every run
reports, for each one, the furthest stage its best tweet reached, so a miss can be
reproduced and explained instead of patched.
"""
from __future__ import annotations

import re

from . import config
from .judge import reach_of, why_not

STAGES = ["not fetched", "fetched, not judged", "filtered out", "kept, ranked too low", "sent to writer", "on the page"]


def report(state: dict) -> list[dict]:
    latest = state.get("latest") or {}
    top = set(latest.get("top_ids", []))
    shown = {i for th in latest.get("themes", []) for i in th.get("tweet_ids", [])}
    week = state["week_start"][:10]
    baselines, reach = state.get("baselines", {}), state.get("reach", {})
    out = []
    for ks in config.KNOWN_STORIES:
        if ks.get("week") and ks["week"] != week:
            continue
        rx = re.compile(ks["match"], re.I | re.S)
        hits = [t for t in state["tweets"].values() if rx.search(t["text"])]
        best, stage, detail = None, 0, ""
        for t in hits:
            if t["id"] in shown:
                s, d = 5, ""
            elif t["id"] in top:
                s, d = 4, "the writer left it out of the themes"
            elif "jev" not in t:
                s, d = 1, ""
            else:
                why = why_not(t, baselines)
                s, d = (3, f"rank {t.get('rank', 0):.2f}") if why is None else (2, why)
            if s > stage or best is None or (s == stage and t["likes"] > best["likes"]):
                best, stage, detail = t, s, d
        out.append({"label": ks["label"], "stage": STAGES[stage] if hits else STAGES[0], "detail": detail,
                    "matches": len(hits), "reach": len(reach_of(best, reach)) if best else 0,
                    "tweet": f"@{best['author']['handle']} ({best['likes']:,} likes): {best['text'][:100]}" if best else ""})
    for r in out:
        print(f"  known story '{r['label']}': {r['stage']}" + (f" ({r['detail']})" if r["detail"] else "")
              + f" | {r['matches']} matching tweets, reach {r['reach']} panel accounts")
    return out
