"""База: старая база без новых колонок должна обновиться сама."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MigrationTest(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.db")
            db = sqlite3.connect(path)
            db.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL,
                          referrer INTEGER, ref_rewarded INTEGER DEFAULT 0, premium_until TEXT,
                          settings TEXT DEFAULT '{}')""")
            db.execute("INSERT INTO users (id, first_seen) VALUES (42, '2026-11-01T00:00:00+00:00')")
            db.commit()
            db.close()
            code = ("import storage; storage.set_remind(42, True); storage.touch(42); "
                    "print(storage.get_user(42)['remind'], storage.exists(42))")
            out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                 env={**os.environ, "DB_PATH": path})
            self.assertEqual(out.stdout.strip(), "1 True", out.stderr)


if __name__ == "__main__":
    unittest.main()
