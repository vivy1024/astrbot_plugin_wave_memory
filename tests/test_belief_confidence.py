"""Evidence-v1 置信度计算（旧版 scoped 信念的候选合并仍在用；信念 v2 的生命周期见 test_belief_v2_lifecycle）。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, debug=lambda *args, **kwargs: None)
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from services.belief_confidence import (
    ACTIVATION_MIN_CONFIDENCE,
    ACTIVATION_MIN_SUPPORT_WINDOWS,
    POLICY_VERSION,
    calculate_confidence,
)


def test_confidence_uses_independent_windows_and_challenge_evidence():
    first = {
        "polarity": "support", "window_key": "window-1", "memory_ids": [1],
        "participants": ["u1"], "window_started_at": 1_000.0, "window_ended_at": 1_000.0,
    }
    one = calculate_confidence([first], now=1_000.0)
    assert one["policy_version"] == POLICY_VERSION
    assert one["summary"]["support_windows"] == 1
    assert one["components"]["confidence"] < ACTIVATION_MIN_CONFIDENCE
    assert not one["activation_eligible"]

    second = {
        "polarity": "support", "window_key": "window-2", "memory_ids": [2],
        "participants": ["u2"], "window_started_at": 1_000.0 + 86_400, "window_ended_at": 1_000.0 + 86_400,
    }
    two = calculate_confidence([first, second], now=1_000.0 + 86_400)
    assert two["summary"]["support_windows"] >= ACTIVATION_MIN_SUPPORT_WINDOWS
    assert two["components"]["confidence"] >= ACTIVATION_MIN_CONFIDENCE
    assert two["activation_eligible"]

    repeated = calculate_confidence([first, second, second], now=1_000.0 + 86_400)
    assert repeated["summary"]["support_windows"] == two["summary"]["support_windows"]
    assert repeated["summary"]["support_messages"] == two["summary"]["support_messages"]
    assert repeated["components"]["confidence"] == two["components"]["confidence"]

    challenge = {
        "polarity": "challenge", "window_key": "window-3", "memory_ids": [3],
        "participants": ["u3"], "window_started_at": 1_000.0 + 172_800, "window_ended_at": 1_000.0 + 172_800,
    }
    challenged = calculate_confidence([first, second, challenge], now=1_000.0 + 172_800)
    assert challenged["summary"]["challenge_windows"] == 1
    assert challenged["components"]["consistency"] < two["components"]["consistency"]
    assert challenged["components"]["confidence"] < two["components"]["confidence"]


