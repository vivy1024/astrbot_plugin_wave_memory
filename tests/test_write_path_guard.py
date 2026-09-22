"""写入路径架构守卫。

目的：防止"业务服务绕过正式写入口直接改 domain 表"这类回归重新出现。
真正的绕过特征是**自行 commit**（不经 WriteCoordinator 的事务边界），
以及直接写受保护的 domain 表。

分层规则：
- 唯一 domain 写入口：services/system_convergence_runtime.py 的 command handler；
- 自有小表 store（trace/feedback/config suggestion/review candidate）可自管 schema 与写入；
- 迁移与恢复脚本按其性质豁免；
- 本轮已收敛的文件必须保持零裸提交、零 domain 表直写。
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parent.parent / "services"

# 由 WriteCoordinator 统一事务提交的正式对象表。业务服务不得绕过直接写。
PROTECTED_DOMAIN_TABLES = frozenset({
    "memories",
    "memory_tags",
    "experience_episodes",
    "scoped_soul_concerns",
    "scoped_soul_mood",
    "scoped_soul_timeline",
    "scoped_soul_relationships",
    "scoped_soul_relationship_events",
    "domain_outbox",
    "write_operations",
})

# 允许直写受保护表的文件。分两类：写入口本体，以及**已登记的历史遗留**
# （遗留只允许减少，不允许新增，见 test_legacy_debts_do_not_grow）。
WRITE_ENTRY_ALLOWLIST = {
    "system_convergence_runtime.py",  # command handlers 本体（唯一正式写入口）
    "system_convergence_test_port.py",  # 测试端口，复用同一 handler 语义
    "scope_recovery_migration.py",  # 一次性迁移
    "legacy_relationship_migration.py",  # 一次性迁移
    "approved_scope_recovery.py",  # 治理恢复作业
    "memory_mutations.py",  # 遗留：批量治理路径，待收敛到 DomainCommand
    "scoped_knowledge_mutations.py",  # 遗留：L1 知识治理，待收敛到 DomainCommand
    "experience_episodes.py",  # 遗留：仅在 coordinator.transaction_blocking 事务内写，无裸 commit
    "daily_diary_bridge.py",  # 群分析桥接同步：将外部分析 traces 导入为群聊日记经历
    "tag_auditor.py",  # 遗留：直接 DELETE memory_tags，待收敛
    "tag_worker.py",  # 遗留：直接 UPDATE memories，待收敛
}

# 关系校准 / 标签治理已收进 ProductionWriteGateway：业务文件只改各自领域表，
# 不得再 INSERT write_operations / domain_outbox。
SHADOW_WRITE_ENTRY_FILES = set()

# 允许自行 commit 的文件（自有小表 store / 网关本体 / 迁移工具 / 已登记遗留）。
SELF_COMMIT_ALLOWLIST = WRITE_ENTRY_ALLOWLIST | {
    "candidate_store.py",
    "trace_store.py",
    "config_suggestion_store.py",
    "feedback_store.py",
    "durable_jobs.py",
    "outbox_dispatcher.py",
    "data_governance_jobs.py",
    "memory_jobs.py",
    "scope_recovery.py",
    "tag_auditor.py",  # 遗留：治理作业自管事务
    "approved_scope_recovery_indexes.py",  # 遗留：索引恢复作业
    "inbound_message_handler.py",  # 遗留：入口自提交
    "lifecycle.py",  # 遗留：好感/画像自提交，含已无调用者的 legacy 事件写入
    "meta_thinking.py",  # 遗留：现场思考自提交
    "mood_trajectory.py",  # 遗留：情绪轨迹写入未走命令链
    "subjective_time.py",  # 遗留：时间锚点写入未走命令链
    "tag_job.py",  # 遗留：标签作业
}

# 本轮已收敛的文件：必须零裸提交、零受保护表直写。
MUST_STAY_CLEAN = (
    "concern_tracker.py",
    "reflection_trigger.py",
    "dream.py",
    "experience_episodes.py",
)

_WRITE_PATTERN = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)
_COMMIT_PATTERN = re.compile(r"\.commit\(\)")


def _service_files() -> list[Path]:
    return sorted(p for p in SERVICES_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _protected_table_writes(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # CREATE TABLE / 文档字符串里的 DDL 不算写入既有对象。
        if "CREATE TABLE" in stripped.upper():
            continue
        for match in _WRITE_PATTERN.finditer(line):
            table = match.group(2).lower()
            if table in PROTECTED_DOMAIN_TABLES:
                findings.append((lineno, f"{match.group(1).upper()} {table}"))
    return findings


def test_guard_targets_exist():
    """守卫本身不能因目录结构调整而空转。"""
    files = {p.name for p in _service_files()}
    for name in MUST_STAY_CLEAN:
        assert name in files, f"services/ 下缺少 {name}，守卫需同步更新"


def test_protected_domain_tables_are_only_written_by_entry_points():
    offenders: dict[str, list[str]] = {}
    for path in _service_files():
        if path.name in WRITE_ENTRY_ALLOWLIST:
            continue
        writes = _protected_table_writes(path)
        if writes:
            offenders[path.name] = [f"L{lineno}: {statement}" for lineno, statement in writes]
    assert not offenders, (
        "以下业务服务绕过正式写入口直接改 domain 表，必须改为 DomainCommand："
        + "\n".join(f"  {name}: {items}" for name, items in offenders.items())
    )


def test_converged_services_never_self_commit():
    """本轮收敛的文件不得自行 commit；写入必须落在 coordinator 事务里。"""
    offenders: list[str] = []
    for path in _service_files():
        if path.name not in MUST_STAY_CLEAN:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _COMMIT_PATTERN.search(line):
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, f"收敛文件出现裸 commit: {offenders}"


def test_only_declared_files_may_self_commit():
    """新增自管事务的文件必须显式登记，避免绕过写入口悄悄扩散。"""
    offenders: list[str] = []
    for path in _service_files():
        if path.name in SELF_COMMIT_ALLOWLIST or path.name in MUST_STAY_CLEAN:
            continue
        text = path.read_text(encoding="utf-8")
        if _COMMIT_PATTERN.search(text):
            offenders.append(path.name)
    assert not offenders, (
        "以下服务自行提交事务，请改为经 WriteCoordinator，或说明理由后登记白名单："
        f"\n{sorted(offenders)}"
    )


def test_concern_writes_are_command_owned():
    """scoped_soul_concerns 只能由命令 handler 写，tracker 全量替换不得回流。"""
    repo_path = SERVICES_DIR.parent / "engine" / "db" / "scoped_soul_repo.py"
    runtime_writes = _protected_table_writes(SERVICES_DIR / "system_convergence_runtime.py")
    assert any("scoped_soul_concerns" in statement for _, statement in runtime_writes), (
        "Concern 命令 handler 应存在对 scoped_soul_concerns 的受控写入"
    )
    tracker = (SERVICES_DIR / "concern_tracker.py").read_text(encoding="utf-8")
    for forbidden in ("replace_concerns", "DELETE FROM scoped_soul_concerns", "def add(", "def tick("):
        assert forbidden not in tracker, f"ConcernTracker 不得保留写路径: {forbidden}"
    assert repo_path.exists()


def test_manual_gateways_do_not_write_the_ledger():
    """人工校准/审批必须走正门：业务文件不得再 INSERT 账本表。"""
    ledger = re.compile(r"INSERT\s+INTO\s+(write_operations|domain_outbox)\b", re.IGNORECASE)
    for name in ("relationship_calibration.py", "tag_governance.py"):
        text = (SERVICES_DIR / name).read_text(encoding="utf-8")
        assert not ledger.search(text), f"{name} 不得再自行写 write_operations/domain_outbox"


# ---- 棘轮：遗留债务只能减少 ------------------------------------------------


def _files_matching(pattern: re.Pattern[str]) -> set[str]:
    hit: set[str] = set()
    for path in _service_files():
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            hit.add(path.name)
    return hit


def _domain_table_write_pattern() -> re.Pattern[str]:
    table_group = "|".join(sorted(PROTECTED_DOMAIN_TABLES, key=len, reverse=True))
    return re.compile(rf"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+({table_group})\b", re.IGNORECASE)


def test_legacy_debt_sets_match_reality():
    """白名单是上限：出现新违规即失败。

    stale（登记了却已不再违规）只做断言性提示，要求维护者删除过期登记，
    但不作为常规回归卡点——很多入口合法地不含裸 commit（提交由 WriteCoordinator 负责）。
    """
    actual_domain_writes = _files_matching(_domain_table_write_pattern())
    newly_bypassing = actual_domain_writes - WRITE_ENTRY_ALLOWLIST
    assert not newly_bypassing, (
        f"新增绕过写入口的文件（必须改用 DomainCommand）: {sorted(newly_bypassing)}"
    )

    actual_self_commit = _files_matching(_COMMIT_PATTERN)
    newly_self_committing = actual_self_commit - SELF_COMMIT_ALLOWLIST
    assert not newly_self_committing, (
        f"新增自行 commit 的服务（必须经 WriteCoordinator）: {sorted(newly_self_committing)}"
    )
