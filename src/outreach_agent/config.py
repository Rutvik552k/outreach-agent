"""Configuration defaults (ADR-001 §3, §5, §6, §7) and the F-07 sync-root fail-fast check."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SyncRootError

_SYNC_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def _default_db_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "outreach-agent" / "state.db"


@dataclass(frozen=True)
class Config:
    db_path: Path = field(default_factory=_default_db_path)
    # C8 / FM8 — per-phase wall clocks (v2.4 two-phase): Phase R (resolve,
    # network on) and Phase X (execute, network none) each get their own kill.
    sandbox_wall_timeout_s: int = 900
    sandbox_resolve_timeout_s: int = 300
    sandbox_image: str = "outreach-agent-sandbox:latest"
    sandbox_cpus: str = "2"
    sandbox_memory: str = "2g"
    sandbox_pids_limit: int = 256
    # V5
    diff_cap_changed_lines: int = 400
    # §7
    model: str = "claude-opus-4-8"
    triage_model: str = "claude-haiku-4-5"
    llm_timeout_s: int = 120
    llm_monthly_spend_cap_usd: float = 60.0
    # NFR-7 (user decision 2026-06-12): default generation backend is the
    # Claude Code CLI on the host (subscription, headless `claude -p`);
    # "anthropic" remains available when an API key is stored in the keyring.
    # The F-13 monthly spend cap gates the anthropic backend ONLY — the
    # claude-code backend is subscription-backed and records 0-cost entries.
    llm_backend: str = "claude-code"  # ∈ {"claude-code", "anthropic"}
    claude_cli_executable: str = "claude"
    claude_cli_timeout_s: int = 300
    # §5 self-imposed caps (~10% of platform secondary limits)
    content_creation_per_min: int = 8
    content_creation_per_hr: int = 50
    upstream_pr_per_day: int = 1
    min_mutation_spacing_s: float = 2.0
    backoff_initial_s: float = 60.0
    backoff_max_retries: int = 3
    secondary_hit_kill_count: int = 2
    secondary_hit_window_h: int = 24
    # C5
    github_timeout_s: float = 30.0
    # §8
    merge_rate_pause_threshold: float = 0.35
    merge_rate_window: int = 10
    merge_rate_min_outcomes: int = 5
    # C4 labels/commands. label_awaiting is the marker text used in the draft
    # PR title — NEVER applied as a label mutation (C4 v2.2: any agent label
    # mutation on the draft makes it ineligible under the C-2 coarse rule, so
    # the gateway has no label capability at all).
    label_awaiting: str = "agent:awaiting-approval"
    label_approve: str = "agent:approve-upstream"
    label_reject: str = "agent:reject"
    comment_approve: str = "/approve"
    comment_reject: str = "/reject"
    # §2[6] review-reply approval commands (C4 machinery reused on the UPSTREAM
    # PR timeline; the reply target is distinct from draft-PR approval). Both
    # start with "/approve"//"/reject", so the gateway's structural-
    # incapability prefix check (C-1) already refuses agent-emitted bodies.
    comment_approve_reply: str = "/approve-reply"
    comment_reject_reply: str = "/reject-reply"
    # §2[7] profile growth: GitHub shows at most 6 pinned repos; pinning is
    # GraphQL-only (no REST endpoint in githubkit 0.15.5 source) → the
    # recommendation is text-only, applied manually by the user.
    pinned_repo_limit: int = 6
    cadence_plan_repo_limit: int = 10
    # §2[1] discovery — allowlist-first per delivery plan; entries "owner/repo:stack"
    discovery_allowlist: tuple[str, ...] = ()
    discovery_labels: tuple[str, ...] = ("good first issue", "help wanted")
    discovery_max_per_query: int = 25
    # §2[2] policy pre-flight — seed hard-skip list (RB: curl-class restrictive repos)
    hard_skip_repos: tuple[str, ...] = ("curl/curl", "ghostty-org/ghostty", "tldraw/tldraw")
    hard_skip_orgs: tuple[str, ...] = ("curl", "ghostty-org", "tldraw", "matplotlib")
    policy_ttl_days: int = 7
    # §7 LLM pricing, USD per MTok (input, output). opus-4-8 cited in ADR §7;
    # unknown models are charged at the MOST expensive known rate (fail-closed
    # toward the spend cap, never an undercount).
    llm_prices_per_mtok: tuple[tuple[str, float, float], ...] = (
        ("claude-opus-4-8", 5.0, 25.0),
    )
    llm_max_output_tokens: int = 8192
    llm_max_retries: int = 2
    # §4 OAuth (V6 hardening)
    oauth_scopes: tuple[str, ...] = ("public_repo", "user:email")
    oauth_listener_timeout_s: int = 180
    # §6 graph-verify
    graph_verify_delay_h: int = 24


def _dropbox_roots(env: dict[str, str]) -> list[Path]:
    """Probe Dropbox info.json files for configured sync roots (best-effort, F-07)."""
    roots: list[Path] = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        base = env.get(var)
        if not base:
            continue
        info = Path(base) / "Dropbox" / "info.json"
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for account in data.values():
            path = account.get("path") if isinstance(account, dict) else None
            if path:
                roots.append(Path(path))
    return roots


def detect_sync_roots(env: dict[str, str] | None = None) -> list[Path]:
    env = dict(os.environ) if env is None else env
    roots = [Path(env[v]) for v in _SYNC_ENV_VARS if env.get(v)]
    roots.extend(_dropbox_roots(env))
    return roots


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def assert_not_sync_root(db_path: Path, env: dict[str, str] | None = None) -> None:
    """Startup fail-fast invariant (ADR §6, F-07).

    Best-effort by design: env-var + Dropbox info.json detection does not cover
    every sync product. The mandated %LOCALAPPDATA% default is the primary
    control; this check is the guard rail.
    """
    for root in detect_sync_roots(env):
        if _is_under(db_path, root):
            raise SyncRootError(
                f"DB path {db_path} falls under detected sync root {root}; "
                "SQLite WAL on cloud-synced folders is a corruption vector "
                "(sqlite.org/howtocorrupt). Move the DB or change the sync setup."
            )


def load_config(db_path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    cfg = Config() if db_path is None else Config(db_path=db_path)
    assert_not_sync_root(cfg.db_path, env)
    return cfg
