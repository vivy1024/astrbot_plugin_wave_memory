"""已执行完毕的一次性迁移与恢复作业（v6 从服务层移出，运行时不导入）。

- approved_scope_recovery / approved_scope_recovery_indexes：批准后的 Scope 恢复与索引重建
- scope_recovery_migration：legacy fanout 数据的 Scope 恢复迁移
- legacy_relationship_migration：旧关系数据迁到 scoped_soul_relationships
- system_convergence_test_port：写入口收敛期的测试端口

只供 scripts/ 下的离线脚本和测试使用。插件运行时的 Scope 恢复维护任务仍在 services/scope_recovery.py。
"""
