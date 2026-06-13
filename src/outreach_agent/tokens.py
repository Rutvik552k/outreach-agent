"""Token source seam (NFR-3) + OAuth token exchange + login resolution.

keyring (Windows Credential Manager) backs the production source. The token
exchange lives HERE (not oauth.py) because this module is on the C-1 HTTP
client allowlist; it talks to GitHub's OAuth token endpoint plus exactly ONE
REST read — ``GET /user`` in :func:`fetch_login` (GAP-2). The C5 gateway
cannot serve that read: constructing the gateway requires the resolved login
(``agent_login``/``fork_owner``), which is exactly what ``auth login`` is
trying to learn — a bootstrap chicken-and-egg. Every OTHER REST call remains
C5's exclusive surface.
"""

from __future__ import annotations

from typing import Protocol

from .errors import CredentialError, OAuthError
from .outbound_safety import register_secret_value

SERVICE_NAME = "outreach-agent"
TOKEN_ENDPOINT = "https://github.com/login/oauth/access_token"
USER_ENDPOINT = "https://api.github.com/user"

# DEF-002: per-credential remediation. Each entry points at the step that
# actually produces the credential — never at re-running the command that just
# failed. The OAuth App client id/secret are a ONE-TIME registration stored
# directly in the credential manager (there is no CLI command that creates
# them; `auth login` *consumes* them — see oauth.py / cmd_auth_login), so the
# guidance is the `keyring` CLI that ships with this venv's keyring package.
_REMEDIATION: dict[str, str] = {
    "github_oauth_token": "run `outreach-agent auth login` to authorize and store a token",
    "github_oauth_client_id": (
        "one-time setup: register a GitHub OAuth App "
        "(GitHub > Settings > Developer settings > OAuth Apps, loopback "
        "callback http://127.0.0.1/callback per ADR §4), then store its "
        "client id with `keyring set outreach-agent github_oauth_client_id`"
    ),
    "github_oauth_client_secret": (
        "one-time setup: store your registered GitHub OAuth App's client "
        "secret with `keyring set outreach-agent github_oauth_client_secret` "
        "(generate it on the OAuth App page if you have not yet)"
    ),
    "anthropic_api_key": (
        "store your Anthropic API key with "
        "`keyring set outreach-agent anthropic_api_key`"
    ),
}


class TokenSource(Protocol):
    def github_token(self) -> str: ...
    def anthropic_api_key(self) -> str: ...


class KeyringTokenSource:
    def __init__(self, service: str = SERVICE_NAME) -> None:
        self.service = service

    def _get(self, name: str) -> str:
        import keyring

        value = keyring.get_password(self.service, name)
        if not value:
            # DEF-001: typed OutreachError subtype (CredentialError), never a
            # bare LookupError — main() catches OutreachError and prints one
            # sanitized line; anything else would leak a traceback.
            remediation = _REMEDIATION.get(
                name, f"store it with `keyring set {self.service} {name}`"
            )
            raise CredentialError(
                f"credential {name!r} not found in Windows Credential Manager "
                f"(service {self.service!r}); {remediation}"
            )
        # M-1: every credential loaded into the process is value-registered so
        # the LLM outbound guard can redact it by exact value (fail-closed).
        register_secret_value(value)
        return value

    def _set(self, name: str, value: str) -> None:
        import keyring

        register_secret_value(value)  # M-1: see _get
        keyring.set_password(self.service, name, value)

    def github_token(self) -> str:
        return self._get("github_oauth_token")

    def anthropic_api_key(self) -> str:
        return self._get("anthropic_api_key")

    def oauth_client_id(self) -> str:
        return self._get("github_oauth_client_id")

    def oauth_client_secret(self) -> str:
        return self._get("github_oauth_client_secret")

    def store_github_token(self, token: str) -> None:
        self._set("github_oauth_token", token)


def exchange_oauth_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    timeout_s: float = 30.0,
) -> str:
    """POST the authorization code + PKCE verifier to GitHub's token endpoint
    (ADR §4: client secret required even with PKCE). Returns the access token."""
    import httpx

    # M-1: the client secret has no fixed prefix the deny-regex could match;
    # exact-value registration is what makes the outbound guard cover it.
    register_secret_value(client_secret)
    resp = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
        timeout=timeout_s,
    )
    if resp.status_code != 200:
        raise OAuthError(f"token endpoint returned {resp.status_code}")
    payload = resp.json()
    token = payload.get("access_token")
    if not token:
        raise OAuthError(f"token exchange failed: {payload.get('error', 'no token')}")
    register_secret_value(str(token))  # M-1: registered at mint, not just at store
    return str(token)


def fetch_login(token: str, *, timeout_s: float = 30.0) -> str:
    """GAP-2: resolve the authenticated user's login for ``auth login``.

    ``GET /user`` returns the account's ``login`` (ground truth: githubkit
    0.15.5 installed source — rest/users.py:87-98 ``users/get-authenticated``
    → ``GET /user``; models/group_0470.py:28 ``login: str = Field()``).
    Bearer auth, explicit timeout, typed OAuthError on any failure so the
    CLI's sanitized handler prints one line — never a traceback, never the
    token.
    """
    import httpx

    try:
        resp = httpx.get(
            USER_ENDPOINT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise OAuthError(
            f"login resolution failed: GET /user transport error "
            f"({type(exc).__name__})"
        ) from exc
    if resp.status_code != 200:
        raise OAuthError(
            f"login resolution failed: GET /user returned {resp.status_code}"
        )
    try:
        login = resp.json().get("login")
    except ValueError as exc:
        raise OAuthError(
            "login resolution failed: GET /user returned non-JSON body"
        ) from exc
    if not login:
        raise OAuthError("login resolution failed: GET /user response has no login")
    return str(login)


class StaticTokenSource:
    """Test/dev seam — never used in production paths."""

    def __init__(self, github: str = "test-token", anthropic: str = "test-key") -> None:
        self._github = github
        self._anthropic = anthropic

    def github_token(self) -> str:
        return self._github

    def anthropic_api_key(self) -> str:
        return self._anthropic
