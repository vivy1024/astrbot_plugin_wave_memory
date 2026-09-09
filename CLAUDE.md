# Wave Memory Plugin — 开发规则

## ⚠️ 最重要的教训（必读）

### AstrBot 配置机制陷阱

AstrBot 插件配置生命周期：
1. 首次安装：从 `_conf_schema.json` 的 default 值生成 config.json
2. 用户打开配置页：读取 config.json 填充表单
3. 用户点保存：**把表单所有字段全部序列化写入 config.json**
4. 插件重启：从 config.json 读取，`.get("key", default)` 的 default 不生效

**致命后果**：
- 新增 schema 字段后旧 config 没有该字段 → AstrBot 保存时可能填 False
- `query_cfg.get("enable_xxx", True)` 在 config 显式写了 False 时默认值不生效
- **所有 bool 开关的 default=true 在升级场景下不可靠**

**防御规则**：
1. 关键功能开关必须在启动时 WARNING 检测
2. 新增 bool 字段时代码加 `if xxx is None: xxx = True`
3. 升级版本时必须考虑旧 config 兼容
4. CHANGELOG 中新增配置项必须注明"升级用户需检查配置"

### 不要信任 hasattr 检查 None

```python
# ❌ self.xxx = None 时 hasattr 仍 True
# ✅ 用 getattr(self, 'xxx', None)
```

### nonlocal 声明

闭包内赋值外层变量必须 nonlocal，否则创建局部变量。

### 方法签名一致性

改底层方法签名时 grep 所有调用点确认匹配。

<!-- SPLICE_1 -->

## AstrBot 框架关键知识

### 两个 WebUI 的区别

- AstrBot 6185：静态配置，控制模块开关/Provider/端口，重启生效
- WaveMemory 9876：热参数，控制已加载模块的算法调参，实时生效

### user_profiles 表

```sql
UNIQUE(user_id, group_id, bot_id)
-- bot_id 是 BotProfile.db_id（"yushu"），不是 QQ 号！
```

### 定时服务

有 `.start()` 的服务构造后必须调用：lifecycle/dream/study/eviction。
`consolidation` 对象可保留，但 v5 默认不 `start()` 后台盲抽循环。

### 好感度双系统（v5 收口）

- AffinityEngine flush 只做触达 / 时间衰减，**不把关键词增量写入正式五维**
- 正式好感由 `wave_memory_record_social_impression` 在系统算出的有限范围内自选
- metadata 仍增量合并（印象时间线 / tags / ledger 不被 flush 覆盖）

---

## 升级兼容性检查（每次发版必做）

1. 新增 schema 字段？→ 代码中 None 守卫 + CHANGELOG 注明
2. 改了 DB 表？→ ALTER TABLE 迁移 + IF NOT EXISTS
3. 改了方法签名？→ grep 调用点
4. 改了 config key？→ 旧 key 迁移

---

## 历史教训

| 日期 | 事件 | 教训 |
|------|------|------|
| 05-29 | GitHub 回退覆盖 | 必须 push |
| 06-14 | release notes 遗漏 | git log 检查 |
| 06-15 | enable_auto_inject=False | 配置升级兼容 |
| 06-15 | lifecycle.start() 未调用 | 构造后必须 start |
| 06-15 | bot_db_ids 用 name.lower() | QQ号≠db_id |
| 06-15 | nonlocal 漏写 | 闭包必须声明 |
| 06-15 | flush 覆盖 MetaThinking | metadata 增量合并 |
| 06-15 | person_registry 无写入 | 表不会自己长数据 |
| 06-15 | hasattr 不检查 None | 用 getattr |
