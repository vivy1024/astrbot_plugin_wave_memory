# Changelog

## v5.1.0 (2026-09-25)：多 Bot 通用化与「Bot 是一个人」

本版本包含两部分：多 Bot 通用化与插件化（内部代号 v6，见 `docs/roadmap-v6.md`），以及按 Bot 本人重划记忆边界（第二部分）。**升级前务必备份 `wave_memory.db`**，两部分的升级须知都要看。

### 一、多 Bot 通用化

Bot 不再只能是静态配置里的两个槽位。本版起 Bot 存进 WaveMemory 自己的 `bot_profiles` 表，数量不限，在 9876「Bot 管理」页新增、编辑、停用、导入导出，保存即热生效。

#### 行为变化

1. **Bot 注册表热重载**：保存 Bot 后原地刷新 ScopeResolver、MetaThinking、好感引擎、写入器的 Bot 关键词、身份安全快照与 WebUI 作用域选项，不需要重启 AstrBot。编辑带乐观锁（版本不一致返回 409），每次修改留历史（每个 Bot 保留 50 条）。
2. **多身份绑定**：一个 Bot 可以有多个 QQ 账号、Cortico 部署名、B 站直播间。Runtime API 按 `bot_id` 或部署名（`scope.deployment` / `X-Cortico-Deployment`）解析 Bot，找不到直接返回 400，**不再默认落到羽书名下**；请求不带会话时用 Bot 绑定的直播间。
3. **规范会话前缀**：`session_prefix` 固定会话 id 与用户 principal 的前缀，与 AstrBot 平台实例名解耦。平台改名后新旧记忆仍是同一条线；事件原始平台 id 另存为 `host_platform_id`。
4. **代码里不再写死角色**：自称词、常驻书设、第一人称经历来源、日记署名、Bot QQ 号、`/context/prepare` 的书设与系统发言者、`user_profiles` 的默认 `bot_id` 全部改从 Profile 读取；身份安全、黑话过滤、召回分段通过 `domain/bot_identity` 快照感知所有已注册 Bot。
5. **修复**：跨平台身份关联拒绝中文平台前缀（`羽书:user:…` 无法关联）。

6. **Cortico 注入走完整编排器**：`/context/prepare`（及别名 `/inject`）不再是另写的简化版，改为复用 AstrBot 路径的 `InjectionOrchestrator` 与同一套通道实例、同一份通道配置（含 Bot Profile 的通道覆盖）。按 `tier` 选通道：`full` 全部、`light` 去掉书设检索/风格样例/人格包、`minimal` 只留记忆、原词、事实、关系。trace 标注来源（默认 `cortico`），可在注入观测台核对。删除了写死的书设与 `LIKE` 模糊检索。
7. **配置统一**：新增 `/api/config/inventory` 与 9876「配置来源」页，把 AstrBot 静态配置、热参数、注入通道、Bot 覆盖四层摊平，每项显示默认值、当前生效值、来源层与生效方式；列出"默认开启但被保存成关闭"的可疑开关。
8. **热参数持久化**：没有静态配置映射的热参数改存 WaveMemory 数据库 `config_overrides`，重启后仍生效（此前只对当前进程有效）。
9. **写死的常量改为热参数**：消息合并窗口 `ingress.debounce_seconds`（默认 4 秒）、最长等待 `ingress.debounce_max_seconds`（默认 12 秒）、注入慢警告 `injection.slow_warning_ms`（默认 2000 毫秒）。

10. **插件化：工具注册表**：18 个内置工具改为在 `tools/builtin_registry.py` 登记 `ToolSpec`（工厂 + 能力开关 + 分组），`main.py` 只按能力开关实例化。同一份工具实例同时交给 AstrBot 与 Runtime API：新增 `POST /api/runtime/v1/tools/<name>`（带 `scope`、`arguments`），`/capabilities` 列出全部工具（`?bot_id=` 时按该 Bot 的工具开关过滤，并返回 Bot 的规范会话前缀）。Bot Profile 的 `tools_allow`/`tools_deny` 在两条路径都生效（AstrBot 侧在 `on_llm_request` 里从本次 ToolSet 移除）。修正：生效的 `wave_memory_affinity` 来自 `affinity_update`，不是 `extra_tools` 里的退役占位。
11. **插件化：通道注册表**：12 个注入通道改为在 `services/injection/channel_registry.py` 登记，顺序与参数不变。外部扩展放 `<plugin_data>/extensions/*.py`，实现 `register(tool_registry, channel_registry=...)` 即可登记工具和通道（外部通道需自带默认配置）。
12. **写入统一**：`/observations/batch` 不再只把文本塞进写入队列，改为走与 AstrBot 消息钩子相同的流程：`message`/`danmaku` 进 InboundMessagePipeline（记住/忘记/teach、入库、黑话积累、纠错自省、好感触达），`self`（Bot 自己说的话）进与 `after_message_sent` 共用的 `_process_bot_reply`（入库、互动计数、未结算印象、自省记录）。`event_id` 必填，按「Bot + 可见性 + 会话 + 平台消息号」去重；AstrBot 路径同样改用平台原始消息号（此前取不到 `message_id` 时不去重）。

13. **热插拔**：新增后台服务注册表与 9876「服务与扩展」页。标签提取、做梦、记忆淘汰、好感生命周期、维护任务执行器可以单独停止/启动/重启（好感引擎停止前先落盘缓冲），不中断 QQ 回复、不重启 AstrBot；重启时任务名加代次后缀，满足 TaskSupervisor 任务名唯一的约束。工具可以在同一页整体停用，AstrBot 与 Cortico 立即不再提供。
14. **可视化**：注入观测台按来源（AstrBot / Cortico）筛选，列表显示来源；v6 之前的 trace 视为 AstrBot。

15. **修复自动备份**：v5 直接复制正在使用的数据库文件，WAL 里还没合并的写入会漏掉；判断"上一份备份"时按文件名排序，会挑到手工留的 `wave_memory_before_*.db`，导致每次启动都复制一遍。改用 SQLite 在线备份接口一次拷完（同一读快照、读不挡写），只按自动备份文件的修改时间判断间隔，在后台线程执行不阻塞启动；轮换时连同 `-wal`/`-shm` 一起删。实测 4.4 GB 库 12 秒，`quick_check` 通过。
16. **outbox 历史清理**：`write_operations`、`domain_outbox`、`outbox_deliveries` 只追加不清理，线上合计约 0.9 GB。记忆淘汰服务每轮顺带删除 30 天前已归档且投递完成的事件、对应投递记录，以及不再被引用的已提交写操作（始终保留最大写序号）。每批 500 条一个短事务，批间让出写线程。在线上库副本上实测可回收约 710 MB。
17. **`/memories/query` 改用 FTS5**：不再对记忆表做 `LIKE '%词%'` 全表扫描（线上副本实测 8.4 秒 → 5 毫秒），并只匹配正文列（全文索引同时收录了发言人昵称）。

18. **代码健康**：一次性迁移与恢复代码（约 4900 行：`approved_scope_recovery`、`scope_recovery_migration`、`legacy_relationship_migration` 等）从服务层移到 `services/migrations_archive/`，插件运行时不导入；`scripts/README.md` 按「还在用 / 已执行完毕」分类。新增 GitHub Actions：Python 3.12 跑 pytest，前端跑类型检查、单测与构建。

19. **拆分组合根**：`main.py` 从 3761 行降到 164 行，只留 `@register` 入口、`initialize/terminate` 和 6 个 AstrBot 钩子的一行转发（AstrBot 按 handler 的 `__module__` 把钩子绑到插件，钩子必须定义在 main 模块）。实现按领域拆成 `app/` 下的 mixin：`bootstrap`（构造期组装）、`startup`（启动与关停）、`ingress`（消息入口与写入）、`injection`（LLM 请求钩子与注入编排）、`maintenance`（维护任务与派生投影），顶层辅助在 `app/common.py`。新增 `scripts/boot_check.py`：真实 AstrBot + 临时空数据目录完整启动一遍插件并走一遍 Runtime API。

20. **中文全文索引**：旧 `fts_memories` 用 unicode61 分词，中文连续字串整段算一个词，搜「张羽」只能命中它单独成词的行，搜不到时 fts5 通道再对全表 `LIKE`（约 8 秒）。新增派生索引 `fts_memories_cjk`：正文按相邻两字切词，查询词组成相邻短语（「张羽」不会命中「张某羽」），不依赖 jieba。由 outbox 消费者 `fts_cjk` 增量维护，历史数据由维护任务 `maintenance.fts_cjk.rebuild` 分批回填（启动时自动排队）；回填完成前 fts5 通道与 `/memories/query` 继续用旧索引。线上副本实测：30.7 万条回填 17 秒；「张羽」旧索引 113 条 → 2696 条（`LIKE` 2710 条，差额为隔离/噪声/归档行），「尖塔」0 → 154 条，查询毫秒级；fts5 通道在新索引就绪后不再退回全表 `LIKE`。

21. **每个 Bot 的书设语料**：Bot Profile 新增 `persona.lore_corpus`（9876「Bot 管理」→ 书设语料）。留空用部署默认语料；`none` 表示这个 Bot 不读书设；填其他语料 id 时须与部署加载的一致，否则书设通道返回 `corpus_not_loaded:<id>`，不会把别的世界观当成自己的设定。书设注入通道与纠错自省同时遵守。**修复**：纠错自省从 v5 起没拿到 CatalogScope，检索书设参考一直是空的，现在按 Bot 的语料检索。

22. **部署与版本可查**：新增 `scripts/deploy_to_container.ps1`，把一个已提交版本部署进 AstrBot 容器——本地单测、卷外数据库快照、容器内代码备份（宿主机 `AstrBot-master/data/backups/wavememory_code`，保留 5 份）、按部署清单替换代码（只动代码文件；上一版有、这一版没有的文件删除）、重启后核对启动日志里的提交号；支持 `-DryRun`、`-Rollback`。部署时写 `_deploy_version.json`，`/api/health` 与 Runtime `/capabilities` 新增 `build`（版本、提交、分支、部署时间），启动日志 `Fully initialized` 带提交号。

23. **扩展热重载**：9876「服务与扩展」页新增「重新加载扩展」（`POST /api/extensions/reload`）。卸下扩展登记的工具（同时从 AstrBot 工具列表撤下）与注入通道，重新执行 `extensions/*.py` 后只实例化扩展部分；内置工具与通道、9876 的工具开关、外部通道的停用状态都不动。扩展改为直接从源码编译（不走 `__pycache__`，否则同一秒内改过的文件会读到旧字节码）。页面列出每个扩展文件登记的工具。

24. **静态配置只重建受影响的服务**：在 9876 设置页保存做梦（`Lifecycle_Settings.enable_dream`、`dream_*`）或记忆淘汰（`Eviction_Settings.*`）相关的静态配置后，只停掉并按新配置重建这一个后台服务（关掉时只停不建、打开时创建并启动），不用重启 AstrBot；保存结果的 `apply_modes.service` 列出每个服务的动作。这些字段在设置页与「配置来源」页的生效方式显示为「重建服务」。此前它们标成"保存即生效"，实际要重启才生效。其余服务依赖较多，仍按原方式生效。

25. **宿主端口**：新增 `domain/host_ports.py`，把核心对 AstrBot 的全部依赖面写成 Protocol：消息事件（7 个读取方法）、插件上下文（查 provider、登记/撤下工具）、工具管理器、LLM provider 的 `text_chat`、函数工具。`scripts/boot_check.py` 用真实 AstrBot 源码核对这些方法都在、全部内置工具满足 `HostTool`，AstrBot 升级改名时启动检查直接报出来；作用域解析与 LLM 降级链按端口标注类型。

26. **修复（上线首跑发现）**：中文全文索引回填任务的数据库连接跨 `asyncio.to_thread` 使用，线程池换线程后报 `SQLite objects created in a thread can only be used in that same thread`，回填失败、新索引一直不就绪（查询方继续用旧索引，不影响功能）。部署脚本改为按「重启前最后一条启动完成行」判断新进程启动（Docker Desktop 上 `docker logs --since` 对该容器始终返回空）。

27. **修复 `wave_memory_search` 工具**：语义检索为空时的原词兜底改为与注入 fts5 通道同一套作用域（本 Bot、可见性、跨群开关）与索引（中文索引就绪后用它），此前只按群过滤、会搜出同群其他 Bot 的记忆，且仍查旧的整句分词索引。上下文展开改为取命中所在会话、本 Bot 的前后各 2 条（在前后 200 个 id 内找）：此前按 `id±2` 且只按群过滤，多群交错写入时常常只剩命中本身，还可能混入其他 Bot 的副本。

