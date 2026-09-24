from __future__ import annotations

from pathlib import Path

import pytest

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


def test_classified_promote_is_hard_disabled():
    from scripts.apply_classified_scope_recovery import _promote
    from services.migrations_archive.scope_recovery_migration import ScopeRecoveryMigrationError

    with pytest.raises(ScopeRecoveryMigrationError) as exc:
        _promote(
            Path("."),
            Path("."),
            Path("./backup"),
            "promote-recovered-database",
        )
    assert "classified_fanout_promote_forbidden" in str(exc.value)
