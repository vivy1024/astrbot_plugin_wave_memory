<div align="center">

# Wave Memory — 基于 VCP TagMemo 浪潮算法的高性能记忆插件

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

**查询路径零 LLM 调用 · 本地计算 < 2ms · 万级记忆规模向量检索 < 1ms**

</div>

---

## 核心特性

### 认知引擎

- **VCP TagMemo V8 浪潮认知引擎**：五阶段级联处理（EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 测地线重排）
- **零 LLM 查询路径**：检索全程本地计算，仅 Embedding API 产生网络延迟
- **有向共现矩阵**：Tag 间方向性关联建模 + 防抖调度器（双缓冲原子切换）
- **内禀残差计算器**：共现矩阵重建后自动重算，捕获 Tag 间非线性关联
- **社区检测**：Label Propagation 轻量实现，Tag 聚类分析

### Tag 体系

- **结构化 Tag**：8 种语义类型（person/topic/entity/event/emotion/fact/location/time）
- **Tag RAG 提取**：embedding 搜索已有 Tag 库注入提取 prompt，提升复用率
- **Consolidation 话题回写**：整合服务提取的段落级话题自动写回每条消息，零额外成本
- **Tag 审计系统**：LLM 驱动的质量审计（合并/重分类/删除建议），SSE 流式进度
- **已有 Tag 参考词表**：高频 Tag 注入 prompt，避免创建语义重复标签

### 记忆管理

- **跨群记忆共享**：所有群共享同一记忆池，跨群人物画像自动合并
- **通用数据源导入**：自动扫描已安装记忆插件数据库，已知插件免配置导入，未知插件 LLM 分析表结构
- **脉冲传播 + 虫洞路由**：模拟神经网络联想激活，发现跨域关联
- **做梦系统 (AgentDream)**：6 小时周期后台记忆巩固，三层时间线涟漪浪潮
- **LLM 摘要整合**：4 小时周期，碎片消息 → 结构化知识（summary + topics + facts + relations）

### 人格与情绪

- **人格进化系统**：多维好感度（熟悉度/信任/趣味/深度/敌意）→ 态度分级 → 动态 prompt 注入
- **跨群人物画像合并**：同一用户在不同群的好感度、表达模式、personality_tags 自动聚合
- **Bot 情绪系统**：根据群消息密度和情感 tag 分布动态生成情绪状态
- **事实三元组提取**：LLM 整合时自动提取 facts（subject/predicate/object）

### WebUI 管理

- **神经云图 /explore**：Sigma.js + Graphology 渲染，星图/联想/人物/路径四视角
- **维护工作台 /maintain**：Tag 审计触发、统计卡片、建议列表、批量操作
- **数据导入面板**：实时进度条 + 互斥锁 + LLM Tag 批量提取
- **生命周期配置面板**：好感度/人格/情绪/做梦/整合全部可配置

---

## 快速开始

### 安装

将插件目录放置在 AstrBot 的 `data/plugins` 下，AstrBot 会自动安装依赖。

### 配置

在 AstrBot 插件配置页面设置：

| 配置项 | 说明 | 示例 |
|--------|------|------|
| `embedding_provider_id` | Embedding 模型 Provider ID（必填） | `siliconflow/Qwen3-Embedding-0.6B` |
| `tag_llm_provider_id` | Tag 提取用的 LLM Provider ID（必填） | `xiaomi/mimo-v2.5-pro` |
| `embedding_dimension` | 向量维度（需与模型匹配） | `1024` |

### AstrBot 版本要求

- AstrBot >= 4.14.0

### WebUI 管理面板

启动后访问 `http://<host>:9876`（端口可配置）

| 页面 | 路径 | 功能 |
|------|------|------|
| 主面板 | `/` | 记忆浏览、查询测试、数据导入、配置 |
| 神经云图 | `/explore` | Tag 关系可视化、社区探索 |
| 维护工作台 | `/maintain` | Tag 审计、质量统计、批量操作 |

### 完整配置项

所有配置均可在 AstrBot 插件配置页面调整：