28. **消息入库报警**：新增入站统计（最近 10 分钟接收 / 被拒，按原因分类，记下未绑定的 Bot 账号）。被拒超过 5 条且过半时，`/api/health` 降级、9876 首页「系统健康」标红，并写明未绑定的账号与处理办法。此前 QQ 换号后所有消息因 `unknown_bot_self_id` 被拒约 20 小时，只在日志里每分钟一条 WARN。
29. **embedding 缓存**：同一句话在一次注入里会被 memory、book_lore 各算一次，入库时再算一次。结果缓存 10 分钟（512 条），并发的同句请求合并成一次 provider 调用；返回副本，调用方改动不影响缓存。
30. **同步通道不再串行**：belief、facts、fewshot、fts5、jargon、soul_state 这 6 个通道的 build 里全是同步读库，在事件循环上依次执行，所有通道的完成时间被拖到同一刻（线上中位数都是 67 ms、最坏 660 ms）。改为在工作线程里执行（`offload_to_thread`）。
31. **打分不再被重要度与访问次数主导**（行为变化）：旧公式「相似度 × 重要度 × 时间衰减 × 访问加成」里重要度（0.1~3）与访问加成（不封顶）压过相似度，而每次召回又给重要度 +0.01，形成「越被召回越重要」：线上被召回最多的是「@某人」这类空消息（访问 1449 次、重要度 3.0），40 条真实查询的前 5 名里 92% 是高访问记忆。现在重要度限幅 0.3~2 后按权重 0.25 参与、访问加成封顶 1.1；召回只记访问次数、不再改重要度。首次启动一次性扣回存量里召回累加的重要度（线上副本 35,520 条、3.8 秒；旧值备份在 `memory_importance_repair_v51`，可还原）。检索实验室可用 `importance_weight`、`access_boost_cap` 参数对比，设为 1 和 3 即旧公式。
32. **删除 `wave_memory_deep_search`**：v5 起就不再注册，能力由 `wave_memory_search` 承担。
33. **标签覆盖率统一口径**：首页、维护页、标签页此前各算各的（维护页只数旧 `memory_tags`，首页分母含噪声与归档），显示的缺失率远高于实际。新增 `/api/tags/coverage`：只算活跃记忆，按「已标 / 太短 / 模型判空 / 失败 / 标签丢失 / 排队 / 不在范围」互斥拆分，给出「应打已打」、积压与预计耗时。标签页新增覆盖面板：分类条、样例下钻、带确认的重新提取、按 Bot 拆分、TagWorker 状态与「立即处理一批」。线上真正无标签 18,473 条（6%），其中 8,850 条是 v5 清理后「状态完成但标签丢失」，可一键重新提取。
34. **书设工作台**（9876「知识 → 书设工作台」）：此前书设是 6 月 GraphRAG 离线快照（整本 10.5 小时），之后的新章节靠手工往 `book_lore.db` 插笔记，插进去的笔记没算向量、也没进索引（线上 2,497 条笔记只有 2,020 条在索引里，第 950 章以后全部缺失），检索和注入都看不到。工作台保存笔记时当场算向量、写库、增量更新笔记索引，Bot 下一句话就能用到；「补齐索引」补上缺失的笔记；扫描插件数据目录与 novel_docs 下的小说原文，列出库里还没有的章节一键导入，也可直接粘贴新章节；笔记可搜索、编辑、删除，改动记在 `book_lore_edits`。
35. **书设注入带上笔记**（行为变化）：书设通道此前只查 GraphRAG 社区摘要，之后的新剧情永远进不了对话。现在另按相似度取 1 条笔记（章节事件、人物、世界观…），白真真的个人经历不参与。
36. **修复**：书设笔记索引删改后检索报 `Cannot return the results in a contiguous 2D array`（按含已删除元素的数量取 k）。
37. **修复「标签丢失」的真正原因**：TagWorker 按模型有没有返回标签决定记 done / skipped，但标签入库前还要过准入（停用词、单字、低置信）。模型给了标签却全被拒掉时仍记 done，记忆上没有任何标签，覆盖率就把它算成「标签丢失」，重新提取也只会再被拒一遍（线上 13,299 次提取属于这种情况，没有任何关联真的被删）。现在按实际挂上的标签判定；首次启动把存量「done 但无标签」改为 skipped（旧状态备份在 `tag_status_repair_v51`）。
38. **shotgun 检索少一次向量调用**：上下文消息入库时已经向量化过，改为直接用库里的向量；库里没有的与当前消息合并成一次调用（此前是两次串行的远端调用，单次约 90 ms，冷连接约 250 ms）。`/api/health` 的 embedding 项新增缓存命中统计。
39. **存储瘦身**：写操作日志与 outbox 历史保留期 30 → 7 天，投影水位 `derived_projection_state` 随之清理（此前只增不减，线上 88 万行）；注入 trace 的完整载荷只留 3 天（`payload_retention_days`，更早的保留通道明细）；中文全文索引改为不存正文副本的 contentless 表（SQLite 3.43+）；删掉两个与主键 / 唯一约束重复的前缀索引；旧全文索引的更新触发器只在正文、昵称、群号变化时重写，不再被访问计数、版本号、向量回填这类更新触发。存量库用 `scripts/compact_database.py` 在停机时离线压缩（清理、重建中文索引、VACUUM）。
40. **共现图落盘，重启不再全量重建**：共现图此前只在内存里，每次启动都从 105 万条标签关联全量重算（单次 15–50 秒）。现在每次重建后写入 `plugin_data/.../cooccurrence_graph.json`（只存裁剪后的正向图，几百个节点），启动时直接加载；快照之后新增的标签关联计入待处理变更，按原有阈值和 30 分钟冷却期再重建。
41. **全量构建挪到子进程**：构建是纯 Python 双重循环（约 250 万次配对），在插件进程的线程里会一直占着 GIL，期间注入被拖到 10–30 秒、写事务被拖长。现在在独立子进程里构建（`engine/cooccurrence_worker.py`），语义增益直接读 `tag_pair_similarity` 表（与原服务同一数据源），线上 12 秒、只占另一个 CPU 核；子进程失败时退回原来的线程构建。
42. **修复启动后共现任务反复重跑**：写入协调器拿写锁时等满 busy_timeout 仍被占用就直接失败，维护任务「标记成功」失败后整体重跑（线上一次启动连跑 4 次、8.5 分钟）。现在 `BEGIN IMMEDIATE` 撞锁时重试 2 次（此时事务函数尚未执行，重试安全）；内生残差落库改为一次批量写入，缩短写事务。

43. **修复注入观测台默认为空**：AstrBot 写入的 trace 的 `bot_id` 列存的是 QQ 号，Bot 的 db_id 在 `bot_profile_id`；trace 也没有 session_id。按 Bot 筛选此前只能匹配 Cortico 写的 71 条，再加群筛选就是 0 条。现在 Bot 同时匹配两列，没有会话信息的旧 trace 按群号回退（线上 3-5 层群 0 → 2,629 条，不同群不串）。
44. **昵称清洗**：QQ 群名片偶尔带 protobuf 残片入库，人物页显示成乱码。新增 `domain/display_name.py`，所有写入口统一清洗（去控制字符与残片、折叠空白）；前端显示前同样兜底。存量只修真正损坏的值（线上 18 处，旧值备份在 `display_name_repair_v51`），纯空白一类样式问题不改写。
45. **WebUI 静态资源压缩与长缓存**：构建时预压缩 `.gz` / `.br`，按 `Accept-Encoding` 返回（首屏主要文件 br 后约为原来的 1/4）；带哈希的资源一年缓存（immutable），HTML 入口 `no-cache`。
46. **清理**：删除旧 v4 静态界面（`static/index.html`、`app.js`、`styles.css` 与 alpine 依赖）、Vite 模板残留；侧栏去掉 `shadcn · Nova` 模板字样；正文字体统一为 Geist（此前声明的 Inter 并未加载）。

#### 升级须知

- 首次启动自动把 `MetaThinking_Bot1/2`（以及任意 `MetaThinking_BotN`）迁进 `bot_profiles`：`db_id` 不变，会话前缀从该 Bot 最近的记忆里检测（线上为「羽书」「白真真」），v5 写死的人设片段按 db_id 补进 Profile。**历史数据一条不改。**
- 迁移之后以数据库为准：**再改 AstrBot 静态配置里的 Bot 槽位不会生效**（升级用户需知晓），请到 9876「Bot 管理」页修改。
- `_conf_schema.json` 旧槽位的身份默认值改为空，只影响全新安装。
- Runtime API 调用方必须带 `scope.bot_id` 或部署名；`/users/<uid>/profile` 需要 `bot_id` 查询参数或请求体字段。
- outbox 清理删除的写操作会带走其幂等键：超过 7 天后重放同一请求会被当作新请求执行。
- 首次启动会删除 `idx_scoped_tags_scope_name`、`idx_scoped_memory_tags_scope_memory` 两个重复索引并替换旧全文索引的更新触发器，均为秒级；文件真正变小需要停机运行 `scripts/compact_database.py`（线上 4.4 GB 约需数分钟）。
- `/memories/query` 需要带至少一个 2 字以上的关键词，或只按 `uid` 查最近记忆。
- `/observations/batch` 的事件必须带 `event_id`（平台原始消息号），写入流程未就绪时返回 503。
- 纠错自省开始检索书设参考（此前一直为空）；不希望某个 Bot 读书设时把它的书设语料设为 `none`。
- 首次启动一次性扣回召回累加的重要度（约 3.5 万条，几秒）；打分公式变化会改变召回排序，如需对比可在检索实验室用 `importance_weight=1`、`access_boost_cap=3` 还原旧公式。
- 书设注入会多带一条笔记（最新章节等），token 略增。
- 首次启动后台回填中文全文索引（线上约 30 万条、十几秒），数据库增大约 200 MB（4.4 GB 库实测）。
- `/context/prepare` 在注入编排器未就绪时返回 503（此前会退回简化检索）；响应新增 `trace_id`、`elapsed_ms`，`channels` 内含每个通道的状态、条数、token 与耗时。

---

### 二、Bot 是一个人

按"羽书是一个人"重新划定记忆边界：只记得自己亲历的事，对同一个人在哪都认得，自己的状态跨场合连续，私聊里的事不在别处说。

#### 行为变化

1. **只记得自己亲历的事**：向量召回（含跨群）、FTS5、标签冷召回与目录映射全部按 `bot_id` 隔离。此前同一部署里的其他 Bot（如白真真）在任何群见过的消息都能被召回。无归属的历史旧行仍对所有 Bot 可见。
2. **Bot 自身状态属于 Bot 本人**：心情只有一个，延续到所有群与私聊；关切、自我经历时间线、信念不再按群分裂；人格、信念与状态在私聊中同样注入。信念的依据仍在它形成的群里校验。
3. **私聊可写**：私聊中可记录印象与好感、人情备忘、关切、经历与日记，也可提审事实。好感数值汇入对这个人的整体态度；印象文字、关系理由、私聊事实等具体内容按 `private:<会话ID>` 保存，只在该私聊可见。私聊里只能记录关于私聊对象本人的印象与人情备忘。
4. **私聊记得对方在群里说过的话**：跨群开关开启时，私聊召回会带上同一 Bot、同一平台下对方本人在群里的已解析发言。
5. **事实跟着 Bot 走**：已批准的群事实在同一 Bot 的其他群与私聊中可用；私聊事实只在该私聊注入；WebUI 事实页可选择私聊会话审核。消息中出现已知实体名（包括 jieba 会切碎的人名）时按原样匹配。
6. **未决能量读对位置**：关系注入与反思触发器改为读取跨群能量池（`group_id=""`），此前一直读到空值或旧行。
7. **跨平台身份关联**：新增 `person_identity_links`（按 Bot 保存），由管理员在 WebUI 人物详情中确认同一个人在不同平台的账号；关联后态度汇总与印象时间线合并。模型不会自动关联身份。
8. **原话证据**：印象时间线接口修复缺少 `import re` 导致的 503；按时间窗口推测的原话标记为 `source_quote_inferred` 并在界面标为"推测原话"。`scripts/backfill_person_timeline_evidence.py` 支持 `--db/--limit/--dry-run`，写库前自动备份。

#### 升级须知

- **升级前务必备份 `wave_memory.db`**。首次启动会在事务内重建 `scoped_soul_*` 与 `scoped_facts`/`scoped_fact_history`/`scoped_fact_reviews`，把 `visibility` 约束放宽为群聊或私聊；保留全部数据、索引与自增序号，已迁移的表会跳过。
- **`cross_group_enabled` 含义调整**（升级用户需检查配置）：开启时同一 Bot 的各群记忆互通，并允许私聊召回对方本人的群发言；无论开关如何，不同 Bot 之间的记忆都不再互通。
- 私聊场合的 legacy 键使用 `private:<会话ID>`，避免与同号群混淆。

