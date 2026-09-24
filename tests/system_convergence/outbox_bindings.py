"""Thin adapter for the frozen production R4 test composition port.

This module only imports the approved port and forwards calls. It contains no SQL, retry, replay,
deduplication, checkpoint, version-filtering, or canonical mutation implementation.
"""

from __future__ import annotations

from typing import Any, Mapping

from tests.system_convergence.contracts import require_module


class _OutboxTestPortBinding:
    def __init__(self, production_port: Any):
        self._production_port = production_port

    def create_runtime(
        self,
        database_path: str,
        *,
        consumers: Mapping[str, Any],
        clock: Any,
    ) -> Any:
        return self._production_port.create_runtime(
            database_path,
            consumers=consumers,
            clock=clock,
        )

    def make_probe_command(self, **kwargs: Any) -> Any:
        return self._production_port.make_probe_command(**kwargs)


def bind_outbox_test_port(reason: str) -> _OutboxTestPortBinding:
    production_port = require_module(
        "services.migrations_archive.system_convergence_test_port",
        ("create_runtime", "make_probe_command"),
        reason,
    )
    return _OutboxTestPortBinding(production_port)
