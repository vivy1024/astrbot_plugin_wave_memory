"""ConnectionManager — 读写分离连接 + WAL + busy_timeout

SQLite WAL 模式下 readers 不阻塞 writers，writers 不阻塞 readers。
利用两个独立连接实现真正的读写并发：
- _write_conn：写操作专用，写锁保护序列化
- _read_conn：读操作专用，无锁，随时可读；只给创建它的线程（事件循环）用，
  其他线程各自懒建一个只读连接（sqlite3 的语句缓存按连接共享，同一连接被多个线程
  同时执行会串位：返回 None / 别的行、报 ``bad parameter or other API misuse``）
- conn 属性：兼容旧代码 db.conn.execute()，自动判断读/写路由
"""

import threading
import sqlite3
import os
import re
import sys
from pathlib import Path
from contextlib import contextmanager
from typing import Optional


# 判断 SQL 是否为写操作
_WRITE_SQL_PATTERN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|ATTACH|DETACH|REINDEX|VACUUM|PRAGMA\s+(?!table_info|database_list|index_list|index_info))",
    re.IGNORECASE,
)


def _close_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


class RawWriteForbiddenError(RuntimeError):
    """事务由其他线程持有时，拒绝绕过事务边界的原始写操作。"""

    reason_code = "raw_write_forbidden"


class _ConnProxy:
    """代理对象，让 db.conn.execute() 自动路由到读/写连接。"""

    def __init__(self, manager: "ConnectionManager"):
        self._mgr = manager

    def execute(self, sql, params=None):
        if _WRITE_SQL_PATTERN.match(sql):
            self._mgr.assert_raw_write_allowed()
            return self._mgr.execute_write(sql, params)
        return self._mgr.execute_read(sql, params)

    def executemany(self, sql, params_seq):
        self._mgr.assert_raw_write_allowed()
        return self._mgr.executemany_write(sql, params_seq)

    def commit(self):
        self._mgr.assert_raw_write_allowed()
        return self._mgr.commit()

    def rollback(self):
        self._mgr.assert_raw_write_allowed()
        return self._mgr.rollback()

    @property
    def in_transaction(self):
        return self._mgr.in_transaction

    def write_transaction(self):
        return self._mgr.write_transaction()

    def migration_transaction(self):
        return self._mgr.migration_transaction()

    def executescript(self, sql):
        self._mgr.assert_raw_write_allowed()
        return self._mgr.executescript(sql)