---

## v5.0.0 正式版 (2026-09-22 最终成熟版)

**架构收敛与认知沉淀全闭环**：在 09-12 跨群好感度合并的基础上，完成了工具箱历史堆叠彻底收敛、5 维未决能量蓄水池与定向跃迁闭环、四级自动原话溯源引擎、群分析日记无缝桥接、以及外置认知 Runtime v1 服务的全量发布！

### 核心亮点

1. **LLM 工具体系大收敛（从 21 个精简至 13 个，净省 1500+ Token Schema）**：
   - **社交动力学单出口**：将 `affinity_update` 与 `social_impression` 深度合流为单一全能的 `wave_memory_record_social_impression`，全面支持 `reason`/`delta` 等直观别名入参，支持自动主观定性推导与原话自动回溯；
   - **统一自然搜索入口**：升级 `wave_memory_search` 为统一工具，融合向量相似度、FTS5 精准词项兜底以及**带严格 Scope 隔离的对话切片窗口（include_context: true）**，彻底下线重复的 `deep_search`；
   - **下线 4 个自愈僵尸工具**：默认关闭日常闲聊中零调用的 `agent_feedback_tools`（`explain_injection`, `feedback_memory`, `suggest_config`, `submit_review_candidate`），释放上千 Token 宝贵预算，消除大模型选择瘫痪；
   - **书设工具收敛**：默认仅向模型暴露单一的 `wave_memory_book_lore_search` 语义检索入口。

2. **多维未决能量与定向跃迁动力学**：
   - 告别单人群聊好感度孤岛：在 `person_unsettled_state` 中建立跨群 `(bot_id, user_id, "")` 5 维能量蓄水池；
   - 平时受严密步长限制（[-2.0, 2.0]），**蓄满 10.0 时触发定向跃迁（上限放宽至 [-5.0, 5.0]）**；
   - 自动合成带 `【关系跃迁】` 标签的因果里程碑短语，定向清空已跃迁维度的能量池，保留其余维度。

3. **四级自动记忆溯源引擎（反查原话证据）**：
   - 彻底修复大模型猜不出整数 `source_memory_id` 导致事实/经历无据悬空的顽疾；
   - 引入四级安全溯源算法：`显式正整数 ID -> 原话模糊查询反查 (LIKE ESCAPE '/') -> 发言人最新有效记忆 -> 当前 Scope 兜底有效记忆`，确保提审“字字有据、铁证如山”。

4. **群分析成果与生活日记无缝桥接（零额外开销）**：
   - 新增 `DailyDiaryBridge`：自动读取 `astrbot_plugin_qq_group_daily_analysis` 产出的 `traces.db` 汇总数据；
   - 复用其对 3200 条消息总结产出的 `SummaryTopic`、`GoldenQuote` 与 `QualityReview`，转化为羽书第一人称生活日记并入选 Bot 经历时间线主干；
   - 避免羽书在聊天会话中重复抓取海量上下文，实现零额外开销的认知自愈闭环。

5. **外置认知 Runtime v1 RESTful 服务**：
   - 新增 `/api/runtime/v1/` 蓝图，支持 Cortico 等独立宿主安全调用 WaveMemory 的记忆、图谱与认知能力；
   - 支持 Bearer Token 认证、跨平台 UID 归一化与专属作用域隔离。

6. **安全与架构加固**：
   - 修复 `memory_search` 上下文切片窗口缺少 Scope 隔离导致跨群消息穿透的严重安全隐患；
   - 修复 FTS5 搜索未消毒转义特殊字符导致语法崩溃的异常；
   - 修复 `affinity_update` 中历史遗留的 `delta NameError`；
   - 架构守卫测试（`test_write_path_guard.py`）严格约束直写行为与事务一致性。

### 自动化测试与验证

- Python 核心与全量套件：**1183 passed / 0 failed / 4 skipped** 全绿（21.02s 跑完）；
- 前端测试套件：**179 passed / 0 failed** Vitest 单元测试全绿；
- 真实 Docker 容器热更新验证通过，系统服务健康接口持续返回 healthy，11.2 万条记忆稳定在线。

---

## v5.0.0 (2026-09-09，含 2026-09-12 修订)

**认知沉淀重大范式转向**：告别旧版本后台定时脚本无节制盲抽的模式，全面转为**「大模型对话现场自主感知提审 + Bot 亲笔日记 + 管理台人工把关审核」**。收敛双时间线体系，彻底清扫历史施工临时表与假资产；索引诊断性能暴提 23 倍；标签神经星云内嵌全息 HUD 自由控制台与算法实验室实时联动。这是不兼容的主版本跃迁：旧 pending 事实/信念/黑话碎屑不再当作可用积攒，按全新自主契约重新沉淀。

### v5.0.0 修订（2026-09-12）

**好感度改为跨群累加，事实去群限制，并清理全仓死代码。** 现场反馈「跨群对某人就不遵循好感度」：记忆与印象时间线本就跨群，唯独关系注入只读当前群那一行，导致同一份 prompt 里时间线写着「救命恩人」而关系状态是「综合值=0」。

#### 修复

- **好感度不分群（模拟真人）**：各群好感维度累加后钳制再推导综合值，存储与主键不动（纯读取侧合并）。实测同一用户跨 3 群累加后综合值 0 → 33、态度 neutral → friendly，与印象时间线叙述自洽。成立前提已核实：`affinity` 列恒等于 `compute_affinity(dimensions)`，全库 1451/1451 行一致。
- **事实不再按群过滤**：`facts` 频道移除 `COALESCE(group_id,'')=?` 硬过滤。实测同一 subject 在当前群命中 0 条 → 去限制后 20 条（散在 7 个群）。
- **修复 WebUI 记忆作业始终失败**：`memory_jobs` 的 `memory.reembed.v1` / `memory.batch.reembed.v1` / `memory.batch.extract_tags.v1` 三个 handler 从未注册进 `DurableJobRunner`，而重新向量化端点确实会入队这些 kind，命中 `job_handler_missing` 直接失败。已接入并补回归测试锁死入队 kind 与 handler 覆盖集合一致。
- **修复既有失败测试**：`test_channel_config` 断言找英文 `'Channel Config'`，而页面早已中文化（实际为「通道配置」）。

#### 清理（净减约 4200 行）

- 删除无人引用的整模块：`consolidation.py`(384)、`compat/scope_adapter.py`(264)、`bot_soul.py`(54) 及配套死测试(282)。
- **好感度公式收口**：`lifecycle` 自研的 `_compute_affection` / `_get_attitude_level` 与 `domain.relationship_policy` 重复，改为委托正式实现；删 `relationship_events` 的两个 0 调用 alias。
- 删除函数级僵尸：`engine/database.py` 12 个遗留 facade 方法、`impression_timeline` 6 个仅测试兼容助手、`lifecycle._record_relationship_events`（legacy `relationship_events` 表的最后写入者）、`main.py` 转发包装、`jargon/sync` 两个被取代的旧解析器、`persona_composer` 两个仅测试函数、`people._person_from_relationship`。
- **废弃配置项清理**：删 4 个 consolidation 兼容键，并移除前端 `HIDDEN_ITEMS` / `isHiddenItem` 整套「隐藏但保留」机制。AstrBot 不会因 `config.json` 多出未知键而拒绝启动（实测 `enable_persona_evolution` 已不在 schema 而线上仍在，插件正常）。
- 删除 17 个全仓零引用脚本（2796 行）；保留 51 个有测试或运维文档引用的。

#### 文档

- **README 配置参考从 6% 补到 100%**：此前 129 个配置字段只记录 8 个，另含 6 个 schema 中已不存在的键。现按 `_conf_schema.json` 重新生成全部 25 组配置表，并补齐顶层字段。
- 新增「写路径与数据一致性」（DomainCommand / WriteCoordinator / Outbox 派发 / 写租约）、「容量与预算」、「运维与治理文档索引」（69 篇 docs 按主题分目录）三节。
- 修正通道清单（移除已删的 `timeline`，补 `holyman_persona`）、项目结构（移除已删模块，补 204 模块规模）、WebUI 页面表（补审查队列 / 算法实验室 / 登录页）、跨群语义说明（好感度与事实已合并，`cross_group_enabled` 只管检索）。
- **新增防漂移测试** `tests/test_readme_schema_coverage.py`：README 必须覆盖全部 schema 字段与分组、不得出现幽灵配置键、不得引用已删模块、通道清单必须与代码一致 —— 任一项漂移即 CI 红。

#### 测试

全量 **1177 passed / 0 failed**。

### 🌟 核心理念重塑：信念与事实全面由大模型自主提审

- **为什么彻底废弃定时抽取循环？**
  - 旧版本依靠后台定时器每隔几小时全表扫聊天切片自动抽取，产生了大量复读机碎片、把群友反串当严肃事实、频繁死锁表等无法根治的质量顽疾。
- **大模型现场自主感知提审（Function Calling 契约）**：
  - **事实（Facts）提审**：对话中出现真实人物/事物变化时，大模型自主调用 `wave_memory_propose_fact`，且**必须附带群友原话（`source_quote`）**，反串/阴阳怪气绝不升格为客观事实。
  - **信念（Beliefs）提审**：模型自主调用 `wave_memory_propose_belief`。信念不再是空泛的人生感悟，而是**对客观事实的深度升华**，强约束必须依托当前群至少 2 条已审核事实，杜绝无根虚妄。
  - **黑话（Cultural Moments）**：大模型捕捉到群内特色梗时调用 `wave_memory_mark_cultural_moment`，与 Holyman 广域词典配合形成「群内原生梗 + 广域词典辅助理解」体系。
  - **Bot 亲笔日记（Diary Episodes）**：模型调用 `wave_memory_record_diary_episode`，在每日总结或重要转折时用自己的口吻写下经历，自动钉入经历时间线。
  - **双通道主动触发闭环**：
    - 现场反思引导（Inbound Reflection）：群聊经历密集交互或纠正时，底层反思机制下发明确的 8 个提审工具指令；
    - 夜间 Cron 定时回顾：配合原生定时任务，Bot 调用流水浏览工具自主复盘今日对话并撰写日记。
- **人工最后一道防线**：所有模型自主提审的资产统一进入 WebUI 审核队列（`review_candidates`），由管理员人工批准后正式入库生效，确保记忆与心智绝对纯净。

- **双时间线彻底分工**：
  - **群友印象时间线（`person_timeline_events`）**：记录「我眼中的他」——沉淀称呼、别名、交往里程碑与加减分账本，支持基于半衰期的指数衰减（默认 21 天，远期事实标明绝对日期），支持按当前群全量分页检索（`/api/people/timeline`）。
  - **Bot 经历时间线（`scoped_soul_timeline`）**：记录「Bot 自己的经历主干」——以每日亲笔日记为骨干，记录成长转折与共同大事件；废弃机械聊天切片。
- **Bot 亲笔日记工具（`wave_memory_record_diary_episode`）**：
  - 极度瘦身与职责单一：仅记录标题、正文、经历摘要、情绪权重，存入 `experience_episodes` 并钉入 `scoped_soul_timeline`。
  - 坚决不越俎代庖：事实、信念、黑话、群友好感与 FewShot 回归各自独立专门工具由 Bot 自主调用。
  - 配合 AstrBot 原生 Cron 定时任务（天然携带真实群聊会话），提供群聊近期流水浏览工具 `wave_memory_browse_recent_chat`。
- **配置参数化暴露（`_conf_schema.json`）**：
  - 在 `Inject_Settings` 新增 `impression_timeline_max_items`（默认 12 条）、`timeline_decay_half_life_days`（默认 21.0 天）、`timeline_boost_old_items_count`（默认 3 条）、`soul_timeline_max_items`（默认 3 条）。
  - 后端 `/api/config`（新旧端点兼容）与通道注入动态读取生效。

### 破坏性变更与除垢

