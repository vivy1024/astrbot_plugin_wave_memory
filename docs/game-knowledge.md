# 游戏知识库

按游戏分库的**攻略常识**（知识卡 + 词条），所有 Bot 共用。直播时 Cortico 在 `/context/prepare` 里带上 `game`，
注入通道 `game_knowledge` 按游戏标识选库，把命中的攻略卡作为「攻略参考」注入；工具
`wave_memory_game_knowledge` 可以随时查。

和其他模块的边界：

| 内容 | 放在哪 |
|---|---|
| 这个游戏的通用攻略、机制、术语 | 游戏知识库（本文） |
| 她自己玩过的事、直播里发生的事 | 记忆（`wave_memory_search`） |
| 玩出来的心得、看法 | 看法（信念） |

WaveMemory 核心里没有任何游戏专用代码：新增一个游戏 = 在管理台「游戏知识库」建一个库、导入或录入知识。
缺氧与我的世界的专门处理只存在于导入适配器里（格式转换），不进入匹配与注入逻辑。

存储在主库 `wave_memory.db` 的三张表：`game_libraries`、`game_knowledge_cards`、`game_knowledge_terms`
（外加两个全文检索表 `fts_game_cards`、`fts_game_terms`，中文按二字组切分）。迁移幂等，旧库启动时自动建表。

---

## 1. 直播请求里的 `game`

```json
POST /context/prepare
{
  "...": "原有字段不变",
  "game": {
    "id": "minecraft",
    "state": {"time_of_day": "night", "health": 6, "dimension": "overworld"},
    "keywords": ["夜晚", "河边"]
  }
}
```

| 字段 | 规则 |
|---|---|
| `id` | 游戏标识：小写英文、数字、`_ . -`，以字母或数字开头，最长 40。也可以直接把 `game` 写成标识字符串。选库时标识认不出，再按库的显示名与别名找 |
| `state` | 扁平对象：键为英文（`[A-Za-z_][A-Za-z0-9_.-]*`），值为数字、布尔或短字符串（≤60 字）。嵌套对象、列表、超长值被忽略并在观测台记警告；最多 40 个键 |
| `keywords` | 场景关键词：短词列表（或逗号分隔的文字），每个 ≤24 字，最多 20 个 |

- **不带 `game` 时行为与没有游戏知识库时完全一致**：通道返回 disabled，编排器直接丢弃，不出现在结果与观测台里。
- `id` 缺失或不合法：只有本通道在观测台报告原因、不注入，不影响其他通道。
- 每次（非 dry_run）带 `game` 的请求会记下「这个会话 / 这个 Bot 正在玩什么」，保留 30 分钟，供工具省略游戏时使用。

## 2. 注入通道 `game_knowledge`

默认配置（配置中心 → 通道，可按 Bot 覆盖）：

| 项 | 默认 | 说明 |
|---|---|---|
| `enabled` | 开 | 只在请求带 `game` 时工作 |
| `max_items` | 2 | 最多注入几张知识卡，上限 2 |
| `top_k` | 3 | 最多附几个词条，上限 5 |
| `token_budget` | 320 | 本通道自己的预算，超出时按卡 / 行截断 |
| `timeout_ms` | 200 | |
| `priority` | 58 | |

- 注入档位：`full` 与 `light` 运行，`minimal` 不运行。运行模式 `memory_only` / `compat_only` 下关闭。
- 私聊安全包络里保留本通道配置（它只读所有 Bot 共用的知识库、不读会话数据）。
- 只注入**启用**的卡与词条；草稿、归档不注入。游戏库停用时整库不注入（观测台显示 skipped）。
- 观测台里每条都有命中原因 `matched_by` 与得分 `score`；触发了但超出张数上限的卡记在「被过滤」里
  （`game_knowledge_max_cards`），关键词命中却被条件挡住的卡可在管理台「试一下」里看到。条目可跳转到管理台对应的卡。

注入示例（请求 `state={"time_of_day":"night","health":6,...}`、`keywords=["夜晚","河边"]`，消息「门口好像有苦力怕，要不要出去打」，
我的世界起始包在临时库里全部启用后的真实输出）：

