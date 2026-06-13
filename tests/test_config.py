from __future__ import annotations

import json
from pathlib import Path

import pytest

from outreach_agent.config import Config, assert_not_sync_root, load_config
from outreach_agent.errors import SyncRootError


def test_sync_root_path_startup_refusal(tmp_path: Path) -> None:
    """F-07: DB path under a OneDrive env-var root → startup refusal."""
    onedrive = tmp_path / "OneDrive"
    onedrive.mkdir()
    db_path = onedrive / "outreach-agent" / "state.db"
    env = {"OneDrive": str(onedrive)}
    with pytest.raises(SyncRootError):
        load_config(db_path=db_path, env=env)


def test_sync_root_onedrive_commercial_variant(tmp_path: Path) -> None:
    root = tmp_path / "OneDrive - Corp"
    root.mkdir()
    env = {"OneDriveCommercial": str(root)}
    with pytest.raises(SyncRootError):
        assert_not_sync_root(root / "sub" / "state.db", env)


def test_sync_root_dropbox_info_json_probe(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    dropbox_root = tmp_path / "DropboxFolder"
    dropbox_root.mkdir()
    info = appdata / "Dropbox" / "info.json"
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"personal": {"path": str(dropbox_root)}}), encoding="utf-8")
    env = {"APPDATA": str(appdata)}
    with pytest.raises(SyncRootError):
        assert_not_sync_root(dropbox_root / "state.db", env)


def test_non_sync_path_accepted(tmp_path: Path) -> None:
    env = {"OneDrive": str(tmp_path / "OneDrive")}
    cfg = load_config(db_path=tmp_path / "local" / "state.db", env=env)
    assert cfg.db_path == tmp_path / "local" / "state.db"


def test_defaults_match_adr() -> None:
    cfg = Config()
    assert cfg.sandbox_wall_timeout_s == 900
    assert cfg.sandbox_resolve_timeout_s == 300  # C8 v2.4 Phase R
    assert cfg.diff_cap_changed_lines == 400
    assert cfg.model == "claude-opus-4-8"
    assert cfg.upstream_pr_per_day == 1
    assert cfg.content_creation_per_min == 8
    assert cfg.content_creation_per_hr == 50
    assert str(cfg.db_path).endswith("outreach-agent\\state.db") or \
        str(cfg.db_path).endswith("outreach-agent/state.db")