- **生产库物理大除垢**：彻底清空 106.7 万行 7 月 Scope 迁移临时表（`scope_recovery_*` / `migration_*`），清空全部低质/假黑话（745+17）与机械切片（6000+条），库体积 VACUUM 释放约 23.5GB。
- **提纯回注有效群友事实**：从 9月6日历史备份提纯 421 条有效群友别名/称呼/确认事实，去重补入 `person_timeline_events`（现共 6,897 条 / 611 人），生图垃圾与代骂全面丢弃。
- **停掉自动抽取循环**：不再每 4 小时 consolidation 抽事实/信念，不再定时 belief emerge，不再按消息计数自动 jargon mine。服务对象可保留，默认不 `start()` 后台循环。
- **关切不再截词**：入站不再因 `@` 或字数截前 60/80 字写关切或主观时间锚点。人味（问安、投喂、回礼）由注入看见人情账/事实/印象后临场发挥。
- **事实必须带原话**：`wave_memory_propose_fact` 必填 `source_quote`。黑话只用于理解原话；已有元数据明确反串/阴阳则拒绝升格为认真事实。
- **信念是已审事实的二审**：提审与 WebUI 批准统一要求当前群 ≥2 条仍为已批准的 `source_fact_ids`。不是信仰，不是人生感悟。
- **风格高光进正式审查队列**：`exemplar_reply` 写入 `review_candidates`（`style`），必须带当轮 message id；废弃旁路表 `exemplar_reply_candidates`。

### WebUI 重构与体验提升

- **全面技术文案脱敏**：建立中央原因码映射表（`reason-label.ts`），替换全站 48+ 处直显的 snake_case `reason_code` 与技术报错，未知 code 走严谨中文兜底。
- **群友印象时间线独立页（`/knowledge/impressions`）**：全量分页浏览当前群全部印象事件（突破人物详情 40 条预览上限），支持群友 ID / 事实类型 / 关键词多维筛选。
- **双时间线全站互链**：人物页 ↔ 心智页 ↔ 经历片段页 ↔ 印象时间线互相跳转；来源 memory / episode 支持深链定位。
- **SoulPage 时间线收敛**：移除读空表 `time_anchors` 的残留组件，只保留经历时间线单主干，补全中文事件类型标签。
- **ExperiencesPage 体验对齐**：补充 `daily_diary` 专属标签，更新经历体系文案。
- **构建体积与现代化**：构建产物统一产出于 `webui/static/app`。
- **系统配置页重新分组**：20 个后端 schema 章节收敛为 25 个功能分组（记忆召回 / 心智与情绪 / 标签与分类…），改为可折叠容器并记住展开状态，常用组默认展开、搜索时强制展开；隐藏 `_system_status` 这类纯说明项。
- **配置页不再暴露后端字段**：移除逐字段渲染的 `键:`/`来源:`/`有效来源:`/`诊断:` 开发者信息；重写 33 处含 `HNSW`/`canonical`/`manifest`/`Scope`/`Catalog`/`3D` 等实现术语的名称与说明，保留技术准确性。
- **生效方式文案纠偏**：`next_run` 原显示「下次生效」易被读成「要重启」，改为「保存即生效」；同时修掉旧实现拼出的「需要保存即生效」病句。侧栏版本徽章不再硬编码 `v1`，改为后端读取 `metadata.yaml` 的真实版本（解析失败则不显示，不编造）。
- **标签神经星云（In-Canvas HUD 控制台）彻底重构**：
  - **配置做进图内**：彻底解决全屏后无法配置的痛点，画布左上角常驻毛玻璃控制抽屉，全屏沉浸探索模式下调参零阻碍。
  - **参数完全自由无限制**：解除 300 节点和 600 条边的硬编码死板截断，节点数（10~5000）与突触连线容量全开放，支持自由输入与滑块联动。
  - **小白通俗人话解读**：每一项参数附带直白人话解释（节点数、置信度门限、脉冲半衰期等），告别生硬技术黑话。
  - **交互式类型过滤透镜**：右下角图例升级为交互式药丸，点击任意类型即可单独高亮/过滤该类别，支持单选与反选。
  - **顶栏极简净化**：全盘清理外部堆砌的花哨冗余按钮，仅保留大标题、返回与刷新。
- **算法实验室融入标签星云**：
  - 从侧栏导航移除独立的 `/lab` 菜单，将其直接作为标签星云内部的核心高级检索能力。
  - 在星云内直接发起拓扑联想检索，**画布节点实时根据检索算法发光高亮**（脉冲共现种子、残差金字塔选定节点等），并在面板下方实时呈现命中的记忆片段，真正实现在图上动态验证算法。
- **真实 9 大标签类型全链路对齐**：
  - 纠正以往缺失定义的大盲区，补全数据库中真实存在的 `event`（事件 4.7万）、`person`（人物 8900）、`location`（地点 3100）、`time`（时间 2200）4 大真实类型。
  - 配备专属深空璀璨配色体系（活力橙、靛青蓝紫、薄荷碧绿、冰晶天蓝），图例、配置弹窗与调色板 100% 严密对齐。
- **索引与派生数据诊断性能暴提 23 倍**：
  - 重构 `_probe_outbox_consumer_lag`、`_probe_memory_vectors`、`_probe_derived_projection` 针对数百万行（208 万行 outbox、29 万行 memories）的查询路径，耗时从 26.2s 降至 1.2s，彻底解决 15s 超时卡死报错。
  - Quart 异步解耦（`asyncio.to_thread`），彻底消除事件循环主线程阻塞。
  - 前端支持自定义 `timeoutMs` 容错与动态秒数错误提示。
- **标签图谱恢复深空配色 + 图例配置化**：按节点类型恢复彩色（keyword/entity/topic/emotion/fact/jargon）与深空渐变背景；图例类型名、顺序、显隐、是否显示计数改为 `Tag_Settings` 可配置，服务端过滤未知类型名。
- **修复标签图谱全屏失效**：`relative` 与 `fixed` 同时写在 class 中，Tailwind 里 `.relative` 排在 `.fixed` 之后将其覆盖，导致「全屏探索」从未真正铺满；改为定位分支互斥，并在全屏切换后补一帧重算视口。同时删除工具栏重复渲染的缩小/复位/仿真按钮。
- **修 `QueryState` 吞掉真实错误**：`description ?? errorMessage(error)` 使同时传入固定说明时真实失败原因永不显示（影响 Jargon 页两处）；现两者并显，错误优先。Jargon 页两处 `catch {` 同时改为保留错误对象。
- **补齐请求竞态守卫**：经历片段页、系统配置页三条加载路径、信念详情弹窗原无竞态防护，快速切换筛选或重试时旧响应可能覆盖新数据；现统一用 `AbortController` 或请求序号丢弃过期响应。

### 现场提审链路修复

- **修复反思引导静默瘫痪（关键）**：`services/reflection_trigger.py` 从 `person_unsettled_state` 查 `text` 列，而该表真实结构只有 `traces`（JSON 数组）。每次触发反思都抛 `OperationalError: no such column: text`，且因「候选为空 + 存在依赖失败」被判定为 `dependency_error`，**整条反思提示被丢弃**（实测 `prompt_len=0`），大模型收不到任何提审引导。改为读 `traces` 并解析 JSON，坏行只跳过该行不拖垮整源。
- **反思提示写明确切工具名**：原 footer 只有「分别使用各自工具」这类中文名词，模型面对 20+ 个 `wave_memory_*` 工具无法定位。现直接给出 `wave_memory_propose_fact` / `wave_memory_propose_belief` / `wave_memory_mark_cultural_moment` / `wave_memory_note_social_anchor` / `wave_memory_record_social_impression` / `wave_memory_affinity_update` / `wave_memory_note_concern` / `wave_memory_note_episode`。
- **测试盲区修正**：原有 fake conn 返回裸字符串而非真实 JSON traces，导致错误列名永远测不出。fake 已改为真实 schema，并新增 5 条回归测试（查 `text` 必须报错、traces 解析、坏行降级、工具名存在、工具名与 `tools/` 声明一致）。

### 架构与运行时

- 数据库备份改为 `DatabaseBackupManager`，不在 `main.py` 前台 `shutil.copy2`。
- 入站并发锁、命令前缀、抢词咽回收到 `InboundMessagePipeline`。
- 群名预热收到 `PlatformContextManager`。
- `on_message` 防抖后正确传递 `message_ts` / `sender_name`。

## v4.7.2 (2026-08-29)

### 管理台与心智运行时

- **心智强制自省**：按当前群重算只读心智状态，不写库、不调模型、不改版本；管理台提供「刷新数据 / 强制自省」。
- **表格适配**：桌面表和手机卡片共用同一套列定义；信念、黑话、记忆、人物、事实、风格样例、注入、索引、维护、标签等页不再各写一份。
- **筛选与批量操作**：Bot/群筛选栏和批量审核条抽成共用组件。
- **可见文案**：时间线、事实、风格样例、标签图、记忆、信念、黑话、注入观测台、人物、导入等页改成中文，去掉 Canonical Scope / ObjectRef / Affinity 等内部词，保留 Bot、群、记忆、标签。
- **插件市场简介与 README 选型说明**：写清为什么选我们——日常检索只依赖向量模型、记忆注入与黑话/风格/信念/好感等通道配合、配置可按模式和通道细调；并写限制与适用场景。

### 人物关系与证据

- **人物历史关系审计**：只读展示本群历史关系事件，不参与好感计算。
- **关系校准证据**：校准必须选择本群真实记忆；群选项与证据解析按当前 Bot 和群对齐。
- **好感筛选与校准面板**：人物页可按好感范围、是否已记录关系筛选，并展示自动学习、人工调整与最终生效值。

## v4.7.1 (2026-08-06)

### Bug 修复与安全防护

- **修复 impression 印象标记泄露到对话的 Bug**：
  - 新增 `@filter.on_decorating_result()` 钩子（在消息发送前拦截），扫描并提取 `<<impression:...>>`（及兼容 `[impression:...]`），写入用户画像的同时从消息文本中彻底剥离所有标记，确保用户和平台端收到的消息干净。
  - 标记格式从 `[xxx]` 升级为 `<<impression:...>>`，防止被表情管理插件（`astrbot_plugin_meme_manager`）提前误删。
- **防止事件循环死锁**：修复 `WriteCoordinator.transaction_blocking` 在活跃 asyncio 事件循环中调用导致的 30s 阻塞假死问题，加入非阻塞安全回退。
- **打断共现重构自激循环**：`CooccurrenceScheduler` 在无数据变更且处于冷却期内时跳过冗余重构，避免 DurableJobRunner 租约超时导致的死循环重建。

### 关系算法与性能优化

- **好感度增长平滑与边际效应**：
  - 群聊被动 Familiarity 增长从 `+0.5` 降为 `+0.05`，引入边际递减公式 `delta / (1 + current / (limit * 0.6))`，防止高频聊天刷爆好感度。
  - 加速衰减：Familiarity（200→60天）、Trust（90→45天）、Fun（30→14天）、Depth（150→90天），高好感阶段享有 15%~30% 的衰减保护。
  - 引入多维度耦合（敌意压制信任、深度促进信任成长、趣味提升熟悉度）。
- **向量索引恢复 Inline Resize（消除内存碎片）**：
  - 恢复 v4.2.1 经典模式：满容量时原地 Inline 扩容 10000 槽位，废弃反复全量重建持久化的级联链路，解决反复分配销毁 464MB 索引导致的 3.7GB glibc 内存碎片（RSS 膨胀）问题。

## v4.7.0 (2026-07-26)

### 瘦身重构：剥离"自主学习"与"学习中心"空壳管道

- **彻底物理移除 StudyService 与 Learning Center**：删除 `services/learning/` 目录、`StudyService`（自主学习 / 世界观内化）和 `book_experience`（书中经历提取），约清理 8,000+ 行冗余代码。
- **清理学习中心 WebUI 与配置策略**：注销 `/learning-center` API 蓝图，移除前端"学习过程"页面，从 `_conf_schema.json` 中移除 `Learning_Settings` 与 `Study_Settings` 配置节。
- **核心能力解耦与保留**：保留 `SelfReflectService` 纯纠正检测逻辑（断开向已删除候选表的写入），保留 `ScopedFewShotRepository`（正式 FewShot 风格注入通道不受影响）。
- **测试套件修补**：修补引用点与 Schema 断言，移除 15 个过时学习中心测试文件，全量测试套件 100% 通过转绿。

### 新增前端数据视角

- **经历片段页面**（`/knowledge/experiences`）：只读浏览 Bot 对话中积累的历史经历片段，支持情感权重筛选与关键词搜索。
- **全量时间锚点探索器**：在 Soul 页面新增可搜索、可分页的全量 Time Anchors 弹窗，不再被底部卡片限制。
- **Outbox 写入管道健康卡片**：维护任务页顶部展示 Domain Outbox / 投递 / 恢复队列实时积压指标。
- **Tag 治理建议 API**：后端 `/api/tags/governance/suggestions` 已完整对接（前端 `ScopedTagGovernancePanel` 早已具备完整操作面板）。

