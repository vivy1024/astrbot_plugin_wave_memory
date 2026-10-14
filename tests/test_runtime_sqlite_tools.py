import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


class SQLiteRuntimeGuardTest(unittest.TestCase):
    def test_is_astrbot_running_returns_true_only_for_running_container(self):
        import sqlite_runtime_guard

        runner = Mock(return_value=Mock(returncode=0, stdout="true\n", stderr=""))

        self.assertTrue(sqlite_runtime_guard.is_astrbot_running("astrbot", runner=runner))
        runner.assert_called_once()
        self.assertIn("docker", runner.call_args.args[0])
        self.assertIn("inspect", runner.call_args.args[0])
        self.assertIn("astrbot", runner.call_args.args[0])

    def test_is_astrbot_running_returns_false_for_stopped_or_missing_container(self):
        import sqlite_runtime_guard

        stopped = Mock(return_value=Mock(returncode=0, stdout="false\n", stderr=""))
        missing = Mock(return_value=Mock(returncode=1, stdout="", stderr="No such object"))

        self.assertFalse(sqlite_runtime_guard.is_astrbot_running("astrbot", runner=stopped))
        self.assertFalse(sqlite_runtime_guard.is_astrbot_running("astrbot", runner=missing))

    def test_assert_astrbot_stopped_blocks_mutation_when_running(self):
        import sqlite_runtime_guard

        runner = Mock(return_value=Mock(returncode=0, stdout="true\n", stderr=""))

        with self.assertRaises(sqlite_runtime_guard.RuntimeRunningError) as ctx:
            sqlite_runtime_guard.assert_astrbot_stopped("repair SQLite runtime", runner=runner)

        self.assertIn("repair SQLite runtime", str(ctx.exception))
        self.assertIn("astrbot", str(ctx.exception))

    def test_assert_astrbot_stopped_fails_closed_when_state_unknown(self):
        import sqlite_runtime_guard

        def broken_runner(*args, **kwargs):
            raise OSError("docker unavailable")

        with self.assertRaises(sqlite_runtime_guard.RuntimeStateUnknownError):
            sqlite_runtime_guard.assert_astrbot_stopped("cleanup", runner=broken_runner)


