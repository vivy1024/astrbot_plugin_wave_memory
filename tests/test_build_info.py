"""build_info：部署戳优先，开发目录读 .git，都没有时只报 metadata 版本。"""

from __future__ import annotations

import json

from utils.build_info import STAMP_FILE, build_info


def _root(tmp_path):
    (tmp_path / "metadata.yaml").write_text("name: x\nversion: v6.0.0-dev\n", encoding="utf-8")
    return tmp_path


def test_deploy_stamp_wins(tmp_path):
    root = _root(tmp_path)
    (root / STAMP_FILE).write_text(json.dumps({"commit": "abc123", "branch": "v6", "deployed_at": "2026-09-25T10:00:00", "dirty": False}), encoding="utf-8")
    info = build_info.__wrapped__(root)
    assert info == {"version": "v6.0.0-dev", "commit": "abc123", "branch": "v6", "source": "deploy_stamp", "deployed_at": "2026-09-25T10:00:00", "dirty": False}


def test_git_head_loose_and_packed_refs(tmp_path):
    root = _root(tmp_path)
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/v6\n", encoding="utf-8")
    (git / "packed-refs").write_text("# pack-refs\nfeedbeef refs/heads/v6\n", encoding="utf-8")
    assert build_info.__wrapped__(root)["commit"] == "feedbeef"
    (git / "refs" / "heads" / "v6").write_text("cafe01\n", encoding="utf-8")
    info = build_info.__wrapped__(root)
    assert (info["commit"], info["branch"], info["source"]) == ("cafe01", "v6", "git")


def test_nothing_known(tmp_path):
    assert build_info.__wrapped__(_root(tmp_path)) == {"version": "v6.0.0-dev", "commit": "", "branch": "", "source": "unknown"}
