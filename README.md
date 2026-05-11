<div align="center">

# Wave Memory — 基于 VCP TagMemo 浪潮算法的高性能记忆插件

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

**查询路径零 LLM 调用 · 本地计算 < 2ms · 万级记忆规模向量检索 < 1ms**

</div>

---

## 核心特性

### 🧠 认知引擎

- **VCP TagMemo V8 浪潮认知引擎**：五阶段级联处理（EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 测地线重排），远超传统 RAG 精度
- **零 LLM 查询路径**：检索全程本地计算，仅 Embedding API 产生网络延迟（~250ms）
- **有向共现矩阵**：Tag 间方向性关联建模 + 防抖调度器（双缓冲原子切换，不阻塞查询）
- **内禀残差计算器**：共现矩阵重建后自动重算，捕获 Tag 间非线性关联
- **社区检测**：Label Propagation 轻量实现，Tag 聚类分析

### 🏷️ Tag 体系

- **结构化 Tag 体系**：8+ 种语义类型（person/topic/entity/event/emotion/fact/location/time/keyword），支持 LLM 批量提取
- **Tag RAG 提取**：embedding 搜索已有 Tag 库注入提取 prompt，大幅提升 Tag 复用率（embedding 不可用时 graceful fallback 到静态词表）
- **Tag 审计系统**：LLM 驱动的 Tag 质量审计（合并/重分类/删除建议），SSE 流式进度，支持批量批准/拒绝

### 🌐 记忆管理

- **跨群记忆共享**：所有群共享同一记忆池，跨群人物画像自动合并
- **通用数据源导入**：自动扫描所有已安装记忆插件的数据库，已知插件免配置导入，未知插件 LLM 自动分析表结构
- **脉冲传播 + 虫洞路由**：模拟神经网络的联想激活，发现跨域关联
- **做梦系统 (AgentDream)**：模拟人脑睡眠记忆巩固，三层时间线涟漪浪潮，6 小时周期自动执行
- **LLM 摘要整合**：定时 4 小时周期，碎片消息 → 结构化知识（summary + topics + facts + relations）

### 🎭 人格与情绪

- **人格进化系统**：多维好感度（熟悉度/信任/趣味/深度/敌意）→ 态度分级 → 动态 prompt 注入
- **Bot 情绪系统**：根据群消息密度和情感 tag 分布动态生成情绪状态，影响回复风格
- **事实三元组提取**：LLM 整合时自动提取结构化 facts（subject/predicate/object），构建知识图谱

### 🖥️ WebUI 管理

- **神经云图 /explore**：Sigma.js + Graphology 全新渲染，支持星图/联想/人物/路径四视角
- **维护工作台 /maintain**：Tag 审计触发、统计卡片、建议列表、批量操作
- **数据导入面板**：实时进度条 + 互斥锁 + LLM Tag 批量提取
- **生命周期配置面板**：好感度/人格/情绪/做梦/整合全部可配置
- **暗色/亮色主题**

---

## 快速开始

### 安装

将插件目录放置在 AstrBot 的 `data/plugins` 下，AstrBot 会自动安装依赖。

### 配置

在 AstrBot 插件配置页面设置：

**必填**：
- `embedding_provider_id`：Embedding 模型 Provider ID
- `tag_llm_provider_id`：Tag 提取用的 LLM Provider ID

**可选**：
- `embedding_dimension`：向量维度（默认 1024）
- WebUI 设置（端口、密码等）
- 查询参数（Top-K、相似度阈值、注入格式等）

### AstrBot 版本要求

- AstrBot >= 4.14.0

### WebUI 管理面板

启动后访问 `http://<host>:9876`（端口可配置）

| 页面 | 路径 | 功能 |
|------|------|------|
| 主面板 | `/` | 记忆浏览、查询测试、数据导入、配置 |
| 神经云图 | `/explore` | Tag 关系可视化、社区探索 |
| 维护工作台 | `/maintain` | Tag 审计、质量统计、批量操作 |

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
│  ├── 动量系统（历史查询加速）                              │
│  └── 输出: activated_tags[], spike_scores                │
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
| ConsolidationService | 4 小时 | LLM 摘要整合 + 事实三元组提取 |
| DreamService | 6 小时 | 记忆巩固（三层时间线涟漪浪潮 + 共振桥梁发现） |
| TagJob | 持续 | 新记忆自动 Tag 提取（batch 500，含 RAG 参考） |
| TagBackfill | 启动时 | 覆盖率 < 90% 时自动补打历史记忆 Tag |

---

## Agent 工具

| 工具名 | 功能 |
|--------|------|
| `wave_memory_search` | 主记忆搜索（五阶段级联） |
| `wave_memory_deep_search` | 多轮联想深度搜索 |
| `wave_memory_person_search` | 按人物查询相关记忆 |

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
│   ├── vector_index.py        # 向量索引（FAISS/NumPy）
│   ├── embedding.py           # Embedding 接口
│   ├── database.py            # SQLite 数据层
│   ├── query_engine.py        # 五阶段查询引擎
│   └── context_segmenter.py   # 上下文分段
├── services/                  # 业务服务
│   ├── lifecycle.py           # 生命周期（好感度/衰减/表达模式）
│   ├── consolidation.py       # LLM 摘要整合 + 事实提取
│   ├── dream.py               # 做梦系统（记忆巩固）
│   ├── persona_evolution.py   # 人格进化注入
│   ├── tag_extractor.py       # Tag 提取（含 RAG）
│   ├── tag_auditor.py         # Tag 审计（LLM 驱动）
│   ├── tag_job.py             # Tag 后台任务
│   ├── message_writer.py      # 消息写入
│   ├── migration.py           # 数据库迁移
│   └── hot_config.py          # 热配置
├── tools/                     # AstrBot Agent 工具
│   ├── memory_search.py       # 记忆搜索工具
│   ├── deep_search.py         # 深度搜索工具
│   └── person_search.py       # 人物搜索工具
└── webui/                     # WebUI 管理面板
    ├── __init__.py            # FastAPI 后端
    ├── source_discovery.py    # 通用数据源发现 + 导入
    ├── importer.py            # 旧版导入器（兼容）
    └── static/                # 前端静态文件
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
