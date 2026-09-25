"""消息入库健康：被拒过多时报警并指出未绑定的账号；偶发被拒不报警；窗口外的旧记录过期。"""

from __future__ import annotations

from services.ingress_health import IngressHealth


class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_mass_rejection_alarms_with_unknown_account():
    clock = _Clock()
    health = IngressHealth(clock=clock)
    for _ in range(8):
        health.record_rejected("unknown_bot_self_id", self_id="20000001")
    health.record_accepted()
    snap = health.snapshot()
    assert snap["status"] == "error"
    assert snap["unknown_self_ids"] == ["20000001"]
    assert snap["reasons"] == {"unknown_bot_self_id": 8}
    text = health.summary_text()
    assert "20000001" in text and "Bot 管理" in text


def test_occasional_rejection_is_ok():
    health = IngressHealth(clock=_Clock())
    for _ in range(40):
        health.record_accepted()
    for _ in range(6):
        health.record_rejected("unknown_bot_self_id", self_id="1")
    assert health.snapshot()["status"] == "ok"  # 6 / 46 不过半
    assert "被拒 6 条" in health.summary_text()


def test_old_records_expire_but_totals_remain():
    clock = _Clock()
    health = IngressHealth(clock=clock)
    for _ in range(10):
        health.record_rejected("unknown_bot_self_id", self_id="9")
    assert health.snapshot()["status"] == "error"
    clock.now += 11 * 60
    health.record_accepted()
    snap = health.snapshot()
    assert snap["status"] == "ok" and snap["rejected"] == 0 and snap["unknown_self_ids"] == []
    assert snap["total_rejected"] == 10 and snap["total_accepted"] == 1