```text
[游戏攻略参考 · 我的世界：以下是这个游戏的通用攻略常识，不是你的亲身经历，也不一定适合眼前的局面；用不用、怎么说由你结合实况自己判断]
【夜里在外面 · 前期】
要点：夜里敌对生物会在黑暗处大量生成，装备不好就回屋或睡觉；在外面尽量走亮的地方，边走边插火把；带一面盾牌，能挡正面的箭和攻击
注意：别往黑的地方钻，野外过夜一定要把自己封起来
【苦力怕 · 前期】
要点：苦力怕靠近后会发出嘶嘶声，停一下就爆炸；听到嘶声马上后退拉开距离，离开范围它就不炸了；用剑把它击退，或者用弓箭远程打；苦力怕怕猫和豹猫，会躲着它们走
注意：爆炸会炸坏方块，别让它靠近家；被闪电劈中的苦力怕爆炸威力更大；苦力怕白天不会被太阳烧死
词条：
- 苦力怕（Creeper、爬行者）：靠近玩家后会爆炸的绿色敌对生物，爆炸会破坏方块
```

同一次请求里「血量低先撤」（`health <= 6`）与「溺尸与河边」（keywords「河边」）也触发了，但超出 2 张上限，记在被过滤里。

## 3. 触发规则

一张知识卡可以有**触发关键词**和**数值条件**：

- 写了关键词：关键词命中才触发；数值条件用来限定——状态里有这个键就必须满足，没有这个键时不判定（不挡）；
  条件前加「必须」则缺键时也不触发。
- 没写关键词：数值条件**全部满足**才触发（状态里缺键 = 未判定 = 不触发）。
- 两样都没写：只能通过工具查到，不会自动注入。

关键词按顺序在三处找：

1. **当前消息**：词出现在消息里就算。英文 / 数字词至少两个字母，并按词边界匹配（`bed` 不会命中 `embedded`）；
   中文单字（如「床」）也照原样匹配。
2. **`game.keywords`**：相等即命中；两边都至少两个字时互相包含也算（「夜晚」命中「夜晚降临」）。
3. **`game.state` 的字符串值**：同上。

游戏自己的标识、显示名、别名不当触发词（否则每句提到这个游戏的话都会触发），导入时会自动去掉并提示。

### 条件语法

每行一个条件（管理台里也这样写），键对应 `game.state` 里的键（不分大小写）：

| 写法 | 含义 |
|---|---|
| `cycle 30..60`、`cycle 30-60`、`cycle 30~60`、`30 <= cycle <= 60`、`cycle 在 30 到 60 之间` | 闭区间 |
| `cycle 200+`、`cycle >= 200` | 至少 |
| `health < 8`、`health <= 6`、`food > 10`（也认 `≤ ≥`） | 数值比较 |
| `time_of_day == night`、`time_of_day = night`、`dimension != the_end`（也认 `≠`） | 相等 / 不等（数字按数值比，文字不分大小写） |
| `dimension in the_nether, nether`、`biome 属于 desert, badlands` | 取值之一 |
| `必须 dimension in the_nether, nether`（也认 `required` / `must` 前缀） | 状态里没有这个键时算不满足 |

对象形式（导入文件里）：`{"key": "cycle", "op": "between", "min": 30, "max": 60}`；`op` 可以是
`eq ne lt lte gt gte between in`，只写 `min` / `max` 时分别是「至少 / 不超过」，`value` 是列表时是 `in`；
加 `"required": true` 等同「必须」。一张卡最多 8 个条件。数值比较遇到非数字的状态值时算未判定。

### 排序

触发的卡按得分排序，取前 `max_items` 张：

- 关键词命中：每个命中（最多算 3 个）消息 3.0 分、`keywords` / `state` 2.5 分，再按词长加一点（每字 0.05，最多 8 字）；
  每个满足的限定条件 +1.0。
- 纯条件卡：每个满足的条件 2.8 分（实况状态直接触发的卡，如「血量低先撤」，排在单个场景关键词前面）。

词条：术语或别名出现在消息、`keywords` 或 `state` 值里即命中，按同样的来源权重排序，取前 `top_k` 个。

## 4. 工具 `wave_memory_game_knowledge`

说明：查攻略 / 机制用；想查自己以前玩过的事用记忆搜索。

| 参数 | 说明 |
|---|---|
| `game` | 可选。游戏标识、显示名或别名。省略时用当前请求带的游戏，其次是这个会话 / 这个 Bot 最近 30 分钟在玩的游戏；都没有就提示写上游戏并列出现有游戏库 |
| `query` | 必填。要查的事或名词，写关键词 |

最多返回 3 张卡、5 个词条，总长不超过 1200 字；只查启用的内容；找不到时明确说没有，并提示不要自行编造攻略。输出示例：

