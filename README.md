<div align="center">

# Wave Memory — 基于 VCP TagMemo 浪潮算法的高性能记忆插件

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-green.svg)](https://github.com/AstrBotDevs/AstrBot)

**查询路径零 LLM 调用 · 本地计算 < 2ms · 万级记忆规模向量检索 < 1ms**

</div>

---

## 核心特性

- **VCP TagMemo V8 浪潮认知引擎**：五阶段级联处理（EPA → 残差金字塔 → 脉冲传播 → 向量融合 → 测地线重排），远超传统 RAG 精度
- **零 LLM 查询路径**：检索全程本地计算，仅 Embedding API 产生网络延迟（~250ms）
- **通用数据源导入**：自动扫描所有已安装记忆插件的数据库，已知插件免配置导入，未知插件 LLM 自动分析表结构
- **结构化 Tag 体系**：8 种语义类型（person/topic/entity/event/emotion/fact/location/time），支持 LLM 批量提取
- **脉冲传播 + 虫洞路由**：模拟神经网络的联想激活，发现跨域关联
- **做梦系统 (AgentDream)**：模拟人脑睡眠记忆巩固，三层时间线涟漪浪潮
- **WebUI 管理面板**：记忆浏览、查询测试台、数据导入（实时进度条 + 互斥锁）、LLM Tag 批量提取、暗色/亮色主题

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
│  Phase 3: 脉冲传播 (Spike Propagation / LIF-Router)       │
│  ├── 种子注入 → 有向共现矩阵 → 虫洞路由                   │
│  └── 输出: activated_tags[], energy_field{}               │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 4: 向量融合 (Vector Fusion)                        │
│  ├── 语义去重 + 核心标签补全                               │
│  └── 融合: fused = (1-α)·query + α·context               │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Phase 5: 测地线重排 (Geodesic Rerank)                    │
│  ├── 复用 Phase 3 能量场                                  │
│  └── finalScore = (1-α)·KNN + α·normalizedGeo           │
└─────────────────────────────────────────────────────────┘
```

---

## 通用数据源导入

Wave Memory 的导入系统不硬编码特定插件，而是：

1. **自动扫描** `plugin_data/` 下所有 `.db` 文件
2. **已知适配器**：LivingMemory、Angel Memory、Self Learning 等免配置直接导入
3. **未知插件**：启发式分析表结构，或调用 LLM 分析字段映射后导入
4. **增量导入（rowid 游标）**：每次只处理上次游标之后的新记录，万级数据秒级完成
5. **自动去重**：基于内容精确匹配，不会重复导入
6. **安全游标**：失败批次不推进游标（下次重试）；数据库清空时自动重置游标

支持的已知数据源：

| 插件 | 数据类型 | 说明 |
|------|----------|------|
| LivingMemory | 对话记录 / 记忆文档 | conversations.db + livingmemory.db |
| Angel Memory | 结构化记忆 | judgment + reasoning |
| Self Learning | 原始消息 / 范例库 / 黑话词典 | raw_messages + exemplar + jargon |

其他插件（如 mnemosyne、simple_memory、vector_memory、self_evolution 等）会被自动发现，用户可一键导入或让 LLM 分析后导入。

---

## 数据架构

```
wave_memory.db
├── memories          原始记忆 (向量 + 元数据)
├── tags              结构化语义节点 (type/aliases/hierarchy/confidence)
├── tag_relations     Tag 间关系图谱 (is_a/part_of/related_to/causes)
├── memory_tags       记忆↔Tag 多对多 (position + relevance)
├── user_profiles     用户画像 (好感度/交互统计/人格标签)
├── bot_mood          Bot 情绪状态
├── expression_patterns 表达模式库
├── facts             结构化事实三元组
└── kv_store          EPA 缓存 + 导入游标 (import_cursor:*)

memory.hnsw           HNSW 向量索引 (记忆检索)
tags.hnsw             HNSW 向量索引 (Tag 检索)
```

---

## 性能指标

| 指标 | 数值 |
|------|------|
| 向量检索 (万级 memories) | < 1ms |
| 脉冲传播 (1700+ nodes) | 0.1ms |
| 残差金字塔 (缓存命中) | 0.3ms |
| EPA 分析 | 0.2ms |
| 测地线重排 | 0.3ms |
| **本地计算总计** | **< 2ms** |
| Embedding API (网络) | ~250ms |
| **端到端总延迟** | **~250ms** |

---

## 目录结构

```
astrbot_plugin_wave_memory/
├── main.py                    # 插件入口 + 生命周期管理
├── metadata.yaml              # AstrBot 插件元数据
├── _conf_schema.json          # 配置 Schema
├── requirements.txt           # Python 依赖
├── engine/                    # 核心算法引擎
│   ├── database.py            # SQLite 数据层
│   ├── vector_index.py        # HNSW 向量索引
│   ├── embedding.py           # Embedding 服务
│   ├── query_engine.py        # 查询引擎
│   ├── cooccurrence.py        # 共现矩阵
│   ├── spike_routing.py       # 脉冲传播 + 虫洞路由
│   ├── residual_pyramid.py    # 残差金字塔
│   ├── geodesic_rerank.py     # 测地线重排
│   └── epa.py                 # EPA 嵌入投影分析
├── services/                  # 业务服务
│   ├── message_writer.py      # 消息写入
│   └── tag_extractor.py       # LLM Tag 提取
├── tools/                     # AstrBot Agent 工具
│   └── memory_search.py       # 记忆搜索 / 记忆存储工具
└── webui/                     # WebUI 管理面板
    ├── __init__.py            # FastAPI 后端
    ├── source_discovery.py    # 通用数据源发现 + 导入
    ├── importer.py            # 旧版导入器（兼容）
    └── static/                # 前端静态文件
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