class DBInventoryTest(unittest.TestCase):
    def test_inventory_discovers_dbs_sidecars_uploads_and_configs(self):
        import db_inventory

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "metadata").mkdir()
            (data_dir / "plugin_data" / "astrbot_plugin_wave_memory").mkdir(parents=True)
            (data_dir / "plugins" / "demo" / "files" / "avatar").mkdir(parents=True)
            (data_dir / "config").mkdir()
            (data_dir / "__pycache__").mkdir()

            for rel in [
                "data_v4.db",
                "data_v4.db-wal",
                "metadata/kv_storage.db",
                "metadata/kv_storage.db-shm",
                "plugin_data/astrbot_plugin_wave_memory/wave_memory.db",
                "plugins/demo/files/avatar/image.png",
                "config/session.json",
                "__pycache__/ignored.pyc",
            ]:
                path = data_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            inventory = db_inventory.inventory_host_data(data_dir)
            rels = {item["relative_path"] for item in inventory["files"]}

        self.assertIn("data_v4.db", rels)
        self.assertIn("data_v4.db-wal", rels)
        self.assertIn("metadata/kv_storage.db", rels)
        self.assertIn("metadata/kv_storage.db-shm", rels)
        self.assertIn("plugin_data/astrbot_plugin_wave_memory/wave_memory.db", rels)
        self.assertIn("plugins/demo/files/avatar/image.png", rels)
        self.assertIn("config/session.json", rels)
        self.assertNotIn("__pycache__/ignored.pyc", rels)

    def test_inventory_marks_sqlite_and_restore_relevant_kinds(self):
        import db_inventory

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "data_v4.db").write_text("db", encoding="utf-8")
            (data_dir / "data_v4.db-wal").write_text("wal", encoding="utf-8")
            upload = data_dir / "plugins" / "demo" / "files" / "doc" / "a.pdf"
            upload.parent.mkdir(parents=True)
            upload.write_text("file", encoding="utf-8")

            inventory = db_inventory.inventory_host_data(data_dir)
            by_path = {item["relative_path"]: item for item in inventory["files"]}

        self.assertEqual(by_path["data_v4.db"]["kind"], "sqlite_db")
        self.assertEqual(by_path["data_v4.db-wal"]["kind"], "sqlite_sidecar")
        self.assertEqual(by_path["plugins/demo/files/doc/a.pdf"]["kind"], "plugin_upload")

    def test_inventory_excludes_known_backup_and_test_databases_by_default(self):
        import db_inventory

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for rel in [
                "data_v4.db",
                "data_v4_bindmount_broken_20260408_094332.db",
                "data_v4_repacked_test.db",
                "data_test_delete.db",
                "scratch_acceptance.db",
                "backups_host/sqlite_runtime/pre_migration_20260624_214153/db/data_v4.db",
                "plugin_data/astrbot_plugin_wave_memory/wave_memory_before_roleplay_quarantine_20260624_124107.db",
            ]:
                path = data_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("db", encoding="utf-8")

            inventory = db_inventory.inventory_host_data(data_dir)
            rels = {item["relative_path"] for item in inventory["files"]}

        self.assertIn("data_v4.db", rels)
        self.assertNotIn("data_v4_bindmount_broken_20260408_094332.db", rels)
        self.assertNotIn("data_v4_repacked_test.db", rels)
        self.assertNotIn("data_test_delete.db", rels)
        self.assertNotIn("scratch_acceptance.db", rels)
        self.assertNotIn("backups_host/sqlite_runtime/pre_migration_20260624_214153/db/data_v4.db", rels)
        self.assertNotIn("plugin_data/astrbot_plugin_wave_memory/wave_memory_before_roleplay_quarantine_20260624_124107.db", rels)


class DBDockerModeTest(unittest.TestCase):
    def test_docker_inventory_parses_container_json(self):
        import db_inventory

        payload = {
            "mode": "docker",
            "data_dir": "/AstrBot/data",
            "files": [{"relative_path": "data_v4.db", "kind": "sqlite_db", "size": 10}],
        }
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(payload), stderr=""))

        result = db_inventory.inventory_docker_data("astrbot", "/AstrBot/data", runner=runner)

        self.assertEqual(result["mode"], "docker")
        self.assertEqual(result["container"], "astrbot")
        self.assertEqual(result["files"][0]["relative_path"], "data_v4.db")
        self.assertIn("docker", runner.call_args.args[0])
        self.assertIn("exec", runner.call_args.args[0])

    def test_docker_health_parses_container_json(self):
        import db_health_check

        payload = {
            "mode": "docker",
            "data_dir": "/AstrBot/data",
            "status": "PASS",
            "databases": [{"relative_path": "data_v4.db", "status": "PASS"}],
        }
        runner = Mock(return_value=Mock(returncode=0, stdout=json.dumps(payload), stderr=""))

        result = db_health_check.check_docker_data("astrbot", "/AstrBot/data", runner=runner)

        self.assertEqual(result["mode"], "docker")
        self.assertEqual(result["container"], "astrbot")
        self.assertEqual(result["status"], "PASS")
        self.assertIn("docker", runner.call_args.args[0])
        self.assertIn("exec", runner.call_args.args[0])


