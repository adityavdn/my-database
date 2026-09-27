"""The database engine: runs parsed statements against the storage layer.

File layout:
    page 0      header: magic bytes + page number of the catalog
    page 1      root of the catalog B-tree (the list of tables and indexes,
                like SQLite's sqlite_master)
    pages 2..   the B-trees for every table and index

Tables are B-trees keyed by rowid:          (rowid,)        -> encoded row
Indexes are B-trees keyed by value + rowid: (value, rowid)  -> empty
So "WHERE email = 'x'" on an indexed column finds the rowid in the index,
then fetches the row from the table, without reading any other rows.
"""
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import ast, record
from .btree import BTree, DuplicateKeyError
from .expressions import (EvalError, columns_used, contains_aggregate,
                          evaluate, truth)
from .pager import Pager
from .parser import parse
from .record import key_order, sort_key
from .tokenizer import SQLSyntaxError

MAGIC = b"MYDB-v1\0"


class DatabaseError(Exception):
    pass


@dataclass
class Result:
    columns: List[str] = field(default_factory=list)
    rows: List[tuple] = field(default_factory=list)
    message: Optional[str] = None


@dataclass
class IndexInfo:
    name: str
    table: str
    column: str
    unique: bool
    tree: BTree
    sql: str


@dataclass
class TableInfo:
    name: str
    columns: List[ast.ColumnDef]
    tree: BTree
    sql: str
    pk: Optional[int] = None                     # position of INTEGER PRIMARY KEY column
    indexes: Dict[str, IndexInfo] = field(default_factory=dict)

    def col_names(self):
        return [c.name.lower() for c in self.columns]


