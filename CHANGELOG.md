# Changelog

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
