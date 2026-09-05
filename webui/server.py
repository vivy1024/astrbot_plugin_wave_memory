"""WebUI 服务器 — Hypercorn 守护线程模式，SO_REUSEADDR 端口可靠释放"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from typing import Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地单测未安装 AstrBot SDK 时的轻量兜底
    class _Logger:
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def debug(self, *args, **kwargs): pass
    logger = _Logger()


class _SecureConfig:
    """仅用于创建带 SO_REUSEADDR 的 socket 并传给 hypercorn。"""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def create_socket(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if sys.platform != "win32" and hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
        sock.set_inheritable(False)
        sock.bind((self.host, self.port))
        sock.listen(128)
        return sock


class Server:
    """WebUI 服务器（守护线程 + Hypercorn）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 7890, password: str = ""):
        self.host = host
        self.port = port
        self.password = password or ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self.app = None

    # ─── 公开接口 ───

    async def start(self) -> None:
        """启动 Hypercorn 守护线程。"""
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.password:
            logger.warning("[WaveMemory WebUI] 注意：正在以 0.0.0.0 免密模式启动 WebUI（适用于 Docker 内部端口映射/开发环境）。若部署于公网，请务必在配置中设置访问密码。")
        if self._thread and self._thread.is_alive():
            logger.info("[WaveMemory WebUI] 服务器已在运行中")
            return

        # 热重载时等待旧端口释放（最多 5s）
        if self._is_port_listening():
            logger.info(f"[WaveMemory WebUI] 端口 {self.port} 被占用，等待释放...")
            for _ in range(10):
                await asyncio.sleep(0.5)
                if not self._is_port_listening():
                    break
            else:
                logger.warning(f"[WaveMemory WebUI] 端口 {self.port} 仍被占用，跳过 WebUI 启动")
                return

        from .app import create_app
        from .container import get_container

        container = get_container()
        self.app = create_app(
            scope_options_source=container.scope_options_source,
            request_scope_provider=container.request_scope_provider,
        )

        self._thread = threading.Thread(
            target=self._run_thread,
            daemon=True,
            name="WaveMemory_WebUI",
        )
        self._thread.start()

        # 快速检查端口（最多 2s），不阻塞插件加载
        for _ in range(4):
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                # AstrBot 加载超时取消 — 线程已启动，不影响功能
                logger.info("[WaveMemory WebUI] 启动中（后台就绪）")
                return
            if self._is_port_listening():
                logger.info(
                    f"[WaveMemory WebUI] 服务启动成功: http://{self.host}:{self.port}"
                )
                return

        logger.warning("[WaveMemory WebUI] 线程已启动但端口无响应")

    async def stop(self) -> None:
        """优雅关闭服务器。"""
        if self._loop and self._shutdown_event:
            try:
                self._loop.call_soon_threadsafe(self._shutdown_event.set)
            except Exception:
                pass

        if self._thread:
            loop = asyncio.get_event_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, self._thread.join, 5.0),
                    timeout=6.0,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._thread = None

        self._loop = None
        self._shutdown_event = None
        logger.info("[WaveMemory WebUI] 服务器已停止")

    # ─── 内部方法 ───

    def _run_thread(self) -> None:
        """在独立线程中运行 Hypercorn。"""
        try:
            import hypercorn.asyncio
            from hypercorn.config import Config as HypercornConfig
        except ImportError:
            logger.error("[WaveMemory WebUI] hypercorn 未安装，WebUI 无法启动")
            return

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._shutdown_event = asyncio.Event()

            config = HypercornConfig()
            config.bind = [f"{self.host}:{self.port}"]
            config.accesslog = None
            config.errorlog = None
            config.loglevel = "WARNING"

            loop.run_until_complete(
                hypercorn.asyncio.serve(
                    self.app,
                    config,
                    shutdown_trigger=self._shutdown_event.wait,
                )
            )
            loop.close()
        except Exception as e:
            logger.error(f"[WaveMemory WebUI] 服务器线程异常: {e}")

    def _is_port_listening(self) -> bool:
        """检查端口是否已在监听。"""
        try:
            check_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                return s.connect_ex((check_host, self.port)) == 0
        except Exception:
            return False
