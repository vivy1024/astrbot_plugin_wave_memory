"""Wave Memory WebUI — Quart + Blueprint + Hypercorn 守护线程架构 (v0.9)

对外接口保持兼容 main.py:
    from .webui import WaveMemoryWebUI
    webui = WaveMemoryWebUI(db=..., ..., port=...)
    await webui.start()
    await webui.stop()
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地单测未安装 AstrBot SDK 时的轻量兜底
    class _Logger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass
    logger = _Logger()

from .container import ServiceContainer, get_container
try:
    from . import server as server
    Server = server.Server
except Exception:  # pragma: no cover - 仅允许蓝图 helper 在缺少 WebUI 运行依赖时被导入测试
    server = None
    Server = None


class WaveMemoryWebUI:
    """Wave Memory WebUI 入口类 — 兼容 main.py 原有调用方式。"""

    def __init__(
        self,
        db,
        query_engine,
        embedding_service,
        memory_index,
        tag_index,
        cooccurrence,
        spike_router=None,
        residual_pyramid=None,
        epa=None,
        geodesic=None,
        tag_extractor=None,
        writer=None,
        write_gateway=None,
        durable_jobs=None,
        data_governance_jobs=None,
        scope_recovery_jobs=None,
        task_supervisor=None,
        host: str = "0.0.0.0",
        port: int = 7890,
        password: str = "",
        plugin_config: dict = None,
        injection_channel_config=None,
        injection_channel_config_setter=None,
        livingmemory_facade=None,
        livingmemory_facade_enabled: bool | None = None,
        livingmemory_alias_tools_registered: bool = False,
        detected_memory_plugins: list[dict[str, Any]] | None = None,
        bot_registry: dict[str, Any] | None = None,
        group_name_resolver=None,
        bot_registry_service=None,
    ):
        # 生产 WebUI 只从真实 registry/数据库组合 options，并且只接受请求显式 Scope。
        from .scope_options import ExplicitRequestScopeProvider, RuntimeScopeOptionsSource

        scope_options_source = RuntimeScopeOptionsSource(
            db=db,
            bot_registry=bot_registry,
            channel_config=injection_channel_config,
            group_name_resolver=group_name_resolver,
        )
        request_scope_provider = ExplicitRequestScopeProvider(bot_registry=bot_registry)

        # 注入所有服务到全局容器
        container = get_container()
        try:
            from ..services.relationship_calibration import RelationshipCalibrationGateway
            calibration = RelationshipCalibrationGateway(write_gateway, db.soul_repository) if write_gateway is not None else None
        except (ImportError, TypeError, ValueError):
            calibration = None

        container.initialize(
            db=db,
            query_engine=query_engine,
            embedding_service=embedding_service,
            memory_index=memory_index,
            tag_index=tag_index,
            cooccurrence=cooccurrence,
            spike_router=spike_router,
            residual_pyramid=residual_pyramid,
            epa=epa,
            geodesic=geodesic,
            tag_extractor=tag_extractor,
            writer=writer,
            write_gateway=write_gateway,
            durable_jobs=durable_jobs,
            data_governance_jobs=data_governance_jobs,
            scope_recovery_jobs=scope_recovery_jobs,
            task_supervisor=task_supervisor,
            password=password,
            plugin_config=plugin_config,
            injection_channel_config=injection_channel_config,
            injection_channel_config_setter=injection_channel_config_setter,
            livingmemory_facade=livingmemory_facade,
            livingmemory_facade_enabled=livingmemory_facade_enabled,
            livingmemory_alias_tools_registered=livingmemory_alias_tools_registered,
            detected_memory_plugins=detected_memory_plugins,
            scope_options_source=scope_options_source,
            request_scope_provider=request_scope_provider,
        )
        container.relationship_calibration = calibration
        container.bot_registry = bot_registry_service

        self._task_supervisor = task_supervisor

        # 创建服务器实例
        if Server is None:
            raise RuntimeError("WebUI runtime dependencies are unavailable")
        self._server = Server(host=host, port=port, password=password or "")
        self._kg_warmup_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动 WebUI 服务器，并后台预热 KG 星图缓存。"""
        await self._server.start()
        if self._task_supervisor is None:
            self._kg_warmup_task = asyncio.create_task(self._async_kg_cache_warmup())
        else:
            self._kg_warmup_task = self._task_supervisor.start(
                "wave-memory:webui-kg-warmup",
                self._async_kg_cache_warmup(),
                owner="webui",
            )
        self._kg_warmup_task.add_done_callback(self._log_kg_warmup_result)

    async def _async_kg_cache_warmup(self) -> dict:
        """后台预热默认 KG 全量图缓存；不阻塞 AstrBot 启动。"""
        logger.info("[WaveMemory] KG cache warmup started")
        from .blueprints.kg import warmup_kg_cache

        result = await asyncio.to_thread(warmup_kg_cache, "facts")
        logger.info(
            f"[WaveMemory] KG cache warmup SUCCESS: layers={result.get('layers')} "
            f"edges={result.get('edges')} elapsed={result.get('elapsed_ms')}ms"
        )
        return result

    @staticmethod
    def _log_kg_warmup_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("[WaveMemory] KG cache warmup canceled")
        except Exception as e:
            logger.warning(f"[WaveMemory] KG cache warmup failed: {e}")

    async def stop(self) -> None:
        """停止 WebUI 服务器，并取消尚未完成的后台预热任务。"""
        if self._kg_warmup_task and not self._kg_warmup_task.done():
            self._kg_warmup_task.cancel()
            try:
                await self._kg_warmup_task
            except asyncio.CancelledError:
                pass
        self._kg_warmup_task = None
        await self._server.stop()
        # 重置容器以便热重载
        ServiceContainer.reset()
