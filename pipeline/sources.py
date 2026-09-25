"""Where tweets come from.

Everything downstream only sees the normalized `Tweet` dict, so switching to the
official X API later means writing one new class with a `search()` method.
"""
from __future__ import annotations

import os
import time
from typing import Iterable, Protocol

import requests


def normalize(raw: dict) -> dict:
    """Map a provider's tweet object onto the fields the pipeline uses."""
    author = raw.get("author") or {}
    handle = author.get("userName") or ""
    tid = str(raw.get("id") or "")
    return {
        "id": tid,
        "url": raw.get("url") or (f"https://x.com/{handle}/status/{tid}" if handle else ""),
        "text": raw.get("text") or "",
        "created_at": raw.get("createdAt") or "",
        "likes": int(raw.get("likeCount") or 0),
        "retweets": int(raw.get("retweetCount") or 0),
        "replies": int(raw.get("replyCount") or 0),
        "quotes": int(raw.get("quoteCount") or 0),
        "views": int(raw.get("viewCount") or 0),
        "is_retweet": bool(raw.get("retweeted_tweet")),
        "is_reply": bool(raw.get("isReply")),
        "author": {
            "handle": handle,
            "name": author.get("name") or handle,
            "followers": int(author.get("followers") or 0),
            "verified": bool(author.get("isBlueVerified")),
            "avatar": author.get("profilePicture") or "",
        },
    }


class TweetSource(Protocol):
    def search(self, query: str, query_type: str, pages: int) -> Iterable[dict]: ...


class TwitterApiIo:
    """https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search"""

    BASE = "https://api.twitterapi.io/twitter/tweet/advanced_search"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["TWITTERAPI_IO_KEY"]
        self.session = requests.Session()
        self.session.headers["x-api-key"] = self.api_key

    def _get(self, params: dict) -> dict:
        for attempt in range(4):
            try:
                r = self.session.get(self.BASE, params=params, timeout=45)
            except requests.RequestException:
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def last_tweets(self, handle: str) -> list[dict]:
        """An account's ~20 most recent original tweets (no replies or retweets)."""
        for attempt in range(3):
            try:
                r = self.session.get("https://api.twitterapi.io/twitter/user/last_tweets",
                                     params={"userName": handle, "includeReplies": "false"}, timeout=45)
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            break
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "error":
            raise RuntimeError(data.get("message") or "error")
        raw = data.get("tweets") or (data.get("data") or {}).get("tweets") or []
        return [t for t in map(normalize, raw) if t["id"] and not t["is_retweet"] and not t["is_reply"]]

    def search(self, query: str, query_type: str = "Top", pages: int = 2):
        cursor = ""
        for _ in range(pages):
            data = self._get({"query": query, "queryType": query_type, "cursor": cursor})
            for raw in data.get("tweets") or []:
                t = normalize(raw)
                if t["id"] and t["text"]:
                    yield t
            if not data.get("has_next_page") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]
