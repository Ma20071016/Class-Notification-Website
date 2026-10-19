"""Verify the shared schema stays current and creates only empty tables."""

import importlib.util
from pathlib import Path
import sqlite3
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "export_schema", PROJECT_ROOT / "database" / "export_schema.py"
)
export_schema = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_schema)


class DatabaseSetupTestCase(unittest.TestCase):
    def test_snapshot_matches_application_schema_constants(self):
        self.assertEqual(
            (PROJECT_ROOT / "database" / "schema.sql").read_text(encoding="utf-8"),
            export_schema.render_schema(),
            "Regenerate with python database/export_schema.py",
        )

    def test_schema_builds_an_empty_consistent_database_idempotently(self):
        schema = (PROJECT_ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.executescript(schema)
        database.executescript(schema)
        self.assertEqual(database.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(database.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])
        tables = {row[0] for row in database.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
        self.assertEqual(tables, {"users", "tasks", "favorites", "attachments"})
        for table in tables:
            self.assertEqual(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        attachment_keys = database.execute("PRAGMA foreign_key_list(attachments)").fetchall()
        self.assertTrue(any(row[2:5] == ("tasks", "task_id", "id") and row[6] == "CASCADE" for row in attachment_keys))


if __name__ == "__main__":
    unittest.main()
