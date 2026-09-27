"""Interactive SQL shell.   Usage:  python3 -m mydb [database-file]"""
import sys
import time

from . import __version__
from .engine import Database, DatabaseError
from .tokenizer import SQLSyntaxError, tokenize

HELP = """\
Type SQL statements ending with ';'  (they can span several lines).

  .tables            list tables
  .schema [TABLE]    show CREATE statements
  .help              show this help
  .quit              exit

Try:  EXPLAIN SELECT * FROM users WHERE id = 1;"""


def format_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def format_table(columns, rows):
    cells = [[format_value(v) for v in r] for r in rows]
    widths = [len(c) for c in columns]
    for r in cells:
        widths = [max(w, len(v)) for w, v in zip(widths, r)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [line, "| " + " | ".join(c.ljust(w) for c, w in zip(columns, widths)) + " |", line]
    for r, raw in zip(cells, rows):
        out.append("| " + " | ".join(
            v.rjust(w) if isinstance(x, (int, float)) else v.ljust(w)
            for v, w, x in zip(r, widths, raw)) + " |")
    out.append(line)
    return "\n".join(out)


def statement_complete(text):
    try:
        tokens = tokenize(text)
    except SQLSyntaxError:
        return False                        # e.g. a string that isn't closed yet
    return len(tokens) > 1 and tokens[-2].kind == "OP" and tokens[-2].value == ";"


def run(db, sql):
    start = time.perf_counter()
    try:
        results = db.execute_script(sql)
    except DatabaseError as e:
        print(f"Error: {e}")
        return
    ms = (time.perf_counter() - start) * 1000
    for r in results:
        if r.columns:
            print(format_table(r.columns, r.rows))
            print(f"({len(r.rows)} row{'s' if len(r.rows) != 1 else ''}, {ms:.1f} ms)")
        elif r.message:
            print(r.message)


def meta_command(db, line):
    parts = line.split()
    cmd = parts[0].lower()
    if cmd in (".quit", ".exit"):
        return False
    if cmd == ".help":
        print(HELP)
    elif cmd == ".tables":
        print("  ".join(db.table_names()) or "(no tables)")
    elif cmd == ".schema":
        print("\n".join(db.schema(parts[1] if len(parts) > 1 else None)) or "(no tables)")
    else:
        print(f"unknown command {cmd} — try .help")
    return True


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "mydb.db"
    interactive = sys.stdin.isatty()
    db = Database(path)
    if interactive:
        print(f"MyDB {__version__} — connected to {path}")
        print("Enter SQL ending with ';'   Type .help for help, .quit to exit.")
    buffer = ""
    try:
        while True:
            prompt = ("mydb> " if not buffer else "  ...> ") if interactive else ""
            try:
                line = input(prompt)
            except EOFError:
                break
            except KeyboardInterrupt:
                print()
                buffer = ""
                continue
            if not buffer and line.strip().startswith("."):
                if not meta_command(db, line.strip()):
                    break
                continue
            buffer += line + "\n"
            if statement_complete(buffer):
                run(db, buffer)
                buffer = ""
        if buffer.strip():
            run(db, buffer)
    finally:
        db.close()


if __name__ == "__main__":
    main()
