# scripts/ 索引

这里的脚本都在插件进程之外运行，大多直接读写 `wave_memory.db`。
**写库的脚本运行前先停 AstrBot 并备份数据库**（`sqlite_runtime_guard.py` 会检查进程是否还在）。

只放能反复使用、不写死某份数据特征的通用运维工具。为某次具体迁移或某批脏数据写的一次性脚本
跑完即删，不在这里留档；当时做了什么见 CHANGELOG 对应版本与提交记录。

## 部署

| 脚本 | 用途 |
|---|---|
| `deploy_to_container.ps1` | 把一个已提交版本部署进本机 AstrBot 容器：单测 → 卷外数据库快照 → 代码备份 → 按清单替换代码 → 重启并核对启动日志里的提交号；`-DryRun` 预览，`-Rollback` 恢复最近一份代码备份，首次部署加 `-PruneUnknown` 清掉 v5 遗留模块 |
| `_deploy_apply.py` | 部署时在容器里套用代码的步骤（只动代码文件，不碰 `.git`、`data/`、`*.db`、`*.tar`）；远程主机上的容器也用它：`git archive` 出 tar 与版本戳 json，拷进容器后运行 |
| `boot_check.py` | 用真实 AstrBot + 临时空数据目录完整启动一遍插件并走一遍 Runtime API（抓单测漏掉的导入错误） |

## 数据库维护（须停 AstrBot）

| 脚本 | 用途 |
|---|---|
| `compact_database.py` | 离线压缩：WAL checkpoint、清理过期流水账、VACUUM |
| `d1_reorder_memories.py` | 把 `memories.vector` 挪到最后一列，让过滤列不落进溢出页；新库另起文件，逐行哈希校验后才可替换 |
| `rebuild_hot_memory_hnsw.py` | 只用活跃候选重建有界的热记忆向量索引 |
| `repair_sqlite_runtime.py` | 离线修复 / 重新打包 SQLite 数据库 |
| `backfill_person_timeline_evidence.py` | 回填印象时间线缺失的原话证据（支持 `--dry-run`，写库前自动备份） |
| `backfill_fact_evidence.py` | 给缺来源记忆的事实回挂可核原话；默认只读预演，`--apply --yes` 才写，事务内复核并生成逐行撤销 SQL |
| `backfill_jargon_tag_chain.py` | 给已确认但缺标签链的黑话补 `source_tags`；默认只读，`--apply --yes` 才写，同样带复核与撤销 SQL |
| `sqlite_runtime_guard.py` | 写库脚本的运行前检查（被其他脚本引用，不要删） |

## 诊断与评测（只读）

| 脚本 | 用途 |
|---|---|
| `db_health_check.py` | 运行时 SQLite 健康检查 |
| `db_inventory.py` | 列出运行时数据库与恢复相关的数据文件 |
| `export_runtime_data.py` | 导出运行时数据副本，供备份与恢复检查 |
| `retrieval_eval.py` | 检索质量评测：抽真实查询、模型评审相关度、算 nDCG / P@k / MRR；`run --mode shotgun` 评测线上注入用的检索 |
| `compare_cooccurrence_kernels.py` | 离线对比共现传播核（同一批查询、两种核的种子命中与涌现） |
| `expr_corpus_audit.py` | 只读审计表达候选在群语料里的真实出现次数与说话人数（`mode=ro` + `query_only=ON`） |

## 约定

- 生产库路径常见：`/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db`
- 群号、Bot ID 等作用域一律走参数，不写死在脚本里
- 不在脚本内提交密钥、用户库、`.env`
