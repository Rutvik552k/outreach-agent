from __future__ import annotations

import base64
import hashlib
import threading
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest

from outreach_agent.config import Config
from outreach_agent.errors import OAuthError
from outreach_agent.oauth import build_authorize_url, generate_pkce, run_login_flow


def test_pkce_is_s256(config: Config) -> None:
    pair = generate_pkce()
    assert 43 <= len(pair.verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pair.verifier.encode()).digest()).rstrip(b"=").decode()
    assert pair.challenge == expected
    assert pair.method == "S256"


def test_authorize_url_contains_pkce_and_state(config: Config) -> None:
    url = build_authorize_url(
        client_id="cid", redirect_uri="http://127.0.0.1:1234/callback",
        state="st4te", challenge="ch4llenge", scopes=config.oauth_scopes,
    )
    params = parse_qs(urlparse(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == ["st4te"]
    assert params["scope"] == ["public_repo user:email"]
    assert url.startswith("https://github.com/login/oauth/authorize?")


def test_loopback_flow_happy_path(config: Config) -> None:
    captured: dict[str, str] = {}
    exchanged: dict[str, str] = {}
    stored: list[str] = []

    def open_browser(url: str) -> None:
        params = parse_qs(urlparse(url).query)
        redirect = params["redirect_uri"][0]
        state = params["state"][0]
        captured["redirect"] = redirect

        def hit() -> None:
            urllib.request.urlopen(f"{redirect}?code=authcode&state={state}", timeout=10)

        threading.Thread(target=hit, daemon=True).start()

    def exchange(*, code: str, code_verifier: str, redirect_uri: str) -> str:
        exchanged.update(code=code, verifier=code_verifier, uri=redirect_uri)
        return "gho_token"

    run_login_flow(
        config=config, client_id="cid", exchange=exchange,
        open_browser=open_browser, store_token=stored.append,
    )
    assert stored == ["gho_token"]
    assert exchanged["code"] == "authcode"
    assert exchanged["uri"] == captured["redirect"]
    assert captured["redirect"].startswith("http://127.0.0.1:")  # V6: loopback literal


def test_state_mismatch_rejected(config: Config) -> None:
    """V6: forged/foreign state → no exchange, OAuthError."""
    def open_browser(url: str) -> None:
        params = parse_qs(urlparse(url).query)
        redirect = params["redirect_uri"][0]

        def hit() -> None:
            try:
                urllib.request.urlopen(
                    f"{redirect}?code=stolen&state=WRONG", timeout=10)
            except urllib.error.HTTPError:
                pass  # 400 expected

        threading.Thread(target=hit, daemon=True).start()

    exchange_calls: list[str] = []
    with pytest.raises(OAuthError, match="state mismatch"):
        run_login_flow(
            config=config, client_id="cid",
            exchange=lambda **kw: exchange_calls.append("x") or "t",  # type: ignore[arg-type]
            open_browser=open_browser, store_token=lambda t: None,
        )
    assert exchange_calls == []  # exchange never attempted