class Database:
    def __init__(self, path):
        self.pager = Pager(path)
        if self.pager.num_pages == 0:                 # brand-new file
            header = self.pager.allocate()
            catalog = BTree.create(self.pager)
            self.pager.write(header, MAGIC + struct.pack("<I", catalog.root))
            self.pager.commit()
        header = self.pager.read(0)
        if header[:8] != MAGIC:
            raise DatabaseError(f"{path} is not a mydb database file")
        self.catalog = BTree(self.pager, struct.unpack_from("<I", header, 8)[0])
        self.in_transaction = False
        self._load_catalog()

    # ================= public API =================

    def execute(self, sql) -> Result:
        """Run SQL (one or more statements); returns the last result."""
        results = self.execute_script(sql)
        return results[-1] if results else Result(message="OK")

    def execute_script(self, sql) -> List[Result]:
        try:
            statements = parse(sql)
        except SQLSyntaxError as e:
            raise DatabaseError(f"syntax error: {e}") from None
        return [self._run(s) for s in statements]

    def close(self):
        if self.in_transaction:
            self.pager.rollback()
        self.pager.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ================= statement dispatch =================

    def _run(self, stmt):
        if isinstance(stmt, ast.Begin):
            if self.in_transaction:
                raise DatabaseError("already in a transaction")
            self.in_transaction = True
            return Result(message="BEGIN")
        if isinstance(stmt, ast.Commit):
            if not self.in_transaction:
                raise DatabaseError("no transaction is active")
            self.pager.commit()
            self.in_transaction = False
            return Result(message="COMMIT")
        if isinstance(stmt, ast.Rollback):
            if not self.in_transaction:
                raise DatabaseError("no transaction is active")
            self.pager.rollback()
            self.in_transaction = False
            self._load_catalog()
            return Result(message="ROLLBACK")

        # Every other statement is atomic: if it fails halfway, undo its changes.
        savepoint = self.pager.savepoint()
        try:
            result = self._dispatch(stmt)
        except Exception as e:
            self.pager.restore(savepoint)
            self._load_catalog()
            if isinstance(e, DatabaseError):
                raise
            if isinstance(e, (EvalError, ValueError, TypeError, struct.error)):
                raise DatabaseError(str(e)) from None
            raise
        if not self.in_transaction:
            self.pager.commit()                       # autocommit mode
        return result

    def _dispatch(self, stmt):
        handlers = {
            ast.CreateTable: self._create_table, ast.DropTable: self._drop_table,
            ast.CreateIndex: self._create_index, ast.DropIndex: self._drop_index,
            ast.Insert: self._insert, ast.Select: self._select,
            ast.Update: self._update, ast.Delete: self._delete,
            ast.Explain: self._explain,
        }
        return handlers[type(stmt)](stmt)

    # ================= catalog =================

    def _load_catalog(self):
        self.tables: Dict[str, TableInfo] = {}
        self.indexes: Dict[str, IndexInfo] = {}
        entries = [record.decode(v)[0] for _, v in self.catalog.scan()]
        for kind, name, table, root, sql in entries:
            if kind == "table":
                (stmt,) = parse(sql)
                self.tables[name.lower()] = self._table_info(stmt, BTree(self.pager, root))
        for kind, name, table, root, sql in entries:
            if kind == "index":
                (stmt,) = parse(sql)
                idx = IndexInfo(name, table.lower(), stmt.column.lower(), stmt.unique,
                                BTree(self.pager, root), sql)
                self.indexes[name.lower()] = idx
                self.tables[table.lower()].indexes[name.lower()] = idx

    @staticmethod
    def _table_info(stmt, tree):
        info = TableInfo(stmt.name, stmt.columns, tree, stmt.sql)
        for i, c in enumerate(stmt.columns):
            if c.primary_key and c.type == "INTEGER":
                info.pk = i
        return info

    def _catalog_put(self, kind, name, table, root, sql):
        self.catalog.insert((name.lower(),), record.encode((kind, name, table, root, sql)))

    def _table(self, name) -> TableInfo:
        t = self.tables.get(name.lower())
        if t is None:
            raise DatabaseError(f"no such table: {name}")
        return t

    def _name_taken(self, name):
        return name.lower() in self.tables or name.lower() in self.indexes

    # ================= DDL =================

    def _create_table(self, s: ast.CreateTable):
        if self._name_taken(s.name):
            if s.if_not_exists:
                return Result(message="table already exists (skipped)")
            raise DatabaseError(f"table {s.name} already exists")
        names = [c.name.lower() for c in s.columns]
        if len(set(names)) != len(names):
            raise DatabaseError("duplicate column name")
        if "rowid" in names:
            raise DatabaseError("'rowid' is a reserved column name")
        pks = [c for c in s.columns if c.primary_key]
        if len(pks) > 1:
            raise DatabaseError("a table can only have one PRIMARY KEY column")
        tree = BTree.create(self.pager)
        self._catalog_put("table", s.name, s.name, tree.root, s.sql)
        info = self._table_info(s, tree)
        self.tables[s.name.lower()] = info
        # UNIQUE columns (and non-integer primary keys) get an automatic index
        for c in s.columns:
            if c.unique or (c.primary_key and c.type != "INTEGER"):
                idx_name = f"autoindex_{s.name}_{c.name}"
                self._create_index(ast.CreateIndex(
                    idx_name, s.name, c.name, True, False,
                    f"CREATE UNIQUE INDEX {idx_name} ON {s.name} ({c.name})"))
        return Result(message=f"table {s.name} created")

    def _drop_table(self, s: ast.DropTable):
        if s.name.lower() not in self.tables:
            if s.if_exists:
                return Result(message="table does not exist (skipped)")
            raise DatabaseError(f"no such table: {s.name}")
        t = self.tables.pop(s.name.lower())
        for idx_name in list(t.indexes):
            self.catalog.delete((idx_name,))
            del self.indexes[idx_name]
        self.catalog.delete((s.name.lower(),))
        return Result(message=f"table {t.name} dropped")

    def _create_index(self, s: ast.CreateIndex):
        if self._name_taken(s.name):
            if s.if_not_exists:
                return Result(message="index already exists (skipped)")
            raise DatabaseError(f"{s.name} already exists")
        t = self._table(s.table)
        col = s.column.lower()
        if col not in t.col_names():
            raise DatabaseError(f"no such column: {s.column}")
        idx = IndexInfo(s.name, t.name.lower(), col, s.unique, BTree.create(self.pager), s.sql)
        # fill the new index from the rows already in the table
        for rowid, row in self._scan(t):
            self._index_add(t, idx, row[col], rowid)
        self._catalog_put("index", s.name, t.name, idx.tree.root, s.sql)
        self.indexes[s.name.lower()] = idx
        t.indexes[s.name.lower()] = idx
        return Result(message=f"index {s.name} created")

    def _drop_index(self, s: ast.DropIndex):
        idx = self.indexes.get(s.name.lower())
        if idx is None:
            if s.if_exists:
                return Result(message="index does not exist (skipped)")
            raise DatabaseError(f"no such index: {s.name}")
        if s.name.lower().startswith("autoindex_"):
            raise DatabaseError("cannot drop an index created by a UNIQUE constraint")
        del self.indexes[s.name.lower()]
        del self.tables[idx.table].indexes[s.name.lower()]
        self.catalog.delete((s.name.lower(),))
        return Result(message=f"index {s.name} dropped")

    # ================= row storage helpers =================

    def _decode_row(self, t, rowid, data):
        values = record.decode(data)[0]
        row = dict(zip(t.col_names(), values))
        row["rowid"] = rowid
        return row

    def _encode_row(self, t, row):
        return record.encode(tuple(row[c] for c in t.col_names()))

    def _scan(self, t):
        for (rowid,), data in t.tree.scan():
            yield rowid, self._decode_row(t, rowid, data)

    def _get(self, t, rowid):
        data = t.tree.get((rowid,))
        return None if data is None else self._decode_row(t, rowid, data)

    def _index_add(self, t, idx, value, rowid):
        if idx.unique and value is not None and self._index_lookup(idx, value):
            raise DatabaseError(f"UNIQUE constraint failed: {t.name}.{idx.column}")
        idx.tree.insert((value, rowid), b"")

    def _index_lookup(self, idx, value):
        rowids = []
        for key, _ in idx.tree.scan((value,)):
            if key_order(key[:1]) != key_order((value,)):
                break
            rowids.append(key[1])
        return rowids

    def _coerce(self, t, col, v):
        where = f"{t.name}.{col.name}"
        if v is None:
            if col.not_null or (col.primary_key and col.type != "INTEGER"):
                raise DatabaseError(f"NOT NULL constraint failed: {where}")
            return None
        if col.type == "INTEGER":
            if isinstance(v, float) and v.is_integer():
                return int(v)
            if isinstance(v, int):
                return v
        elif col.type == "REAL":
            if isinstance(v, (int, float)):
                return float(v)
        elif col.type == "TEXT":
            if isinstance(v, str):
                return v
        else:
            return v
        raise DatabaseError(f"type mismatch: cannot store {v!r} in {col.type} column {where}")

    def _next_rowid(self, t):
        last = t.tree.last_key()
        return 1 if last is None else last[0] + 1

    # ================= DML =================

    def _insert(self, s: ast.Insert):
        t = self._table(s.table)
        names = t.col_names()
        target = [c.lower() for c in s.columns] if s.columns else names
        for c in target:
            if c not in names:
                raise DatabaseError(f"table {t.name} has no column named {c}")
        if len(set(target)) != len(target):
            raise DatabaseError("column listed twice")
        for values in s.rows:
            if len(values) != len(target):
                raise DatabaseError(f"expected {len(target)} values, got {len(values)}")
            row = {c.name.lower(): (evaluate(c.default, {}) if c.default else None)
                   for c in t.columns}
            for c, e in zip(target, values):
                row[c] = evaluate(e, {})
            for c in t.columns:
                row[c.name.lower()] = self._coerce(t, c, row[c.name.lower()])
            if t.pk is not None and row[names[t.pk]] is not None:
                rowid = row[names[t.pk]]
            else:
                rowid = self._next_rowid(t)
                if t.pk is not None:
                    row[names[t.pk]] = rowid
            try:
                t.tree.insert((rowid,), self._encode_row(t, row))
            except DuplicateKeyError:
                raise DatabaseError(f"UNIQUE constraint failed: {t.name}.{names[t.pk]}") from None
            for idx in t.indexes.values():
                self._index_add(t, idx, row[idx.column], rowid)
        return Result(message=f"{len(s.rows)} row(s) inserted")

    def _update(self, s: ast.Update):
        t = self._table(s.table)
        names = t.col_names()
        for col, _ in s.assignments:
            if col.lower() not in names:
                raise DatabaseError(f"no such column: {col}")
        matches = list(self._matching_rows(t, s.where))   # collect first, then modify
        for rowid, old in matches:
            new = dict(old)
            for col, e in s.assignments:
                new[col.lower()] = evaluate(e, old)
            for c in t.columns:
                new[c.name.lower()] = self._coerce(t, c, new[c.name.lower()])
            new_rowid = rowid
            if t.pk is not None:
                if new[names[t.pk]] is None:
                    raise DatabaseError(f"NOT NULL constraint failed: {t.name}.{names[t.pk]}")
                new_rowid = new[names[t.pk]]
            if new_rowid != rowid:
                if t.tree.get((new_rowid,)) is not None:
                    raise DatabaseError(f"UNIQUE constraint failed: {t.name}.{names[t.pk]}")
                t.tree.delete((rowid,))
            new["rowid"] = new_rowid
            t.tree.insert((new_rowid,), self._encode_row(t, new), replace=True)
            for idx in t.indexes.values():
                if key_order((old[idx.column],)) != key_order((new[idx.column],)) or new_rowid != rowid:
                    idx.tree.delete((old[idx.column], rowid))
                    self._index_add(t, idx, new[idx.column], new_rowid)
        return Result(message=f"{len(matches)} row(s) updated")

    def _delete(self, s: ast.Delete):
        t = self._table(s.table)
        matches = list(self._matching_rows(t, s.where))
        for rowid, row in matches:
            t.tree.delete((rowid,))
            for idx in t.indexes.values():
                idx.tree.delete((row[idx.column], rowid))
        return Result(message=f"{len(matches)} row(s) deleted")

    # ================= query planning =================

    def _plan(self, t, where):
        """Pick the cheapest way to find rows matching WHERE.

        Looks for a condition 'column = constant' on the primary key or an
        indexed column. Otherwise falls back to reading every row (a scan).
        The full WHERE is still checked on every candidate row, so the plan
        only has to narrow things down, never be exact.
        """
        terms = []

        def split_and(e):
            if isinstance(e, ast.Binary) and e.op == "AND":
                split_and(e.left)
                split_and(e.right)
            elif e is not None:
                terms.append(e)

        split_and(where)
        equalities = []
        for term in terms:
            if isinstance(term, ast.Binary) and term.op == "=":
                for col, other in ((term.left, term.right), (term.right, term.left)):
                    if (isinstance(col, ast.Column) and not list(columns_used(other))
                            and not contains_aggregate(other)):
                        equalities.append((col.name.lower(), other))
        pk_name = t.col_names()[t.pk] if t.pk is not None else None
        for col, value_expr in equalities:
            if col in ("rowid", pk_name):
                return ("rowid", evaluate(value_expr, {}),
                        f"SEARCH {t.name} USING PRIMARY KEY ({col}=?)")
        for col, value_expr in equalities:
            for idx in t.indexes.values():
                if idx.column == col:
                    return ("index", (idx, evaluate(value_expr, {})),
                            f"SEARCH {t.name} USING INDEX {idx.name} ({col}=?)")
        return ("scan", None, f"SCAN {t.name} (reads every row)")

    def _candidate_rows(self, t, plan):
        kind, arg, _ = plan
        if kind == "rowid":
            if isinstance(arg, float) and arg.is_integer():
                arg = int(arg)
            if isinstance(arg, int):
                row = self._get(t, arg)
                if row is not None:
                    yield arg, row
        elif kind == "index":
            idx, value = arg
            if value is not None:
                for rowid in self._index_lookup(idx, value):
                    yield rowid, self._get(t, rowid)
        else:
            yield from self._scan(t)

    def _matching_rows(self, t, where):
        for rowid, row in self._candidate_rows(t, self._plan(t, where)):
            if where is None or truth(evaluate(where, row)) is True:
                yield rowid, row

    def _explain(self, s: ast.Explain):
        stmt = s.statement
        if isinstance(stmt, (ast.Select, ast.Update, ast.Delete)) and stmt.table:
            detail = self._plan(self._table(stmt.table), stmt.where)[2]
        elif isinstance(stmt, ast.Select):
            detail = "CONSTANT ROW (no table)"
        else:
            detail = f"{type(stmt).__name__.upper()} (no query plan)"
        return Result(["plan"], [(detail,)])

    # ================= SELECT =================

    def _select(self, s: ast.Select):
        if s.table is not None:
            t = self._table(s.table)
            rows = [row for _, row in self._matching_rows(t, s.where)]
            all_columns = [c.name for c in t.columns]
            empty_row = {c: None for c in t.col_names()}
            empty_row["rowid"] = None
        else:
            t, all_columns, empty_row = None, [], {}
            rows = [{}]
            if s.where is not None and truth(evaluate(s.where, {})) is not True:
                rows = []

        # expand SELECT *
        exprs, headers = [], []
        for e, alias in s.items:
            if isinstance(e, ast.Star):
                if t is None:
                    raise DatabaseError("SELECT * needs a FROM clause")
                exprs += [ast.Column(c) for c in all_columns]
                headers += all_columns
            else:
                exprs.append(e)
                headers.append(alias or ast.to_sql(e))

        aggregate_mode = bool(s.group_by) or s.having is not None or any(
            contains_aggregate(e) for e in exprs)

        # each context is (row, group); group is None unless aggregating
        if aggregate_mode:
            if s.group_by:
                groups = {}
                for r in rows:
                    key = tuple(sort_key(evaluate(g, r)) for g in s.group_by)
                    groups.setdefault(key, []).append(r)
                contexts = [(g[0], g) for g in groups.values()]
            else:
                contexts = [(rows[0] if rows else empty_row, rows)]
            if s.having is not None:
                contexts = [(r, g) for r, g in contexts if truth(evaluate(s.having, r, g)) is True]
        else:
            contexts = [(r, None) for r in rows]

        output = [(tuple(evaluate(e, r, g) for e in exprs), r, g) for r, g in contexts]

        if s.distinct:
            seen, unique = set(), []
            for item in output:
                k = tuple(sort_key(v) for v in item[0])
                if k not in seen:
                    seen.add(k)
                    unique.append(item)
            output = unique

        if s.order_by:
            lowered = [h.lower() for h in headers]

            def order_value(expr, item):
                out, r, g = item
                if isinstance(expr, ast.Literal) and isinstance(expr.value, int):
                    if not 1 <= expr.value <= len(out):
                        raise DatabaseError(f"ORDER BY column {expr.value} out of range")
                    return out[expr.value - 1]
                if isinstance(expr, ast.Column) and expr.name.lower() in lowered \
                        and expr.name.lower() not in r:
                    return out[lowered.index(expr.name.lower())]
                return evaluate(expr, r, g)

            # stable sort by each key, last key first
            for expr, desc in reversed(s.order_by):
                output.sort(key=lambda item: sort_key(order_value(expr, item)), reverse=desc)

        if s.offset is not None:
            output = output[max(0, int(evaluate(s.offset, {}) or 0)):]
        if s.limit is not None:
            limit = evaluate(s.limit, {})
            if limit is not None and limit >= 0:
                output = output[:int(limit)]

        return Result(headers, [o for o, _, _ in output])

    # ================= introspection (for the shell) =================

    def table_names(self):
        return sorted(t.name for t in self.tables.values())

    def schema(self, table=None):
        out = []
        for t in sorted(self.tables.values(), key=lambda t: t.name.lower()):
            if table and t.name.lower() != table.lower():
                continue
            out.append(t.sql + ";")
            for idx in t.indexes.values():
                if not idx.name.lower().startswith("autoindex_"):
                    out.append(idx.sql + ";")
        return out