| 分组 | 配置项 | 默认值 | 说明 |
|------|--------|--------|------|
| **基础** | embedding_provider_id | - | Embedding 模型 ID（必填） |
| | tag_llm_provider_id | - | Tag 提取 LLM ID（推荐 mimo-v2.5-pro） |
| | embedding_dimension | 1024 | 向量维度 |
| **记忆召回** | enable_auto_inject | true | 自动注入记忆到 prompt |
| | inject_top_k | 5 | 注入记忆条数 |
| | min_similarity | 0.35 | 最低相似度阈值 |
| | enable_spike_routing | true | 脉冲传播（联想能力） |
| | enable_residual_pyramid | true | 残差金字塔（复杂问题） |
| | enable_epa | true | EPA 嵌入投影分析 |
| | enable_geodesic_rerank | true | 测地线重排 |
| **跨群记忆** | cross_group_enabled | true | 所有群共享记忆池 |
| | cross_group_persona_merge | true | 跨群人物画像合并 |
| **好感度引擎** | familiarity_halflife_days | 200 | 熟悉度半衰期 |
| | trust_halflife_days | 90 | 信任半衰期 |
| | fun_halflife_days | 30 | 趣味半衰期 |
| | depth_halflife_days | 150 | 深度半衰期 |
| | hostility_halflife_days | 60 | 敌意半衰期 |
| | intimate_threshold | 60 | 亲密态度阈值 |
| | friendly_threshold | 30 | 友好态度阈值 |
| | neutral_threshold | 10 | 中立态度阈值 |
| **人格与情绪** | enable_affinity | true | 好感度系统 |
| | enable_persona_evolution | true | 人格进化 |
| | enable_mood | true | Bot 情绪 |
| | positive_emotion_threshold | 0.6 | 正面情绪触发阈值 |
| | negative_emotion_threshold | 0.4 | 负面情绪触发阈值 |
| | enable_dream | true | 做梦系统 |
| | dream_interval_hours | 6.0 | 做梦间隔 |
| | enable_consolidation | true | LLM 摘要整合 |
| | consolidation_interval_hours | 4.0 | 整合间隔 |
| | consolidation_topic_backfill | true | 话题回写 memory_tags |
| **Tag 提取** | tag_extraction_enabled | true | 启用 LLM Tag 提取 |
| | max_tags_per_message | 10 | 每条消息最大 Tag 数 |
| | tag_backfill_batch_size | 50 | 回填批次大小 |
| | tag_blacklist | - | Tag 黑名单（逗号分隔） |
| | consolidation_skip_topics | 日常闲聊,灌水... | 话题回写过滤词 |
| **消息过滤** | min_message_length | 4 | 最小消息长度 |
| | max_message_length | 2000 | 最大消息长度 |
| | group_whitelist | - | 群组白名单 |
| | group_blacklist | - | 群组黑名单 |
| **存储** | max_memories | 100000 | 最大记忆条数 |
| | memory_decay_enabled | true | 记忆衰减 |
| | decay_rate | 0.995 | 每日衰减率 |
| **性能** | embedding_batch_size | 10 | Embedding 批量大小 |
| | write_flush_interval | 30 | 写入刷新间隔（秒） |
| | cooccurrence_rebuild_interval | 3600 | 共现矩阵重建间隔（秒） |

### 热调参（运行时生效）

WebUI 配置 Tab 底部的"热调参"区域，滑块调节后立即生效，无需重启：

| 参数 | 范围 | 默认 | 说明 |
|------|------|------|------|
| spike.firing_threshold | 0.01-0.5 | 0.10 | 脉冲发射阈值，越低越容易激活 |
| spike.base_decay | 0.1-0.5 | 0.25 | 常规传播衰减 |
| spike.wormhole_decay | 0.3-0.9 | 0.70 | 虫洞传播衰减 |
| spike.tension_threshold | 0.3-2.0 | 1.0 | 虫洞张力阈值 |
| spike.max_hops | 1-8 | 4 | 最大传播跳数 |
| query.min_similarity | 0.1-0.8 | 0.35 | 最低相似度阈值 |
| query.boost_alpha_base | 0.1-0.6 | 0.3 | 浪潮增强基础因子 |
| geodesic.energy_weight | 0-1.0 | 0.3 | 测地线能量权重 |
| residual.boost_range | 0-1.0 | 0.6 | 残差增益范围 |

---

## 实测数据

以下数据来自实际生产环境（3 个 QQ 群，持续运行 30+ 天）：

### 规模指标

