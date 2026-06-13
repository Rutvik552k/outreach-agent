"""SQLite persistence (ADR-001 §6): WAL, single writer, hash-chained
audit_log + rate_budget (V4), additive migrations keyed by schema_version.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ChainIntegrityError

GENESIS_HASH = "0" * 64

_MIGRATIONS: list[list[str]] = [
    # v1 — initial schema
    [
        """CREATE TABLE config_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE candidates (
            candidate_id TEXT PRIMARY KEY,
            repo_full_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            issue_url TEXT NOT NULL,
            stack TEXT NOT NULL CHECK (stack IN ('python','rust','nodejs','react')),
            contribution_type TEXT NOT NULL CHECK (contribution_type IN
                ('bugfix-static-analysis','test-addition','issue-triage','dependency-bump')),
            score_json TEXT NOT NULL,
            discovered_at TEXT NOT NULL
        )""",
        """CREATE TABLE policy_verdicts (
            candidate_id TEXT PRIMARY KEY REFERENCES candidates(candidate_id),
            verdict TEXT NOT NULL CHECK (verdict IN ('cleared','blocked')),
            reasons_json TEXT NOT NULL,
            sources_checked_json TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            ttl_expires_at TEXT NOT NULL
        )""",
        """CREATE TABLE contributions (
            contribution_id TEXT PRIMARY KEY,
            candidate_id TEXT REFERENCES candidates(candidate_id),
            repo_full_name TEXT NOT NULL,
            fork_full_name TEXT,
            branch TEXT,
            base_sha TEXT,
            state TEXT NOT NULL,
            state_reason TEXT,
            fork_draft_pr_number INTEGER,
            fork_draft_pr_node_id TEXT,
            upstream_pr_number INTEGER,
            upstream_pr_node_id TEXT,
            merge_commit_sha TEXT,
            merged_at TEXT,
            prepared_json TEXT,
            last_synced_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE audit_log (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL CHECK (actor IN ('agent','user')),
            phase TEXT NOT NULL CHECK (phase IN ('intent','confirmed','failed','info')),
            endpoint TEXT NOT NULL,
            contribution_id TEXT,
            outcome_json TEXT NOT NULL,
            rate_state_json TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            row_hash TEXT NOT NULL
        )""",
        """CREATE TABLE rate_budget (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            category TEXT NOT NULL CHECK (category IN ('content_creation','other_mutation')),
            kind TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            contribution_id TEXT,
            prev_hash TEXT NOT NULL,
            row_hash TEXT NOT NULL
        )""",
        """CREATE TABLE review_threads (
            thread_id TEXT PRIMARY KEY,
            contribution_id TEXT NOT NULL REFERENCES contributions(contribution_id),
            upstream_comment_id INTEGER NOT NULL,
            author_login TEXT NOT NULL,
            body TEXT NOT NULL,
            draft_response TEXT,
            response_state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE kpi_outcomes (
            outcome_id TEXT PRIMARY KEY,
            contribution_id TEXT NOT NULL REFERENCES contributions(contribution_id),
            outcome TEXT NOT NULL,
            counts_in_merge_rate INTEGER NOT NULL,
            graph_credit TEXT,
            recorded_at TEXT NOT NULL
        )""",
        """CREATE TABLE profile_actions (
            action_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE secondary_limit_hits (
            hit_id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            retry_after_s REAL
        )""",
        "CREATE INDEX idx_contrib_state ON contributions(state)",
        "CREATE INDEX idx_budget_ts ON rate_budget(ts)",
        "CREATE INDEX idx_budget_kind_ts ON rate_budget(kind, ts)",
    ],
    # v2 — additive (ADR §6): C-2 github_object_id on audit events (C6 v2.2),
    # llm_spend ledger (§7/F-13). The object id is ALSO embedded in outcome_json
    # so it is covered by the existing hash chain; the column is the queryable
    # mirror and verify_chains() asserts column == outcome_json value.
    [
        "ALTER TABLE audit_log ADD COLUMN github_object_id TEXT",
        "CREATE INDEX idx_audit_contrib ON audit_log(contribution_id)",
        """CREATE TABLE llm_spend (
            entry_id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            model TEXT NOT NULL,
            purpose TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL
        )""",
        "CREATE INDEX idx_llm_spend_ts ON llm_spend(ts)",
    ],
]

SCHEMA_VERSION = len(_MIGRATIONS)

AUDIT_CHAIN_HEAD_KEY = "audit_chain_head"
BUDGET_CHAIN_HEAD_KEY = "budget_chain_head"

# Every integrity-failure pause reason carries this prefix; cli.py keys its
# block-everything FM12 branch on it. Adding a new integrity pause through
# _pause_chain_break keeps the CLI gating correct without code changes there.
CHAIN_BREAK_PAUSE_PREFIX = "chain-break: "
GLOBAL_PAUSE_KEY = "global_pause"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_ulid() -> str:
    # Monotonic-enough unique id for a single-writer local process.
    return uuid.uuid7().hex if hasattr(uuid, "uuid7") else uuid.uuid4().hex


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def chain_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


_AUDIT_PAYLOAD_FIELDS = ("event_id", "ts", "actor", "phase", "endpoint",
                         "contribution_id", "outcome_json", "rate_state_json")
_BUDGET_PAYLOAD_FIELDS = ("entry_id", "ts", "category", "kind", "endpoint", "contribution_id")


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    ts: str
    actor: str
    phase: str
    endpoint: str
    contribution_id: str | None
    outcome: dict[str, Any]
    rate_state: dict[str, Any]
    prev_hash: str
    row_hash: str


class Database:
    """Owns the SQLite connection. Single writer, WAL mode."""

    def __init__(self, db_path: Path | str, *, verify_chains_on_open: bool = True) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        if verify_chains_on_open:
            self.verify_chains()

    def close(self) -> None:
        self.conn.close()

    # -- migrations (additive-only, ADR §6) ---------------------------------

    def _migrate(self) -> None:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config_meta'"
        )
        version = 0
        if cur.fetchone():
            row = self.conn.execute(
                "SELECT value FROM config_meta WHERE key='schema_version'"
            ).fetchone()
            version = int(row["value"]) if row else 0
        for idx in range(version, SCHEMA_VERSION):
            with self.transaction():
                for stmt in _MIGRATIONS[idx]:
                    self.conn.execute(stmt)
                self.conn.execute(
                    "INSERT INTO config_meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(idx + 1),),
                )

    # -- transactions --------------------------------------------------------

    def transaction(self) -> "_Txn":
        return _Txn(self.conn)

    # -- config_meta ---------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM config_meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO config_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def set_global_pause(self, reason: str) -> None:
        self.set_meta(GLOBAL_PAUSE_KEY, reason)

    def clear_global_pause(self) -> None:
        self.conn.execute("DELETE FROM config_meta WHERE key=?", (GLOBAL_PAUSE_KEY,))

    def global_pause_reason(self) -> str | None:
        return self.get_meta(GLOBAL_PAUSE_KEY)

    # -- hash-chained appends (V4) -------------------------------------------
    # Callers must hold an open transaction; appends participate in it so the
    # audited action and its audit row commit or roll back together.

    def append_audit(
        self,
        *,
        actor: str,
        phase: str,
        endpoint: str,
        contribution_id: str | None = None,
        outcome: dict[str, Any] | None = None,
        rate_state: dict[str, Any] | None = None,
        github_object_id: str | None = None,
    ) -> AuditEvent:
        prev = self.get_meta(AUDIT_CHAIN_HEAD_KEY, GENESIS_HASH) or GENESIS_HASH
        outcome = dict(outcome or {})
        if github_object_id is not None:
            # Embedded in outcome_json so the existing chain hash covers it (C-2);
            # the dedicated column is a queryable mirror checked at verify time.
            outcome["github_object_id"] = github_object_id
        payload = {
            "event_id": new_ulid(),
            "ts": utc_now_iso(),
            "actor": actor,
            "phase": phase,
            "endpoint": endpoint,
            "contribution_id": contribution_id,
            "outcome_json": canonical_json(outcome),
            "rate_state_json": canonical_json(rate_state or {}),
        }
        row_hash = chain_hash(prev, payload)
        self.conn.execute(
            "INSERT INTO audit_log(event_id, ts, actor, phase, endpoint, contribution_id,"
            " outcome_json, rate_state_json, github_object_id, prev_hash, row_hash)"
            " VALUES(:event_id, :ts, :actor, :phase, :endpoint, :contribution_id,"
            " :outcome_json, :rate_state_json, :github_object_id, :prev_hash, :row_hash)",
            {**payload, "github_object_id": github_object_id,
             "prev_hash": prev, "row_hash": row_hash},
        )
        self.set_meta(AUDIT_CHAIN_HEAD_KEY, row_hash)
        return AuditEvent(
            event_id=payload["event_id"], ts=payload["ts"], actor=actor, phase=phase,
            endpoint=endpoint, contribution_id=contribution_id,
            outcome=outcome, rate_state=rate_state or {},
            prev_hash=prev, row_hash=row_hash,
        )

    def append_budget_entry(
        self,
        *,
        category: str,
        kind: str,
        endpoint: str,
        contribution_id: str | None = None,
    ) -> str:
        prev = self.get_meta(BUDGET_CHAIN_HEAD_KEY, GENESIS_HASH) or GENESIS_HASH
        payload = {
            "entry_id": new_ulid(),
            "ts": utc_now_iso(),
            "category": category,
            "kind": kind,
            "endpoint": endpoint,
            "contribution_id": contribution_id,
        }
        row_hash = chain_hash(prev, payload)
        self.conn.execute(
            "INSERT INTO rate_budget(entry_id, ts, category, kind, endpoint, contribution_id,"
            " prev_hash, row_hash)"
            " VALUES(:entry_id, :ts, :category, :kind, :endpoint, :contribution_id,"
            " :prev_hash, :row_hash)",
            {**payload, "prev_hash": prev, "row_hash": row_hash},
        )
        self.set_meta(BUDGET_CHAIN_HEAD_KEY, row_hash)
        return payload["entry_id"]

    # -- startup full-chain verification (FM12) ------------------------------

    def verify_chains(self) -> None:
        self._verify_chain("audit_log", _AUDIT_PAYLOAD_FIELDS, AUDIT_CHAIN_HEAD_KEY)
        self._verify_chain("rate_budget", _BUDGET_PAYLOAD_FIELDS, BUDGET_CHAIN_HEAD_KEY)

    def _pause_chain_break(self, detail: str) -> None:
        self.set_global_pause(f"{CHAIN_BREAK_PAUSE_PREFIX}{detail}")

    def _verify_chain(self, table: str, fields: tuple[str, ...], head_key: str) -> None:
        prev = GENESIS_HASH
        for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY seq"):
            payload = {f: row[f] for f in fields}
            expected = chain_hash(prev, payload)
            if row["prev_hash"] != prev or row["row_hash"] != expected:
                self._pause_chain_break(f"hash chain break in {table} at seq={row['seq']}")
                raise ChainIntegrityError(
                    f"{table} chain broken at seq={row['seq']}: stored row_hash "
                    f"{row['row_hash']} != recomputed {expected}. All operations halted."
                )
            if table == "audit_log" and row["github_object_id"] is not None:
                # C-2: mirror column must match the hash-covered outcome_json value,
                # otherwise the column has been tampered with independently.
                embedded = json.loads(row["outcome_json"]).get("github_object_id")
                if embedded != row["github_object_id"]:
                    self._pause_chain_break(
                        f"github_object_id mirror mismatch in audit_log seq={row['seq']}"
                    )
                    raise ChainIntegrityError(
                        f"audit_log seq={row['seq']}: github_object_id column "
                        f"{row['github_object_id']!r} != hash-covered value {embedded!r}. "
                        "All operations halted."
                    )
            prev = row["row_hash"]
        stored_head = self.get_meta(head_key, GENESIS_HASH) or GENESIS_HASH
        if stored_head != prev:
            self._pause_chain_break(f"hash chain head mismatch for {table}")
            raise ChainIntegrityError(
                f"{table} chain head mismatch: config_meta has {stored_head}, "
                f"recomputed {prev}. All operations halted."
            )


class _Txn:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._owns = False

    def __enter__(self) -> sqlite3.Connection:
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
            self._owns = True
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._owns:
            return
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
