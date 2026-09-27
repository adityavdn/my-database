"""Simulate the power going out in the middle of a commit."""
import os
import unittest
from unittest import mock

from mydb import Database
from mydb.pager import Pager
from tests.helpers import TempDBTest


class PowerCut(Exception):
    pass


class TestCrashRecovery(TempDBTest):
    def test_crash_during_commit_rolls_back(self):
        self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        self.db.execute("INSERT INTO t (v) VALUES ('original')")

        # A big transaction touching many pages...
        self.db.execute("BEGIN")
        self.db.execute("UPDATE t SET v = 'changed'")
        self.db.execute("INSERT INTO t (v) VALUES " + ", ".join(["('x" + "y" * 200 + "')"] * 300))

        # ...and the power dies after only 2 pages reach the disk.
        real_write = Pager._write_to_disk
        written = []

        def dying_write(pager, n, data):
            if len(written) == 2:
                raise PowerCut()
            written.append(n)
            real_write(pager, n, data)

        with mock.patch.object(Pager, "_write_to_disk", dying_write):
            with self.assertRaises(PowerCut):
                self.db.execute("COMMIT")

        self.assertTrue(os.path.exists(self.path + "-journal"))   # the "hot" journal
        self.db.pager.file.close()                                 # process dies

        # Restart: the journal is replayed and the half-written commit is undone.
        self.db = Database(self.path)
        self.assertFalse(os.path.exists(self.path + "-journal"))
        self.assertEqual(self.rows("SELECT v FROM t"), [("original",)])

    def test_journal_without_magic_is_ignored(self):
        self.db.execute("CREATE TABLE t (x INT)")
        self.db.close()
        with open(self.path + "-journal", "wb") as f:
            f.write(b"garbage")
        self.db = Database(self.path)
        self.assertEqual(self.rows("SELECT * FROM t"), [])


if __name__ == "__main__":
    unittest.main()