| 指标 | 数值 |
|------|------|
| 总记忆数 | 30,936 |
| 总 Tag 数 | 25,356 |
| Tag 覆盖率 | 63.4%（19,620 条有 tag） |
| 平均 Tag/条 | 2.03 |
| 人物注册 | 561 人 |
| 用户画像 | 877 条（跨群合并） |
| 事实三元组 | 325 条 |
| 共现图 | 25,031 节点 / 67,980 有向边 |
| 活跃群数 | 3 |

### Tag 类型分布

| 类型 | 数量 | 说明 |
|------|------|------|
| topic | 6,513 | 话题/讨论主题 |
| event | 5,165 | 事件/行为 |
| entity | 3,417 | 具体事物（游戏/小说/品牌） |
| fact | 3,165 | 事实性信息 |
| keyword | 3,155 | 通用关键词 |
| emotion | 1,951 | 情绪/态度 |
| person | 1,476 | 人名/昵称 |
| location | 289 | 地点 |
| time | 225 | 时间标记 |

### 性能指标

| 指标 | 数值 |
|------|------|
| 向量检索（3 万级 memories） | < 1ms |
| 脉冲传播（25,000+ nodes） | 0.1ms |
| 残差金字塔（缓存命中） | 0.3ms |
| EPA 分析 | 0.2ms |
| 测地线重排 | 0.3ms |
| **本地计算总计** | **< 2ms** |
| Embedding API（网络） | ~250ms |
| **端到端总延迟** | **~250ms** |

### Tag 提取模型对比

| 模型 | 平均 Tag/条 | 风格 | 适用场景 |
|------|-------------|------|----------|
| Claude Opus 4.7 (windsurf) | 2.26 | 覆盖面广，偶有冗余 | 需要高召回率 |
| Claude Opus 4.6 (kiro-cc) | 1.49 | 精炼精准，无冗余 | 需要高精度 |
| MiMo v2.5 Pro (xiaomi) | 2.0+ | 均衡，免费无限流 | 日常使用（推荐） |

### 24 小时运行统计

| 指标 | 数值 |
|------|------|
| 收到消息 | 2,879 条 |
| Tag 提取成功 | 1,624 次 |
| 工具调用（羽书主动搜索记忆） | 27 次 |
| Lifecycle flush（好感度更新） | 4 次 |
| 后台服务全部正常 | Dream/Consolidation/Lifecycle/TagJob |

---

## 算法架构

```
用户输入
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 1: EPA 嵌入投影分析                                │
│  ├── 加权 PCA 基底（K-Means 聚类 → SVD 分解）            │
│  ├── 逻辑深度 / 跨域共振 / 信息熵                         │
│  └── 输出: logicDepth, entropy, dominantAxes             │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 2: 残差金字塔 (Residual Pyramid)                   │
│  ├── Gram-Schmidt 正交化投影                              │
│  ├── 递归搜索残差向量 → 捕获微弱信号                       │
│  └── 输出: levels[], coverage, novelty                   │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 3: 脉冲传播 (Spike Routing)                        │
│  ├── Tag 共现图 → 有向共现矩阵                            │
│  ├── 脉冲激活 + 虫洞路由（跨域跳跃）                       │
│  └── 输出: activated_tags[], energy_field{}               │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 4: 向量融合 (Vector Fusion)                        │
│  ├── 多源向量加权融合（query + EPA + spike）              │
│  ├── 内禀残差补偿                                         │
│  └── 输出: fused_vector                                  │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 5: 测地线重排 (Geodesic Rerank)                    │
│  ├── 能量场复用 + 三层防御                                │
│  ├── 语义距离 + 时间衰减 + 关联度综合排序                  │
│  └── 输出: final_results[]                               │
└─────────────────────────────────────────────────────────┘
```

---

## 后台服务

| 服务 | 周期 | 功能 |
|------|------|------|
| LifecycleService | 30 分钟 | 好感度 flush + 表达模式聚合 + 记忆衰减标记 |
| ConsolidationService | 4 小时 | LLM 摘要整合 + 事实三元组提取 + topics 回写 memory_tags |
| DreamService | 6 小时 | 记忆巩固（三层时间线涟漪浪潮 + 共振桥梁发现） |
| TagJob | 持续 | 新记忆自动 Tag 提取（batch 50，含 RAG 参考） |
| TagBackfill | 启动时 | 覆盖率 < 90% 时自动补打历史记忆 Tag |

