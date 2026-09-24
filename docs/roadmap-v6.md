# WaveMemory v6 架构改进计划

> 2026-09-24 基于 v5.0.0 源码评估编写。结论来自读代码与容器只读检查；207 个测试文件尚未运行，
> 开工前先跑一次测试基线。工作区当前有 115 处未提交改动，重构前需要先落地或确认。

## 进度（2026-09-25，分支 v6，未推送、未部署）

| 编号 | 状态 | 说明 |
|---|---|---|
| P0 | 部分 | 卷外备份已做（F 盘快照）；端口/令牌按家庭局域网威胁模型暂缓；统一部署脚本未做 |
| P1 | 完成 | bot_profiles 表 + 热重载注册表 + Bot 管理页；生产代码不再写死角色 |
| P2 | 大部分 | 配置来源总览、热参数落库、写死常量改热参数；静态配置瘦身与 schema_version 迁移未做 |
| P3 | 大部分 | 工具/通道注册表、extensions/ 外部扩展；main.py 仍约 3900 行，宿主适配接口未定义 |
| P4 | 大部分 | 后台服务单独启停、工具开关、Bot 热加；「只重启受影响服务」与扩展热重载未做 |
| P5 | 完成 | 注入走完整编排器、写入走统一流程、工具经 Runtime 调用；Cortico 侧同步与工具代理 |
| P6 | 完成 | Bot 管理、配置来源、服务与扩展页；观测台按来源筛选；探索页仍内嵌旧版（有用，保留） |
| P7 | 完成 | 迁移代码移到 services/migrations_archive/、scripts 分类、CI |
| P8 | 部分 | 备份修复、outbox 历史清理、memories/query 改 FTS5；FTS5 中文分词召回问题待解决，书设按 Bot 语料未做 |

新发现待办：`fts_memories` 用 unicode61 分词，中文连续字串整体成一个词，「张羽」只召回到含该词的 2710 条里的 146 条；
需要改为分词后的影子列或 trigram 方案，并做一次全量重建。

## 总览

| 编号 | 工作流 | 解决什么 | 规模 | 依赖 |
|---|---|---|---|---|
| P0 | 安全与部署一致性 | 公开默认令牌、端口暴露、三份代码不一致 | 小 | 无 |
| P1 | 多 Bot 通用化 | 两个写死槽位、代码里写死的羽书/白真真 | 大 | P0 |
| P2 | 配置统一 | 三层配置分散、常量写死、需重启不透明 | 中 | P1 |
| P3 | 插件化 | 通道/工具/服务手写注册、绑死 AstrBot | 大 | P1 |
| P4 | 热插拔 | 后台服务、Bot、扩展改动必须重启容器 | 中 | P3 |
| P5 | Cortico 接入 | 注入走简化版、写入不全、工具缺失 | 中 | P2、P3 部分 |
| P6 | 可视化补齐 | Cortico 路径不可见、缺 Bot/服务管理页 | 中 | P1、P4、P5 |
| P7 | 代码健康 | 一次性迁移代码混在服务层、无 CI | 中 | 随时 |
| P8 | 性能与数据 | 4.3 GB 单库、LIKE 检索、备份 | 小到中 | P5 |

建议里程碑：**M0**（P0 + 测试基线 + 处理未提交改动）→ **M1**（P1）→ **M2**（P2 + P5 注入部分）
→ **M3**（P3 + P5 写入与工具）→ **M4**（P4 + P6）→ P7、P8 穿插进行。

---

## P0 安全与部署一致性

### 现状（已核实）

- `webui/blueprints/runtime.py`：`WAVEMEMORY_RUNTIME_TOKEN` 未设置时退回 `DEFAULT_DEV_TOKEN = "yushu-dev-token"`；
  仓库是公开的，`AstrBot-master` 的 compose 文件没有设置这个环境变量，Cortico 部署用的也正是默认值。
- `docker-compose.override.yml` 写的是 `"9876:9876"`，Docker 会绑定到宿主机所有网卡。
  Runtime API 可以读记忆（`context/prepare`、`memories/query`），也可以写（`observations/batch`、`commands`）。
- 代码有三份：开发仓库、`AstrBot-master/data/plugins/astrbot_plugin_wave_memory`（9 月 12 日的旧副本）、
  Docker 卷 `astrbot_data_runtime` 里实际运行的那份。开发仓库与容器副本有 34 个 `.py` 文件内容不同。
  没有找到同步脚本。
- 真实数据库在卷里：`plugin_data/astrbot_plugin_wave_memory/wave_memory.db`，约 4.3 GB。

