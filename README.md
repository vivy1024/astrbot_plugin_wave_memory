<div align="center">

# Wave Memory

[![Version](https://img.shields.io/badge/version-v4.7.2-blue.svg)](https://github.com/vivy1024/astrbot_plugin_wave_memory/releases)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot](https://img.shields.io/badge/AstrBot-≥4.14-green.svg)](https://github.com/AstrBotDevs/AstrBot)

**零外部依赖的五阶段记忆检索引擎 + 灵魂人格系统**

*SQLite + HNSW + 纯数学 — 不需要 Neo4j、不需要 Elasticsearch、不需要向量数据库*

[快速开始](#快速开始) · [检索引擎](#-检索引擎) · [灵魂系统](#-灵魂系统) · [WebUI](#-webui-管理面板) · [Releases](https://github.com/vivy1024/astrbot_plugin_wave_memory/releases)

</div>

---

### Highlights

- 🧠 **五阶段零 LLM 检索** — EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 测地线重排，查询 < 50ms
- 🌐 **有向共现矩阵** — 替代 Neo4j 的图关联能力，13 万节点 / 44 万有向边
- 💬 **灵魂人格系统** — 自我人格编排、信念涌现、经历精选、情绪轨迹、做梦巩固、自省纠错
- 🗣️ **文化融入** — 群内黑话 + Holyman 分层知识库 + 健康风格范例注入 + 绰号识别
- ⏰ **记忆生命周期** — 时间衰减 + 重要性分级 + 自动淘汰，像人一样遗忘
- 🔍 **时间感知检索** — 说"昨天/上周"自动加时间过滤，群隔离精确加权
- 📊 **交互式知识图谱** — Three.js 3D 星图渲染，六层数据图层，多跳路径探索
- 🔧 **零配置启动** — 填 2 个 Provider ID 即跑，所有子系统自动按条件就绪

### Recent Releases

| 版本 | 日期 | 重点 |
|------|------|------|
| **v4.7.2** | 2026-08-29 | 管理台中文与表格重构：心智强制自省、人物历史关系审计、DeclarativeDataTable/筛选栏抽取、可见文案去内部词 |
| **v4.7.1** | 2026-08-06 | 稳定性修复与好感度平滑：发送前拦截清洗印象标记防泄露、WriteCoordinator死锁与共现循环防御、关系衰减优化、向量索引恢复 Inline Resize |
| **v4.7.0** | 2026-07-26 | 瘦身重构 + 3D 增强：清理学习中心空壳(−11K行)、CDN 本地化、力导向聚类、节点降噪、经历/时间锚点/Outbox 新页面 |
| **v4.6.3** | 2026-07-21 | 开放 Scope 检索、跨群同文 soft-delete、热 HNSW 对齐读路径、person 跨群与观察门禁 |
| **v4.6.2** | 2026-07-20 | 数据库治理 Phase 0：稳定标签 upsert、Schema 兼容迁移、2000ms 注入预算与 Scoped Runtime 诊断 |
| **v4.6.0** | 2026-07-19 | Scoped Runtime、Tag 治理、Learning Center、关系/Soul、受限 HNSW 热冷召回与全历史 Timeline |
| **v4.5.0** | 2026-07-06 | 前端优化 + 认知资源管理：BookLore/FewShot/Facts/People/Indexes 独立 API 与真实数据闭环 |
| **v4.2.1** | 2026-07-05 | Holyman GitHub 更新握手：轻量检查缓存 · 强制刷新 · 预览确认同步 · lint 清零 |
| **v4.2.0** | 2026-07-05 | React WebUI Holyman 黑话治理补全：筛选/批量审核 · 显式命中注入 · 神经云图与审计 UI 修复 |
| **v4.1.0** | 2026-07-03 | 3D神经云图星空版：动力学引力 · 3D粒子流数据线 · 高分屏精准点击 · WebGL彻底销毁自愈 |
| **v4.0.0** | 2026-07-03 | React WebUI首发：Vite + React + TS + Tailwind v4 + shadcn/ui 全量管理页面迁移 |
| **v3.3.0** | 2026-07-03 | Holyman 分层黑话资产重建 · 精选运行时匹配过滤 |
| **v3.2.0** | 2026-07-03 | 通道化注入编排器 · 注入 Trace 持久存储 · Agent 只读/受控反馈工具 |
| **v3.1.0** | 2026-07-03 | 运行模式 · 通道配置热更新模型 · 学习对象审查登记表 |
| **v3.0.1** | 2026-07-03 | 性能优化：优化 SQLite 缓存与 HNSWlib/EPA 内存消耗 |
| **v3.0.0** | 2026-06-30 | PersonaComposer 分层人格 · 主动对话共用自我人格上下文 · few-shot 健康过滤 · 安全边界收口 |
| **v2.3.4** | 2026-06-30 | Holyman 黑话知识库分层 · 候选批量审核 · 证据层 tabs · 屏蔽项回显 |
| **v2.3.2** | 2026-06-27 | 注入指标时间序列 · SVG 折线图 · 模块消耗排行榜 · 自定义日期筛选 |

---

## 定位与边界

WaveMemory 是 AstrBot 记忆插件：负责记录、整理、检索、注入、审计和反馈记忆。

| 是 | 不是 |
|----|------|
| 记忆存储与召回后端 | 插件总线 |
| 注入通道编排器 | 通用学习系统 |
| 记忆/事实/信念/风格/黑话等学习对象审计 | 自动改其他插件配置的控制器 |
| LivingMemory-compatible facade | `astrbot_plugin_livingmemory` 伪装 |

### 运行模式

| 模式 | 自动注入 | 默认能力 | 适用场景 |
|------|----------|----------|----------|
| `full` | 开 | 基础记忆 + 高级通道 + WebUI + Agent 反馈 | WaveMemory 独立承担记忆与人格增强 |
| `memory_only` | 开 | 消息采集、writer、向量检索、基础记忆注入、trace、搜索/记住工具、兼容 facade | 只要记忆，不要人格/黑话/BookLore/few-shot/mood 等高级能力 |
| `compat_only` | 关 | writer、query、LivingMemory-compatible facade、可选工具别名、最小 trace | 给 SelfLearning/ChatPlus 等外部插件当记忆后端，避免重复注入 |

---

## 🧠 检索引擎


查询路径零 LLM 调用。五阶段纯计算管线，用算法替代外部基础设施。

```
用户消息 → Embedding
     ↓
┌─ EPA 嵌入投影分析 ─────────────────────────────────────┐
│  PCA 投影查询向量 → 能量分布熵 → logic_depth (聚焦度)   │
│  聚焦 → 精确搜索    发散 → 积极联想                     │
└────────────────────────────────────────────────────────┘
     ↓
┌─ 残差金字塔 (Gram-Schmidt 正交分解) ──────────────────┐
│  逐层剥离已理解的语义，确保复合查询每个面都被召回       │
└────────────────────────────────────────────────────────┘
     ↓
┌─ 脉冲传播 (有向共现图能量扩散) ───────────────────────┐
│  多跳扩散 · 虫洞机制 · 内生残差加权 · 动态动量         │
│  发现查询中未直接提及但语义关联的记忆                   │
└────────────────────────────────────────────────────────┘
     ↓
┌─ 向量融合 + HNSW 检索 ───────────────────────────────┐
│  (1-α)×原始查询 + α×联想上下文 → cosine top-k          │
└────────────────────────────────────────────────────────┘
     ↓
┌─ 测地线重排 ──────────────────────────────────────────┐
│  共现图路径距离修正向量直线距离 · 三级降级保鲁棒        │
└────────────────────────────────────────────────────────┘
     ↓
   Top-K 记忆
```

### 核心算法模块

| 模块 | 原理 | 解决什么问题 |
|------|------|-------------|
| EPA | PCA 投影 → 能量熵 → 聚焦度 | 自适应调节检索激进度 |
| 残差金字塔 | Gram-Schmidt 逐层正交分解 | 复合查询多面召回 |
| 脉冲传播 | 图 BFS + 能量衰减 + 虫洞 | 间接关联发现 |
| 测地线重排 | 图拓扑距离修正余弦距离 | 语义流形曲率补偿 |
| 内生残差 | SVD 邻居子空间不可解释度 | 信息价值度量 |
| 语义增益 | 钟形函数 · 黄金邻接区 | 过滤冗余/噪声共现 |
| 有向共现矩阵 | 序位势能 × 语义增益 × 残差锚定 | 替代图数据库 |
| FTS5 | SQLite 全文搜索 | 精确人名/专有名词 |

### 并行注入通道

`InjectionOrchestrator` 并发运行通道；每通道独立 `timeout_ms`，单通道 timeout/error 不阻塞整体注入。总耗时超过 500ms 输出通道耗时分解。

```
├─ safety（近期上下文去重 · 身份污染过滤）
├─ memory（五阶段语义召回）
├─ fts5（人名/专有名词精确召回）
├─ timeline（相关时间线事件）
├─ facts（三元组事实）
├─ persona（自我人格/经历/对象画像）
├─ belief（已审核信念）
├─ jargon（已确认黑话）
├─ fewshot（已批准健康风格样本）
├─ book_lore（世界观知识）
└─ affinity（关系/互动摘要）
```

通道配置在 9876 WebUI「通道配置」热更新：`enabled`、`priority`、`top_k/max_items`、`token_budget`、`timeout_ms`、`min_score`。

### Benchmark

5 个 QQ 群，持续运行 80+ 天：

| 指标 | 数值 |
|------|------|
| 记忆规模 | 126,000+ 条 |
| 共现图 | 133,000 节点 / 447,000 有向边 |
| 查询延迟（本地计算） | < 50ms |
| 端到端延迟（含远程 Embedding） | ~850ms |
| 存储 | 1.7GB SQLite + 592MB HNSW |
| 外部依赖 | 零（仅需 Embedding API） |

---

## 💬 灵魂系统

独立于检索引擎的人格模拟层。让 bot 不只是"能记住"，而是"像人一样成长"。

### 认知与情感

| 模块 | 功能 |
|------|------|
| PersonaComposer | 自我人格 / 信念 / 经历 / 风格样本分层编排，控制主 prompt 优先级 |
| PersonaEvolution | 认知+互动+facts 驱动的对话对象画像注入 |
| BeliefEngine | 从对话中涌现稳定判断（信念），只注入 active 信念 |
| BeliefEmergenceService | 从关系事件与经历中生成待审核信念候选 |
| ExperienceEpisodeService | 记录 bot 经历片段、回复、内心、结果和来源记忆 |
| DesireEngine | 事件触发冲动 → 与信念博弈 → 决定行为 |
| MoodTrajectory | valence/arousal 二维情绪轨迹，走势摘要注入对话 |
| SubjectiveTime | 用重要事件锚定时间感，替代机械时间戳 |

### 社交认知（v1.5）

| 功能 | 说明 |
|------|------|
| 认知度 | bot 在群里看到过此人多少条消息（被动认知） |
| 互动度 | bot 直接和此人对话过几次（主动互动） |
| Facts 画像 | 从 facts 表零 LLM 组装"关于他"（如"纠正 xxx / 计划 300小时学AI"） |
| 跨群画像合并 | 同一用户在不同群的数据自动聚合 |
| 绰号识别 | Consolidation 自动提取"A 被叫做 B"写入 facts + person_registry |
| 多 Bot 支持 | 2+ Bot 共存，独立互动数据，`bot_id` 使用 db_id 隔离 |
| 防骚扰 | 辱骂 N 次 → 自动冷却静默（翻倍机制，上限 1 小时） |
| 身份安全 | 拦截认爹/认主/契约/猫娘/RP 等身份污染，不写入长期人格 |
| 攻击边界 | 极端辱骂只注入安全边界，不把“怼回去”当默认风格 |

### 自主学习

| 模块 | 功能 |
|------|------|
| SelfReflect | 检测群友纠正信号 → 搜索知识 → 内化为高权重记忆 |
| DreamService | 6h 周期离线联想，三层时间线涟漪浪潮强化记忆 |
| StudyService | 从 BookLore 知识库主动学习 |
| Consolidation | 4h 周期 LLM 摘要 → facts + relations + social + nicknames |

### 文化融入

| 模块 | 功能 |
|------|------|
| 黑话系统 | 统计预筛 → LLM 三步推断 → 自动挖掘群内梗 → 注入可用词汇 |
| Holyman 知识库 | 精选词条 / 文化概念 / 语录证据 / 原始语料 / 候选 / 屏蔽项分层管理 |
| Few-Shot 风格 | 每天提取高代表性回复入库，仅注入已批准且无攻击/身份污染的健康范例 |
| ConcernTracker | 维护当前在意的话题，影响主动插话决策 |

### 记忆生命周期

```
新消息写入 (importance=1.0)
  → 被召回 +0.02 · 被做梦联想 +0.05
  → 时间衰减 ×0.997^天
  → noise 7天未访问 → 删除
  → chat 30天未访问 → 脱索引
  → importance < 0.1 → 深度清理
```

---

## 📊 WebUI 管理面板

默认首页是 `webui/frontend` 的 Vite + React + TypeScript + Tailwind CSS v4 + shadcn/ui 单页应用，构建产物发布到 `webui/static/app`，运行时仍由 Quart + Hypercorn 纯 Python 托管静态文件，不依赖 Node.js。旧 Alpine.js 首页保留在 `/legacy`，用于安全回滚。

| 路由 | 页面 | 功能 |
|------|------|------|
| `/#/dashboard` | 概览 | 系统健康 · 模块就绪度 · 注入指标趋势 · 错误监控 |
| `/#/injection` | 注入观察台 | trace 筛选 · 命中/过滤上下文 · Sheet 详情抽屉 · 最终注入预览 |
| `/#/channels` | 通道配置 | enabled/priority/top_k/token_budget/timeout_ms/min_score 热更新 · validation diff 预览 |
| `/#/learning` | 学习对象审查 | memory/facts/belief/jargon/few-shot/persona/affinity/timeline/operation memory 登记表 |
| `/#/feedback` | Agent 反馈 | 记忆反馈 · 配置建议 · 审查候选 · 人工批准/拒绝/忽略 |
| `/#/compatibility` | 兼容模式 | LivingMemory-compatible facade 状态 · 工具别名 · 重复记忆插件风险 |
| `/legacy` | 旧版首页 | 原单文件 Alpine.js 面板，作为回滚入口 |

开发命令：

```bash
cd webui/frontend && npm install
cd webui/frontend && npm run dev
cd webui/frontend && npm run typecheck
cd webui/frontend && npm run build
```

发布约定：提交 `webui/static/app/index.html` 与 hashed JS/CSS 静态产物；后端 `/` 优先服务 React 构建产物，产物缺失时自动 fallback 到旧版首页。

---

## 🔧 Agent 工具

工具注册受运行模式门控；`compat_only` 默认只保留 LivingMemory-compatible 别名（开启时）。

| 工具 | 功能 | 权限 |
|------|------|------|
| wave_memory_search | 五阶段语义搜索 | allowed |
| wave_memory_deep_search | FTS5 全文关键词搜索 | allowed |
| wave_memory_person_search | 人物记忆/画像/社交关系 | full/memory_only |
| wave_memory_affinity | 关系/互动查询 | full |
| wave_memory_facts | 事实知识三元组 | allowed |
| wave_memory_tag_graph | 标签共现图谱探索 | allowed |
| wave_memory_remember | 主动存储重要信息 | allowed，走统一 writer 去重 |
| wave_memory_explain_injection | 读取 trace，解释通道命中/过滤/预算/耗时 | read-only |
| wave_memory_feedback_memory | 对 trace 中命中的 memory 记录 useful/useless/misleading/duplicate | 低风险 useful 可软提升 |
| wave_memory_suggest_config | 基于 trace 证据提交配置建议 | pending_review，不自动应用 |
| wave_memory_submit_review_candidate | 提交 memory/fact/belief/style/jargon 候选 | pending_review，不自动提升 |
| book_lore_search | 书设知识库语义搜索 | full |
| book_lore_graph | 书设实体关系图谱 | full |
| recall_long_term_memory | LivingMemory 风格搜索别名 | 可选，默认关闭 |
| memorize_long_term_memory | LivingMemory 风格写入别名 | 可选，默认关闭 |

---

## 🚀 快速开始

### 安装

将插件目录放入 AstrBot `data/plugins/`，自动安装依赖。

### 配置

| 配置项 | 说明 | 推荐值 |
|--------|------|--------|
| `embedding_provider_id` | Embedding 模型 | `siliconflow/Qwen3-Embedding-0.6B` |
| `tag_llm_provider_id` | Tag/黑话/风格用 LLM | `xiaomi/mimo-v2.5-pro` |
| `embedding_dimension` | 向量维度 | `1024` |

AstrBot >= 4.14.0 · Python 3.10+ · WebUI 默认端口 9876

---

## 📋 配置参考

所有参数可在 AstrBot 6185 配置页调整，部分也可在 9876 WebUI 实时修改。

### 基础配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| embedding_provider_id | （必填） | Embedding 模型 Provider ID |
| tag_llm_provider_id | （必填） | Tag/黑话/风格用 LLM |
| embedding_dimension | 1024 | 向量维度 |

### 运行模式 (Runtime_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| runtime_mode | full | `full` / `memory_only` / `compat_only` |

### 记忆召回 (Query_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| enable_auto_inject | true | 自动注入记忆到 prompt；`compat_only` 下默认忽略旧 true |
| inject_top_k | 5 | 注入记忆条数 |
| min_similarity | 0.35 | 最低相似度 |
| enable_spike_routing | true | 脉冲传播（`memory_only`/`compat_only` 默认关闭） |
| enable_residual_pyramid | true | 残差金字塔（`memory_only`/`compat_only` 默认关闭） |
| enable_epa | true | EPA 嵌入投影分析（`memory_only`/`compat_only` 默认关闭） |
| enable_geodesic_rerank | true | 测地线重排（`memory_only`/`compat_only` 默认关闭） |

### 注入通道 (Channel_Settings)

| 字段 | 说明 |
|------|------|
| enabled | 是否启用通道；safety 不可关闭 |
| priority | 注入排序优先级 |
| top_k / max_items | 检索/输出条数 |
| token_budget | 单通道预算；最终仍受全局预算裁剪 |
| timeout_ms | 单通道超时；timeout 不阻塞其他通道 |
| min_score | 通道最低分数阈值 |

### Trace (Trace_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| retention_days | 14 | 注入 trace 保留天数 |
| max_rows | 5000 | trace 最大条数，超出仅保留最新 |
| max_preview_chars | 1200 | 请求、最终注入、通道明细的单字段预览长度 |

### 兼容模式 (Compatibility_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| livingmemory_alias_tools_enabled | false | 注册 `recall_long_term_memory` / `memorize_long_term_memory` 别名 |
| compat_only_auto_inject_enabled | false | `compat_only` 下显式允许 WaveMemory 原生自动注入 |

### 社交认知 (Social_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| group_weight_current | 1.5 | 当前群记忆权重 |
| group_weight_cross | 0.8 | 跨群记忆权重 |
| abuse_trigger_count | 3 | 辱骂触发冷却次数 |
| abuse_cooldown_base | 600 | 冷却起步秒数 |
| abuse_cooldown_max | 3600 | 冷却上限秒数 |
| aba_window_seconds | 30 | 连续对话窗口 |

### 黑话系统 (Jargon_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| enabled | true | 启用黑话系统 |
| min_frequency | 5 | 最低频率阈值 |
| max_inject | 3 | 单次最多注入数 |
| global_threshold | 3 | 跨群全局化阈值 |

### 风格学习 (FewShot_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| enabled | true | 启用风格学习 |
| min_score | 0.7 | 最低风格评分 |
| max_inject | 3 | 每次注入范例数 |
| drift_threshold | 0.5 | 漂移告警阈值 |

### 人格与情绪 (Lifecycle_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| enable_persona_evolution | true | 对话对象画像注入 |
| enable_mood | true | Bot 情绪 |
| enable_dream | true | 做梦系统 |
| dream_interval_hours | 6.0 | 做梦间隔 |
| enable_consolidation | true | LLM 摘要整合 |
| consolidation_interval_hours | 4.0 | 整合间隔 |

### 多 Bot / MetaThinking

| 配置组 | 说明 |
|--------|------|
| MetaThinking_Bot1 / Bot2 | bot QQ、名称、db_id、别名、主动插话、排除 source |
| MetaThinking_Settings | 规则过滤、主动插话频率、静默时段、Provider fallback |
| PersonaComposer | 无单独配置；自动使用 bot registry、BeliefEngine、经历检索、Few-Shot |

### 与 SelfLearning / ChatPlus 共存

| 目标 | WaveMemory 推荐配置 | 外部插件建议 |
|------|---------------------|--------------|
| WaveMemory 独立注入 | `runtime_mode=full` | 关闭外部插件的重复记忆注入/长期记忆工具 |
| WaveMemory 只做基础记忆 | `runtime_mode=memory_only` | 关闭外部插件的重复写入或重复注入能力 |
| WaveMemory 做兼容后端 | `runtime_mode=compat_only` + `livingmemory_alias_tools_enabled=true` | 外部插件调用 `recall_long_term_memory` / `memorize_long_term_memory`；不要同时启用 WaveMemory 原生自动注入 |

### 记忆淘汰 (Eviction_Settings)

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| enabled | true | 启用淘汰 |
| noise_ttl_days | 7 | noise 保留天数 |
| chat_stale_days | 30 | chat 闲置天数 |

---

## 运维排查

### 查看一次注入 trace

1. 打开 9876 WebUI → 注入观察台。
2. 用时间、群聊/私聊、sender、bot、channel、status、has_error 筛选。
3. 点开 trace 详情，检查：
   - `request.message_preview`
   - `final_preview`
   - `budget.total_tokens / total_latency_ms`
   - `channels[].status / latency_ms / tokens / hit_items / filtered_items`
   - `feedback`

只读 API：

```text
GET /api/injection/traces?limit=100&channel=memory&status=ok
GET /api/injection/traces/<trace_id>
```

### 判断重复注入

| 现象 | 看哪里 | 处理 |
|------|--------|------|
| 同一内容在最终 prompt 重复 | trace detail → `final_preview` + channel previews | 检查多个通道是否命中同一内容；调低重复通道 `priority/top_k/max_items` |
| AstrBot 最近上下文又被记忆注入 | trace detail → `filtered_items.reason=recent_context_duplicate` | 调大 `Inject_Settings.skip_recent_minutes` 或确认 SafetyChannel 正常 |
| SelfLearning/ChatPlus 与 WaveMemory 都注入 | 兼容模式页 → duplicate warnings | 改 `runtime_mode=compat_only` 或关闭外部插件重复注入 |
| 写入重复记忆 | writer 统计 duplicate / 同内容同群近期重复 | 统一 writer 去重会跳过重复写入，不删除旧数据 |

### 关闭高级通道，保留纯记忆

6185 配置页：

```text
Runtime_Settings.runtime_mode = memory_only
```

效果：

- 保留：消息采集、writer、向量检索、基础 memory 注入、trace、搜索/记住工具、兼容 facade。
- 默认关闭：persona、belief、jargon、few-shot、BookLore、affinity、mood、dream、consolidation、Study、SelfReflect 等高级能力。
- 验证：注入观察台 trace 只应出现 memory/safety/可选 timeline/facts/fts5 等基础通道，不应出现 persona/belief/jargon/fewshot/book_lore。

### Agent 反馈安全边界

| 工具 | 行为 |
|------|------|
| `wave_memory_explain_injection` | 只读 trace |
| `wave_memory_feedback_memory` | 记录 useful/useless/misleading/duplicate；不删除记忆 |
| `wave_memory_suggest_config` | 写入 pending 配置建议；不自动应用 |
| `wave_memory_submit_review_candidate` | 写入 pending 候选；不自动提升 belief/style/jargon |

禁止项：批量删除、关闭 safety、关闭 audit、改 Provider、改其他插件配置、改 AstrBot 人格、伪装插件身份。

### 性能告警

日志格式：

```text
[WaveMemory] inject_memory 耗时过长: <total_ms>ms > 500ms | channels=[{'channel': 'memory', 'status': 'hit', 'ms': 123.4}]
```

排查顺序：

1. 找最大 `ms` 的通道。
2. 如果 status=`timeout`：调低该通道 `top_k/max_items`，或适当调高 `timeout_ms`。
3. 如果 status=`hit` 且 tokens/chars 高：调低 `token_budget/top_k/max_items`。
4. 如果总耗时高但单通道都低：检查 Embedding Provider 延迟、SQLite/HNSW IO、宿主机负载。
5. 用注入观察台确认调整后的 trace 耗时。

---

## ⚙️ 后台服务

| 服务 | 周期 | 功能 |
|------|------|------|
| TagWorker | 持续 | 新消息自动 Tag 提取（batch 100） |
| ConsolidationService | 4h | LLM 摘要整合 → facts + relations + social + nicknames |
| DreamService | 6h | 记忆巩固（三层时间线涟漪浪潮） |
| LifecycleService | 30min | 互动统计 + 记忆衰减 |
| EvictionService | 6h | noise/chat 过期清理 |
| StudyService | 6h | 从 BookLore 主动学习 |
| BeliefEmergence | 15min 触发 | 关系事件 → 待审核信念候选 |
| JargonMining | 每 10 条消息 | 黑话候选挖掘 |
| FewShot Extract | 每天 | 健康风格范例提取 |
| PersonaComposer | 每次注入 | 自我人格 / 信念 / 经历 / 风格样本排序编排 |

---

## 🗺️ 功能地图

| 子系统 | 启用条件 | 配置位置 |
|--------|----------|----------|
| 运行模式 | 自动 | 6185: Runtime_Settings.runtime_mode |
| 向量索引 | Embedding Provider 已配置 | 6185: embedding_provider_id |
| Tag 提取 | Tag LLM Provider 已配置 | 6185: tag_llm_provider_id |
| 共现矩阵 | Tag 覆盖率 > 20% | 自动 |
| 脉冲传播 | 共现矩阵就绪且 full 模式 | 6185: enable_spike_routing |
| 残差金字塔 | Embedding + 共现矩阵且 full 模式 | 6185: enable_residual_pyramid |
| EPA 分析 | Tag 覆盖率 > 20% 且 full 模式 | 6185: enable_epa |
| 测地线重排 | 共现矩阵节点 > 1000 且 full 模式 | 6185: enable_geodesic_rerank |
| FTS5 召回 | full/memory_only | 通道配置 |
| 注入编排器 | enable_auto_inject 且非默认 compat_only | 9876: 通道配置 / 注入观察台 |
| Trace Store | 自动 | 6185: Trace_Settings / 9876: 注入观察台 |
| Agent 反馈 | full/memory_only | 9876: Agent 反馈 |
| LivingMemory-compatible facade | 自动 | 6185: Compatibility_Settings / 9876: 兼容模式 |
| 记忆整合 | LLM Provider 可用且 full 模式 | 6185: enable_consolidation |
| PersonaComposer | full 模式 | 自动 |
| 信念引擎 | 记忆整合就绪且 full 模式 | 自动 |
| 经历片段 | v2.2 schema 已迁移且 full 模式 | 自动 |
| 做梦系统 | enable_dream=true 且 full 模式 | 6185: enable_dream |
| 黑话系统 | LLM + 聊天积累且 full 模式 | 6185: Jargon_Settings |
| Holyman 知识库 | 内置 assets + WebUI 且 full 模式 | 9876: 黑话页 |
| 风格学习 | LLM + bot 回复积累且 full 模式 | 6185: FewShot_Settings |
| 注入指标 | 自动 | 9876: 概览页 / 注入指标 |
| 身份安全 | 自动 | 无需配置 |
| 防骚扰 | 自动 | 6185/9876: Social_Settings |
| 记忆淘汰 | 自动 | 9876: 淘汰天数参数 |

---

## 项目结构

```
├── engine/                      # 检索引擎（纯算法，零 LLM）
│   ├── query_engine.py          # 五阶段管线编排
│   ├── spike_routing.py         # 脉冲传播
│   ├── residual_pyramid.py      # 残差金字塔
│   ├── epa.py                   # 嵌入投影分析
│   ├── geodesic_rerank.py       # 测地线重排
│   ├── directed_cooccurrence.py # 有向共现矩阵
│   ├── intrinsic_residual.py    # 内生残差
│   ├── semantic_gain.py         # 语义增益
│   └── vector_index.py          # HNSW 索引
├── services/                    # 灵魂系统 + 业务服务
│   ├── persona_composer.py      # 自我人格/信念/经历/风格编排
│   ├── persona_evolution.py     # 对话对象画像
│   ├── belief_engine.py         # 信念引擎
│   ├── belief_emergence.py      # 信念涌现
│   ├── experience_episodes.py   # 经历片段
│   ├── identity_safety.py       # 身份污染防线
│   ├── desire_engine.py         # 欲望引擎
│   ├── mood_trajectory.py       # 情绪轨迹
│   ├── subjective_time.py       # 主观时间
│   ├── consolidation.py         # 记忆整合
│   ├── dream.py                 # 做梦系统
│   ├── self_reflect.py          # 自省系统
│   ├── study_service.py         # 主动学习
│   ├── jargon/                  # 黑话 / Holyman 分层知识库
│   └── few_shot/                # 健康风格学习
├── tools/                       # 9 个 Agent 工具
├── webui/                       # Web 管理面板
└── main.py                      # 插件入口
```

---

## 致谢

核心检索算法源自 [VCPChat](https://github.com/lioensky/VCPChat) / [VCPToolBox](https://github.com/lioensky/VCPToolBox) by [@lioensky](https://github.com/lioensky)。

## License

AGPLv3
