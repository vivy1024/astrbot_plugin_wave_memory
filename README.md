# astrbot_plugin_wave_memory

基于 VCP TagMemo 浪潮算法的 AstrBot 高性能记忆插件。

**查询路径零 LLM 调用，延迟 < 500ms。**

## 特性

- **自动记忆**：群聊/私聊消息自动存入记忆库（异步不阻塞）
- **自动召回**：每次对话前自动检索相关记忆注入上下文
- **主动工具**：模型可主动搜索记忆 (`wave_memory_search`) 和存储重要信息 (`wave_memory_remember`)
- **残差金字塔**：多层语义分解，复杂问题多维度召回
- **脉冲传播**：通过 Tag 共现图发现间接关联
- **EPA 分析**：根据问题聚焦度动态调整检索策略
- **测地线重排**：基于共现拓扑修正向量距离偏差
- **做梦系统**：后台定时记忆巩固与联想发现
- **记忆衰减**：长期未访问的记忆自然淡化
- **数据迁移**：支持从 angel_memory / livingmemory / self_learning 导入历史数据

## 安装

### 通过 AstrBot 插件市场（推荐）

在 WebUI → 插件市场中搜索 `wave_memory` 安装。

### 手动安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/vivy1024/astrbot_plugin_wave_memory.git
```

容器内安装依赖：

```bash
docker exec astrbot pip install hnswlib numpy scikit-learn
```

重启 AstrBot。

## 配置

在 WebUI → 插件 → Wave Memory 中配置：

### Embedding 设置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `embedding_provider_id` | Embedding 模型 ID（需先在模型提供商中添加 Embedding 类型） | 空 |
| `dimension` | 向量维度（必须与 Embedding 模型一致） | 1024 |

### Tag 设置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `tag_llm_provider_id` | Tag 提取用的 LLM（建议选便宜快的） | 空 |
| `max_tags_per_message` | 每条消息最大 Tag 数 | 10 |
| `tag_extraction_enabled` | 是否启用 LLM Tag 提取 | true |

### 查询设置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `enable_auto_inject` | 自动注入记忆到 LLM 请求 | true |
| `inject_top_k` | 每次注入的记忆条数 | 5 |
| `enable_spike_routing` | 启用脉冲传播（间接关联） | true |
| `enable_residual_pyramid` | 启用残差金字塔（多维度召回） | true |
| `enable_epa` | 启用 EPA 分析 | false |
| `enable_geodesic_rerank` | 启用测地线重排 | false |

### 存储设置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `max_memories` | 最大记忆条数 | 100000 |
| `memory_decay_enabled` | 启用记忆衰减 | true |
| `decay_rate` | 每天衰减率 | 0.995 |

## 工作原理

### 写入路径（异步后台）

```
消息 → 异步队列 → Embedding → 存 SQLite + hnswlib 索引
                 → LLM 提取 Tag → 存 Tag + 更新共现矩阵
```

不阻塞回复。

### 查询路径（同步，< 500ms）

```
用户消息 → Embedding (1次API, ~200ms)
         → hnswlib 向量检索 (<1ms)
         → [可选] 残差金字塔多层分解 (<5ms)
         → [可选] 脉冲传播图遍历 (<1ms)
         → [可选] 测地线重排 (<1ms)
         → 注入 top_k 记忆到 LLM 上下文
```

**零 LLM 调用**，延迟仅取决于 Embedding API。

### 对比

| | self_learning (LightRAG) | wave_memory |
|---|---|---|
| 查询延迟 | 50 秒 | ~200ms |
| 查询时 LLM 调用 | 2-3 次 | 0 次 |
| 间接关联 | 有（慢） | 有（本地快） |
| 多维度召回 | 无 | 有 |
| 记忆衰减 | 无 | 有 |
| 做梦巩固 | 无 | 有 |

## 算法来源

核心算法移植自 [VCP ToolBox](https://github.com/lioensky/VCPToolBox) 的 TagMemo V8 浪潮算法：

- **EPA (Embedding Projection Analysis)**：PCA 投影分析查询聚焦度
- **残差金字塔**：Gram-Schmidt 正交化多层语义分解
- **脉冲传播**：共现矩阵 + 虫洞路由的神经元联想
- **测地线重排**：基于能量距离场的拓扑感知重排

Python 实现使用 `hnswlib`（C++ HNSW 向量索引）+ `numpy`（向量运算）+ `SQLite`（持久化存储）。

## 与其他插件的关系

- **可替代 self_learning 的知识检索功能**（关掉 self_learning 的 `knowledge_engine`）
- **可与 self_learning 共存**（各自注入不冲突，self_learning 保留风格学习）
- **可替代 livingmemory 的记忆检索**（wave_memory 更快且有图遍历能力）

## 开发

```bash
# 本地测试
cd AstrBot/data/plugins/astrbot_plugin_wave_memory
python -c "from engine.database import WaveMemoryDB; print('OK')"
```

## License

MIT