```text
[游戏攻略参考 · 我的世界｜查「苦力怕」]
【苦力怕 · 前期】
要点：苦力怕靠近后会发出嘶嘶声，停一下就爆炸；听到嘶声马上后退拉开距离，离开范围它就不炸了；用剑把它击退，或者用弓箭远程打；苦力怕怕猫和豹猫，会躲着它们走
注意：爆炸会炸坏方块，别让它靠近家；被闪电劈中的苦力怕爆炸威力更大；苦力怕白天不会被太阳烧死
出处：WaveMemory 我的世界起始包（模型起草，未审核）
……
词条：
- 苦力怕（Creeper、爬行者）：靠近玩家后会爆炸的绿色敌对生物，爆炸会破坏方块
（以上是攻略参考，不是你的亲身经历；想查自己以前玩过的事，用记忆搜索 wave_memory_search）
```

工具属于能力组 `game_knowledge`，`memory_only` 模式下不提供。Bot 配置了工具白名单（`tools_allow`）时需要把它加进去。

## 5. 通用导入 / 导出格式

管理台导入导出、仓库起始包（`assets/game_knowledge/*.json`）都用这个格式：

```json
{
  "format": "wavememory.game_knowledge",
  "format_version": 1,
  "game": {"id": "slay_the_spire", "name": "杀戮尖塔", "aliases": ["STS"], "description": ""},
  "cards": [
    {
      "key": "sts_elite_early",
      "title": "第一幕精英怎么打",
      "stage": "第一幕",
      "points": ["先确认手里有没有能打多段伤害的牌"],
      "pitfalls": ["血量不到一半时别硬进精英房"],
      "trigger": {"keywords": ["精英", "乐加维林"], "conditions": ["act == 1"]},
      "source": "手工整理",
      "source_url": "",
      "status": "draft",
      "note": "审核备注，不注入"
    }
  ],
  "terms": [
    {"key": "sts_vuln", "term": "易伤", "aliases": ["Vulnerable"], "explanation": "受到的攻击伤害增加 50%", "status": "draft"}
  ]
}
```

顶层：

| 字段 | 说明 |
|---|---|
| `format` | 固定 `wavememory.game_knowledge`（有 `cards` 或 `terms` 数组时也按通用格式识别） |
| `format_version` | 当前 1；更新的版本会提示「未知字段被忽略」 |
| `game` | 可选，只作提示：导入总是进当前选中的游戏库；文件里的游戏不同时会警告 |
| `pack` | 可选，起始包用：`{"id", "title", "description", "force_status": "draft"}`，`force_status` 强制所有条目的导入状态 |

知识卡：

| 字段 | 说明 |
|---|---|
| `key` | 可选，库内唯一键（`[A-Za-z0-9][A-Za-z0-9_.:-]*`，≤80）。不给时按标题生成 `c_<哈希>`，同标题重复导入落在同一个键上 |
| `title` | 必填，≤80 字 |
| `stage` | 阶段，自由填写（≤40 字），管理台可按阶段筛选 |
| `points`（别名 `tips`） | 要点，列表或按行的文字；每条 ≤300 字，最多 40 条 |
| `pitfalls`（别名 `cautions`） | 注意 / 坑，同上。`points` 与 `pitfalls` 至少要有一条 |
| `trigger.keywords` 或 `keywords` | 触发关键词，每个 ≤24 字，最多 30 个 |
| `trigger.conditions` 或 `conditions` | 条件，文字或对象（见第 3 节），最多 8 个 |
| `source` / `source_url` | 出处文字 / 链接（只收 http(s)） |
| `source_type` | `manual` `import` `tutorial` `starter_pack` `memory_migration` 或其他小写标识；不给时文件导入为 `import` |
| `status` | `draft` `active` `archived`，见下方「导入状态」 |
| `note` | 审核备注，不注入 |
| `meta` | 任意小对象（≤4000 字符），原样保存 |
| `version` | 导出时带上；导入时忽略 |

词条：`key`（不给时生成 `t_<哈希>`）、`term`（必填）、`aliases`、`explanation`（必填）、`related`、`source`、`source_url`、
`source_type`、`status`、`note`、`meta`。

**导入状态**（管理台「导入状态」）：`auto` 按文件里写的状态，没写的为草稿；`draft` 全部草稿（推荐）；`active` 全部启用。
文件带 `pack.force_status` 时以它为准。

**已有同键条目**（管理台「已有条目」）：

| 方式 | 效果 |
|---|---|
| `skip` | 跳过，不动已有条目（默认） |
| `update` | 用本次内容覆盖正文 |
| `merge` | 合并补全：列表字段本次内容在前、旧条目里本次没有的保留在后；条件按键合并（本次为准，其他键保留）；`meta` 合并；单值字段本次不为空时替换 |

三种方式都**不改已有条目的状态**（归档的仍是归档）；内容没有变化时记为「未变」。一次最多导入 2000 个条目。
导入前先预览：识别格式、统计新增 / 已有 / 不合法、列出警告与样例，确认后才写入。