### 3D 神经云图渲染依赖本地化与视觉增强

- **CDN 本地化**：three.js r128、gsap 3.12.5、tailwind 3.4.16、alpine 3.14.9 等 12 个渲染依赖全部下载到 `webui/static/vendor/`，explore.html 与 index.html 零外部域名引用。修复了 cdn.tailwindcss.com 已不可达导致的管理面板样式丢失。
- **渲染依赖自检与降级提示**：新增 `verifyRenderDependencies()` 逐项探测 THREE/OrbitControls/EffectComposer/UnrealBloomPass/gsap，缺失时全屏显示具体文件名并通过 postMessage 通知 React 父窗口。
- **动态图例**：右下角图例改为只显示当前图谱实际出现的节点类型（原硬编码 17 种 → 实际 ~6 种）。
- **节点标签降噪**：后端 memory 节点 label 去除 `@昵称(QQ号)` 格式噪声，前端兜底正则清洗 + 截取限制 24 字符。
- **力导向布局增强**：斥力半径 6→12、弹簧理想长度 15→10、新增重力项与阻尼防震荡，迭代 20 次（≤400 节点），节点自然聚簇。
- **呼吸动画**：节点 ±4% 正弦脉动，相位按空间坐标错开。
- **脉冲波衰减**：粒子从源到目标 scale 1.4→0.6、opacity 0.9→0.3，每次重置随机错开节奏。
- **标签密度控制**：默认 `core` 模式只显示最重要的 40 个节点标签，sprite scale 加硬上限 3.6 防止巨字遮挡。

## v4.6.3 (2026-07-21)

### 检索开放 Scope 与跨群同文治理

- **读路径开放 Scope**：检索不再被 bot/session 硬门禁挡住；有无完整 Scope 均可按群/内容召回，Scope 主要影响排序偏好。
- **person_search 跨群**：默认本群；`scope=all_groups` 按 QQ 跨群只读检索；跨群 recent 按群分桶，避免主群刷满 limit。
- **同文 collapse**：注入/FTS/QueryEngine 路径折叠同人同句跨群重复，减刷屏（不物理 fanout）。
- **跨群同文 soft-delete**：`scripts/cross_group_same_content_dedupe_dryrun.py` 支持 cluster 时间窗 dry-run/apply；确认令 + `--allow-production`；可选 FTS purge 与热 HNSW `mark_deleted`。
- **热 HNSW 与读路径对齐**：legacy 准入排除 `archived/evicted/deleted/noise`，避免 inactive 占满 knn 槽；`scripts/rebuild_hot_memory_hnsw.py` 可按 policy 重建。
- **观察/结案工具**：`scripts/retrieval_readiness_readonly.py`（含 hot_hnsw 抽样）、`scripts/observation_idle_check.py`。
- **Job lease / eviction 噪声**：幂等租约与 eviction 告警降噪（不改变数据边界）。
- **memory 注入超时收敛**：注入 embedding 1.5s 硬超时 soft-fail；HNSW/cold hydrate 进 `asyncio.to_thread`；通道超时后 cancel 任务，避免远程 embedding 把整次注入拖到 ~12s。
- **默认不 fanout promote**；硬 DROP soft-deleted 与双 Bot 历史去重仍需运营明确授权。

## v4.6.2 (2026-07-20)

### 治理 Phase 0：防再污染止血

- **legacy Tag upsert**：`INSERT OR IGNORE` 后按 name 解析真实 `tag_id`，避免陈旧 `lastrowid` 触发 `memory_tags` FK 失败；写后先 commit 再读，兼容读写分离。
- **pair_similarity schema 统一**：建表与迁移统一为 `tag_id_a/tag_id_b/similarity/updated_at`，幂等升级旧 `tag_a/tag_b` 表。
- **注入默认 source_filter**：纳入 `chat`，与 `classify_source` 对齐，避免普通群聊记忆被默认滤掉。
- **memory 通道超时**：默认/慢注入告警对齐 2000ms，适配在线 embedding。
- **TagWorker 失败记账**：`IntegrityError`/异常写入 `failed` 并递增 `attempts`；`attempts>=5` 不再热循环。
- **诊断**：新增只读 `legacy_scope_debt` probe（legacy/formal 计数、无向量、关系旧新表差、pair schema）。

## v4.6.1 (2026-07-20)

### 稳定性修复：外键、共现、索引保留与向量补偿

- **Tag 提取状态完整性**：规范化 `tag_extraction_status` 列与 `ON DELETE CASCADE`；幂等迁移清理 orphan/缺列旧表；TagWorker 写回前复核 Scope，单条 `IntegrityError` 不拖垮整批。
- **共现矩阵防抖重建**：`CooccurrenceScheduler` 合并阈值/强制重建到共享屏障，重建后保留增量 generation，维护任务走 `force_rebuild`。
- **HNSW generation 保留**：新增 `generation_retention`（默认/最小 2），成功发布后只保留当前 generation 与回滚窗口。
- **向量超时恢复**：MessageWriter 有限重试后仍可 `vector=None` 落库；新增 scoped `memory.vector_backfill.v1` 与 durable `maintenance.vector_backfill.run`，启动与超时后自动分批补偿。
- **维护任务修复**：`pair_similarity` 维护事务去掉非法 `actor=` 参数，避免任务反复失败。
- **验证**：聚焦稳定性测试 37 项通过。生产生效需同步运行时并重启；历史无向量记忆由 backfill job 分批恢复。

## v4.6.0 (2026-07-19)

### Scoped Runtime、治理闭环与受限召回

- **正式 Scoped Runtime**：记忆、知识、灵魂、关系、学习对象和 Tag 投影统一进入可审计 Scope 边界；写入通过协调器、outbox 和 durable job 执行。
- **Learning / Tag 治理闭环**：补齐候选、审核、晋升、回滚、有效标签投影、冲突补偿、图谱与诊断接口，管理台不再依赖旧 blackbox 页面。
- **关系与书设兼容**：恢复五维关系兼容、当前群排行/黑名单边界、staged legacy relationship migration；白真真第一人称书设改为 bot 级注入资产。
- **受限 HNSW 与 legacy 兼容**：热记忆索引改为 Tag 驱动、有硬上限的热层；正式 Catalog Tag 与 legacy Tag 使用独立索引；legacy 仅可同群冷召回，避免跨 Scope 泄漏。
- **Timeline 全历史**：`timeline_days=0` 表示全历史，正式 Scope 严格过滤、legacy 行仅同群回退。
- **验证**：`python -m pytest -q` 通过 767 项、跳过 1 项；WebUI lint、测试与 `npm run build` 均通过。未执行生产数据库迁移 apply。

## v4.5.0 (2026-07-06)

### 前端优化 + 认知资源管理前端

- **认知资源管理矩阵**：新增 BookLore、FewShot、Facts、People、Indexes 五个独立页面，完成“发现 -> 管理 -> 验证 -> 调参”的只读管理闭环。
- **按领域拆分后端**：提供 BookLore、FewShot、Facts、People 与 Indexes 的独立 API，危险写操作继续保持二次确认边界。
- **BookLore 管理补齐**：BookLore 页面接入真实 API，展示实体、社区、关系、notes 列表和索引健康入口，明确世界观/书设知识库不是群聊记忆、不是人格指令。
- **FewShot / Facts / People / Indexes 接入真实数据**：风格范例、稳定事实关系、人物画像/好感、向量/FTS5/EPA/BookLore HNSW 诊断页从静态占位升级为 loading/error/empty/list 状态完整的只读页面。
- **v4.5 WebUI 体验闭环**：总览动作入口、记忆来源语义、智能导入向导、注入观测台、通道热配置、学习对象审查、维护工作台、灵魂只读边界等页面补齐风险标识和跨页面跳转。
- **v4.4 神经云图 + 高级检索拆分**：补齐查询实验台、view/query 配置拆分、debug envelope、EPA/残差金字塔/脉冲传播/测地线阶段解释和维护边界文案。
- **v4.3 黑话 / Holyman / 注入治理**：补齐 Holyman 审计面板、候选分流、黑话审核可见性、注入通道元数据、Agent 反馈与学习对象审查闭环。
- **v4.2.2 先止血**：保留 WebUI/Jargon/兼容性修复契约，确保旧配置、旧静态入口和本地测试环境 fallback 不阻断启动。
- **全量验证**：新增 v4.2.2 / v4.3.0 / v4.4.0 / v4.5.0 契约测试；当前 `python -m pytest -q`、`npm run lint`、`npm run build` 均通过（Vite 仍提示既有 chunk-size warning）。

## v4.2.1 (2026-07-05)

### Holyman GitHub 更新握手与前端质量清理

- **Holyman 在线更新握手**：新增 `/api/jargon/holyman/update/check` 轻量检查接口，支持 30 分钟缓存、`force=true` 强制刷新、`checked_at/cached/warning/has_update` 状态字段，避免列表加载时频繁直连 GitHub。
- **预览确认同步体验**：React Jargon 页同步 kirors 式流程，进入广域黑话页后静默轮询检查版本，展示本地/远端版本、缓存状态、检查时间和 warning；真实写入仍需先预览差异并人工确认。
- **Holyman 同步缓存自愈**：成功写入分层资产后清空更新检查缓存，防止前端继续显示旧的可更新状态；保留旧 ready 资产无远端 commit 元数据时的兼容行为。
- **WebUI lint 清零**：拆分 `auth-context`，清理 shadcn 非组件导出警告、未使用 `catch` 参数和有意筛选触发加载的 Hook 依赖提示，`npm run lint` 达到 0 warning / 0 error。
- **回归兼容修复**：补回 KG full graph cache 旧 key alias、Holyman legacy/gaming 分类标签旧契约和黑话注入“仅供理解”文案，确保全量 Python 回归测试通过。

## v4.2.0 (2026-07-05)

### React WebUI Holyman 黑话治理与审计体验修复

- **Holyman 广域黑话管理补全**：React Jargon 页恢复并增强精选口癖、文化概念、声音样本与知识、原始语料、待审核候选、屏蔽项六层视图，补齐搜索、分类/状态筛选、全选当前筛选结果、批量启用/停用、批量通过与批量拒绝并屏蔽。
- **黑话注入安全收束**：JargonInjector 取消释义单字重合度联想，只在用户消息显式命中已确认黑话词条时注入解释；提示文案明确“不改变系统身份、不要求模仿或主动使用这些表达”。
- **Holyman 分层契约兼容**：`/api/jargon/holyman` 增加 React WebUI 兼容 alias，修复 status API 404、Radix Tabs 状态阻塞、catchphrases 列表无法完整渲染等问题；资产类型统一为 `global_jargon_reference`。
- **WebUI 审计与神经云图修复**：合入 3D NeuroGalaxy 星云聚类、遗留 Alpine 入口恢复、Beliefs/Jargon 审计 UI 对齐、信念多维 Trace 详情对齐和灵魂配置下拉 bot 名称泛化。
- **前端工程验证**：新增 `holymanFilters.ts` 纯筛选 helper 与 `node --test` 覆盖，刷新静态 React bundle，确保运行时 `webui/static/app` 指向最新构建产物。
- **后续 proposal**：将公网“神经云图”群友前台概念转存到 `docs/proposals/neural-cloud-public-frontend.html`，作为未来受控 proposal/审批队列方向，不参与当前运行时代码。

## v4.1.0 (2026-07-03)

### 3D 神经云图星空版重塑与前端 Console 全量自愈

- **3D 神经云图契约自愈**：打通了 V3.x 后端异构关系图谱层，兼容 `relationship` 到 `affinity`、`holyman` 到 `jargon`、`belief_emergence` 到 `belief` 的自愈映射。彻底解决了“信念/灵魂/黑话”图谱连线与筛选不工作的缺陷
- **3D 物理与运动特效**：
  - 粒子数据流（Data Flow Trails）：生成沿样条曲线连线流动发光的 3D 能量粒子流。
  - 节点激活呼吸（Glow Waves）：实现选定或悬停节点高亮发光波纹。
  - 视差星海（Nebula Parallax）：构建了双层自转星海微粒（1000 颗星），具有极强 3D Parallax 空间深度。
  - 3D 弹簧力学布局（Spring-Force）：引入质点弹簧物理，提供拖拽回弹手感，社区自动引力聚成星系。
  - 4K高分屏拾取：自适应 devicePixelRatio，消灭了高分屏或浏览器缩放时 Raycaster 点不准的隐患。
  - WebGL 防崩销毁：重写 `disposeGraph` 对 Scene / Material / Texture 进行严苛显存销毁，杜绝 Context Lost 导致的浏览器黑屏崩溃。
