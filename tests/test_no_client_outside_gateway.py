"""C-1 [BLOCKER] — structural-incapability guard (sign-off v2.1, C4 v2.1).

Two layers, both CI-enforced (this file runs in the default pytest lane AND
as its own explicitly-named CI job — see .github/workflows/ci.yml):
1. Build-breaking scan: no HTTP-client-capable import — third-party OR
   stdlib — and no dynamic-import mechanism anywhere in src outside the
   per-file C5 allowances. This is the load-bearing control that makes "the
   gateway is the only path to the GitHub client" true beyond the happy path.
2. Capability assertions: GitHubGateway exposes no method capable of adding
   approval labels or posting /approve-class comments on an existing fork
   draft PR.

H-1 hardening (audit step 6) — scanner scope and rationale:

* FORBIDDEN third-party transports: the whole root package is banned, even
  for packages NOT currently installed (requests/aiohttp/...): banning them
  is free and keeps the rule correct if the dependency set ever grows.
* FORBIDDEN stdlib modules: `urllib.request`, `http.client` (the audit's
  minimum — always-present HTTP clients), plus every other stdlib module
  that can open an outbound network connection by itself: `socket` (raw
  TCP is enough to speak HTTP), `asyncio` (open_connection — nothing in
  src needs asyncio; add a per-file allowance deliberately if that ever
  changes), `ftplib`/`smtplib`/`poplib`/`imaplib`/`telnetlib` (exfil-capable
  clients), and `xmlrpc` (wraps http.client). `ssl` alone is NOT banned: it
  cannot connect without a socket, and banning it adds noise without
  capability removal. Bare `import urllib` / `import http` are also banned:
  legitimate code always imports the specific safe submodule
  (`urllib.parse`, `http.server`), and a bare package import is only useful
  as a reach-through to the client submodules.
* DYNAMIC-IMPORT MECHANISM is banned outright (any `importlib` import,
  any `__import__` reference, any `.import_module()` /
  `.spec_from_file_location()` / `.module_from_spec()` call): static
  analysis cannot resolve a dynamic import's TARGET
  (`import_module("ht"+"tpx")`), so the only sound rule is to ban the
  mechanism itself. Nothing in src uses dynamic import; if it ever must,
  that is a deliberate, reviewed allowance — not a default.
* CEILING (documented honestly, per the audit): an AST scan cannot stop a
  determined in-process adversary — `eval`/`exec`, getattr-chains over
  already-imported modules (e.g. `http.server` internally imports
  `http.client`), or C extensions are out of reach. That residual is
  exactly what sign-off C-5 (hash-pinned venv, `--require-hashes` install,
  enforced in CI and scripts/check.ps1) exists to close: the scanner stops
  accidental/refactor bypasses; hash-pinning stops the malicious-dep ones.
* Allowances are keyed by path RELATIVE to src (not bare filename), so a
  rogue `sub/tokens.py` gains nothing. Allowlisted files are still scanned
  for everything outside their narrow allowance.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from conftest import FORK, UPSTREAM, FakeGitHubClient
from outreach_agent.errors import StructuralIncapabilityError
from outreach_agent.github_gateway import GitHubGateway

SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "outreach_agent"

# Per-file allowances (relative posix path → transport roots it may import).
# github_gateway.py is the C5 chokepoint; tokens.py talks ONLY to the OAuth
# token endpoint; oauth.py runs the local loopback listener (socket only —
# its http.server / urllib.parse imports are not banned modules).
ALLOWANCES: dict[str, set[str]] = {
    "github_gateway.py": {"githubkit"},
    "tokens.py": {"httpx"},
    "oauth.py": {"socket"},
}

# Whole-root bans (match root or any submodule). See module docstring.
FORBIDDEN_ROOTS = {
    # third-party HTTP/network transports (installed or not)
    "httpx", "httpcore", "githubkit", "requests", "urllib3", "aiohttp",
    "anyio", "h11", "h2", "pycurl", "websockets", "websocket",
    # stdlib network-client-capable modules
    "urllib.request", "http.client", "socket", "asyncio",
    "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
    # dynamic-import mechanism (target-unresolvable → ban the mechanism)
    "importlib",
}
# Bare package imports that only serve as reach-through to banned submodules.
FORBIDDEN_BARE = {"urllib", "http"}
# Dynamic-import call attributes flagged anywhere (catches aliased importlib).
DYNAMIC_IMPORT_CALL_ATTRS = {
    "import_module", "spec_from_file_location", "module_from_spec",
}


def _matches(name: str, roots: set[str]) -> bool:
    return any(name == r or name.startswith(r + ".") for r in roots)


def scan_for_client_imports(src_dir: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(src_dir.rglob("*.py")):
        rel = path.relative_to(src_dir).as_posix()
        allowed = ALLOWANCES.get(rel, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                # check the module itself AND each `from m import x` as `m.x`
                # (catches `from http import client` / `from urllib import request`)
                names = [node.module] + [
                    f"{node.module}.{alias.name}" for alias in node.names
                ]
            for name in names:
                bare_hit = isinstance(node, ast.Import) and name in FORBIDDEN_BARE
                if (bare_hit or _matches(name, FORBIDDEN_ROOTS)) \
                        and not _matches(name, allowed):
                    violations.append(f"{rel}:{node.lineno}: import {name}")
                    break  # one violation per import node is enough
            if isinstance(node, ast.Name) and node.id == "__import__":
                violations.append(
                    f"{rel}:{node.lineno}: __import__ reference "
                    "(dynamic-import mechanism banned, H-1)"
                )
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in DYNAMIC_IMPORT_CALL_ATTRS):
                violations.append(
                    f"{rel}:{node.lineno}: call to .{node.func.attr}() "
                    "(dynamic-import mechanism banned, H-1)"
                )
    return violations


def test_no_http_client_import_outside_gateway() -> None:
    """The build-breaking rule itself: a violating import anywhere in src
    fails this test, which fails CI."""
    violations = scan_for_client_imports(SRC_DIR)
    assert violations == [], (
        "HTTP client / githubkit imported outside the C5 gateway allowlist "
        f"(C-1 BLOCKER): {violations}"
    )


def test_scanner_detects_violating_import(tmp_path: Path) -> None:
    """Positive proof the scanner bites: a raw label-add via githubkit or an
    httpx import outside C5 is detected and would fail the build."""
    bad = tmp_path / "rogue.py"
    bad.write_text(
        "import httpx\n"
        "from githubkit import GitHub\n"
        "def self_approve(token, fork, n):\n"
        "    GitHub(token).rest.issues.add_labels(\n"
        "        *fork.split('/'), n, data=['agent:approve-upstream'])\n",
        encoding="utf-8",
    )
    (tmp_path / "clean.py").write_text("import json\n", encoding="utf-8")
    violations = scan_for_client_imports(tmp_path)
    assert len(violations) == 2
    assert any("httpx" in v for v in violations)
    assert any("githubkit" in v for v in violations)


# -- H-1 negative tests: the audit's three named bypass classes ----------------


def test_scanner_catches_dynamic_import_of_httpx(tmp_path: Path) -> None:
    """H-1 bypass class 1: `importlib.import_module("httpx")` is an ast.Call,
    not an ast.Import — the old scanner was silent. Both the importlib import
    AND the .import_module() call must now be flagged."""
    (tmp_path / "rogue.py").write_text(
        "import importlib\n"
        'client = importlib.import_module("httpx")\n',
        encoding="utf-8",
    )
    violations = scan_for_client_imports(tmp_path)
    assert any("import importlib" in v for v in violations)
    assert any("import_module" in v for v in violations)


def test_scanner_catches_aliased_dynamic_import(tmp_path: Path) -> None:
    """Aliasing importlib does not evade: the import is flagged and the
    attribute call is flagged independently of the receiver name."""
    (tmp_path / "rogue.py").write_text(
        "import importlib as il\n"
        'gh = il.import_module("github" "kit")\n',
        encoding="utf-8",
    )
    violations = scan_for_client_imports(tmp_path)
    assert len(violations) >= 2


def test_scanner_catches_dunder_import(tmp_path: Path) -> None:
    """H-1 bypass class 2: `__import__("urllib.request")` — any reference to
    __import__ (call OR aliasing like `f = __import__`) is flagged."""
    (tmp_path / "rogue.py").write_text(
        'mod = __import__("urllib.request")\n', encoding="utf-8",
    )
    violations = scan_for_client_imports(tmp_path)
    assert any("__import__" in v for v in violations)
    (tmp_path / "rogue.py").write_text("f = __import__\n", encoding="utf-8")
    assert any("__import__" in v for v in scan_for_client_imports(tmp_path))


def test_scanner_catches_stdlib_http_roots(tmp_path: Path) -> None:
    """H-1 bypass class 3: stdlib HTTP clients are always installed. All of
    `from http import client`, `import urllib.request`, `import socket`, and
    bare `import urllib` must be flagged outside the allowances."""
    cases = (
        "from http import client\n",
        "import urllib.request\n",
        "from urllib.request import urlopen\n",
        "import socket\n",
        "import urllib\n",
        "import http\n",
        "import asyncio\n",
    )
    for code in cases:
        (tmp_path / "rogue.py").write_text(code, encoding="utf-8")
        assert scan_for_client_imports(tmp_path), f"NOT flagged: {code!r}"


def test_scanner_allows_safe_net_adjacent_stdlib(tmp_path: Path) -> None:
    """The narrow legitimate set stays importable anywhere: urllib.parse and
    http.server are not client-capable bans (oauth.py relies on them)."""
    (tmp_path / "fine.py").write_text(
        "from urllib.parse import parse_qs, urlencode, urlparse\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "import json\n",
        encoding="utf-8",
    )
    assert scan_for_client_imports(tmp_path) == []


def test_allowances_are_relative_path_keyed(tmp_path: Path) -> None:
    """A rogue file MERELY NAMED like an allowlisted one, in a subdirectory,
    gains no allowance."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "tokens.py").write_text("import httpx\n", encoding="utf-8")
    violations = scan_for_client_imports(tmp_path)
    assert any("sub/tokens.py" in v for v in violations)


