"""The pager: reads and writes the database file in fixed-size pages.

The file is just an array of 4096-byte pages. Page N lives at byte N * 4096.
Everything above this layer (the B-tree, the catalog) asks the pager for
"page 7" and never touches the file directly.

Crash safety — the rollback journal (same idea as SQLite):
  Changes stay in memory until COMMIT. To commit we
    1. copy the ORIGINAL version of every page we're about to overwrite into
       a separate "<db>-journal" file, and fsync it to disk;
    2. write the new pages into the database file, and fsync;
    3. delete the journal.  <- this is the moment the commit "happens".
  If the power dies during step 2, the database file is half old/half new.
  But the journal still exists, so the next time the database opens it copies
  the original pages back — as if the commit never started. If the power dies
  before step 1 finishes, the database file was never touched at all.
"""
import os
import struct

PAGE_SIZE = 4096
JOURNAL_MAGIC = b"MYDBJRNL"


class Pager:
    def __init__(self, path):
        self.path = path
        self.journal_path = path + "-journal"
        self.file = open(path, "r+b" if os.path.exists(path) else "w+b")
        self._recover()                              # hot journal = crashed commit
        self.file.seek(0, os.SEEK_END)
        size = self.file.tell()
        if size % PAGE_SIZE:
            raise ValueError(f"{path} is not a mydb database file")
        self.num_pages = size // PAGE_SIZE
        self._committed_pages = self.num_pages
        self.cache = {}       # page number -> bytes (clean or dirty)
        self.dirty = set()    # pages changed since the last commit

    # ---- page access ----

    def read(self, n) -> bytes:
        if n in self.cache:
            return self.cache[n]
        if n >= self.num_pages:
            raise IndexError(f"page {n} does not exist")
        data = self._read_from_disk(n)
        self.cache[n] = data
        return data

    def write(self, n, data: bytes):
        if len(data) > PAGE_SIZE:
            raise ValueError("page overflow")
        self.cache[n] = bytes(data).ljust(PAGE_SIZE, b"\0")
        self.dirty.add(n)

    def allocate(self) -> int:
        n = self.num_pages
        self.num_pages += 1
        self.write(n, b"")
        return n

    # ---- transactions ----

    def commit(self):
        if not self.dirty:
            return
        # 1. save original pages to the journal
        with open(self.journal_path, "wb") as j:
            j.write(JOURNAL_MAGIC + struct.pack("<I", self._committed_pages))
            for n in sorted(self.dirty):
                if n < self._committed_pages:        # new pages have no original
                    j.write(struct.pack("<I", n) + self._read_from_disk(n))
            j.flush()
            os.fsync(j.fileno())
        # 2. write the new pages
        for n in sorted(self.dirty):
            self._write_to_disk(n, self.cache[n])
        self.file.flush()
        os.fsync(self.file.fileno())
        # 3. commit point
        os.remove(self.journal_path)
        self.dirty.clear()
        self._committed_pages = self.num_pages

    def rollback(self):
        for n in self.dirty:
            self.cache.pop(n, None)                  # forget changes; re-read from disk
        self.dirty.clear()
        self.num_pages = self._committed_pages

    def savepoint(self):
        """Snapshot of uncommitted state, so one failed statement can be undone
        without throwing away the rest of the transaction."""
        return self.num_pages, set(self.dirty), {n: self.cache[n] for n in self.dirty}

    def restore(self, sp):
        num_pages, dirty, saved = sp
        for n in self.dirty - dirty:
            self.cache.pop(n, None)
        self.cache.update(saved)
        self.dirty = set(dirty)
        self.num_pages = num_pages

    # ---- disk I/O ----

    def _read_from_disk(self, n) -> bytes:
        self.file.seek(n * PAGE_SIZE)
        return self.file.read(PAGE_SIZE)

    def _write_to_disk(self, n, data):
        self.file.seek(n * PAGE_SIZE)
        self.file.write(data)

    def _recover(self):
        if not os.path.exists(self.journal_path):
            return
        with open(self.journal_path, "rb") as j:
            data = j.read()
        if data[:8] == JOURNAL_MAGIC and len(data) >= 12:
            (orig_pages,) = struct.unpack_from("<I", data, 8)
            pos, entry = 12, 4 + PAGE_SIZE
            while pos + entry <= len(data):          # ignore a torn final entry
                (n,) = struct.unpack_from("<I", data, pos)
                self._write_to_disk(n, data[pos + 4:pos + entry])
                pos += entry
            self.file.truncate(orig_pages * PAGE_SIZE)
            self.file.flush()
            os.fsync(self.file.fileno())
        os.remove(self.journal_path)

    def close(self):
        self.file.close()
