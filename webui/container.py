"""ServiceContainer 单例 — 统一管理 WebUI 所需的所有服务依赖"""

from __future__ import annotations

from typing import Any, Optional


class ServiceContainer:
    """服务容器（单例）。

    所有 Blueprint 通过 get_container() 获取服务引用，
    避免 Blueprint 直接持有业务对象。
    """

    _instance: Optional["ServiceContainer"] = None

    def __new__(cls) -> "ServiceContainer":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        # ─── 核心服务 ───
        self.db: Any = None
        self.query_engine: Any = None
        self.embedding_service: Any = None
        self.memory_index: Any = None
        self.tag_index: Any = None
        self.cooccurrence: Any = None
        self.spike_router: Any = None
        self.residual_pyramid: Any = None
        self.epa: Any = None
        self.geodesic: Any = None
        self.tag_extractor: Any = None
        self.writer: Any = None
        self.write_gateway: Any = None
        self.durable_jobs: Any = None
        self.data_governance_jobs: Any = None
        self.scope_recovery_jobs: Any = None
        self.task_supervisor: Any = None
        self.jargon_service: Any = None
        self.plugin_config: dict = {}
        self.injection_channel_config: Any = None
        self.injection_channel_config_setter: Any = None
        self.injection_channels: list[Any] = []
        self.livingmemory_facade: Any = None
        self.livingmemory_facade_enabled: bool = False
        self.livingmemory_alias_tools_registered: bool = False
        self.detected_memory_plugins: list[dict[str, Any]] = []
        self.scope_options_source: Any = None
        self.request_scope_provider: Any = None
        self.soul_repository: Any = None
        self.relationship_calibration: Any = None
        self.fewshot_repository: Any = None
        # v6 Bot 注册表（services.bot_registry.BotRegistry）；Bot 管理页和 Runtime API 用它解析 Bot。
        self.bot_registry: Any = None
        # Runtime API 的注入入口（services.injection.runtime_prepare.RuntimeContextPreparer）。
        self.runtime_context_preparer: Any = None
        # 工具注册表（services.tool_registry.ToolRegistry）：Runtime API 与 9876 工具页共用。
        self.tool_registry: Any = None
        # Runtime 写入入口（WaveMemoryPlugin.ingest_observation）：与 AstrBot 消息钩子同一套处理。
        self.observation_ingestor: Any = None
        # 后台服务注册表（services.service_registry.ServiceRegistry）：9876「服务与扩展」页用。
        self.service_registry: Any = None
        # 扩展热重载入口（WaveMemoryPlugin.reload_extensions）。
        self.extension_reloader: Any = None
        # 入站消息接收/被拒统计（services.ingress_health.IngressHealth）。
        self.ingress_health: Any = None
        # 当前 TagWorker（可能被服务注册表重启，所以存取值函数）；标签覆盖面板用。
        self.tag_worker_getter: Any = None
        # 书设工作台（services.book_lore_workbench.BookLoreWorkbench）；书设没加载时为 None。
        self.book_lore_workbench: Any = None

        # ─── 认证 ───
        self.password: str = ""
        self.sessions: set = set()

    def initialize(
        self,
        *,
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
        password: str = "",
        plugin_config: dict = None,
        injection_channel_config=None,
        injection_channel_config_setter=None,
        livingmemory_facade=None,
        livingmemory_facade_enabled: bool | None = None,
        livingmemory_alias_tools_registered: bool = False,
        detected_memory_plugins: list[dict[str, Any]] | None = None,
        scope_options_source: Any = None,
        request_scope_provider: Any = None,
        soul_repository: Any = None,
        fewshot_repository: Any = None,
    ) -> None:
        """注入所有服务引用。"""
        self.db = db
        self.query_engine = query_engine
        self.embedding_service = embedding_service
        self.memory_index = memory_index
        self.tag_index = tag_index
        self.cooccurrence = cooccurrence
        self.spike_router = spike_router
        self.residual_pyramid = residual_pyramid
        self.epa = epa
        self.geodesic = geodesic
        self.tag_extractor = tag_extractor
        self.writer = writer
        self.write_gateway = write_gateway
        self.durable_jobs = durable_jobs
        self.data_governance_jobs = data_governance_jobs
        self.scope_recovery_jobs = scope_recovery_jobs
        self.task_supervisor = task_supervisor
        self.jargon_service = None
        self.password = password
        self.plugin_config = plugin_config or {}
        self.injection_channel_config = injection_channel_config
        self.injection_channel_config_setter = injection_channel_config_setter
        self.livingmemory_facade = livingmemory_facade
        self.livingmemory_facade_enabled = bool(livingmemory_facade if livingmemory_facade_enabled is None else livingmemory_facade_enabled)
        self.livingmemory_alias_tools_registered = bool(livingmemory_alias_tools_registered)
        self.detected_memory_plugins = list(detected_memory_plugins or [])
        self.scope_options_source = scope_options_source
        self.request_scope_provider = request_scope_provider
        self.soul_repository = soul_repository or getattr(db, "soul_repository", None)
        self.fewshot_repository = fewshot_repository or getattr(db, "fewshot_repository", None)

    @classmethod
    def reset(cls) -> None:
        """重置单例（用于热重载/测试）。"""
        cls._instance = None


def get_container() -> ServiceContainer:
    """获取全局 ServiceContainer 实例。"""
    return ServiceContainer()