导出：`GET /api/game-knowledge/games/<id>/export[?status=active]`，得到上面的格式（条件导出为对象形式），可再导入到另一个库或另一套 WaveMemory。

## 6. 现有资产

### 缺氧教程（B 站《缺氧从新开始》）

管理台选中 `oni` 库（没有就先建：标识 `oni`、显示名「缺氧」），依次导入：

1. `oni_tutorial_cards.json`（策略卡，自动识别）：每集一张卡，键 `oni_ep01`…，标题 `EP01 …`；`prefer` → 要点，`avoid` → 注意；
   `phase` → 阶段名（筑基开局 / 工业转型与自动化 / 中后期能源与工业 / 补天飞天与裂口毕业）；`approx_cycle_hint`
   `"0-30"` → `cycle 0..30`，`"200+"` → `cycle >= 200`；出处 `B站教程《缺氧从新开始》EPxx「标题」（BV…）` 加视频链接；
   关键词取集数标记（`EP08`、`第8集`）、触发词与标题里的名词；状态为**启用**。
2. `oni_timeline.json`（时间线）以「合并补全」导入：按集数对齐到同一张卡，补上阶段目标、分步做法、典型痛点。
   `extras`（非主轴补充）只有知识点编号、没有正文，不导入并提示。

资产里同一个 BV 号标在多集上时（可能错配），相关卡的备注会提示核对出处。这些周期条件按教程的节奏划分：
状态里带 `cycle` 时，不在区间内的卡不会注入（例如第 12 周期问制氧，标在 30–100 周期的制氧卡不会出现），需要时在管理台放宽或删掉条件。

### 记忆表里的旧知识点（`source='oni_lore'`）

以前直接导入记忆表、不属于任何 Bot 的字幕知识点，内容形如 `[EP06·标题 · #18/30 · mid] 正文`。管理台「记忆表里的旧知识点」
→ 检查 / 迁移为草稿：

- 每行一张草稿卡，键 `oni_lore_m<记忆 id>`，重复迁移只补新行；**原记忆行不删不改**；记忆表里没有这类行时直接跳过。
- 解析集数、标题、序号与阶段代码（`early/mid/late/space` → 阶段名与对应 `cycle` 条件）；标题取正文第一个冒号前的短句；
  正文按句切成要点；关键词只从标题取；出处复用同一集教程卡的视频链接。
- 解析不出前缀的行也会导入，并在备注里说明。字幕转写错字较多，备注提示校对后再启用。
- `memories.source` 没有索引，检查与迁移要扫整张记忆表，只在管理员点按钮时执行。

### 我的世界起始包（`assets/game_knowledge/minecraft_starter.json`）

Java 版生存前期常识（按服务端 1.20.6 起草），38 张卡、31 个词条，由模型离线起草，导入后**全部是草稿**。
管理台「起始包」一键导入（游戏库不存在时按包里的信息新建 `minecraft`）。数值条件假定 Cortico 在 `state` 里发送
`time_of_day`（`night` / `day`）、`health`（0–20）、`food`（0–20）、`dimension`；键名不同时在管理台改条件。

### 其他游戏

如杀戮尖塔：新建游戏库（如 `slay_the_spire`），手工录入或按第 5 节格式准备文件导入即可，不需要改代码。

## 7. 管理 API（`/api/game-knowledge`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/overview` | 游戏库列表（含计数）、起始包、可迁移的旧来源、触发规则说明 |
| POST | `/games` | 新建游戏库 `{item: {id, name, aliases, description, enabled}}` |
| GET / PUT | `/games/<id>` | 详情（含阶段统计）/ 修改（必须带读到的 `version`） |
| GET / POST | `/games/<id>/cards`、`/games/<id>/terms` | 列表（`status stage q source_type limit offset`）/ 新建 |
| GET / PUT | `/games/<id>/cards/<n>`、`/games/<id>/terms/<n>` | 详情 / 编辑（带 `version`，对不上返回 409 与 `current_version`） |
| POST | `/games/<id>/cards/status`、`/games/<id>/terms/status` | 批量启用 / 归档 / 退回草稿 `{ids, status, versions?}` |
| POST | `/games/<id>/import/preview`、`/games/<id>/import` | 导入预览 / 导入 `{data, format?, status?, on_existing?}` |
| GET | `/games/<id>/export` | 导出通用格式 |
| POST | `/games/<id>/trial` | 试匹配 `{message, keywords, state, include_drafts}`，不写数据 |
| POST | `/packs/<pack>/import` | 导入起始包（草稿） |
| POST | `/legacy/<source>/scan`、`/legacy/<source>/migrate` | 检查 / 迁移记忆表旧知识点 |