# -- gateway capability surface (closed set) ----------------------------------

EXPECTED_MUTATIONS = {
    "fork_repo", "create_draft_pr_on_fork", "close_fork_draft_pr",
    "create_upstream_pr", "comment", "reply_to_review_comment",
}
EXPECTED_READS = {
    "search_issues", "get_pr", "list_review_comments", "get_commit",
    "list_commits", "get_timeline_events", "get_repo_file", "mutation_landed",
    # Step 4b deliberate additions, re-reviewed against C4 v2.1 — both are
    # READS with no label/approval capability:
    # - list_pr_reviews: changes-requested detection (§2[6]),
    #   GET .../pulls/{n}/reviews (githubkit rest/pulls.py:2729).
    # - list_own_repos: profile-growth repo data (§2[7]),
    #   GET /user/repos (githubkit rest/repos.py:21979).
    "list_pr_reviews", "list_own_repos",
    # I-1 fix (audit step 6) deliberate addition, re-reviewed against C4 v2.1 —
    # READ ONLY, no label or approval capability added:
    # - get_repo_default_branch: upstream PR base resolution so approve-sync
    #   never assumes "main"; GET /repos/{owner}/{repo} → default_branch
    #   (githubkit 0.15.5 rest/repos.py:1529, models/group_0187.py:93).
    "get_repo_default_branch",
}