- **React Console 全量自愈**：
  - 12 项潜在 Bug 自愈：包括数字表单类型强制Number转换、Recharts 图表 NaN 保护、15s 超时 AbortController、Token 过期 location hash 自动重定向登录、巨型 trace 50kb 截断保护、LoginPage 乐观刷新等。
  - 11 项美学抛光：包括 Module 排行 `min-w-0` 挤压防护、KPI卡高度对齐、Traceback 等宽 mono 代码框、Approved / Destructive 按钮语义着色等。

## v4.0.0 (2026-07-03)

### 记忆基础设施、受控反馈与 React WebUI 首发

- **运行模式**：新增 `full`、`memory_only`、`compat_only` 三种运行模式，旧配置自动兼容，提供启动日志说明与自愈门控，避免与外部记忆插件重复注入
- **通道化注入编排**：移除原有 `main.py` 复杂的单体注入代码，由全新 `InjectionOrchestrator` 通道编排器接管（支持 safety, memory, fts5, timeline, facts, persona, belief, jargon, fewshot, book_lore, affinity 11个独立通道的并发执行、优先级排序与预算裁剪）
- **注入 Trace 数据库**：SQLite 物理设计 `injection_traces` 与 `injection_trace_channels` 两张持久表，全量承载注入性能、Latency、Hit / Filtered 审计与最终预览，支持自动 retention 保留清理
- **Agent 审核与控制边界**：新增 permission_policy 权限控制，Agent 可做只读 Trace 解释、Soft useful/useless 提升、提出热配置建议和提交学习候选词，禁止直接写操作、批量删除或修改核心安全配置
- **LivingMemory 兼容层**：新增兼容 facade、可选 `recall_long_term_memory` 与 `memorize_long_term_memory` 工具别名
- **Holyman 黑话分层重建**：重构并解耦 Holyman 词库，拆分为精选口癖（catchphrases）、文化概念（concepts）、语录证据（examples）、原始语料（corpus/raw）与质量报告，运行时仅允许匹配明确使能的 catchphrase 精选层，避免人设指令混入运行时干扰人格
- **React 管理面板**：新增 `webui/frontend` Vite + React + TypeScript + Tailwind CSS v4 + shadcn/ui 前端工程，默认首页切换为单页应用（HashRouter）并发布静态产物，支持 `/legacy` 回滚与自愈 fallback
- **性能优化**：合入 SQLite cache、HNSWlib/EPA、pair similarity 相关内存优化，降低大规模索引运行压力

## v3.0.0 (2026-06-30)

### 白真人格 / 经历 / 信念分层重构

- **PersonaComposer**：新增自我人格编排层，将人格、信念、精选经历、健康风格样本拆出独立职责，避免 MetaThinking 硬编码 fallback 决定白真真风格
- **主注入收口**：主回复与主动对话统一复用自我人格上下文；注入顺序改为人格 → 信念 → 经历 → 对话对象画像 → 其他辅助块
- **安全边界收缩**：移除 `attack_back` 默认风格升级，极端辱骂仅保留安全边界，不再默认“怼回去”
- **few-shot 净化**：few-shot 提取与注入增加攻击性 / 身份污染过滤，坏样本不再回灌为风格模板
- **文档同步**：更新 README 功能地图与项目结构，补齐 PersonaComposer、BeliefEmergence、ExperienceEpisodeService、identity_safety 等真实能力

## v2.3.3 (2026-06-30)

### Holyman 黑话知识库分层与候选审核

- **分层资产导入**：将 Holyman 从扁平词库升级为精选词条、文化概念、语录证据、原始语料、候选、屏蔽项与质量报告的知识库结构
- **安全匹配收口**：仅精选词条与已确认 DB 条目参与 confirmed match，候选/语料/例句仅作为参考层，不再自动进入激活层
- **WebUI 分层展示**：黑话页改为知识库 tabs，概念/例句/语料/候选/屏蔽项分区展示，候选支持搜索、全选、批量通过与批量拒绝并屏蔽
- **候选审核回显**：新增批量候选审核 API，并让 `/api/jargon/holyman` 合并 DB 审核状态与 blocklist，刷新后立即可见
- **验证覆盖**：新增 Holyman 导入回归测试，确保质量门禁、候选审核、上下文锚点与前端契约稳定

## v2.3.2 (2026-06-27)

### 注入指标时间序列分析

- **SQLite 指标持久化**：新增 `injection_metrics` 表记录每次 `inject_memory` 的耗时、token 与字符数样本，支持升级时自动建表和 31 天保留期清理
- **时间范围聚合 API**：`GET /api/system/metrics/injection` 支持 `range=1d|3d|7d|1mo` 与 `from/to` 日历自定义查询，返回 summary、series 与 ranking
- **WebUI 趋势图**：概览页新增原生 SVG 折线图，不引入 Chart.js 等外部图表库，支持总量、主记忆、灵魂、信念、关系、黑话等曲线开关
- **模块消耗排行榜**：新增按模块 token 总量、均值与占比排序的注入消耗榜，便于定位高消耗注入通道
- **测试覆盖**：新增 `tests/test_injection_metrics.py` 覆盖样本存储、时间桶聚合、排行榜与过期清理

## v2.3.1 (2026-06-26)

### KG 3D 可视化迁移

- **Three.js 3D 引擎**：知识图谱 WebUI 从 Sigma.js/Graphology 迁移为 Three.js 3D 星图，支持 3D OrbitControls、节点射线拾取、人物/标签/记忆多层展示
- **KG 全图与探索 API**：补齐 `/api/kg/full`、人物列表、人物子图、实体详情、时间线、路径探索等前端契约，便于首屏和交互按统一数据结构加载
- **启动缓存预热**：WebUI 启动后后台预热 KG cache，降低首次进入知识图谱页面的加载等待
- **WebGL 降级保护**：自动检测 WebGL 可用性，headless/无 GPU 环境显示降级提示，避免 Three.js 初始化异常中断页面脚本
- **运行时验证**：已同步开发目录、宿主运行时目录和 Docker 容器路径；验证 API、静态资源、页面加载、容器启动日志、全量单元测试与 KG 3D 契约测试通过

## v2.3.0 (2026-06-25)

### 黑话上下文证据与检索升级

- **原始上下文锚点**：黑话条目现在保存 `source_memory_id/source_message_ts/source_sender_id/source_context/candidate_type`，可回填原始聊天证据
- **动态上下文窗口**：新增 `GET /api/jargon/<id>/context`，支持前后消息窗口检索和 fallback 证据展示
- **统计预筛增强**：候选记录保留 `source_contexts`，便于后续定位与回溯
- **人名/昵称分流**：疑似人物称呼不再确认成黑话，改写入人物事实，降低黑话污染
- **WebUI 证据弹窗**：本地黑话列表可直接查看证据窗口，支持 anchor 高亮与筛选

## v2.2.1 (2026-06-25)

### Hotfix

- **Holyman WebUI 修复**：移除 `get_holyman()` 内部重复 `import json`，避免 Python 将 `json` 判定为未初始化局部变量，导致本地 `phrases.json` 加载失败
- **运行时同步清理**：确认 Holyman API 不再返回调试字段，不再输出 `[DEBUG_HOLYMAN_LOAD_FAILED]`

## v2.2.0 (2026-06-25)

### 经历与关系事件重构

- **经历片段服务**：新增 `experience_episodes`，把长期交互从普通消息沉淀为可检索、可注入的经历材料
- **关系事件服务**：新增 `relationship_events`，记录关系变化、互动事件与长期轨迹
- **v2.2 迁移脚本**：新增 `engine/db/migrations/v2_2_experience_rework.py`，为经历重构和后续自学习打基础
- **信念涌现增强**：新增 `belief_emergence`，让信念从摘要/互动中进入可审核的长期认知层

### 身份安全与污染隔离

- **身份安全守卫**：新增 `identity_safety`，降低认爹、主仆、亲属称呼、临时 RP 等群聊梗污染长期身份的风险
- **角色扮演污染隔离**：新增 `quarantine_roleplay_memory.py`，支持扫描并隔离历史 RP/身份污染记忆
- **旧社交数据清理**：补齐 `cleanup_legacy_social_data.py`、`full_cleanup_identity.py` 等治理脚本

### 数据治理与运行时工具

- **DB 健康检查**：新增 `db_health_check.py`、`db_inventory.py`，便于盘点运行时 SQLite 状态
- **运行时导出与修复**：新增 `export_runtime_data.py`、`repair_sqlite_runtime.py`、`sqlite_runtime_guard.py`
- **测试覆盖**：新增 `test_rework_core.py`、`test_identity_safety.py`、`test_runtime_sqlite_tools.py`
- **运行时工具安全**：避免误扫备份目录，导出只覆盖 inventory 纳入的 SQLite 文件

### Holyman / 广域黑话语料

- **内置参考语料扩展**：大幅扩充 `assets/holyman/corpus.json` 与 `phrases.json`
- **高可用同步服务**：新增 Holyman 本地 fallback、在线同步、代理同步与热重载能力
- **黑话推断增强**：接入广域参考语料，提升群体语感、抽象黑话与网络梗理解

### 关键稳定性修复

- **MessageChain 污染修复**：4 秒防抖不再重写原生消息链，避免历史消息出现 `[{text=..., type=text}]` 嵌套序列化
- **多 bot 防抖隔离**：撤销跨 bot 文本去重，防抖 key 改为 `bot_id:group_id:sender_id`，避免一个 bot 误杀另一个 bot 的回复链路
- **主事件回复恢复**：移除正常主事件路径上的 `event.should_call_llm(False)`，避免空回复/不回复
- **并发锁修复**：修复 `_process_in_lock` 作用域问题，恢复 group lock 实际效果
- **主链路健壮性**：补齐 `json` 导入，修复 `desire_engine=None` 误调用、纯图片消息长度门槛误杀、去重 key 缺少 `group_id` 等问题

## v2.1.0 (2026-06-25)

### 灵魂系统升级

- **15 天关系半衰衰减**：关系状态不再永久静态累积，会随时间自然淡化
- **生理节律 / 心境状态**：引入 bot 当天状态、节律与心境注入，让回复更有实时状态感
- **主动插话增强**：支持主动插话、抢词咽回、4 秒消息合并防抖与群聊并发队列锁
- **实时 Persona 注入**：将本小时 @ 次数、最近互动状态、上次回复等上下文交给主对话人格判断

### WebUI 管理面板升级

- **灵魂 / 信念 / 黑话 / 图谱管理**：补齐管理页面，不再停留在只读展示
- **神经云图升级**：新增 GSAP 脑电波扩散、一键斩断连接、图谱交互增强
- **批量管理**：信念与黑话支持搜索、分页、批量选择、批量激活/删除等操作
- **全选 2.0**：支持“全选当页”与“跨页全选全部”，并加入 JS 缓存熔断保护

### API 扩展

- **CRUD 端点补齐**：新增/完善 `soul`、`beliefs`、`jargon`、`kg`、`memories` 管理 API
- **批量操作端点**：为 WebUI 的信念、黑话、图谱和灵魂状态管理提供完整后端能力
- **信念审核流**：支持 pending → active 审核，旧摘要生成的无证据信念降级为 legacy/pending，避免污染长期认知

### 性能与稳定性

- **AstrBot schema 兼容**：`inference_thresholds` 类型从 `str` 改为 `string`
- **consolidation 写入线程池化**：减少同步 DB 写入卡住事件循环的风险
- **DB 读写分离**：inject 查询不再等待 consolidation 写锁
- **配置自愈独立判断**：`enable_auto_inject` 单独关闭也能触发恢复，降低升级后配置失效风险

## v2.0.1 (2026-06-21)

### 数据治理

- **bot_id 统一为 db_id**：beliefs 表 bot_id 从 QQ 号统一为 db_id（如 "yushu"），修复三重身份混乱
- **consolidation 排除 bot 自我 facts**：bot 名字不再被当作 subject 写入 facts，清除 328 条历史污染
- **互动计数清零**：重置早期脏数据（seifer=773 等），v2.0 逻辑重新累积
- **启动自动备份**：每次启动前自动备份 DB，保留最近 N 个（配置 `backup_max_count`，默认 5）

### 黑话学习升级

- **递进重推机制**：词频跨过阈值 [3,6,10,20,40,60,100] 时重新推断含义，no_info 不再定终身
- **上下文条数放开**：推断时给 LLM 的上下文从 5 条提升到 15 条（配置 `max_context`）
- **LLM 候选验证**（可选）：统计候选后用 LLM 批量验证，减少噪声词（配置 `llm_validate`）
- **全部参数配置化**：新增 12 个 Jargon_Settings 配置项，消灭所有硬编码

