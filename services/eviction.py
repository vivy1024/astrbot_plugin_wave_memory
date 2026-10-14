"""EvictionService — 定期清理只追加的写入流水账。

记忆只增不删：记忆本身不删除，也不按时间淘汰。噪声记忆在进入时就已被标记，
召回与注入都不会取它，所以这里不做任何删除。

噪声删除已移除（v5.1.2）：它唯一的作用是省磁盘，却会真的丢掉消息；
`noise_ttl_days` 也不再存在。`chat` 冷层降级由热索引策略在有界重建里完成，
不改写 canonical Memory 行。
"""


from __future__ import annotations

import asyncio
from typing import Optional

from astrbot.api import logger

from ..engine.database import WaveMemoryDB
from ..engine.vector_index import VectorIndex


class EvictionService:
    """维护服务 — 只清理写入流水账（outbox 事件/投递/操作日志/投影水位）。"""

    def __init__(
        self,
        db: WaveMemoryDB,
        memory_index: VectorIndex,
        eviction_interval_hours: float = 6.0,
        write_gateway=None,
    ):
        self.db = db
        self.memory_index = memory_index
        self.interval = eviction_interval_hours * 3600
        self.write_gateway = write_gateway
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._stats: dict = {}
        # 写操作日志与 outbox 事件每天约 30 MB，投递完成后只留 7 天用于排查与幂等重放
        self.outbox_retention_days = 7.0

    def start(self, supervisor=None):
        self._running = True
        if supervisor is None:
            self._task = asyncio.create_task(self._loop())
        else:
            self._task = supervisor.start(
                "wave-memory:eviction", self._loop(), owner="eviction"
            )
        logger.info("[WaveMemory] EvictionService started (outbox retention=%dd)",
                    int(self.outbox_retention_days))

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        """定时清理循环。首次延迟 10 分钟。"""
        await asyncio.sleep(600)
        while self._running:
            try:
                await self.prune_outbox_history()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[EvictionService] Error: {e}")
            await asyncio.sleep(self.interval)

    async def prune_outbox_history(self) -> dict | None:
        """清理 7 天前已投递完成的 outbox 历史与投影水位（写入口的只追加日志，会无限增长）。"""
        coordinator = getattr(self.write_gateway, "coordinator", None)
        prune = getattr(coordinator, "prune_outbox_history", None)
        if not callable(prune):
            return None
        result = await prune(retention_days=self.outbox_retention_days)
        if any(result.get(key) for key in ("events", "deliveries", "operations", "projections")):
            logger.info("[EvictionService] outbox 历史清理: %s", result)
        self._stats["outbox_pruned"] = self._stats.get("outbox_pruned", 0) + int(result.get("events", 0))
        return result

    @property
    def stats(self) -> dict:
        return dict(self._stats)
