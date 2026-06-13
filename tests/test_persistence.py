from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from outreach_agent.errors import ChainIntegrityError
from outreach_agent.persistence import (
    GLOBAL_PAUSE_KEY,
    SCHEMA_VERSION,
    Database,
)


def _seed(db: Database) -> None:
    for i in range(3):
        db.append_audit(actor="agent", phase="info", endpoint=f"test:{i}",
                        outcome={"i": i})
        db.append_budget_entry(category="content_creation", kind="comment",
                               endpoint=f"POST /test/{i}")


def test_schema_version_recorded(db: Database) -> None:
    assert db.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_chain_verifies_clean(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    db = Database(path)
    _seed(db)
    db.close()
    db2 = Database(path)  # verify_chains_on_open
    db2.close()


def test_hash_chain_tamper_detected_at_startup(tmp_path: Path) -> None:
    """V4/FM12: mutate an audit row out-of-band → startup halts + global pause."""
    path = tmp_path / "s.db"
    db = Database(path)
    _seed(db)
    db.close()

    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE audit_log SET outcome_json='{\"i\":999}' WHERE seq=2")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError):
        Database(path)

    raw = sqlite3.connect(str(path))
    pause = raw.execute(
        "SELECT value FROM config_meta WHERE key=?", (GLOBAL_PAUSE_KEY,)
    ).fetchone()
    raw.close()
    assert pause is not None and "hash chain break" in pause[0]


def test_budget_chain_tamper_detected(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    db = Database(path)
    _seed(db)
    db.close()

    raw = sqlite3.connect(str(path))
    raw.execute("UPDATE rate_budget SET kind='fork_create' WHERE seq=1")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError):
        Database(path)


def test_chain_head_mismatch_detected(tmp_path: Path) -> None:
    """Deleting the newest rows without updating the head breaks verification."""
    path = tmp_path / "s.db"
    db = Database(path)
    _seed(db)
    db.close()

    raw = sqlite3.connect(str(path))
    raw.execute("DELETE FROM audit_log WHERE seq=(SELECT MAX(seq) FROM audit_log)")
    raw.commit()
    raw.close()

    with pytest.raises(ChainIntegrityError):
        Database(path)


def test_transaction_rolls_back_atomically(db: Database) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.append_audit(actor="agent", phase="info", endpoint="will:rollback")
            raise RuntimeError("boom")
    count = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE endpoint='will:rollback'"
    ).fetchone()["n"]
    assert count == 0
    db.verify_chains()  # chain head must still be consistent after rollback
