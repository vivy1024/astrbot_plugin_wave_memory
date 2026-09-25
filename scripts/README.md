# scripts/ 索引

这里的脚本都在插件进程之外运行，大多直接读写 `wave_memory.db`。
多数默认 **只读 / dry-run**；带 `--apply` 的须确认令 + 运营授权。
**写库的脚本运行前先停 AstrBot 并备份数据库**（`sqlite_runtime_guard.py` 会检查进程是否还在）。

v6 按「还在用 / 已执行完毕」分了类。已执行完毕的脚本没有移动位置：它们靠 `Path(__file__)`
推算仓库根目录，挪进子目录会失效；需要复查历史操作时仍可原样运行。

## 还在用（运维）

| 脚本 | 用途 |
|---|---|
| `deploy_to_container.ps1` | 把一个已提交版本部署进 AstrBot 容器：单测 → 卷外数据库快照 → 代码备份 → 按清单替换代码 → 重启并核对启动日志里的提交号；`-DryRun` 预览，`-Rollback` 恢复最近一份代码备份，首次部署加 `-PruneUnknown` 清掉 v5 遗留模块 |
| `_deploy_apply.py` | 部署脚本在容器里调用的套用步骤（只动代码文件，不碰 `.git`、`data/`、`*.db`、`*.tar`） |
| `boot_check.py` | 用真实 AstrBot + 临时空数据目录完整启动一遍插件并走一遍 Runtime API（抓单测漏掉的导入错误） |
| `db_health_check.py` | 运行时 SQLite 健康检查 |
| `db_inventory.py` | 列出运行时数据库与恢复相关的数据文件 |
| `export_runtime_data.py` | 导出运行时数据副本，供备份与恢复检查 |
| `repair_sqlite_runtime.py` | 离线修复 / 重新打包 SQLite 数据库 |
| `sqlite_runtime_guard.py` | 写库脚本的运行前检查（被其他脚本引用，不要删） |
| `rebuild_hot_memory_hnsw.py` | 只用活跃候选重建有界的热记忆向量索引 |
| `backfill_person_timeline_evidence.py` | 回填印象时间线缺失的原话证据（支持 `--dry-run`，写库前自动备份） |
| `post_governance_healthcheck.py` | 数据治理后的只读健康检查 |
| `observation_idle_check.py` | 观察空闲抽检（库 + 热 HNSW inactive） |
| `retrieval_readiness_readonly.py` | 检索结案门禁（含 config / person / collapse / HNSW） |
| `cross_group_same_content_dedupe_dryrun.py` | 跨群同文 dry-run；可选 soft-delete apply + FTS/HNSW |
| `govern_wave_memory_storage.py` | 离线、可回滚的存储治理 |

## 已执行完毕（一次性迁移、治理与验收，只留作记录）

- **2026-06 身份污染与来源清理**：`cleanup_legacy_social_data.py`、`full_cleanup_identity.py`、
  `quarantine_roleplay_memory.py`、`migrate_sources.py`
- **2026-07 Scope 与 fanout 治理**：`scope_quality_migration.py`、`accept_five_success_criteria.py`、
  `apply_classified_scope_recovery.py`、`phase2_scope_recovery.py`、`verify_phase2_production_readonly_status.py`、
  `mark_fanout_duplicates.py`、`fanout_*.py`、
  `build_fanout_cleanup_sample.py`、`refresh_fanout_cutover_package.py`、`inventory_active_unscoped_hold_groups.py`、
  `quarantine_bot_unscoped_noise_dryrun.py`、`unscoped_owned_formalize_dryrun.py`、`wave_governance_readiness_preflight.py`
- **2026-07 关系证据补全**：`apply_event_audit_only_production.py`、`fill_missing_formal_relationships.py`、
  `refill_missing_evidence_summaries.py`、`relationship_evidence_*.py`、`run_evidence_summary_*`、`run_same_bot_grants_pilot.sh`
- **2026-07 验收冒烟**：`smoke_*.py`
- **2026-09 v5 收口**：`migrate_legacy_facts_dryrun.py`、`migrate_legacy_facts_apply.py`、
  `purge_noisy_relationship_events.py`、`reset_impressions_and_halve_affinity.py`

对应的服务层迁移代码已移到 `services/migrations_archive/`，插件运行时不导入。

治理 / fanout / phase2 工具链 **默认不要在生产 apply**；当时的执行记录见 CHANGELOG 对应版本。

## 约定

- 生产库路径常见：`/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db`
- 含 `--allow-production` / confirmation 的脚本：必须双确认
- 不在脚本内提交密钥、用户库、`.env`
