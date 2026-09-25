"""scripts/_deploy_apply.py：部署/回滚只动代码文件，按清单清理，保护数据与旧检出。"""

from __future__ import annotations

import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_deploy_apply.py"
spec = importlib.util.spec_from_file_location("_deploy_apply", SCRIPT)
deploy_apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy_apply)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _archive(tmp_path: Path, name: str, files: dict[str, str], prefix: str = "") -> str:
    src = tmp_path / f"src_{name}"
    _write(src, files)
    out = tmp_path / f"{name}.tar"
    with tarfile.open(out, "w") as tar:
        for rel in files:
            tar.add(src / rel, arcname=prefix + rel)
    return str(out)


@pytest.fixture
def plugin(tmp_path):
    root = tmp_path / "astrbot_plugin_wave_memory"
    _write(root, {
        "main.py": "v5",
        "services/legacy_mod.py": "old",
        "services/__pycache__/x.pyc": "c",
        "wave_memory.db": "",
        "old_sync.tar": "t",
        ".git/HEAD": "ref: refs/heads/master",
        "node_modules/a.js": "n",
        "data/cmd_config.json": "{}",
        "logs/a.log": "l",
        "notes.txt": "user",
    })
    return root


PROTECTED = ["wave_memory.db", "old_sync.tar", ".git/HEAD", "node_modules/a.js", "data/cmd_config.json", "logs/a.log"]


def test_first_deploy_keeps_unknown_and_protected(tmp_path, plugin):
    archive = _archive(tmp_path, "v6", {"main.py": "v6", "app/bootstrap.py": "b"})
    stamp = tmp_path / "stamp.json"
    stamp.write_text(json.dumps({"commit": "abc"}), encoding="utf-8")
    result = deploy_apply.apply(str(plugin), archive, stamp=str(stamp))
    assert result["removed"] == []
    assert result["unknown"] == ["notes.txt", "services/legacy_mod.py"]
    assert (plugin / "main.py").read_text() == "v6"
    assert not (plugin / "services" / "__pycache__").exists()
    assert json.loads((plugin / "_deploy_version.json").read_text())["commit"] == "abc"
    assert (plugin / "_deploy_manifest.txt").read_text().split() == ["app/bootstrap.py", "main.py"]
    assert all((plugin / rel).exists() for rel in PROTECTED)


def test_prune_unknown_only_removes_code(tmp_path, plugin):
    archive = _archive(tmp_path, "v6", {"main.py": "v6"})
    result = deploy_apply.apply(str(plugin), archive, prune_unknown=True)
    assert result["removed"] == ["services/legacy_mod.py"]
    assert result["unknown"] == ["notes.txt"]
    assert not (plugin / "services").exists()  # 空目录清掉
    assert all((plugin / rel).exists() for rel in PROTECTED)


def test_second_deploy_removes_files_dropped_since_manifest(tmp_path, plugin):
    deploy_apply.apply(str(plugin), _archive(tmp_path, "a", {"main.py": "1", "app/old.py": "o"}))
    result = deploy_apply.apply(str(plugin), _archive(tmp_path, "b", {"main.py": "2"}))
    assert result["removed"] == ["app/old.py"]
    assert not (plugin / "app").exists()
    assert (plugin / "services" / "legacy_mod.py").exists()  # 清单外，保留


def test_rollback_from_prefixed_backup_restores_and_clears_stamp(tmp_path, plugin):
    stamp = tmp_path / "stamp.json"
    stamp.write_text("{}", encoding="utf-8")
    deploy_apply.apply(str(plugin), _archive(tmp_path, "v6", {"main.py": "v6", "app/new.py": "n"}), stamp=str(stamp))
    backup = _archive(tmp_path, "backup", {"main.py": "v5", "data/cmd_config.json": "overwritten?"}, prefix="astrbot_plugin_wave_memory/")
    result = deploy_apply.apply(str(plugin), backup)
    assert (plugin / "main.py").read_text() == "v5"
    assert result["removed"] == ["app/new.py"]
    assert (plugin / "data" / "cmd_config.json").read_text() == "{}"  # 备份里的运行时目录不套用
    assert not (plugin / "_deploy_version.json").exists()


def test_refuses_archive_without_main(tmp_path, plugin):
    with pytest.raises(SystemExit):
        deploy_apply.apply(str(plugin), _archive(tmp_path, "bad", {"README.md": "x"}))
    assert (plugin / "main.py").read_text() == "v5"