def _public_methods() -> set[str]:
    return {
        name for name in vars(GitHubGateway)
        if not name.startswith("_") and callable(getattr(GitHubGateway, name))
    }


def test_gateway_mutation_surface_is_closed_set() -> None:
    """C5: the mutation surface is a closed set. Any new method must be added
    here deliberately and re-reviewed against C4 v2.1."""
    assert _public_methods() == EXPECTED_MUTATIONS | EXPECTED_READS


def test_gateway_has_no_label_capability() -> None:
    """No gateway method can add ANY label (the C-2 coarse rule makes any
    agent label mutation on the draft fatal, so the capability is absent
    entirely; the awaiting-approval marker lives in the draft title/body)."""
    assert not any("label" in name.lower() for name in _public_methods())


def test_comment_on_fork_draft_refused(gateway: GitHubGateway,
                                       fake_client: FakeGitHubClient) -> None:
    """The generic comment mutation cannot target the fork owner's repos —
    no agent comment may ever land on an existing fork draft PR (C4 v2.1)."""
    with pytest.raises(StructuralIncapabilityError):
        gateway.comment(repo_full_name=FORK, issue_number=7, body="status note")
    assert not any(c[0] == "create_issue_comment" for c in fake_client.calls)


def test_approve_command_comment_refused_anywhere(gateway: GitHubGateway,
                                                  fake_client: FakeGitHubClient) -> None:
    """Defense in depth: approval-class command bodies are refused even on
    upstream targets."""
    for body in ("/approve", "thanks!\n/approve", "/reject"):
        with pytest.raises(StructuralIncapabilityError):
            gateway.comment(repo_full_name=UPSTREAM, issue_number=5, body=body)
    with pytest.raises(StructuralIncapabilityError):
        gateway.reply_to_review_comment("acme", "some-lib", 9, 1, "/approve")
    assert not any(c[0] in ("create_issue_comment", "create_review_comment_reply")
                   for c in fake_client.calls)


def test_approve_command_evasions_refused(gateway: GitHubGateway,
                                          fake_client: FakeGitHubClient) -> None:
    """M-2 hardening: NFKC + zero-width-strip + anywhere-in-body matching.
    Fullwidth solidus, zero-width-split command, and mid-line embedding are
    all refused."""
    evasions = (
        "／approve",                        # fullwidth solidus homoglyph
        "looks good\nplease ／approve now",
        "/app\u200brove",                       # zero-width-split command
        "I think we should /approve this one",  # mid-line, not a line prefix
        "fine by me — /reject-reply",           # reply-token variant
    )
    for body in evasions:
        with pytest.raises(StructuralIncapabilityError):
            gateway.comment(repo_full_name=UPSTREAM, issue_number=5, body=body)
    assert not any(c[0] == "create_issue_comment" for c in fake_client.calls)


def test_legitimate_upstream_comment_still_allowed(gateway: GitHubGateway,
                                                   fake_client: FakeGitHubClient) -> None:
    result = gateway.comment(repo_full_name=UPSTREAM, issue_number=5,
                             body="Reproduced on 3.12; minimal repro attached.")
    assert result["id"]
    assert any(c[0] == "create_issue_comment" for c in fake_client.calls)
