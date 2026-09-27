"""Evaluating expressions against a row, with SQL's NULL rules.

SQL uses three-valued logic: a comparison involving NULL is neither true nor
false, it's NULL ("unknown"). So  NULL = NULL  is NULL, and WHERE only keeps
rows where the condition is exactly TRUE.
"""
import re

from . import ast
from .record import sort_key


class EvalError(Exception):
    pass


AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


def truth(v):
    """SQL truthiness: None stays None (unknown)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v != 0
    try:
        return float(v) != 0
    except ValueError:
        return False


def compare(a, b):
    ka, kb = sort_key(a), sort_key(b)
    return (ka > kb) - (ka < kb)


def contains_aggregate(e):
    if isinstance(e, ast.Func) and e.name in AGGREGATES:
        return True
    for child in children(e):
        if contains_aggregate(child):
            return True
    return False


def columns_used(e):
    if isinstance(e, ast.Column):
        yield e.name.lower()
    for child in children(e):
        yield from columns_used(child)


def children(e):
    if isinstance(e, ast.Unary):
        return [e.operand]
    if isinstance(e, ast.Binary):
        return [e.left, e.right]
    if isinstance(e, ast.IsNull):
        return [e.operand]
    if isinstance(e, ast.Like):
        return [e.operand, e.pattern]
    if isinstance(e, ast.InList):
        return [e.operand, *e.items]
    if isinstance(e, ast.Between):
        return [e.operand, e.low, e.high]
    if isinstance(e, ast.Func):
        return list(e.args)
    return []


def like_to_regex(pattern):
    out = []
    for ch in pattern:
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def evaluate(e, row, group=None):
    """row: dict of lower-case column name -> value.
    group: list of rows, when evaluating aggregates (COUNT, SUM, ...)."""
    if isinstance(e, ast.Literal):
        return e.value

    if isinstance(e, ast.Column):
        key = e.name.lower()
        if key not in row:
            raise EvalError(f"no such column: {e.name}")
        return row[key]

    if isinstance(e, ast.Unary):
        v = evaluate(e.operand, row, group)
        if e.op == "NOT":
            t = truth(v)
            return None if t is None else int(not t)
        if v is None:
            return None
        if not isinstance(v, (int, float)):
            raise EvalError(f"cannot negate {v!r}")
        return -v

    if isinstance(e, ast.Binary):
        op = e.op
        if op in ("AND", "OR"):
            left = truth(evaluate(e.left, row, group))
            # short-circuit where the answer is already known
            if op == "AND" and left is False:
                return 0
            if op == "OR" and left is True:
                return 1
            right = truth(evaluate(e.right, row, group))
            if op == "AND":
                if right is False:
                    return 0
                return None if (left is None or right is None) else 1
            if right is True:
                return 1
            return None if (left is None or right is None) else 0

        a, b = evaluate(e.left, row, group), evaluate(e.right, row, group)
        if a is None or b is None:
            return None
        if op == "||":
            return f"{a}{b}"
        if op in ("=", "!=", "<", "<=", ">", ">="):
            c = compare(a, b)
            return int({"=": c == 0, "!=": c != 0, "<": c < 0,
                        "<=": c <= 0, ">": c > 0, ">=": c >= 0}[op])
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            raise EvalError(f"cannot apply '{op}' to {a!r} and {b!r}")
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%"):
            if b == 0:
                return None                     # SQLite returns NULL for x/0
            if isinstance(a, int) and isinstance(b, int):
                q = abs(a) // abs(b) * (1 if (a >= 0) == (b >= 0) else -1)   # truncate toward 0
                return q if op == "/" else a - b * q
            return a / b if op == "/" else a % b

    if isinstance(e, ast.IsNull):
        v = evaluate(e.operand, row, group)
        return int((v is None) != e.negated)

    if isinstance(e, ast.Like):
        v, p = evaluate(e.operand, row, group), evaluate(e.pattern, row, group)
        if v is None or p is None:
            return None
        return int(bool(like_to_regex(str(p)).fullmatch(str(v))) != e.negated)

    if isinstance(e, ast.InList):
        v = evaluate(e.operand, row, group)
        if v is None:
            return None
        values = [evaluate(i, row, group) for i in e.items]
        if any(x is not None and compare(v, x) == 0 for x in values):
            return int(not e.negated)
        if None in values:
            return None
        return int(e.negated)

    if isinstance(e, ast.Between):
        v = evaluate(e.operand, row, group)
        lo, hi = evaluate(e.low, row, group), evaluate(e.high, row, group)
        if v is None or lo is None or hi is None:
            return None
        return int((compare(v, lo) >= 0 and compare(v, hi) <= 0) != e.negated)

    if isinstance(e, ast.Func):
        if e.name in AGGREGATES:
            return aggregate(e, group)
        return scalar_function(e, [evaluate(a, row, group) for a in e.args])

    raise EvalError(f"cannot evaluate {type(e).__name__}")


def aggregate(e, group):
    if group is None:
        raise EvalError(f"{e.name}() is not allowed here")
    if e.star:
        if e.name != "COUNT":
            raise EvalError(f"{e.name}(*) is not valid")
        return len(group)
    if len(e.args) != 1:
        raise EvalError(f"{e.name}() takes exactly one argument")
    values = [evaluate(e.args[0], r) for r in group]
    values = [v for v in values if v is not None]
    if e.distinct:
        seen, unique = set(), []
        for v in values:
            if sort_key(v) not in seen:
                seen.add(sort_key(v))
                unique.append(v)
        values = unique
    if e.name == "COUNT":
        return len(values)
    if not values:
        return None
    if e.name in ("MIN", "MAX"):
        pick = min if e.name == "MIN" else max
        return pick(values, key=sort_key)
    if any(not isinstance(v, (int, float)) for v in values):
        raise EvalError(f"{e.name}() needs numbers")
    total = sum(values)
    return total if e.name == "SUM" else total / len(values)


def scalar_function(e, args):
    name, n = e.name, len(args)

    def need(count):
        if n != count:
            raise EvalError(f"{name}() takes {count} argument(s)")

    if name in ("UPPER", "LOWER", "LENGTH", "ABS", "TYPEOF"):
        need(1)
        v = args[0]
        if name == "TYPEOF":
            return {type(None): "null", int: "integer", float: "real", str: "text"}[type(v)]
        if v is None:
            return None
        if name == "UPPER":
            return str(v).upper()
        if name == "LOWER":
            return str(v).lower()
        if name == "LENGTH":
            return len(str(v))
        return abs(v)
    if name == "ROUND":
        if n not in (1, 2):
            raise EvalError("ROUND() takes 1 or 2 arguments")
        if args[0] is None:
            return None
        return float(round(args[0], int(args[1]) if n == 2 else 0))
    if name in ("COALESCE", "IFNULL"):
        return next((a for a in args if a is not None), None)
    raise EvalError(f"no such function: {name}")