class ConnectionManager:
    """读写分离的 SQLite 连接管理器。

    - 写连接：单独一个，所有写操作通过写锁序列化
    - 读连接：单独一个，不加锁，WAL 保证快照隔离
    - conn 代理：兼容旧代码 db.conn.execute()，自动路由
    """

    def __init__(self, db_path: str):
        self._write_lock = threading.RLock()
        self._transaction_owner_thread_id: Optional[int] = None
        self.db_path = db_path
        self._shared_memory_connection = db_path == ":memory:"
        parent = os.path.dirname(os.path.abspath(db_path))
        if not self._shared_memory_connection and parent:
            os.makedirs(parent, exist_ok=True)

        # 写连接负责 bootstrap/migration 和 legacy allowlist mutation。
        self._write_conn = self._create_write_connection()
        # 文件数据库的读连接使用 mode=ro + query_only；内存测试库只能共享连接。
        self._read_conn = (
            self._write_conn if self._shared_memory_connection else self._create_read_connection()
        )

        self._owner_thread_id = threading.get_ident()
        self._thread_local = threading.local()
        self._thread_read_conns: dict[int, tuple[threading.Thread, sqlite3.Connection]] = {}
        self._thread_read_lock = threading.Lock()
        self._read_generation = 0

        # 兼容旧代码 db.conn.execute()
        self.conn = _ConnProxy(self)

        self.memory_index = None  # 可选，删除时同步清索引

    def _create_write_connection(self) -> sqlite3.Connection:
        """创建 bootstrap/legacy allowlist 使用的写连接。"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA cache_size=-8192")
        return conn

    def _create_read_connection(self, cache_kib: int = 8192) -> sqlite3.Connection:
        """打开不会建库、不会切 WAL 的 query-only 文件读连接。"""
        uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(f"PRAGMA cache_size=-{int(cache_kib)}")
        return conn

    def _reader(self) -> sqlite3.Connection:
        """当前线程的读连接：创建线程用 ``_read_conn``，其他线程各用一个自己的。"""
        if self._shared_memory_connection or threading.get_ident() == self._owner_thread_id:
            return self._read_conn
        local = self._thread_local
        conn = getattr(local, "conn", None)
        if conn is not None and getattr(local, "generation", -1) == self._read_generation:
            return conn
        conn = self._create_read_connection(cache_kib=2048)
        local.conn, local.generation = conn, self._read_generation
        with self._thread_read_lock:
            # 顺带关掉已退出线程留下的连接（线程池的线程常驻，一次性线程才会留下）
            for ident, (thread, stale) in list(self._thread_read_conns.items()):
                if not thread.is_alive():
                    self._thread_read_conns.pop(ident, None)
                    _close_quietly(stale)
            previous = self._thread_read_conns.get(threading.get_ident())
            if previous is not None and previous[1] is not conn:
                _close_quietly(previous[1])
            self._thread_read_conns[threading.get_ident()] = (threading.current_thread(), conn)
        return conn

    def _close_thread_readers(self) -> None:
        with self._thread_read_lock:
            self._read_generation += 1
            for _thread, conn in self._thread_read_conns.values():
                _close_quietly(conn)
            self._thread_read_conns.clear()

    def assert_raw_write_allowed(self) -> None:
        """原始代理不得等待并越过另一个线程正在持有的事务。"""
        owner = self._transaction_owner_thread_id
        if owner is not None and owner != threading.get_ident():
            raise RawWriteForbiddenError(
                "raw proxy write is forbidden while another thread owns the transaction"
            )

    def execute_write(self, sql, params=None):
        """写操作 — 通过写锁序列化，走写连接。"""
        with self._write_lock:
            return self._write_conn.execute(sql, params or ())

    def executemany_write(self, sql, params_seq):
        """批量写操作。"""
        with self._write_lock:
            return self._write_conn.executemany(sql, params_seq)

    def executescript(self, sql):
        """执行多条 SQL 脚本（写操作）。"""
        with self._write_lock:
            return self._write_conn.executescript(sql)

    def commit(self):
        """提交写连接事务。"""
        with self._write_lock:
            self._write_conn.commit()

    def rollback(self):
        """回滚写连接事务。"""
        with self._write_lock:
            self._write_conn.rollback()

    @property
    def in_transaction(self) -> bool:
        return bool(self._write_conn.in_transaction)

    @staticmethod
    def _rollback_preserving_error(connection: sqlite3.Connection) -> None:
        """尽力回滚；清理失败不得覆盖正在传播的业务/迁移异常。"""
        active_error = sys.exc_info()[0] is not None
        try:
            connection.rollback()
        except BaseException:
            if not active_error:
                raise

    @contextmanager
    def write_transaction(self):
        """持有写锁覆盖 BEGIN IMMEDIATE 到 COMMIT/ROLLBACK 的完整事务。"""
        with self._write_lock:
            if self._write_conn.in_transaction:
                raise RuntimeError("connection already has an active transaction")
            self._write_conn.execute("BEGIN IMMEDIATE")
            self._transaction_owner_thread_id = threading.get_ident()
            try:
                try:
                    yield self._write_conn
                except BaseException:
                    self._rollback_preserving_error(self._write_conn)
                    raise
                else:
                    try:
                        self._write_conn.commit()
                    except BaseException:
                        self._rollback_preserving_error(self._write_conn)
                        raise
            finally:
                self._transaction_owner_thread_id = None

    @contextmanager
    def migration_transaction(self):
        """迁移专用事务：完整持锁并在事务外保存/恢复 foreign_keys。"""
        with self._write_lock:
            if self._write_conn.in_transaction:
                raise RuntimeError("connection already has an active transaction")
            original_foreign_keys = int(
                self._write_conn.execute("PRAGMA foreign_keys").fetchone()[0]
            )
            self._write_conn.execute("PRAGMA foreign_keys=OFF")
            try:
                self._write_conn.execute("BEGIN IMMEDIATE")
                self._transaction_owner_thread_id = threading.get_ident()
                try:
                    yield self._write_conn
                except BaseException:
                    self._rollback_preserving_error(self._write_conn)
                    raise
                else:
                    try:
                        self._write_conn.commit()
                    except BaseException:
                        self._rollback_preserving_error(self._write_conn)
                        raise
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    if self._write_conn.in_transaction:
                        try:
                            self._write_conn.rollback()
                        except BaseException:
                            if not active_error:
                                raise
                    try:
                        self._write_conn.execute(
                            f"PRAGMA foreign_keys={'ON' if original_foreign_keys else 'OFF'}"
                        )
                    except BaseException:
                        if not active_error:
                            raise
                finally:
                    self._transaction_owner_thread_id = None

    def execute_read(self, sql, params=None):
        """读操作 — 无锁，走当前线程的读连接。WAL 保证不被写阻塞。"""
        return self._reader().execute(sql, params or ())

    def execute(self, sql, params=None):
        """通用 execute — 自动判断读/写路由。兼容旧代码。"""
        if _WRITE_SQL_PATTERN.match(sql):
            return self.execute_write(sql, params)
        return self.execute_read(sql, params)

    def executemany(self, sql, params_seq):
        """通用 executemany — 全部走写连接。"""
        return self.executemany_write(sql, params_seq)

    @property
    def closed(self):
        try:
            self._read_conn.execute("SELECT 1")
            return False
        except Exception:
            return True

    def reopen(self):
        """重新打开所有连接。"""
        with self._write_lock:
            try:
                self._write_conn.close()
            except Exception:
                pass
            try:
                self._read_conn.close()
            except Exception:
                pass
            self._close_thread_readers()
            self._write_conn = self._create_write_connection()
            self._read_conn = (
                self._write_conn if self._shared_memory_connection else self._create_read_connection()
            )

    def close(self):
        """关闭所有连接。"""
        with self._write_lock:
            try:
                self._write_conn.close()
            except Exception:
                pass
            try:
                self._read_conn.close()
            except Exception:
                pass
            self._close_thread_readers()

    def _sync_index_delete(self, ids):
        """删除记忆时同步清除向量索引标记。"""
        if self.memory_index and ids:
            try:
                self.memory_index.mark_deleted([int(i) for i in ids])
            except Exception:
                pass
