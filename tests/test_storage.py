import os
import random
import shutil
import tempfile
import unittest

from mydb import record
from mydb.btree import BTree, DuplicateKeyError
from mydb.pager import PAGE_SIZE, Pager


class TestRecord(unittest.TestCase):
    def test_roundtrip(self):
        values = (None, 0, -5, 2**62, 3.25, "", "héllo ✓", "a'b")
        self.assertEqual(record.decode(record.encode(values))[0], values)

    def test_sort_order(self):
        vals = ["b", 3, None, 1.5, "a", -2]
        self.assertEqual(sorted(vals, key=record.sort_key), [None, -2, 1.5, 3, "a", "b"])


class TestBTree(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.pager = Pager(self.path)
        self.tree = BTree.create(self.pager)

    def tearDown(self):
        self.pager.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_random_inserts_stay_sorted(self):
        keys = random.sample(range(1_000_000), 5000)
        for k in keys:
            self.tree.insert((k,), str(k).encode())
        self.assertEqual([k for (k,), _ in self.tree.scan()], sorted(keys))
        for k in keys[:100]:
            self.assertEqual(self.tree.get((k,)), str(k).encode())
        self.assertIsNone(self.tree.get((-1,)))
        self.assertGreater(self.pager.num_pages, 10)     # really spread over many pages

    def test_duplicates_and_replace(self):
        self.tree.insert((1,), b"a")
        with self.assertRaises(DuplicateKeyError):
            self.tree.insert((1,), b"b")
        self.tree.insert((1,), b"b", replace=True)
        self.assertEqual(self.tree.get((1,)), b"b")

    def test_delete(self):
        for k in range(2000):
            self.tree.insert((k,), b"x" * 50)
        for k in range(0, 2000, 2):
            self.assertTrue(self.tree.delete((k,)))
        self.assertFalse(self.tree.delete((0,)))
        self.assertEqual([k for (k,), _ in self.tree.scan()], list(range(1, 2000, 2)))
        self.assertEqual(self.tree.last_key(), (1999,))

    def test_scan_from_key(self):
        for k in range(0, 1000, 10):
            self.tree.insert((k,), b"")
        self.assertEqual([k for (k,), _ in self.tree.scan((55,))][:3], [60, 70, 80])

    def test_composite_text_keys(self):
        for i in range(1500):
            self.tree.insert((f"user{i % 50}", i), b"")
        hits = [k for k, _ in self.tree.scan(("user7",)) if k[0] == "user7"]
        self.assertEqual(len(hits), 30)

    def test_large_values_split_by_size(self):
        for i in range(500):
            self.tree.insert((i,), bytes(random.randint(0, 900)))
        self.assertEqual(len(list(self.tree.scan())), 500)
        with self.assertRaises(ValueError):
            self.tree.insert((9999,), bytes(PAGE_SIZE))

    def test_persists_after_commit(self):
        for k in range(3000):
            self.tree.insert((k,), b"v")
        self.pager.commit()
        root = self.tree.root
        self.pager.close()
        self.pager = Pager(self.path)
        self.assertEqual(len(list(BTree(self.pager, root).scan())), 3000)


if __name__ == "__main__":
    unittest.main()
