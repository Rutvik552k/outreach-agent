"""OAuth login — ADR §4 (authorization-code + PKCE S256, V6 loopback hardening).

Ground truth (ADR §4): GitHub PKCE S256 added 2025-07-14; client secret still
required even with PKCE; loopback literal 127.0.0.1 recommended over
localhost. V6 hardening, all enforced here: bind 127.0.0.1 only (never
0.0.0.0), accept exactly one request, validate the single-use
cryptographically random state before any exchange, short listener timeout.

This module contains NO HTTP client (C-1 allowlist): the token exchange is an
injected callable implemented in tokens.py. Device flow is documented
fallback only — not built (ADR §4).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

from .config import Config
from .errors import OAuthError

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"


@dataclass(frozen=True)
class PkcePair:
    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce() -> PkcePair:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(*, client_id: str, redirect_uri: str, state: str,
                        challenge: str, scopes: tuple[str, ...]) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": " ".join(scopes),
    })


@dataclass
class _Callback:
    code: str | None = None
    error: str | None = None


def _make_handler(expected_state: str, result: _Callback) -> type[BaseHTTPRequestHandler]:
    state_used = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            params = parse_qs(urlparse(self.path).query)
            state = (params.get("state") or [""])[0]
            code = (params.get("code") or [""])[0]
            if state_used.is_set():
                result.error = "state already consumed (single-use, V6)"
            elif not state or not hmac.compare_digest(state, expected_state):
                result.error = "state mismatch — possible CSRF/interception (V6)"
            elif not code:
                result.error = (params.get("error") or ["no code in callback"])[0]
            else:
                state_used.set()
                result.code = code
            self.send_response(200 if result.code else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"outreach-agent: login complete, you can close this tab."
                if result.code else b"outreach-agent: login failed."
            )

        def log_message(self, *args: object) -> None:  # silence stdlib logging
            pass

    return Handler


class TokenExchanger(Protocol):
    def __call__(self, *, code: str, code_verifier: str, redirect_uri: str) -> str: ...


def run_login_flow(
    *,
    config: Config,
    client_id: str,
    exchange: TokenExchanger,
    open_browser: Callable[[str], None],
    store_token: Callable[[str], None],
) -> None:
    """V6-hardened loopback flow. The server binds 127.0.0.1:<ephemeral>,
    serves exactly one request, and dies."""
    pkce = generate_pkce()
    state = generate_state()
    result = _Callback()

    server = HTTPServer(("127.0.0.1", 0), _make_handler(state, result))
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    server.timeout = config.oauth_listener_timeout_s

    try:
        open_browser(build_authorize_url(
            client_id=client_id, redirect_uri=redirect_uri, state=state,
            challenge=pkce.challenge, scopes=config.oauth_scopes,
        ))
        server.handle_request()  # exactly one request (V6)
    except socket.timeout as exc:
        raise OAuthError(
            f"loopback listener timed out after {config.oauth_listener_timeout_s}s (V6)"
        ) from exc
    finally:
        server.server_close()

    if result.code is None:
        raise OAuthError(f"authorization failed: {result.error or 'no callback received'}")

    token = exchange(code=result.code, code_verifier=pkce.verifier,
                     redirect_uri=redirect_uri)
    if not token:
        raise OAuthError("token endpoint returned no access_token")
    store_token(token)
