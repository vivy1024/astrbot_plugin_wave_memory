"""工具名与合并映射：唯一来源在 ``domain/tool_names.py``，这里只转发，保持 ``tools.names`` 可用。"""

from __future__ import annotations

try:
    from ..domain.tool_names import *  # noqa: F401,F403
    from ..domain.tool_names import LEGACY_TO_MERGED, MERGED_TOOLS, merged_for, normalize_tool_ref  # noqa: F401
except ImportError:  # pragma: no cover - 顶层导入
    from domain.tool_names import *  # noqa: F401,F403
    from domain.tool_names import LEGACY_TO_MERGED, MERGED_TOOLS, merged_for, normalize_tool_ref  # noqa: F401
