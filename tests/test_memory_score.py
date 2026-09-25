"""打分：默认重要度限幅加权、访问加成封顶；参数可还原旧公式；检索实验室参数有上下限。"""

from __future__ import annotations

import math

import pytest

from engine.query_engine import QueryOptions, memory_score


def test_default_damps_importance_and_access():
    _, imp_factor, boost = memory_score(0.64, 3.0, 1.0, 1449)
    assert imp_factor == pytest.approx(1.25) and boost == pytest.approx(1.1)
    # 修复后被召回上千次的空消息重要度回到 1.0：相似度高 0.1 的普通记忆排在它前面
    hot, *_ = memory_score(0.60, 1.0, 1.0, 1449)
    cold, *_ = memory_score(0.70, 1.0, 1.0, 0)
    assert cold > hot


def test_legacy_formula_is_recoverable_via_params():
    score, imp_factor, boost = memory_score(0.64, 3.0, 0.9, 100, importance_weight=1.0, access_boost_cap=3.0)
    legacy_boost = 1.0 + math.log2(101) * 0.15
    assert score == pytest.approx(0.64 * 3.0 * 0.9 * legacy_boost)
    assert imp_factor == 3.0 and boost == pytest.approx(legacy_boost)


def test_low_importance_still_penalised_but_bounded():
    _, factor, _ = memory_score(0.5, 0.1, 1.0, 0)
    assert factor == pytest.approx(1 + 0.25 * (0.3 - 1))


def test_query_lab_params_are_clamped():
    assert QueryOptions(params={"importance_weight": 2.0}).params["importance_weight"] == 1.0
    assert QueryOptions(params={"access_boost_cap": 0.5}).params["access_boost_cap"] == 1.0
    assert QueryOptions(params={"importance_weight": 0.5, "access_boost_cap": 1.3}).params["importance_weight"] == 0.5


def test_touch_importance_repair_is_reversible_and_once(tmp_path):
    import sqlite3

    from engine.db.connection import ConnectionManager
    from engine.db.migrations.importance_touch_repair import BACKUP_TABLE, repair_touch_inflated_importance

    path = str(tmp_path / "wm.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, importance REAL, access_count INTEGER)")
    conn.executemany("INSERT INTO memories VALUES (?, ?, ?)", [
        (1, 3.0, 1449),   # 被刷满：回到 1.0
        (2, 1.3, 30),     # 1.0 + 30 次召回
        (3, 1.5, 0),      # 主动记住，没被召回：不动
        (4, 1.25, 5),     # 做梦强化 5 次（每次 +0.05）：保留 0.04×5
        (5, 0.3, 50),     # 扣完为负：下限 0.1
    ])
    conn.commit()
    conn.close()
    cm = ConnectionManager(path)
    assert repair_touch_inflated_importance(cm) == 4
    assert repair_touch_inflated_importance(cm) == 0  # 只执行一次
    rows = dict(cm.conn.execute("SELECT id, importance FROM memories").fetchall())
    assert rows[1] == 1.0 and rows[2] == pytest.approx(1.0) and rows[3] == 1.5
    assert rows[4] == pytest.approx(1.2) and rows[5] == pytest.approx(0.1)
    backup = dict(cm.conn.execute(f"SELECT id, importance FROM {BACKUP_TABLE}").fetchall())
    assert backup == {1: 3.0, 2: 1.3, 4: 1.25, 5: 0.3}
