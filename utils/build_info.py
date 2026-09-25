"""当前运行的是哪份代码：部署脚本写的版本戳，开发目录退回读 .git。

``scripts/deploy_to_container.ps1``（openclaw 工作区）部署时在插件根目录写 ``_deploy_version.json``：
提交号、分支、部署时间、部署时工作区是否有未提交改动。/api/health 与 Runtime /capabilities
带上这份信息，线上跑的是哪个提交一眼可查。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STAMP_FILE = "_deploy_version.json"


def _metadata_version(root: Path) -> str:
    try:
        text = (root / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"^version:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else ""


def _git_head(root: Path) -> dict[str, Any]:
    git_dir = root / ".git"
    if git_dir.is_file():  # worktree：.git 是指向真实目录的文件
        match = re.match(r"gitdir:\s*(.+)", git_dir.read_text(encoding="utf-8").strip())
        git_dir = (root / match.group(1)).resolve() if match else git_dir
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not head.startswith("ref: "):
        return {"commit": head, "branch": ""}
    ref = head[5:]
    commit = ""
    ref_path = git_dir / ref
    if ref_path.is_file():
        commit = ref_path.read_text(encoding="utf-8").strip()
    else:
        try:
            for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    commit = line.split(" ", 1)[0]
                    break
        except OSError:
            pass
    return {"commit": commit, "branch": ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref}


@lru_cache(maxsize=1)
def build_info(root: Path = ROOT) -> dict[str, Any]:
    info: dict[str, Any] = {"version": _metadata_version(root), "commit": "", "branch": "", "source": "unknown"}
    stamp = root / STAMP_FILE
    if stamp.is_file():
        try:
            data = json.loads(stamp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            info.update({key: data.get(key) for key in ("commit", "branch", "deployed_at", "dirty") if key in data})
            info["source"] = "deploy_stamp"
            return info
    head = _git_head(root)
    if head:
        info.update(head)
        info["source"] = "git"
    return info


__all__ = ["STAMP_FILE", "build_info"]
