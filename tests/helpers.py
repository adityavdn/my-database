import os
import shutil
import tempfile
import unittest

from mydb import Database


class TempDBTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.db")
        self.db = Database(self.path)

    def tearDown(self):
        try:
            self.db.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def reopen(self):
        self.db.close()
        self.db = Database(self.path)

    def rows(self, sql):
        return self.db.execute(sql).rows

    def value(self, sql):
        return self.db.execute(sql).rows[0][0]