### 记忆精细化

- **facts 时间衰减**：facts 加 `last_reinforced` 字段，被反复提到的事实保鲜，长期没人提的降权（配置 `facts_decay_rate`）
- **facts 原子类型分类**：新增 5 种类型（EPISODIC/FACTUAL/RELATIONAL/PREFERENCE/PLANNED），差异化衰减速率
  - 事件类 20 天淡出，身份类几乎不衰减，计划类 33 天淡出
  - 纯规则分类器，零 LLM 调用

### 新增配置项

| 配置组 | 新增项 |
|--------|--------|
| Jargon_Settings | min_messages, mine_cooldown, top_k, max_context, context_keep, window_days, jieba_threshold, inference_thresholds, llm_validate, weight_idf, weight_burst, weight_concentration |
| Storage_Settings | facts_decay_rate, backup_max_count |

## v2.0.0 (2026-06-19)

### 认知架构升级

- **时间线记忆通道**：inject 新增第 8 通道，按时间排序注入最近 7 天与当前用户相关的事件摘要。bot 现在有连续时间感知（"昨天和他跑团""前天他来问设定"）
- **QQ 号统一身份**：facts.subject 迁移为 QQ 号（3841 条成功映射），换昵称不再断裂。consolidation 写入时自动 resolve 到 QQ 号
- **inject 与 AstrBot 去重**：跳过最近 30 分钟的记忆（大概率在 AstrBot 300 条对话历史中），避免重复注入浪费 token
- **短期感知注入**：persona_text 注入"本小时他@你 N 次" + "你上次对他说了什么"，bot 有对话连续感
- **删除硬编码门控**：不再有"15次/小时上限"，把频率信息告诉 bot 让它自己判断

### 新功能

- **/teach 命令**：管理员灌入知识 → 写 facts 三元组 + 高权重记忆（importance=2.5）
- **社交工具重做**：wave_memory_affinity 改为查互动排行 / 7天活跃 / 某人信息（不再查废弃的好感度分数）
- **Tag 质量降级**：启动时检测 keyword 垃圾率，> 50% 自动关闭脉冲传播（防止垃圾 Tag 污染联想）

### 配置

- **新增 Inject_Settings**：astrbot_context_window / skip_recent_minutes / timeline_max / facts_max / enable_timeline
- **persona 去缓存**：含实时状态需每次重新生成（有索引后 <5ms）

## v1.5.2 (2026-06-19)

### 代码清理

- **删除全部废弃代码**：ATTITUDE_INSTRUCTIONS/BAIZZ/_ATTITUDE_REGISTRY/DIMENSION_HINTS 常量（60行） + `_affection_to_attitude` 方法 + `_merge_profiles` 中的 attitude/dimensions 死计算
- **性能修复**：`memories.sender_id` 加索引，`_get_message_count` 从全表扫描变为索引查询
- **@register 版本号同步**：从硬编码 0.8.0 更新为 1.5.2

### 配置清理

- **删除 `Affinity_Constraints`**（好感度约束配置组，已废弃）
- **删除 `enable_affinity`**（好感度开关，概念已变为互动积累）
- **删除 Bot 配置中的 `meta_prompt`**（MetaThinking 不再独立调 LLM）
- **更新 MetaThinking_Settings 描述**："对话规则过滤与防骚扰"
- **更新 Lifecycle_Settings 描述**："灵魂系统"

### 文档

- **README 全面更新**：恢复配置参考表(6组) + 后台服务列表(8个) + 功能地图(15子系统)
- **社交系统描述同步**：改为 v1.5 实际行为（认知+互动+facts 驱动）

## v1.5.1 (2026-06-18)

### 社交认知优化

- **persona 注入改为 facts 驱动**：不再用 LLM 生成印象，直接从 facts 表零 LLM 组装"关于他"（如"纠正 xxx / 计划 300小时学AI"）
- **认知+互动双维度**：区分"bot 看到过他多少条消息"（认知）和"直接对话过几次"（互动），更准确反映关系
- **删除 `_update_user_impressions`**：不再额外调 LLM 生成印象

### 配置化

- **Social_Settings 加入 AstrBot 6185 配置页**：群权重/辱骂阈值/ABA窗口 都可在配置页修改
- **9876 热调参持久化**：修改后自动写回 config.json，重启不丢失
- **两个入口统一**：6185 改→重启生效，9876 改→实时生效+自动持久化

### WebUI

- **概览面板改为"社交认知"**：显示有互动用户数 + 互动 TOP 5 + facts 数
- **AstrBot 配置页说明更新**：反映 v1.5 体系

### Bug 修复

- `_update_user_impressions` prompt 未定义（NameError）
- `_abuse_tracker` 冷却过期后 count 衰减 + 清理（防内存泄漏）
- `provider.text_chat` 参数修正

## v1.5.0 (2026-06-18)

### 好感度系统重设计

- **删除数字好感度 → 态度模板映射**：不再有 5 档态度指令（intimate/friendly/neutral/cold/hostile），改为自然语言印象注入
- **互动积累（纯规则）**：每次 bot 回复某用户，interaction_count +1 + last_seen 更新，零 LLM 开销
- **自然语言印象**：consolidation 周期自动对活跃用户（互动>5次）LLM 生成一句话印象，直接注入 persona_text
- **PersonaEvolution 改造**：注入"互动 N 次（熟人/老熟人）+ 印象原文"，让 LLM 自己理解该怎么说话
- **删除好感度隐藏标记方案**：`[好感:+N|印象]` 注入 + on_decorating_result 解析全部移除

### 防骚扰

- **辱骂冷却机制**：连续 @bot 辱骂 3 次 → 触发 10 分钟静默冷却 → 继续辱骂冷却时间翻倍（上限 1 小时）
- 前 2 次辱骂仍然怼回去，第 3 次开始直接无视
- 不需要手动拉黑名单，bot 自己学会了不理骚扰者

## v1.4.0 (2026-06-17)

### 架构重构：MetaThinking 合并到主对话

- **彻底删除独立 LLM 调用**：MetaThinking 不再有 priority=1 的前置 LLM "想一下"。态度判断完全由 PersonaEvolution 在 inject_memory 中注入，bot 在主对话里用自己的系统人格自然思考
- **一次调用完成一切**：记忆+关系+态度+好感度+信念+情绪 → 一次 LLM 调用 → 自然回复
- **好感度靠规则驱动**：LifecycleService 互动频率 + 极端事件硬规则，不再每条消息调 LLM 精算

### 检索增强

- **群隔离精确化**：主搜索当前群 ×1.5 权重、跨群 ×0.8；FTS5 按群权重排序取 top 10
- **时间感知检索**：检测"昨天/上周/之前"等时间词，自动加时间范围过滤（如"昨天" → 最近 48h）
- **好感度阈值调整**：intimate≥80, friendly≥50, neutral≥20（50=friendly 起步，向上空间合理）

### 性能修复

- **事件循环阻塞修复**：_rebuild_memory_index / cooccurrence / EPA 改为 asyncio.to_thread（不再卡 bot 3-6 分钟无响应）
- **PairSimilarity 延迟加载**：启动时不同步计算 200 万对相似度（省 16s 阻塞）
- **配置自愈**：检测到全部开关被 AstrBot 配置页误写为 False 时，自动恢复 + 写回 config

### 数据清理

- 删除 26021 个低质量 keyword 标签（使用次数<2 的噪声）
- 好感度全部重置为 50（2189 用户），metadata 清空重新积累印象
- 共现矩阵/EPA 重启后自动重建

### Bug 修复

- **schema float 类型**：12 个浮点字段从 type:string 改为 type:float，修复 AstrBot 配置页保存报错
- **jargon 编辑 UNIQUE 约束**：PUT /api/jargon 捕获唯一键冲突返回 409 而非 500

## v1.3.1 (2026-06-16)

### 功能可发现性

- **WebUI 模块就绪度面板增强**：每个子系统增加"依赖条件"字段，未就绪时直接显示需要什么条件才能启用
- **README 功能地图**：新增完整子系统一览表，列出每个模块的启用条件、配置位置和说明
- **MetaThinking 描述更新**：README 反映 v1.3.0 架构改造后的实际行为

## v1.3.0 (2026-06-16)

### 记忆召回质量提升

- **FTS5 精确召回通道**：inject_memory 新增第 7 通道，jieba 分词 → FTS5 MATCH → 与向量结果去重合并。精确人名/专有名词不再被语义漂移淹没
- **SelfReflect 纠正提权**：被群友纠正后学到的知识 importance 提升到 3.0 + 同步写入 facts 表
- **facts 1-跳关联扩展**：facts 通道命中实体后自动沿三元组走 1 跳，关联知识一起注入

### MetaThinking 架构改造（省 LLM 调用）

- **消灭独立 LLM 判断**：删除 priority=1 的 `meta_thinking_check` 独立 LLM 调用（原来每条 @bot 消息先"想一下"再回复），态度判断改由 PersonaEvolution 通道统一注入
- **规则链前置过滤 `_should_engage()`**：@bot/引用/私聊→must_reply | 30s内回复过/兴趣词→may_reply | 其他→skip。skip 时不消耗任何 token
- **好感度更新后置异步**：好感度/印象/标签评估移到 `after_message_sent` 后台执行，不阻塞主回复
- **ABA 连续对话追踪**：新增 `_reply_tracker` 记录 bot 最近回复了谁，支持自然连续对话

### 自然度提升

- **黑话注入格式改造**：从 `<jargon>"xxx"在这个群的意思是"yyy"</jargon>` 改为 `[群内词汇（你可以自然使用）]\n- "xxx" → yyy`，鼓励 bot 主动使用而非只是理解
- **consolidation 绰号提取**：prompt 新增 nicknames 字段，自动从对话中识别"A 被叫做 B"类型绰号，写入 facts + person_registry aliases

### 性能 + 可发现性

- **Tag Worker 提速**：默认 batch 50→100 + source=noise 消息跳过打标签，减少无效 LLM 调用
- **配置页功能说明**：schema 顶部新增只读说明块，引导用户区分 6185（基础开关）和 9876（高级调参）
- **高级检索依赖提示**：spike/pyramid/epa/geodesic 开关的 hint 写明前置依赖条件

## v1.1.0 (2026-06-15)

### 知识图谱交互改进

- **expandNode 改真 KG 邻居**：展开节点改为调 `/api/kg/entity/<name>` 获取语义邻居，不再走旧 cooccurrence 社区
- **焦点探索模式**：双击节点自动展开其 KG 邻居，支持渐进式图谱探索
- **标签遮挡动态隐藏**：`labelRenderedSizeThreshold` 调至 12，400 节点时小节点不显示标签
- **边标签按缩放显隐**：`edgeLabelRenderedSizeThreshold` 设 1.5，缩小时自动隐藏边标签
- **配置面板首次加载 pills 为空修复**：loadGalaxy 完成后自动调 loadKgConfig()

### 学习/BDI 质量

- **study_service 内化加 pending 审查**：学习系统写入改为 source=bzz_pending + importance=0.5，WebUI 审批后才提升
- **旧信念批量归档**：信念审核页新增"一键归档全部旧信念"按钮 + 后端 batch-archive 端点
- **consolidation social 关系验证**：新增诊断日志，输出 social 提取 raw/written 计数 + 内容
- **黑话含义纠正能力**：黑话表格释义列支持双击 inline edit，不再需要打开弹窗

### 报错可视化

- **全面错误收集**：main.py 12 处关键 except 块补全 `_record_err`（WebUI/Jargon/MetaThinking/SelfReflect/BeliefEngine/BDI 全覆盖）
- **配置页标注"需重启"参数**：schema 加 `restart_required` 标记；保存时动态检测并提示
- **概览页错误区域 30s 定时刷新**：系统状态 + 错误列表每 30 秒自动更新

### 稳定性/架构

- **HNSW 死 ID 修复**：eviction 调 `mark_deleted` 替代不存在的 `remove`，修复 AttributeError
- **tag_relations.created_at NULL 补全**：启动时一次性 migration 填充空 created_at 行
- **jargon 预热性能**：LIMIT 20000→10000 + 7 天→3 天，启动速度提升 ~50%
- **DB 体积监控**：/api/system 返回 db_size_mb（含 WAL）；概览页显示体积 + 超 2GB 警告
- **consolidation 与 belief_engine 初始化顺序保护**：assert + 注释说明顺序约束

### 代码质量