### 计划

1. 端口改为 `"127.0.0.1:9876:9876"`，只允许本机访问。
2. Runtime API 不再有默认令牌：没配置令牌时只接受回环地址的请求，否则拒绝；令牌通过 `.env` 注入容器。
3. Cortico 的 `cortico-world-wavememory` 改用宿主密钥表（`host.secret`）读取令牌，去掉代码里的默认值和配置里的明文。
4. 定一个唯一的部署方式：开发仓库以只读方式挂载进容器的插件目录，或者用一条同步脚本加版本戳。
   9876 的 `/health` 和首页显示运行中的 git 提交号。
5. 旧的 `AstrBot-master/data/plugins/...` 副本标注为废弃或删除（删除前需确认），同步更新 openclaw 的 CLAUDE.md。
6. 确认 4.3 GB 数据库有卷外备份（`backup_lifecycle` 的实际产物路径和保留策略）。

**验收**：局域网其他机器访问 9876 被拒；不带令牌的 Runtime 请求返回 401；9876 显示的版本号与开发仓库 HEAD 一致。

---

## P1 多 Bot 通用化（重点）

### 现状

- `_conf_schema.json` 只有 `MetaThinking_Bot1` / `MetaThinking_Bot2` 两个固定对象，
  `main.py:_build_bot_registry` 只遍历这两个键。加第三个角色必须改 schema 和代码。
- 注册表按 QQ 号为键，每个 Bot 只能绑定一个 QQ 号，没有 Cortico、B 站等非 QQ 身份。
- 会话 id 的格式是 `{AstrBot 平台实例 id}:{kind}:{会话号}`（`services/scopes.py`），羽书是 `羽书:group:…`。
  在 AstrBot 里给平台改个名，新旧记忆就分成两条线；Cortico 写入时也只能模仿 `羽书` 这个名字。
- 生产代码里写死的角色内容：

| 位置 | 写死内容 |
|---|---|
| `services/identity_safety.py` | 羽书的自称词、防猫娘化规则、`羽书/白真真` 目标正则 |
| `services/daily_diary_bridge.py` | `bot_name="羽书"`、会话 `羽书:group:…` |
| `services/jargon/statistical_filter.py` | 两个 QQ 号 `2500447291`、`1336495069` |
| `services/self_reflect.py` | `白真真：` 前缀 |
| `engine/query_engine.py` | `bzz_experience`（白真真的记忆来源） |
| `services/lifecycle.py`、`main.py:1580`、`engine/db/social_repo.py` | 默认 `bot_id="yushu"` |
| `webui/blueprints/runtime.py` | 《没钱修什么仙》书设 3 行、`system_speakers` 里的 yushu/corti |
| `_conf_schema.json` 默认值 | 羽书、白真真的名字、QQ 号、兴趣词、`bzz_*` 来源 |

### 目标设计

**Bot Profile v2**，存进 WaveMemory 自己的数据库表 `bot_profiles`（带版本），不再依赖 AstrBot 的静态 schema：

```text
BotProfile
├─ identity    db_id(稳定主键,永不改)、显示名、别名、启用状态
├─ bindings    多个身份绑定:[{host: astrbot, platform: <平台实例id>, self_id: <QQ号>},
│              {host: cortico, deployment: yushu-live}, {host: bilibili, room: 24292304} …]
├─ session_key 规范会话前缀(羽书沿用 "羽书",新 Bot 自定),与平台实例名解耦
├─ persona     自称词、身份安全规则、日记署名、书设语料 id、专属记忆来源命名空间
├─ behavior    MetaThinking、主动插话、兴趣词、排除来源、模型
├─ channels    该 Bot 的注入通道覆盖配置(在全局通道配置之上)
└─ tools       工具白名单或黑名单
```

### 步骤

1. 建 `bot_profiles` 表和仓储层，配套 JSON 导入导出。
2. 首次启动时把 `MetaThinking_Bot1/2` 自动迁移进表；`db_id` 保持 `yushu`、`baizz` 不变，**已有数据一条都不用动**。
   旧配置键继续作为只读回退保留一个版本。
3. 会话 id 规范化：绑定里记录「平台实例 id → 规范前缀」的映射。羽书的规范前缀就是 `羽书`，历史数据零迁移；
   以后平台改名只需改映射。
4. 把上表列出的写死内容逐项移入 Profile：身份安全规则改成每个 Bot 可配的规则集，书设改成按语料 id 取，
   `bzz_*` 改成每个 Bot 的来源命名空间，默认 `yushu` 改成「请求里必须带 Bot」，缺了就报错，不再静默落到羽书名下。
