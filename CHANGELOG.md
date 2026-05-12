# Changelog

## v0.4.3 (2025-07-13)

### 新功能

- **Consolidation topics 回写 memory_tags**：整合服务提取的段落级话题标签自动写回每条消息，零额外 LLM 成本，短消息不再需要单独猜话题

### 改进

- Tag backfill batch_size 500→50，避免 LLM 截断导致 tag 错位
- 空 tag 结果标记 `skipped` 而非 `done`，不阻塞重新处理
- Consolidation topic 回写过滤泛化词（日常闲聊/灌水等）

## v0.4.1 (2025-07-13)

### 修复

- **deep_search 工具不可用**：方法名 `execute` → `call`，对齐 AstrBot FunctionTool 接口
- **memory_search 偶发 TypeError**：timestamp 字段为 ISO 字符串，解析后再计算时间衰减

## v0.4.0 (2025-07-13)

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

## v0.3.0 (2025-07-07)

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
