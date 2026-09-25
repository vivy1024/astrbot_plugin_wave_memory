"""在容器里把一份代码归档套到插件目录上（deploy_to_container.ps1 调用，部署与回滚共用）。

    python3 _deploy_apply.py <插件目录> <归档 .tar/.tar.gz> [--stamp 版本戳.json] [--prune-unknown]

- 归档可以是 ``git archive`` 的产物（无前缀），也可以是代码备份（顶层是插件目录名）。
- 只动代码：旧的 git 检出、node_modules、data/、logs/、*.db、*.tar 不读不写不删。
- 上次部署清单（``_deploy_manifest.txt``）里有、这次没有的文件删除；清单外的未知文件保留并列出，
  ``--prune-unknown`` 时删除其中的 .py 与 ``webui/static/`` 产物（首次部署清理旧版遗留模块）。
- 清掉 ``__pycache__`` 与空目录，写新清单；给了 ``--stamp`` 就写 ``_deploy_version.json``，
  没给且归档里也没有时删掉旧版本戳（避免显示错误的提交号）。
输出一行 JSON：written / removed / unknown。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile

MANIFEST = "_deploy_manifest.txt"
STAMP = "_deploy_version.json"
BOOKKEEPING = {MANIFEST, STAMP}
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
SKIP_TOP = {"data", "logs"}
SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm", ".tar", ".tar.gz")


def _managed(rel: str) -> bool:
    """这个相对路径归不归部署管。"""
    parts = rel.split("/")
    if parts[0] in SKIP_TOP or any(part in SKIP_DIRS for part in parts):
        return False
    return not rel.endswith(SKIP_SUFFIXES)


def _files(root: str) -> set[str]:
    found: set[str] = set()
    for base, dirs, files in os.walk(root):
        rel_base = os.path.relpath(base, root).replace(os.sep, "/")
        rel_base = "" if rel_base == "." else rel_base + "/"
        dirs[:] = [d for d in dirs if _managed(rel_base + d + "/x")]
        for name in files:
            rel = rel_base + name
            if _managed(rel):
                found.add(rel)
    return found


def _extract(archive: str, stage: str, plugin_name: str) -> str:
    with tarfile.open(archive) as tar:
        try:
            tar.extractall(stage, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(stage)
    entries = os.listdir(stage)
    if entries == [plugin_name] and os.path.isdir(os.path.join(stage, plugin_name)):
        return os.path.join(stage, plugin_name)  # 代码备份带着顶层目录
    return stage


def apply(plugin: str, archive: str, *, stamp: str | None = None, prune_unknown: bool = False) -> dict:
    plugin = os.path.abspath(plugin)
    stage_root = tempfile.mkdtemp(prefix="wm_apply_")
    try:
        source = _extract(archive, stage_root, os.path.basename(plugin))
        new_files = _files(source)
        if not any(rel == "main.py" for rel in new_files):
            raise SystemExit("归档里没有 main.py，拒绝套用")
        manifest_path = os.path.join(plugin, MANIFEST)
        old_manifest: set[str] = set()
        if os.path.isfile(manifest_path):
            with open(manifest_path, encoding="utf-8") as f:
                old_manifest = {line.strip() for line in f if line.strip()}
        existing = _files(plugin) if os.path.isdir(plugin) else set()
        payload = new_files - BOOKKEEPING
        removed = sorted(((old_manifest & existing) - payload) - BOOKKEEPING)
        unknown = sorted(existing - payload - old_manifest - BOOKKEEPING)
        if prune_unknown:
            prunable = [rel for rel in unknown if rel.endswith(".py") or rel.startswith("webui/static/")]
            removed = sorted(set(removed) | set(prunable))
            unknown = [rel for rel in unknown if rel not in prunable]

        for rel in removed:
            os.remove(os.path.join(plugin, rel))
        for rel in sorted(payload):
            dst = os.path.join(plugin, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(source, rel), dst)
        for base, dirs, _files_ in os.walk(plugin, topdown=False):
            rel_base = os.path.relpath(base, plugin).replace(os.sep, "/")
            if rel_base != "." and not _managed(rel_base + "/x"):
                continue
            for d in dirs:
                path = os.path.join(base, d)
                if d == "__pycache__":
                    shutil.rmtree(path, ignore_errors=True)
                elif _managed(("" if rel_base == "." else rel_base + "/") + d + "/x") and not os.listdir(path):
                    os.rmdir(path)

        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(payload)) + "\n")
        stamp_path = os.path.join(plugin, STAMP)
        if stamp:
            shutil.copy2(stamp, stamp_path)
        elif STAMP in new_files:
            shutil.copy2(os.path.join(source, STAMP), stamp_path)
        elif os.path.exists(stamp_path):
            os.remove(stamp_path)
        return {"written": len(payload), "removed": removed, "unknown": unknown}
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("plugin")
    parser.add_argument("archive")
    parser.add_argument("--stamp")
    parser.add_argument("--prune-unknown", action="store_true")
    args = parser.parse_args(argv)
    result = apply(args.plugin, args.archive, stamp=args.stamp, prune_unknown=args.prune_unknown)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
