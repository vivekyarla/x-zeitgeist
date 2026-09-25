"""The thesis's key phrases and which theme each one points to.

The writer marks key phrases as **phrase**{n}, where n is the 1-based theme number.
Older theses (or a missing/bad {n}) fall back to the theme whose name, summary, and
tweets share the most words with the phrase.
"""
from __future__ import annotations

import re

PHRASE = re.compile(r"\*\*(.+?)\*\*(?:\{(\d+)\})?")
_WORD = re.compile(r"[a-z0-9$][a-z0-9$.\-/]*")
_STOP = {"the", "and", "for", "with", "its", "new", "from", "that", "this", "into", "over", "are", "all"}


def plain(text: str) -> str:
    return PHRASE.sub(lambda m: m.group(1), text)


def _words(s: str) -> set[str]:
    return {w.strip(".-/") for w in _WORD.findall(s.lower()) if len(w) > 2 and w not in _STOP}


def _best_theme(phrase: str, themes: list[dict]) -> int | None:
    want = _words(phrase)
    best, best_score = None, 0.0
    for i, th in enumerate(themes):
        head = _words(th.get("name", "") + " " + th.get("summary", ""))
        body = _words(" ".join(t.get("text", "") for t in th.get("tweets", [])))
        score = 2 * len(want & head) + len(want & body)
        if score > best_score:
            best, best_score = i, score
    return best


def segments(thesis: str, themes: list[dict]) -> list[tuple[str, bool, int | None]]:
    """[(text, is_key_phrase, theme_index_or_None)], theme index 0-based."""
    out, pos = [], 0
    for m in PHRASE.finditer(thesis):
        if m.start() > pos:
            out.append((thesis[pos:m.start()], False, None))
        idx = int(m.group(2)) - 1 if m.group(2) else None
        if idx is None or not 0 <= idx < len(themes):
            idx = _best_theme(m.group(1), themes)
        out.append((m.group(1), True, idx))
        pos = m.end()
    if pos < len(thesis):
        out.append((thesis[pos:], False, None))
    return out