class RuntimeDataExportTest(unittest.TestCase):
    def test_export_host_data_copies_restore_files_and_writes_manifest(self):
        import export_runtime_data

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            target = Path(tmp) / "export"
            (data_dir / "plugin_data" / "astrbot_plugin_wave_memory").mkdir(parents=True)
            (data_dir / "config").mkdir(parents=True)
            (data_dir / "plugins" / "demo" / "files" / "doc").mkdir(parents=True)
            for rel, content in {
                "data_v4.db": "db",
                "data_v4.db-wal": "wal",
                "plugin_data/astrbot_plugin_wave_memory/wave_memory.db": "wave",
                "config/astrbot_config.json": "{}",
                "plugins/demo/files/doc/a.pdf": "file",
            }.items():
                path = data_dir / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            manifest = export_runtime_data.export_host_data(data_dir, target)

            self.assertEqual(manifest["source"], "host-copy")
            self.assertTrue((target / "manifest.json").exists())
            self.assertTrue((target / "db" / "data_v4.db").exists())
            self.assertTrue((target / "db" / "data_v4.db-wal").exists())
            self.assertTrue((target / "db" / "plugin_data" / "astrbot_plugin_wave_memory" / "wave_memory.db").exists())
            self.assertTrue((target / "config" / "config" / "astrbot_config.json").exists())
            self.assertTrue((target / "uploads" / "plugins" / "demo" / "files" / "doc" / "a.pdf").exists())
            by_path = {item["relative_path"]: item for item in manifest["files"]}
            self.assertIn("sha256", by_path["data_v4.db"])
            self.assertEqual(by_path["plugins/demo/files/doc/a.pdf"]["kind"], "plugin_upload")

    def test_export_docker_data_reports_docker_cp_failure_in_manifest(self):
        import export_runtime_data

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "export"
            inventory = {"mode": "docker", "data_dir": "/AstrBot/data", "files": [{"relative_path": "data_v4.db", "kind": "sqlite_db", "size": 10}]}
            runner = Mock(side_effect=[
                Mock(returncode=0, stdout=json.dumps(inventory), stderr=""),
                Mock(returncode=1, stdout="", stderr="copy failed"),
            ])

            manifest = export_runtime_data.export_docker_data("astrbot", "/AstrBot/data", target, runner=runner)

            self.assertEqual(manifest["source"], "docker-runtime")
            self.assertEqual(manifest["container"], "astrbot")
            self.assertEqual(manifest["error"], "copy failed")
            self.assertTrue((target / "manifest.json").exists())
            self.assertIn("docker", runner.call_args.args[0])
            self.assertIn("cp", runner.call_args.args[0])

    def test_export_docker_data_copies_only_inventory_files(self):
        import export_runtime_data

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "export"
            inventory = {
                "mode": "docker",
                "data_dir": "/AstrBot/data",
                "files": [
                    {"relative_path": "data_v4.db", "kind": "sqlite_db", "size": 8192},
                    {"relative_path": "config/astrbot_config.json", "kind": "config", "size": 2},
                ],
            }

            def runner(args, **kwargs):
                if args[:3] == ["docker", "exec", "astrbot"]:
                    return Mock(returncode=0, stdout=json.dumps(inventory), stderr="")
                if args[:2] == ["docker", "cp"]:
                    source = args[2]
                    destination = Path(args[3])
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if source.endswith("data_v4.db"):
                        conn = sqlite3.connect(destination)
                        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY)")
                        conn.commit()
                        conn.close()
                    else:
                        destination.write_text("{}", encoding="utf-8")
                    return Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(args)

            manifest = export_runtime_data.export_docker_data("astrbot", "/AstrBot/data", target, runner=runner)

            self.assertEqual(manifest["source"], "docker-runtime")
            self.assertTrue((target / "db" / "data_v4.db").exists())
            self.assertTrue((target / "config" / "config" / "astrbot_config.json").exists())
            sources = {item["relative_path"] for item in manifest["files"]}
            self.assertEqual(sources, {"data_v4.db", "config/astrbot_config.json"})


