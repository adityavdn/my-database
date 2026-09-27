# MyDB

A SQL database built from scratch in pure Python, with no dependencies.

It stores data in a single file using **fixed-size pages** and **B+ trees** (the
same design SQLite and PostgreSQL use), parses and runs real SQL, uses indexes
to speed up queries, and survives crashes mid-write thanks to a **rollback journal**.

```
mydb> SELECT course, COUNT(*) AS students, ROUND(AVG(score), 1) AS avg_score
  ...> FROM students GROUP BY course ORDER BY avg_score DESC;
+--------------+----------+-----------+
| course       | students | avg_score |
+--------------+----------+-----------+
| Data Science |        2 |      90.2 |
| Computing    |        2 |      77.8 |
| Maths        |        2 |      67.0 |
+--------------+----------+-----------+

mydb> EXPLAIN SELECT * FROM students WHERE course = 'Maths';
| SEARCH students USING INDEX idx_course (course=?) |
```

## Quick start

Needs only Python 3.8+.

```bash
python3 -m mydb school.db                        # interactive SQL shell
python3 -m mydb demo.db < examples/demo.sql      # run the demo script
```

Or use it from Python:

```python
from mydb import Database

with Database("app.db") as db:
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    db.execute("INSERT INTO users (name) VALUES ('Adi'), ('Priya')")
    print(db.execute("SELECT * FROM users").rows)   # [(1, 'Adi'), (2, 'Priya')]
```

## What it supports

| Area | Features |
|---|---|
| **Tables** | `CREATE TABLE`, `DROP TABLE`, types `INTEGER` / `REAL` / `TEXT`, `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT` |
| **Queries** | `SELECT` with `WHERE`, `ORDER BY`, `LIMIT`/`OFFSET`, `DISTINCT`, `GROUP BY`, `HAVING`, aliases |
| **Expressions** | `+ - * / %`, `\|\|`, comparisons, `AND`/`OR`/`NOT`, `IS NULL`, `LIKE`, `IN`, `BETWEEN`, SQL NULL logic |
| **Functions** | `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `COUNT(DISTINCT …)`, `UPPER`, `LOWER`, `LENGTH`, `ABS`, `ROUND`, `COALESCE`, `TYPEOF` |
| **Changes** | `INSERT` (multi-row), `UPDATE`, `DELETE` |
| **Indexes** | `CREATE [UNIQUE] INDEX`, automatic indexes for `UNIQUE` columns, a query planner, and `EXPLAIN` |
| **Transactions** | `BEGIN` / `COMMIT` / `ROLLBACK`, atomic statements, crash recovery |

## How it works

```
 SQL text
    │  tokenizer.py     "SELECT name FROM users"  →  [SELECT] [name] [FROM] [users]
    ▼
 parser.py              tokens → syntax tree (recursive descent)
    │
    ▼
 engine.py              query planner + executor, catalog of tables and indexes
    │                   (expressions.py evaluates WHERE, functions, aggregates)
    ▼
 btree.py               every table and index is a B+ tree; one node = one page
    │
    ▼
 pager.py               reads/writes 4 KB pages, caches them, runs transactions
    │                   and the rollback journal
    ▼
 database file          [page 0: header][page 1: catalog][page 2..: tables & indexes]
```

**Storage.** The file is an array of 4 KB pages. Each table is a B+ tree keyed by
`rowid`. Leaves hold the rows and link to the next leaf, so a full scan walks the leaves in
order, and a lookup by key reads only a few pages even with thousands of rows.

**Indexes.** An index is a second B+ tree keyed by `(column value, rowid)`. For
`WHERE email = 'x'`, the planner finds the rowid in the index and fetches just that
row, instead of scanning the whole table. `EXPLAIN` shows which plan it picked.

**Crash safety.** Changes stay in memory until `COMMIT`. To commit, the original
copies of the pages about to change are first written to a `-journal` file and flushed
to disk, then the new pages are written, then the journal is deleted. If the power fails
partway through, the next startup finds the journal and restores the original pages.
`tests/test_crash.py` simulates exactly this.

**NULL logic.** Comparisons with `NULL` give `NULL` (unknown), and `WHERE` only keeps
rows where the condition is true. So `NULL = NULL` is not true, as in real SQL.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

There are 28 tests:

- Record encoding
- B+ tree behaviour (thousands of random inserts, deletes, page splits)
- Every SQL feature
- Constraint errors, and whether a failed statement leaves anything behind
- Query plans
- Transactions and persistence
- A simulated crash in the middle of a commit

## Limitations and roadmap

- [ ] `JOIN` across tables
- [ ] Multi-column indexes, and range scans (`<`, `>`) using an index
- [ ] Reusing space from deleted rows and dropped tables (free-page list, `VACUUM`)
- [ ] Rebalancing B+ tree nodes after deletes
- [ ] Rows larger than about 1 KB (overflow pages)
- [ ] Write-ahead log (WAL) so readers don't block writers