- **explore.html 拆分**：900+ 行单文件拆为 explore.html(308行 HTML) + kg.js(909行 图谱逻辑) + kg-config.js(64行 配置面板)
- **_conf_schema.json 数值字段 type 注释**：10 个浮点 string 字段加 `_note` 说明 AstrBot 限制
- **_conf_schema.json restart_required 标记**：embedding/dimension/webui 等 6 个重启参数标记

### Bug 修复

- **"记住"命令 sender_name 未定义**：提前赋值 sender_name，修复 NameError（v1.0.1 引入的潜在 bug）
- **source_discovery 映射预检误报**：LLM 将逻辑字段名（sender/group）与实际列名搞混 → 加本地快速预检，映射 value 都存在于表列中则跳过 LLM 校验
- **Embedding "Event loop is closed"**：NVIDIA provider 热重载后 event loop 关闭 → 捕获后清缓存重试一次，避免整个 embedding 通道永久失效

## v1.0.2 (2026-06-14)

### 改进

- **系统健康面板**：概览页"引擎状态"改为动态健康面板，从后端实时获取 11 个服务的状态（就绪/降级/未加载）+ 降级原因。不再硬编码"✓ 就绪"。
- **EPA 降级说明**：EPA 基底未就绪时显示具体原因（"需 ≥20 个 tag 向量,持续聊天自动积累"）
- **config 类型校验修复**：string 类型字段保存时强制 str() 转换，修复 AstrBot "Expected string, got float" 校验报错（影响 min_similarity 等 13 个数值字段）

### Bug 修复

- `_conf_schema.json` injection_format 更新为结构化标签格式

## v1.0.1 (2026-06-14)

### 新功能

- **"记住/忘记"显式命令**：用户说"记住xxx"→即时写入(importance=2.0 source=explicit)；"忘记xxx"→匹配记忆软删除(importance=0.01)。关键词：记住/记下/remember、忘记/忘掉/forget/别记
- **参与者相关性加权**：inject_memory 五阶段结果后按 sender 关联加权（自己×1.4 / bot×1.2），重排后取 top_k，防止群聊串线
- **关系自动发现**：consolidation prompt 新增 social 字段，自动推断人际关系（朋友/互怼/师徒/情侣/对立/合作）写入 facts，知识图谱自动丰富
- **记忆来源追溯**：injection_format 默认改为 `<memory from='{sender}' time='{time}'>` 结构化标签，让 LLM 更容易引用来源

### Bug 修复

- **_bot_registry 崩溃**：防御性 getattr 避免初始化未完成时属性不存在导致插件加载失败
- **知识图谱图层过滤**：非 facts 图层（信念/关切/黑话/好感度/社区）被关系类型筛选误过滤→0 节点
- **时间/权重筛选误杀**：非 facts 图层 ts=0 被时间范围过滤掉，统一原则只对 facts 图层生效

### 文档

- README 更新 v1.0（知识图谱/实测 10.4 万数据/WebUI 新功能）
- CHANGELOG 日期修正（2025→2026）+ v1.0.0 完整变更记录

## v1.0.0 (2026-06-14)

### 知识图谱化全面改造

- **交互式知识图谱**：从 tag 统计共现升级为语义知识图谱(facts+tag_relations)
  - 全量 5700+ 条关系一次加载到前端,纯 JS 过滤零延迟(132ms)
  - 6 层数据图层可选：事实/信念/关切/黑话/好感度/社区
  - 配置面板：节点数/关联强度/时间范围/关系类型/节点类型
  - 10 种语义边标签(discusses/mentions/decides/supports/opposes/creates/uses/knows/reacts_to/relates_to)
  - 人物画像卡(QQ/好感度/别名/personality_tags) + 实体消歧(同 QQ 合并)
  - 时间线视图(纵轴事件流) + 多跳路径(BFS 语义链)
  - 节点拖拽(Sigma.js) + 内容编辑(手动添加事实)
  - 语义向量检索(五阶段管线) + GSAP 动效

### WebUI 功能补全

- 配置页 schema 驱动全量生成(20 组配置全部可编辑)
- 信念审核页 + 黑话审核页 + 灵魂状态页
- 记忆管理：翻页/搜索/筛选/批量操作 全打通(10.4 万条可管理)
- 维护页：quality + audit/trigger 端点补全

### 灵魂层修复

- 救活 06-12 集体停摆的 5 个 BDI 服务(belief/concern/desire/mood/time)
- 信念质量管线：pending 待审 + prompt 语境约束 + strength 阈值过滤
- 黑话起死回生：修 jieba.dt import 致命 bug + 词频预热(34 条入库)
- 时间锚点接线(强情绪→add_anchor)

### 性能优化

- galaxy 缓存：3.19s → 0.008s (400×)
- 搜索跳过 COUNT：2.7s → 0ms
- keyset 深翻页：1.7s → 0.015s (100×)
- tag 审计候选查询：18.8s → ms 级

### Bug 修复

- 5 处后端列名错误(beliefs/soul 三端点)
- bot_mood 历史数据污染(15 行 BookLore KG 误写)清理
- 神经云图前端 404(全部端点对接)
- 人物列表陈旧(改从 memories 聚合)
- 星图筛选堆叠(改 hidden)
- _bot_registry 防御性 getattr

## v0.6.0 (2026-06-04)

### 架构重构

- **数据层拆分 (P1)**：database.py 重构为 Facade 模式，内部委托 5 个 Repo（MemoryRepo, TagRepo, SocialRepo, KnowledgeRepo, BookLoreRepo）
- **ConnectionManager**：线程写锁 + WAL + closed/reopen，统一连接管理
- **预计算架构 (P2)**：PairSimilarityService（标签对相似度预计算 + O(1) Map 查表）+ SemanticGain 钟形增益函数
- **三级降级 (P3)**：GeodesicReranker 支持 L0/L1/L2 降级 + try/catch 兜底
- **TagWorker**：匀速后台标签提取（每5分钟醒一次，一次 batch 调用），替代实时打标签
- **MessageWriter 简化**：只负责 embedding + 写入，不再同步打标签

### 改进

- DirectedCooccurrence：语义增益调制边权重 + 反向锚定高残差节点
- CooccurrenceScheduler：修复防抖 bug，改成满阈值+过冷却期才触发（阈值 0.05）
- IntrinsicResidualCalculator：top-N(max_tags=3000) + 按需加载向量
- ResidualPyramid：接收 db 参数，analyze() 按需取向量
- QueryEngine：删除全量 tag 缓存，改为按需加载；过滤改成只看相似度
- SpikeRouter：删除 CooccurrenceMatrix import，改用 DirectedCooccurrence
- VectorIndex：新增 mark_deleted 方法
- TagBackfillJob：覆盖率改成 >=2 标签才算覆盖
- ConsolidationService：LIKE 查询改前缀匹配
- 所有 tools：call() 加 db 存活检测 + reopen
- main.py：_terminated 防重入 + bg_tasks 追踪 + 残差间隔保护(30min)
- WebUI：鉴权中间件 + CORS 收紧
- _conf_schema.json：embedding_provider_id 去掉 _special

### 删除

- engine/cooccurrence.py（死代码，被 directed_cooccurrence 替代）
- services/migration.py（死代码，从未被调用）

## v0.5.0 (2026-05-29)

### 新功能

- **配置完善**：所有硬编码参数暴露到 AstrBot 插件配置界面
  - 新增 Cross_Group_Settings（跨群记忆开关 + 画像合并开关）
  - 新增 Affinity_Settings（五维度半衰期 + 态度阈值 + flush 间隔）
  - Lifecycle 新增情绪阈值、做梦参数、consolidation 话题回写开关
  - Tag_Settings 新增 tag_blacklist、consolidation_skip_topics
- **WebUI 热调参面板**：配置 Tab 新增滑块区域，9 个参数实时调节无需重启
- **README 完整配置文档**：50+ 配置项完整说明表 + 热调参文档

### 改进

- EPA 和测地线重排默认改为启用
- DreamService 种子数/联想数参数化
- PersonaEvolution 态度阈值可配置
- ConsolidationService topic_backfill 开关 + skip_topics 可配置
- QueryEngine 跨群过滤受配置控制

## v0.4.3 (2026-05-28)

### 新功能

- **Consolidation topics 回写 memory_tags**：整合服务提取的段落级话题标签自动写回每条消息，零额外 LLM 成本，短消息不再需要单独猜话题

### 改进

- Tag backfill batch_size 500→50，避免 LLM 截断导致 tag 错位
- 空 tag 结果标记 `skipped` 而非 `done`，不阻塞重新处理
- Consolidation topic 回写过滤泛化词（日常闲聊/灌水等）

## v0.4.1 (2026-05-28)

### 修复

- **deep_search 工具不可用**：方法名 `execute` → `call`，对齐 AstrBot FunctionTool 接口
- **memory_search 偶发 TypeError**：timestamp 字段为 ISO 字符串，解析后再计算时间衰减

## v0.4.0 (2026-05-27)

### 新功能

- **跨群记忆共享**：去掉 group_id 过滤，所有群共享同一记忆池；跨群人物画像自动合并
- **Tag 审计系统**：LLM 驱动的 Tag 质量审计（合并/重分类/删除建议），SSE 流式进度
- **Tag RAG 提取**：embedding 搜索已有 Tag 库注入提取 prompt，提升 Tag 复用率
- **维护工作台 WebUI**：`/maintain` 页面 — 统计卡片、审计触发、建议列表、批量批准/拒绝
- **社区检测**：Label Propagation 轻量实现，用于 Tag 聚类分析
- **神经云图重构**：Sigma.js + Graphology 全新渲染，支持星图/联想/人物/路径四视角

### 改进

- Tag 提取引入已有 Tag 库参考词表（静态 top-200 fallback）
- 审计 API 支持 action 类型过滤
- 审计触发加并发保护，防止重复执行
- 维护面板 XSS 防护

### 修复

- SSE 审计端点从 POST 改为 GET（EventSource 兼容）
- 批量 resolve API 兼容前端简化格式
- Tag RAG 补充 keyword 等未列出类型避免丢失
- WebUI 查询 bot_mood 使用 is_active 而非 expires_at

---

## v0.3.0 (2026-05-20)

### 新功能

- **人格进化系统**：多维好感度引擎（familiarity/trust/fun/depth/hostility）→ 态度分级 → 动态 prompt 注入
- **生命周期服务**：好感度 flush + 表达模式聚合 + 记忆衰减标记，30 分钟 tick 周期
- **做梦系统**：6 小时周期后台记忆巩固，三层时间线（近期涟漪/中期回音/深渊浪潮）+ 共振桥梁发现
- **Bot 情绪系统**：根据群消息密度和情感 tag 分布动态设置情绪（energetic/cheerful/concerned），注入 prompt
- **事实三元组提取**：consolidation 整合时提取结构化 facts（subject/predicate/object）写入 facts 表
- **人物搜索工具**：person_registry + memory_mentions 双层架构，支持按人物查询相关记忆
- **深度搜索工具**：wave_memory_deep_search，多轮联想搜索
- **LLM 摘要整合**：定时 4 小时周期，碎片消息 → 结构化知识（summary + topics + facts + relations）
- **VCP 完整对齐**：Phase 1-7 全部实现（EPA/残差金字塔/脉冲传播/向量融合/测地线重排/有向共现/内禀残差）
- **LLM 辅助导入验证**：未知数据源自动 LLM 分析表结构 + 字段映射

### 改进

- 有向共现矩阵 + 防抖调度器（双缓冲原子切换，不阻塞查询）
- 内禀残差计算器（共现矩阵重建后自动重算）
- 导入系统：rowid 游标增量导入 + 安全游标（失败不推进）+ 连续重复提前终止
- 导入 batch_size 10→50, limit 500→5000, 批量去重
- Tag 提取改为 JSON 文档批处理
- 数据源列表 60s 缓存 + 手动刷新强制失效
- WebUI：导入进度条 + 导入/LLM提取按钮互斥 + 模型配置迁移到智能导入 Tab

### 修复

- `_ensure_tag` 处理 UNIQUE 约束冲突
- 发送者列表按 sender_id 分组，显示最新昵称
- `on_message` 中好感度引擎变量名 content → message
- SQL 优先级 bug：filter 条件必须加括号再拼 AND rowid
- 游标安全性：有 error 的批次不推进游标 + memories 为空时重置
- 配置页模型下拉框为空 / 不显示当前值
- 导入全部失败（缺少 group_id 参数）
- 导入进度超 100% 问题
- 数据源加载慢 + 导入/提取并发卡死
- tag_cfg NameError + tag_extraction_status migration + tag_job startup delay

---

## v0.2.1

- 数据源进度估算 + 配置面板只读展示
- 数据源列表批量 IN 查询避免超时
- 初始版本稳定化