---

## Agent 工具

| 工具名 | 功能 |
|--------|------|
| `wave_memory_search` | 主记忆搜索（五阶段级联） |
| `wave_memory_deep_search` | FTS5 全文搜索 + 上下文窗口 |
| `wave_memory_person_search` | 按人物查询相关记忆和画像 |

---

## 通用数据源导入

Wave Memory 的导入系统不硬编码特定插件：

1. **自动扫描** `plugin_data/` 下所有 `.db` 文件
2. **已知适配器**：LivingMemory、Angel Memory、Self Learning 等免配置直接导入
3. **未知插件**：启发式分析表结构，或调用 LLM 分析字段映射后导入
4. **增量导入（rowid 游标）**：每次只处理上次游标之后的新记录
5. **自动去重**：基于内容精确匹配
6. **安全游标**：失败批次不推进游标，数据库清空时自动重置

---

## 项目结构

```
├── main.py                    # 插件入口
├── metadata.yaml              # AstrBot 插件元数据
├── CHANGELOG.md               # 版本变更记录
├── CLAUDE.md                  # 开发规则 & 发版流程
├── engine/                    # 核心算法
│   ├── epa.py                 # EPA 嵌入投影分析
│   ├── residual_pyramid.py    # 残差金字塔
│   ├── spike_routing.py       # 脉冲传播 + 虫洞路由
│   ├── geodesic_rerank.py     # 测地线重排
│   ├── directed_cooccurrence.py # 有向共现矩阵 + 社区检测
│   ├── intrinsic_residual.py  # 内禀残差计算器
│   ├── vector_index.py        # HNSW 向量索引
│   ├── embedding.py           # Embedding 接口
│   ├── database.py            # SQLite 数据层
│   └── query_engine.py        # 五阶段查询引擎
├── services/                  # 业务服务
│   ├── lifecycle.py           # 生命周期（好感度/衰减/表达模式）
│   ├── consolidation.py       # LLM 摘要整合 + topics 回写
│   ├── dream.py               # 做梦系统（记忆巩固）
│   ├── persona_evolution.py   # 人格进化（跨群画像合并）
│   ├── tag_extractor.py       # Tag 提取（含 RAG + 参考词表）
│   ├── tag_auditor.py         # Tag 审计（LLM 驱动）
│   ├── tag_job.py             # Tag 后台任务
│   ├── message_writer.py      # 消息写入
│   └── hot_config.py          # 热配置
├── tools/                     # AstrBot Agent 工具
│   ├── memory_search.py       # 记忆搜索
│   ├── deep_search.py         # 深度搜索（FTS5）
│   └── person_search.py       # 人物搜索
└── webui/                     # WebUI 管理面板
    ├── __init__.py            # FastAPI 后端
    ├── source_discovery.py    # 通用数据源发现 + 导入
    └── static/                # 前端
        ├── index.html         # 主面板
        ├── explore.html       # 神经云图
        └── maintain.html      # 维护工作台
```

---

## 贡献者

| 贡献者 | 角色 | 链接 |
|--------|------|------|
| [@lioensky](https://github.com/lioensky) | 算法原作者 — VCP TagMemo 浪潮认知引擎设计者 | [VCPChat](https://github.com/lioensky/VCPChat) · [VCPToolBox](https://github.com/lioensky/VCPToolBox) |
| [@vivy1024](https://github.com/vivy1024) | AstrBot 移植 & 工程实现 | [Wave Memory](https://github.com/vivy1024/astrbot_plugin_wave_memory) |

---

## 致谢

核心算法设计源自 [VCPChat](https://github.com/lioensky/VCPChat) / [VCPToolBox](https://github.com/lioensky/VCPToolBox) by lioensky：
- TagMemo V8 浪潮算法（脉冲传播 + 虫洞路由 + 动量系统）
- EPA 嵌入投影分析（加权 PCA + 跨域共振检测）
- 残差金字塔（Gram-Schmidt 正交化 + 能量截断）
- 测地线重排（能量场复用 + 三层防御）
- AgentDream 做梦系统（三层时间线涟漪浪潮）

---

## License

This project is licensed under AGPLv3.
