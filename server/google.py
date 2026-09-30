"""Sign in with Google (OpenID Connect, authorization code flow with PKCE).

Only the `openid email profile` scopes are requested: Google's basic sign-in scopes, which
share nothing beyond the account's name, email, and profile picture. No refresh token is asked
for (access_type=online) and the access token Google returns is discarded unused; the app keeps
only the account's stable id (`sub`) and email.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "openid email profile"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class GoogleError(Exception):
    """A sentence to show the person on the sign-in page."""


def start(client_id: str, redirect_uri: str, domains: tuple[str, ...]) -> tuple[str, dict]:
    """The URL to send the browser to, and what to keep in the session until the callback."""
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
              "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256",
              "access_type": "online", "prompt": "select_account"}
    if len(domains) == 1:
        params["hd"] = domains[0]  # a hint for the account chooser; the real check is in claims()
    return f"{AUTH_URL}?{urlencode(params)}", {"state": state, "nonce": nonce, "verifier": verifier}


def exchange(client_id: str, client_secret: str, redirect_uri: str, code: str, verifier: str) -> dict:
    """Trade the code for an ID token and return its claims.

    The token comes straight from Google's token endpoint over TLS, which OpenID Connect
    (Core 3.1.3.7) accepts in place of checking the signature; claims() checks the rest.
    """
    try:
        r = requests.post(TOKEN_URL, timeout=15, data={
            "code": code, "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    except requests.RequestException:
        raise GoogleError("Couldn't reach Google. Try again in a moment.")
    if r.status_code != 200:
        raise GoogleError("Google didn't accept the sign-in. Try again.")
    token = (r.json() or {}).get("id_token", "")
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise GoogleError("Google sent back something unexpected. Try again.")


def claims(c: dict, client_id: str, nonce: str, domains: tuple[str, ...]) -> tuple[str, str, str]:
    """Check the ID token's claims; returns (sub, email, name)."""
    if c.get("iss") not in ISSUERS or c.get("aud") != client_id or c.get("nonce") != nonce:
        raise GoogleError("That sign-in wasn't meant for this site. Try again.")
    if float(c.get("exp", 0)) < time.time():
        raise GoogleError("That sign-in expired. Try again.")
    email = str(c.get("email", "")).strip().lower()
    if not c.get("sub") or not email or c.get("email_verified") not in (True, "true"):
        raise GoogleError("Your Google account needs a verified email address.")
    if domains:
        # `hd` is set only for Google Workspace accounts, so a personal Google account that
        # happens to use a work address doesn't count.
        hd = str(c.get("hd", "")).lower()
        if hd not in domains or email.rsplit("@", 1)[-1] not in domains:
            raise GoogleError(f"Sign in with your {' or '.join('@' + d for d in domains)} Google account.")
    return str(c["sub"]), email, str(c.get("name", ""))[:200]
