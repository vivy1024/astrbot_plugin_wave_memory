from __future__ import annotations

from services.migrations_archive.approved_scope_recovery import (
    APPROVED_SCOPE_RECOVERY_POLICY,
    APPROVED_SCOPE_RECOVERY_RULE_VERSION,
    FORBIDDEN_FANOUT_RULE_VERSIONS,
)


def test_recovery_policy_is_no_fanout_v4():
    assert APPROVED_SCOPE_RECOVERY_RULE_VERSION.endswith("/4")
    assert "no-fanout" in APPROVED_SCOPE_RECOVERY_POLICY
    assert "classified-scope-recovery/1" in FORBIDDEN_FANOUT_RULE_VERSIONS
    assert "approved-group-scope-recovery/3" in FORBIDDEN_FANOUT_RULE_VERSIONS

