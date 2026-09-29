"""The timeline panel: the accounts whose posts make up "the timeline".

Built from anchor accounts (settings.json → panel.anchors): an account joins the panel
when at least `min_overlap` anchors follow it. Those are the people the scene itself
pays attention to, so stories are discovered by who is posting and reacting, not by
guessing keywords. Rebuilt every `refresh_days`; saved to data/panel.json.
"""
from __future__ import annotations

import collections
import json
from datetime import datetime, timedelta
from pathlib import Path

from . import config

PANEL_PATH = Path(__file__).resolve().parent.parent / "data" / "panel.json"


def load() -> dict:
    return json.loads(PANEL_PATH.read_text()) if PANEL_PATH.exists() else {}


def _settings_key() -> list:
    p = config.PANEL
    return [[(g["name"], g["share"], sorted(h.lower() for h in g["accounts"])) for g in p["groups"]],
            p["min_overlap"], p["max_accounts"], sorted(h.lower() for h in p["include"]),
            sorted(h.lower() for h in p["exclude"])]


def refresh(src, now: datetime, force: bool = False) -> dict:
    """Return the panel, rebuilding it if it's stale or the panel settings changed.

    Each anchor group (e.g. "tech culture", "GTM") fills its share of the panel with the
    accounts most followed by that group's anchors, so one group can't crowd out the other.
    """
    p, panel = config.PANEL, load()
    fresh = panel.get("built_at") and now - datetime.fromisoformat(panel["built_at"]) < timedelta(days=p["refresh_days"])
    if panel and fresh and panel.get("settings") == _settings_key() and not force:
        return panel

    exclude = {h.lower().lstrip("@") for h in p["exclude"]}
    info: dict[str, dict] = {}
    per_group: list[tuple[dict, collections.Counter, int]] = []
    loaded = []
    for g in p["groups"]:
        counts, ok = collections.Counter(), 0
        for a in [h.lstrip("@") for h in g["accounts"] if h.strip()]:
            try:
                follows = src.followings(a, max_pages=p["max_pages_per_anchor"])
            except Exception as e:
                print(f"  panel: couldn't load who @{a} follows ({e})")
                continue
            ok += 1
            loaded.append(a)
            print(f"  panel [{g['name']}]: @{a} follows {len(follows)}")
            for u in follows:
                counts[u["handle"].lower()] += 1
                info[u["handle"].lower()] = u
        per_group.append((g, counts, ok))

    total_share = sum(g["share"] for g, _, _ in per_group) or 1
    chosen: dict[str, dict] = {}
    for g, counts, ok in per_group:
        slots = round(p["max_accounts"] * g["share"] / total_share)
        need = min(p["min_overlap"], max(1, ok))
        ranked = sorted((k for k, c in counts.items() if c >= need and k not in exclude and k not in chosen),
                        key=lambda k: (-counts[k], -info[k]["followers"]))[:slots]
        for k in ranked:
            chosen[k] = {"handle": info[k]["handle"], "group": g["name"], "overlap": counts[k],
                         "followers": info[k]["followers"]}
    for g in p["groups"]:  # anchors and hand-picked accounts are always on the panel
        for h in g["accounts"]:
            chosen.setdefault(h.lower().lstrip("@"), {"handle": h.lstrip("@"), "group": g["name"], "overlap": 0,
                                                      "followers": info.get(h.lower(), {}).get("followers", 0)})
    for h in p["include"]:
        chosen.setdefault(h.lower().lstrip("@"), {"handle": h.lstrip("@"), "group": "added", "overlap": 0, "followers": 0})
    accounts = [a for k, a in chosen.items() if k not in exclude]
    panel = {"built_at": now.isoformat(), "settings": _settings_key(), "anchors_loaded": loaded, "accounts": accounts}
    PANEL_PATH.parent.mkdir(exist_ok=True)
    PANEL_PATH.write_text(json.dumps(panel, indent=1))
    by_group = collections.Counter(a["group"] for a in accounts)
    print(f"  panel: {len(accounts)} accounts {dict(by_group)} from {len(loaded)} anchors")
    return panel


def handles(panel: dict) -> list[str]:
    return [a["handle"] for a in panel.get("accounts", [])]
