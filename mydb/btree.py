"""An on-disk B+ tree. Every table and every index is one of these.

Each node is one page.
  * Leaf nodes hold the actual (key, value) pairs, sorted by key, and point to
    the next leaf — so a full-table scan is just "walk the leaves left to right".
  * Internal nodes hold only keys and child page numbers, used to steer a
    search down to the right leaf.

    internal:            [ 50 | 100 ]
                        /     |      \
    leaves:    [1..49] -> [50..99] -> [100..] -> (next leaf pointers)

Finding a key among a million rows takes ~3-4 page reads instead of a million.

When a node gets too full to fit in its page, it splits in two and passes a
separator key up to its parent (which may split too). The root never moves:
when it splits, its contents are copied to a new page and the root becomes an
internal node above the two halves. That way the root page number stored in
the catalog never changes.

Keys are tuples of values (a table key is (rowid,); an index key is
(column_value, rowid)). Values are raw bytes.
"""
import bisect
import struct

from . import record
from .pager import PAGE_SIZE

LEAF, INTERNAL = 1, 2
HEADER = struct.Struct("<BIH")                  # node type, next-leaf page, key count
MAX_ENTRY = (PAGE_SIZE - HEADER.size) // 4      # ensures at least 4 entries per page


class DuplicateKeyError(Exception):
    pass


class Node:
    __slots__ = ("page", "leaf", "keys", "values", "children", "next")

    def __init__(self, page, leaf, keys=None, values=None, children=None, next_leaf=0):
        self.page = page
        self.leaf = leaf
        self.keys = keys or []
        self.values = values if values is not None else ([] if leaf else None)
        self.children = children if children is not None else ([] if not leaf else None)
        self.next = next_leaf

    def entry_sizes(self):
        if self.leaf:
            return [len(record.encode(k)) + 4 + len(v) for k, v in zip(self.keys, self.values)]
        return [len(record.encode(k)) + 4 for k in self.keys]

    def serialize(self) -> bytes:
        parts = [HEADER.pack(LEAF if self.leaf else INTERNAL, self.next, len(self.keys))]
        if self.leaf:
            for k, v in zip(self.keys, self.values):
                parts += [record.encode(k), struct.pack("<I", len(v)), v]
        else:
            parts.append(struct.pack(f"<{len(self.children)}I", *self.children))
            parts += [record.encode(k) for k in self.keys]
        return b"".join(parts)

    @classmethod
    def load(cls, page, data):
        kind, nxt, n = HEADER.unpack_from(data, 0)
        pos = HEADER.size
        keys = []
        if kind == LEAF:
            values = []
            for _ in range(n):
                k, pos = record.decode(data, pos)
                (length,) = struct.unpack_from("<I", data, pos)
                pos += 4
                keys.append(k)
                values.append(bytes(data[pos:pos + length]))
                pos += length
            return cls(page, True, keys, values, None, nxt)
        if kind == INTERNAL:
            children = list(struct.unpack_from(f"<{n + 1}I", data, pos))
            pos += 4 * (n + 1)
            for _ in range(n):
                k, pos = record.decode(data, pos)
                keys.append(k)
            return cls(page, False, keys, None, children)
        raise ValueError(f"page {page} is not a B-tree node (corrupt database?)")


class BTree:
    def __init__(self, pager, root):
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager):
        page = pager.allocate()
        pager.write(page, Node(page, True).serialize())
        return cls(pager, page)

    # ---- helpers ----

    def _load(self, page):
        return Node.load(page, self.pager.read(page))

    def _save(self, node):
        self.pager.write(node.page, node.serialize())

    @staticmethod
    def _pos(keys, key, right=False):
        ordered = [record.key_order(k) for k in keys]
        find = bisect.bisect_right if right else bisect.bisect_left
        return find(ordered, record.key_order(key))

    @staticmethod
    def _same(a, b):
        return record.key_order(a) == record.key_order(b)

    def _find_leaf(self, key):
        node = self._load(self.root)
        while not node.leaf:
            node = self._load(node.children[self._pos(node.keys, key, right=True)])
        return node

    # ---- lookups ----

    def get(self, key):
        leaf = self._find_leaf(key)
        i = self._pos(leaf.keys, key)
        if i < len(leaf.keys) and self._same(leaf.keys[i], key):
            return leaf.values[i]
        return None

    def scan(self, start=None):
        """Yield (key, value) in key order, beginning at the first key >= start."""
        if start is None:
            node = self._load(self.root)
            while not node.leaf:
                node = self._load(node.children[0])
            i = 0
        else:
            node = self._find_leaf(start)
            i = self._pos(node.keys, start)
        while True:
            yield from zip(node.keys[i:], node.values[i:])
            if not node.next:
                return
            node, i = self._load(node.next), 0

    def last_key(self):
        node = self._load(self.root)
        while not node.leaf:
            node = self._load(node.children[-1])
        if node.keys:
            return node.keys[-1]
        last = None                     # rightmost leaf emptied by deletes
        for last, _ in self.scan():
            pass
        return last

    # ---- changes ----

    def insert(self, key, value: bytes, replace=False):
        if len(record.encode(key)) + 4 + len(value) > MAX_ENTRY:
            raise ValueError(f"row too large (max about {MAX_ENTRY} bytes)")
        split = self._insert(self.root, key, value, replace)
        if split:                       # root split: grow the tree by one level
            sep, right_page = split
            root = self._load(self.root)
            root.page = self.pager.allocate()
            self._save(root)
            self._save(Node(self.root, False, [sep], None, [root.page, right_page]))

    def _insert(self, page, key, value, replace):
        node = self._load(page)
        if node.leaf:
            i = self._pos(node.keys, key)
            if i < len(node.keys) and self._same(node.keys[i], key):
                if not replace:
                    raise DuplicateKeyError(key)
                node.values[i] = value
            else:
                node.keys.insert(i, key)
                node.values.insert(i, value)
        else:
            i = self._pos(node.keys, key, right=True)
            split = self._insert(node.children[i], key, value, replace)
            if split is None:
                return None
            sep, right_page = split
            node.keys.insert(i, sep)
            node.children.insert(i + 1, right_page)
        if len(node.serialize()) <= PAGE_SIZE:
            self._save(node)
            return None
        return self._split(node)

    def _split(self, node):
        # split by bytes, not count, so both halves are guaranteed to fit
        sizes = node.entry_sizes()
        total, acc, mid = sum(sizes), 0, len(sizes) // 2
        for i, s in enumerate(sizes):
            acc += s
            if acc >= total / 2:
                mid = i
                break
        n = len(node.keys)
        right_page = self.pager.allocate()
        if node.leaf:
            mid = max(1, min(mid, n - 1))
            right = Node(right_page, True, node.keys[mid:], node.values[mid:], None, node.next)
            node.keys, node.values, node.next = node.keys[:mid], node.values[:mid], right_page
            sep = right.keys[0]
        else:
            mid = max(1, min(mid, n - 2))
            sep = node.keys[mid]                       # promoted to the parent
            right = Node(right_page, False, node.keys[mid + 1:], None, node.children[mid + 1:])
            node.keys, node.children = node.keys[:mid], node.children[:mid + 1]
        self._save(node)
        self._save(right)
        return sep, right_page

    def delete(self, key) -> bool:
        # Simple deletion: remove from the leaf, no rebalancing. The tree stays
        # correct; nodes can just become emptier than ideal.
        leaf = self._find_leaf(key)
        i = self._pos(leaf.keys, key)
        if i < len(leaf.keys) and self._same(leaf.keys[i], key):
            del leaf.keys[i], leaf.values[i]
            self._save(leaf)
            return True
        return False
