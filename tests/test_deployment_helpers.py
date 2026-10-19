"""Exercise release/backup tools using isolated fixtures, never the real instance."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "deploy" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


backup_data = load_helper("backup_data")
build_release = load_helper("build_release")


class BackupHelperTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.database = self.instance / "class_notice.sqlite3"

    def seed_database(self):
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                "PRAGMA foreign_keys = ON;"
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);"
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, author_id INTEGER REFERENCES users(id), title TEXT);"
                "CREATE TABLE favorites (user_id INTEGER REFERENCES users(id), task_id INTEGER REFERENCES tasks(id));"
                "INSERT INTO users VALUES (1, 'fixture-user');"
                "INSERT INTO tasks VALUES (1, 1, 'fixture-notice');"
                "INSERT INTO favorites VALUES (1, 1);"
            )

    def seed_attachment(self, name="a" * 32 + ".pdf", contents=b"%PDF-1.4 fixture"):
        self.seed_database()
        uploads = self.instance / "uploads"
        uploads.mkdir()
        (uploads / name).write_bytes(contents)
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                "CREATE TABLE attachments (id TEXT PRIMARY KEY, "
                "task_id INTEGER REFERENCES tasks(id), name TEXT, stored_name TEXT UNIQUE, "
                "media_type TEXT, size INTEGER, created_at TEXT);"
            )
            connection.execute(
                "INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("fixture-attachment", 1, "中文附件.pdf", name, "application/pdf", len(contents),
                 "2026-10-19T00:00:00Z"),
            )
        return uploads / name

    def test_consistent_backup_and_private_secret_excludes_credentials_and_older_backups(self):
        self.seed_database()
        secret_bytes = b"temporary-test-secret\n"
        (self.instance / "secret_key").write_bytes(secret_bytes)
        (self.instance / "admin-credentials.txt").write_text("fixture-only-password")
        old = self.instance / "backups" / "older"
        old.mkdir(parents=True)
        (old / "old.sqlite3").write_text("old fixture")
        backup = backup_data.create_backup(self.instance)
        self.assertEqual(backup.parent, self.instance / "backups")
        self.assertEqual({p.name for p in backup.iterdir()},
                         {"class_notice.sqlite3", "secret_key", "backup-info.json"})
        self.assertEqual((backup / "secret_key").read_bytes(), secret_bytes)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        for path in backup.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with sqlite3.connect(backup / "class_notice.sqlite3") as connection:
            self.assertEqual(connection.execute("SELECT * FROM tasks").fetchall(), [(1, 1, "fixture-notice")])
            self.assertEqual(connection.execute("SELECT * FROM favorites").fetchall(), [(1, 1)])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        metadata = json.loads((backup / "backup-info.json").read_text())
        self.assertEqual(metadata["table_row_counts"], {"favorites": 1, "tasks": 1, "users": 1})
        self.assertTrue(metadata["secret_key_copied"])
        self.assertTrue(metadata["created_at_utc"].endswith("Z"))
        self.assertNotIn(secret_bytes.decode().strip(), json.dumps(metadata))
        self.assertNotIn("fixture-only-password", json.dumps(metadata))

    def test_online_wal_backup_includes_committed_data(self):
        self.seed_database()
        with sqlite3.connect(self.database) as live:
            self.assertEqual(live.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
            live.execute("INSERT INTO tasks VALUES (2, 1, 'committed-while-open')")
            live.commit()
            backup = backup_data.create_backup(self.instance)
            with sqlite3.connect(backup / "class_notice.sqlite3") as snapshot:
                self.assertEqual(snapshot.execute("SELECT title FROM tasks WHERE id=2").fetchone(),
                                 ("committed-while-open",))

    def test_attachment_backup_copies_referenced_originals_and_can_be_restored(self):
        original = self.seed_attachment()
        (original.parent / "orphan.pdf").write_bytes(b"excluded orphan")
        (original.parent / "previews").mkdir()
        (original.parent / "previews" / "cache.pdf").write_bytes(b"excluded derived preview")
        backup = backup_data.create_backup(self.instance)
        uploads = backup / "uploads"
        self.assertEqual([path.name for path in uploads.iterdir()], [original.name])
        self.assertEqual((uploads / original.name).read_bytes(), original.read_bytes())
        self.assertEqual(stat.S_IMODE(uploads.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((uploads / original.name).stat().st_mode), 0o600)
        metadata = json.loads((backup / "backup-info.json").read_text())
        self.assertEqual(metadata["attachment_files"], [{
            "id": "fixture-attachment", "stored_name": original.name, "size": original.stat().st_size,
        }])
        self.assertEqual(metadata["attachment_count"], 1)
        self.assertEqual(metadata["attachment_bytes"], original.stat().st_size)
        restored = self.root / "restored"
        shutil.copytree(backup, restored)
        with sqlite3.connect(restored / "class_notice.sqlite3") as snapshot:
            row = snapshot.execute("SELECT stored_name, size FROM attachments").fetchone()
            self.assertEqual((restored / "uploads" / row[0]).stat().st_size, row[1])
            self.assertEqual(snapshot.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_attachment_copy_holds_writer_lock_until_snapshot_and_files_are_complete(self):
        original = self.seed_attachment()
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        copy_file = shutil.copyfileobj
        attempted = []

        def copy_while_attempting_delete(source, destination, *args, **kwargs):
            with sqlite3.connect(self.database, timeout=0) as writer:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    writer.execute("DELETE FROM attachments")
                attempted.append(True)
            return copy_file(source, destination, *args, **kwargs)

        with patch.object(backup_data.shutil, "copyfileobj", side_effect=copy_while_attempting_delete):
            backup = backup_data.create_backup(self.instance)
        self.assertEqual(attempted, [True])
        with sqlite3.connect(self.database, timeout=0) as writer:
            writer.execute("DELETE FROM attachments")
        original.unlink()
        with sqlite3.connect(backup / "class_notice.sqlite3") as snapshot:
            self.assertEqual(snapshot.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], 1)
        self.assertTrue((backup / "uploads" / original.name).is_file())

    def test_attachment_path_traversal_is_rejected_and_partial_backup_removed(self):
        self.seed_attachment()
        for name in ("../outside.pdf", "/tmp/outside.pdf", "..\\outside.pdf", ".", "..", "", "bad\x00.pdf"):
            with self.subTest(name=name):
                with sqlite3.connect(self.database) as connection:
                    connection.execute("UPDATE attachments SET stored_name = ?", (name,))
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    backup_data.create_backup(self.instance)
                self.assertEqual(list((self.instance / "backups").iterdir()), [])

    def test_attachment_symlink_and_missing_file_are_rejected(self):
        original = self.seed_attachment()
        contents = original.read_bytes()
        original.unlink()
        with self.assertRaises(OSError):
            backup_data.create_backup(self.instance)
        outside = self.root / "outside.pdf"
        outside.write_bytes(contents)
        original.symlink_to(outside)
        with self.assertRaises((OSError, ValueError)):
            backup_data.create_backup(self.instance)
        self.assertEqual(list((self.instance / "backups").iterdir()), [])

    def test_attachment_directory_symlink_is_rejected(self):
        original = self.seed_attachment()
        actual = self.root / "original-uploads"
        original.parent.rename(actual)
        original.parent.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "uploads directory"):
            backup_data.create_backup(self.instance)
        self.assertEqual(list((self.instance / "backups").iterdir()), [])

    def test_attachment_size_mismatch_is_rejected(self):
        original = self.seed_attachment()
        original.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "size-mismatched"):
            backup_data.create_backup(self.instance)
        self.assertEqual(list((self.instance / "backups").iterdir()), [])

    def test_attachment_special_file_is_rejected_without_blocking(self):
        original = self.seed_attachment()
        original.unlink()
        os.mkfifo(original)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            backup_data.create_backup(self.instance)
        self.assertEqual(list((self.instance / "backups").iterdir()), [])

    def test_empty_attachment_table_does_not_require_upload_directory(self):
        original = self.seed_attachment()
        with sqlite3.connect(self.database) as connection:
            connection.execute("DELETE FROM attachments")
        original.unlink()
        original.parent.rmdir()
        backup = backup_data.create_backup(self.instance)
        self.assertFalse((backup / "uploads").exists())
        metadata = json.loads((backup / "backup-info.json").read_text())
        self.assertEqual(metadata["attachment_files"], [])
        self.assertEqual(metadata["attachment_count"], 0)
        self.assertEqual(metadata["attachment_bytes"], 0)

    def test_missing_source_fails_without_creating_database_or_output(self):
        with self.assertRaises(ValueError):
            backup_data.create_backup(self.instance)
        self.assertFalse(self.database.exists())
        self.assertFalse((self.instance / "backups").exists())

    def test_corrupt_database_fails_and_removes_partial_backup(self):
        self.database.write_bytes(b"not a SQLite database")
        output = self.root / "backups"
        with self.assertRaises(sqlite3.DatabaseError):
            backup_data.create_backup(self.instance, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_foreign_key_failure_removes_partial_backup(self):
        self.seed_database()
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO favorites VALUES (999, 999)")
        output = self.root / "backups"
        with self.assertRaisesRegex(ValueError, "foreign key check"):
            backup_data.create_backup(self.instance, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_missing_secret_is_reported_and_backup_names_are_unique(self):
        self.seed_database()
        first = backup_data.create_backup(self.instance)
        second = backup_data.create_backup(self.instance)
        self.assertNotEqual(first, second)
        metadata = json.loads((first / "backup-info.json").read_text())
        self.assertFalse(metadata["secret_key_copied"])
        self.assertIn("CLASS_NOTICE_SECRET_KEY", metadata["external_secret_key_note"])
        self.assertFalse((first / "secret_key").exists())

    def test_cli_uses_environment_instance_and_does_not_print_secret(self):
        self.seed_database()
        (self.instance / "secret_key").write_text("cli-secret-fixture")
        environment = os.environ.copy()
        environment["CLASS_NOTICE_INSTANCE_PATH"] = str(self.instance)
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "deploy" / "backup_data.py"),
             "--output", str(self.root / "output")],
            env=environment, text=True, capture_output=True, check=True,
        )
        self.assertTrue((Path(result.stdout.strip()) / "class_notice.sqlite3").is_file())
        self.assertNotIn("cli-secret-fixture", result.stdout + result.stderr)

    def test_secret_symlink_is_rejected(self):
        self.seed_database()
        outside = self.root / "outside-secret"
        outside.write_text("fixture")
        (self.instance / "secret_key").symlink_to(outside)
        with self.assertRaises(ValueError):
            backup_data.create_backup(self.instance)
        self.assertFalse((self.instance / "backups").exists())


class ReleaseHelperTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        for name in build_release.RELEASE_FILES:
            (self.project / name).write_text("release fixture\n")
        for name in build_release.RELEASE_DIRECTORIES:
            (self.project / name).mkdir()
        (self.project / "templates" / "index.html").write_text("fixture page")
        (self.project / "static" / "app.js").write_text("// fixture")
        (self.project / "deploy" / "class-notice.env.example").write_text("CLASS_NOTICE_INSTANCE_PATH=/var/lib/class-notice")
        (self.project / "tests" / "test_fixture.py").write_text("# fixture")
        (self.project / "database" / "schema.sql").write_text("CREATE TABLE fixture (id INTEGER PRIMARY KEY);\n")
        (self.project / "database" / "README.md").write_text("Schema-only source; no user records.\n")
        (self.project / "database" / "export_schema.py").write_text("# schema export fixture\n")

    def test_archive_allowlist_excludes_runtime_state_secrets_caches_and_archives(self):
        for name in ("instance", ".openai", ".venv", "unlisted"):
            directory = self.project / name
            directory.mkdir()
            (directory / "secret_key").write_text("excluded-fixture")
        (self.project / ".env").write_text("excluded-fixture")
        tests = self.project / "tests"
        for name in (".DS_Store", ".env", ".env.production", "secret_key", "admin-credentials.txt",
                     "state.sqlite3", "state.sqlite3-wal", "release.tar.gz", "code.pyc"):
            (tests / name).write_text("excluded-fixture")
        cache = tests / "__pycache__"
        cache.mkdir()
        (cache / "compiled.pyc").write_text("excluded-fixture")
        uploads = tests / "uploads"
        uploads.mkdir()
        (uploads / "private-attachment.pdf").write_bytes(b"excluded-fixture")
        screenshots = tests / "artifacts" / "attachments"
        screenshots.mkdir(parents=True)
        (screenshots / "preview-desktop.png").write_bytes(b"excluded-fixture")
        (self.project / "database" / "private.sqlite3").write_bytes(b"excluded-fixture")
        (self.project / "database" / "private.sqlite3-wal").write_bytes(b"excluded-fixture")
        output = build_release.build_release(self.project)
        self.assertEqual(output.parent, self.project / "dist")
        with tarfile.open(output) as archive:
            names = set(archive.getnames())
            expected = {"class-notice"}
            expected.update("class-notice/" + name for name in build_release.RELEASE_FILES + build_release.RELEASE_DIRECTORIES)
            expected.update({"class-notice/templates/index.html", "class-notice/static/app.js",
                             "class-notice/deploy/class-notice.env.example", "class-notice/tests/test_fixture.py",
                             "class-notice/database/schema.sql", "class-notice/database/README.md",
                             "class-notice/database/export_schema.py"})
            self.assertEqual(names, expected)
            self.assertIn("class-notice/attachments.py", names)
            self.assertIn("class-notice/document_preview.py", names)
            self.assertIn("class-notice/CONTRIBUTING.md", names)
            self.assertIn("class-notice/database/schema.sql", names)
            self.assertFalse(any(name == "class-notice/tests/artifacts" or
                                 name.startswith("class-notice/tests/artifacts/") for name in names))
            for member in archive.getmembers():
                self.assertFalse(member.issym() or member.islnk())
                if member.isfile():
                    self.assertNotIn(b"excluded-fixture", archive.extractfile(member).read())

    def test_existing_output_is_not_overwritten(self):
        output = self.root / "release.tar.gz"
        output.write_bytes(b"already-exists")
        with self.assertRaises(FileExistsError):
            build_release.build_release(self.project, output)
        self.assertEqual(output.read_bytes(), b"already-exists")

    def test_symlink_in_included_directory_is_rejected_before_output_creation(self):
        outside = self.root / "outside.txt"
        outside.write_text("do not include")
        (self.project / "static" / "linked.txt").symlink_to(outside)
        output = self.root / "release.tar.gz"
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            build_release.build_release(self.project, output)
        self.assertFalse(output.exists())

    def test_symlink_allowlisted_directory_is_rejected(self):
        (self.project / "templates" / "index.html").unlink()
        (self.project / "templates").rmdir()
        (self.project / "templates").symlink_to(self.project / "static", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic links"):
            build_release.build_release(self.project)

    def test_missing_required_entry_is_rejected(self):
        (self.project / "requirements-production.txt").unlink()
        with self.assertRaisesRegex(ValueError, "Required release entry"):
            build_release.build_release(self.project)
        self.assertFalse((self.project / "dist").exists())

    def test_default_names_are_unique_and_cli_reports_new_output(self):
        first = build_release.build_release(self.project)
        second = build_release.build_release(self.project)
        self.assertNotEqual(first, second)
        helper = self.project / "deploy" / "build_release.py"
        shutil.copyfile(PROJECT_ROOT / "deploy" / "build_release.py", helper)
        output = self.root / "explicit-release.tar.gz"
        result = subprocess.run([sys.executable, str(helper), "--output", str(output)],
                                text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), str(output))
        self.assertTrue(tarfile.is_tarfile(output))


if __name__ == "__main__":
    unittest.main()