5. 9876 新增「Bot 管理」页：新增、编辑、停用、导入导出；保存后重建 scope 解析器和注册表（热生效，见 P4）。
6. Runtime API 请求改为按绑定解析 Bot（Cortico 传部署名或 db_id），不再依赖调用方拼 `羽书:group:…`。

**验收**：不重启就能在 9876 上加第三个 Bot，并在测试群收发和入库；生产代码里搜 `羽书|白真真|yushu|baizz|2500447291|1336495069`，
除迁移脚本、测试和素材外结果为零；羽书、白真真的历史记忆和好感召回结果与改造前一致。

**风险**：身份安全规则从代码变成配置后，规则被误删会放开防护。规则集要自带默认模板，删空时给警告。

---

## P2 配置统一

### 现状

- 三层配置分在三处：AstrBot 静态配置（26 组、135 项、32 个开关，重启生效）、9876 注入通道配置（热生效）、
  `HotConfig` 的 16 个算法参数（热生效）。
- 已有 `services/config/effective_config.py`、`settings_state.py`、通道配置的版本号、差异对比和「需重启」判断，但只覆盖通道这一部分。
- 写死的常量：消息合并 4 秒和 12 秒、`/context/prepare` 的停用词表和书设、注入慢警告 2000 ms 等。
- AstrBot 保存表单时会把布尔开关覆盖成 false（CLAUDE.md 的头号教训），目前靠启动时打警告来防。

### 计划

1. 统一成一个配置注册表：每个配置项声明类型、范围、默认值、作用域（全局 / Bot / 通道）和生效方式（热生效 / 需重启）。
2. 合并顺序固定为：内置默认 < AstrBot 静态配置 < Bot Profile < 9876 热覆盖。每个最终值都能查到来源。
3. AstrBot 静态配置只保留启动必需项（端口、密码、嵌入模型 id、运行模式），其余逐步迁到 9876 管理；
   静态配置加 `schema_version` 和迁移函数，从根上避开布尔覆盖问题。
4. 把写死的常量改成配置项（有合理默认值，不强迫用户配）。
5. 9876 设置页：列出每一项的当前值、来源、是否需重启，支持差异对比和导入导出；每次注入的 trace 里记录配置版本号（通道部分已经有了）。

**验收**：任意一项配置都能在 9876 上查到当前值和来源；修改热生效项后下一次注入就用新值；
升级后旧配置文件能自动迁移，布尔开关不会被翻转。

---

## P3 插件化

### 现状

- 注入通道已经有统一接口（`InjectionChannel` Protocol）和编排器，但 11 个通道在 `main.py:1011` 附近手写实例化。
- 21 个工具都直接继承 AstrBot 的 `FunctionTool`，在 `main.py:1432-1496` 分组手写注册。
- 约 50 个服务在 `main.py.__init__`（624 行）里一次性创建；`webui/container.py` 是带 30 个 `Any` 字段的全局单例。
- 模型调用和向量生成借用 AstrBot 的 provider（`llm_fallback`、`embedding` 等 8 处）；
  引擎和大部分服务只依赖 AstrBot 的 logger（48 个文件），耦合浅。
- `domain/commands.py` 已有写入命令抽象（`DomainCommand`），写入协调器在用。

### 计划

1. **通道注册表**：`register_channel(name, factory, deps, default_config)`，内置通道自注册；
   支持从 `plugin_data/.../extensions/` 加载外部通道，比如以后单独做一个「直播专用」通道。
2. **工具注册表（和宿主无关）**：`ToolSpec(name, schema, handler(ctx, args), capability, modes)`。
   配两个适配器：AstrBot 适配器包成 `FunctionTool`；Runtime 适配器开成 `/api/runtime/v1/tools/{name}`，
   并在 `/capabilities` 里列出全部工具。21 个工具逐个迁移，每迁一个跑一次对应测试。
3. **服务注册表**：`ServiceSpec(name, deps, start, stop, health, config_keys)`，由 `TaskSupervisor` 统一管理，按依赖顺序启动。
4. **宿主适配层**：把 AstrBot 专属的部分收进三个接口：`HostEvents`（消息钩子）、`LLMClient`、`EmbeddingClient`。
   这样 WaveMemory 以后可以单独作为记忆服务运行（给 Cortico 用），不强制依赖 AstrBot。这一步只定接口，是否真做独立运行以后再定。