class DBRepairTest(unittest.TestCase):
    def _create_sqlite_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO demo (name) VALUES ('before')")
            conn.commit()
        finally:
            conn.close()

    def test_repair_database_creates_backup_sidecars_rebuilds_and_report(self):
        import repair_sqlite_runtime

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db_path = data_dir / "data_v4.db"
            self._create_sqlite_db(db_path)
            for suffix in ("-wal", "-shm"):
                (data_dir / f"data_v4.db{suffix}").write_text("sidecar", encoding="utf-8")
            backup_root = Path(tmp) / "backups"

            with patch.object(repair_sqlite_runtime, "assert_astrbot_stopped") as guard:
                report = repair_sqlite_runtime.repair_database(db_path, data_dir=data_dir, backup_root=backup_root)

            guard.assert_called_once_with("repair SQLite runtime")
            self.assertEqual(report["status"], "repaired")
            self.assertTrue(Path(report["backup_dir"]).exists())
            self.assertTrue((Path(report["backup_dir"]) / "data_v4.db").exists())
            self.assertTrue((Path(report["backup_dir"]) / "data_v4.db-wal").exists())
            self.assertTrue((Path(report["backup_dir"]) / "repair_report.json").exists())
            conn = sqlite3.connect(db_path)
            try:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("SELECT name FROM demo").fetchone()[0], "before")
            finally:
                conn.close()

    def test_repair_database_preserves_original_when_source_cannot_open(self):
        import repair_sqlite_runtime

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            db_path = data_dir / "broken.db"
            db_path.write_bytes(b"not sqlite")
            original = db_path.read_bytes()

            with patch.object(repair_sqlite_runtime, "assert_astrbot_stopped"):
                report = repair_sqlite_runtime.repair_database(db_path, data_dir=data_dir, backup_root=Path(tmp) / "backups")

            self.assertEqual(report["status"], "failed")
            self.assertEqual(db_path.read_bytes(), original)
            self.assertTrue((Path(report["backup_dir"]) / "repair_report.json").exists())


class DBHealthCheckTest(unittest.TestCase):
    def _create_sqlite_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO demo (name) VALUES ('ok')")
            conn.commit()
        finally:
            conn.close()

    def test_check_sqlite_db_reports_ok_for_valid_database(self):
        import db_health_check

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "data_v4.db"
            self._create_sqlite_db(db_path)

            result = db_health_check.check_sqlite_db(db_path, relative_path="data_v4.db")

        self.assertEqual(result["relative_path"], "data_v4.db")
        self.assertEqual(result["open"], "ok")
        self.assertEqual(result["quick_check"], "ok")
        self.assertEqual(result["integrity_check"], "ok")
        self.assertIn(result["journal_mode"], {"delete", "wal", "memory", "off", "truncate", "persist"})
        self.assertEqual(result["backup_api_copy"], "ok")
        self.assertEqual(result["status"], "PASS")

    def test_check_sqlite_db_reports_failure_for_corrupt_file(self):
        import db_health_check

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "broken.db"
            db_path.write_bytes(b"not a sqlite database")

            result = db_health_check.check_sqlite_db(db_path, relative_path="broken.db")

        self.assertEqual(result["open"], "failed")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("error", result)

    def test_check_host_data_uses_inventory_and_reports_overall_status(self):
        import db_health_check

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._create_sqlite_db(data_dir / "data_v4.db")
            (data_dir / "backups_host" / "sqlite_runtime" / "pre_migration" / "db").mkdir(parents=True)
            self._create_sqlite_db(data_dir / "backups_host" / "sqlite_runtime" / "pre_migration" / "db" / "data_v4.db")
            (data_dir / "plugin_data" / "astrbot_plugin_wave_memory").mkdir(parents=True)
            self._create_sqlite_db(data_dir / "plugin_data" / "astrbot_plugin_wave_memory" / "wave_memory.db")

            report = db_health_check.check_host_data(data_dir)

        self.assertEqual(report["status"], "PASS")
        self.assertEqual({item["relative_path"] for item in report["databases"]}, {
            "data_v4.db",
            "plugin_data/astrbot_plugin_wave_memory/wave_memory.db",
        })
        self.assertNotIn("backups_host/sqlite_runtime/pre_migration/db/data_v4.db", {item["relative_path"] for item in report["databases"]})


if __name__ == "__main__":
    unittest.main()
