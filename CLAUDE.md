# Wave Memory Plugin — 开发规则

## 版本号与发版

当前版本：**v0.4.0**

### 版本号规则（SemVer）

```
v{major}.{minor}.{patch}
```

- **major**：破坏性变更（API 不兼容、数据库 schema 不兼容迁移）
- **minor**：新功能（向后兼容）
- **patch**：bug 修复、性能优化

### 发版检查清单

1. 确认所有改动已 push 到 master
2. 更新 `metadata.yaml` 中的 `version` 字段
3. 更新 `CHANGELOG.md`（新版本写在最前面）
4. 提交：`git commit -m "chore: bump version to vX.Y.Z"`
5. 打 tag：`git tag vX.Y.Z`
6. 推送 tag：`git push origin vX.Y.Z`
7. 创建 GitHub Release（标题 = tag，body = CHANGELOG 对应段落）

### 何时发版

| 场景 | 动作 |
|------|------|
| 完成一个功能阶段（spec 全部 P 完成） | minor 版本 |
| 修了 bug 但没新功能 | patch 版本 |
| 日常开发中间态 | 直接 push，不打 tag |
| 需要 AstrBot 插件市场更新 | 必须 tag + release |

### 版本号变更位置

- `metadata.yaml` → `version: vX.Y.Z`
- `CHANGELOG.md` → 新增对应版本段落

---

## 项目结构

```
├── main.py              # 插件入口
├── metadata.yaml        # AstrBot 插件元数据
├── CHANGELOG.md         # 版本变更记录
├── engine/              # 核心算法（EPA、共现、残差、金字塔）
├── services/            # 业务服务（生命周期、整合、Tag提取、审计）
├── webui/               # WebUI 服务 + 静态页面
│   ├── __init__.py      # FastAPI 路由
│   └── static/          # HTML 页面（explore/maintain）
└── tools/               # AstrBot function tools
```

## 开发约束

- Python 3.10+，依赖随 AstrBot 环境
- SQLite 单文件数据库，schema 变更用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` 兼容
- WebUI 独立端口 9876，不依赖 AstrBot Pages
- LLM 调用通过 `self.context.get_using_provider()` 获取 provider
- Embedding 通过 `self.context.get_using_provider()` 获取，不可用时 graceful fallback