5. **拆组合根**：`main.py` 按领域拆成几个启动模块（存储、检索、注入、社交、后台任务、WebUI）；
   服务容器改成有类型的字段，不再全是 `Any`。

**验收**：新增一个通道或工具只需要新建一个文件，不用改 `main.py`；
Cortico 调 `/capabilities` 能拿到和 AstrBot 一样的工具清单；`main.py` 降到 1000 行以内。

---

## P4 热插拔

### 现状

注入通道能热开关（只是跳过执行）；后台服务（做梦、自省、黑话、标签 worker、记忆淘汰、MetaThinking 等）
只在启动时按静态开关创建；改 Bot、加工具、加通道都要重启整个 AstrBot 容器。

### 计划

1. 基于 P3 的服务注册表，9876 上可以单独启动、停止、重启某个后台服务：先等进行中的任务收尾，再按依赖顺序处理。
2. 需重启的配置改为「只重启受影响的服务」，不再重启容器。
3. Bot 注册表热重载：保存 Bot Profile 后原地重建 scope 解析器、MetaThinking 和对应通道配置。
4. 外部通道和工具支持重新加载。工具在 AstrBot 侧能否动态注销，需要先验证 AstrBot 4.14 的 API；
   做不到就退一步：工具常驻，由 handler 按开关决定是否执行。

**验收**：在 9876 上停掉「做梦」服务、改完参数再启动，全程不中断 QQ 回复；加一个 Bot 不用重启容器。

---

## P5 Cortico 接入

1. **注入**：`/context/prepare` 改为复用 `InjectionOrchestrator`，走完整通道（全文加向量检索、关系、信念、黑话等），
   读 Bot 和通道配置，写入 trace 并标注来源为 cortico。删掉写死的书设和 `LIKE` 检索。
2. **写入**：把 `InboundMessagePipeline` 和 `on_bot_sent` 里与宿主无关的部分抽成共享流程
   （「记住」「忘记」和 `/teach`、黑话积累、纠错自省、生命周期统计、群名、互动次数、好感）。
   `/observations/batch` 和 AstrBot 钩子都调用这套流程，事件 id 用平台消息号（与 AstrBot 去重一致）。
3. **Cortico 侧**：`cortico-world-wavememory` 定时读事件库（`qq.message`、`qq.self`、B 站弹幕），
   按事件来源算会话，批量写入；9876 离线时游标不前进，恢复后补写。工具从 `/capabilities` 自动获取。

**验收**：测试群里同一句话，AstrBot 路径和 Cortico 路径注入的记忆块一致；Cortico 写入的消息能被「记住」、会更新好感、不重复入库。

---

## P6 可视化补齐

- 注入观测台收录 Runtime API 的 trace，可以按来源（AstrBot / Cortico）筛选。
- 新增「Bot 管理」页（P1）、「服务状态」页（P4：运行状态、健康、启停按钮）、配置来源视图（P2）。
- 新版前端的「探索」页目前是占位，要么做完，要么下线旧的 `static/explore.html`、`app.js`、`kg.js`，只保留一套前端。

---

## P7 代码健康

- 一次性的迁移和恢复代码混在服务层：`approved_scope_recovery`（1361 行）、`scope_recovery_migration`（1300 行）、
  `legacy_relationship_migration`（1028 行）、`system_convergence_runtime`（1341 行），
  以及生产目录里的 `system_convergence_test_port`。确认已在生产库执行完毕后，移到 `migrations/archive/`。
- `scripts/` 下 53 个一次性脚本，按「仍在用 / 已完成」分类归档。
- 仓库是公开的但没有 CI：加 GitHub Actions，跑 pytest、前端类型检查和前端构建。
- 先跑一次测试基线，记录现状失败项，之后每个阶段都不能新增失败。

---

## P8 性能与数据

- `/context/prepare` 现在对记忆表做多个 `LIKE '%词%'` 查询，全表扫描，库已经 4.3 GB。P5 改为走 FTS5 和向量检索后自然解决。
- 评估 4.3 GB 的组成（向量、trace、历史表），检查淘汰策略和 trace 保留期是否生效，必要时安排 `VACUUM` 的维护窗口。
- 书设库 `book_lore.db` 与多 Bot 语料 id 对齐（P1）。

---

## 兼容性原则（全程遵守）

- 羽书、白真真的 `db_id` 和已有会话 id 不变，不做批量数据改写。
- 每个新增配置项都要有默认值、旧配置迁移函数，并在 CHANGELOG 里注明。
- 每一步都单独发版，可以回滚；数据库结构变化只加不减（`ADD COLUMN` / `IF NOT EXISTS`）。
