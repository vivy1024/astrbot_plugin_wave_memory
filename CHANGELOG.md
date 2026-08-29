# Changelog

## v4.7.2 (2026-08-29)

### 管理台与心智运行时

- **心智强制自省**：按当前群重算只读心智状态，不写库、不调模型、不改版本；管理台提供「刷新数据 / 强制自省」。
- **表格适配**：桌面表和手机卡片共用同一套列定义；信念、黑话、记忆、人物、事实、风格样例、注入、索引、维护、标签等页不再各写一份。
- **筛选与批量操作**：Bot/群筛选栏和批量审核条抽成共用组件。
- **可见文案**：时间线、事实、风格样例、标签图、记忆、信念、黑话、注入观测台、人物、导入等页改成中文，去掉 Canonical Scope / ObjectRef / Affinity 等内部词，保留 Bot、群、记忆、标签。
- **插件市场简介**：改为面向群聊使用者的一句话能力说明，不再堆 BDI / Three.js 等术语。

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
