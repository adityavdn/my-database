"""The syntax tree the parser produces. Each SQL statement becomes one of these."""
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


# ---- expressions ----

@dataclass
class Literal:
    value: Any


@dataclass
class Column:
    name: str


@dataclass
class Unary:
    op: str          # '-', '+', 'NOT'
    operand: Any


@dataclass
class Binary:
    op: str          # + - * / % || = != < <= > >= AND OR
    left: Any
    right: Any


@dataclass
class IsNull:
    operand: Any
    negated: bool


@dataclass
class Like:
    operand: Any
    pattern: Any
    negated: bool


@dataclass
class InList:
    operand: Any
    items: List[Any]
    negated: bool


@dataclass
class Between:
    operand: Any
    low: Any
    high: Any
    negated: bool


@dataclass
class Func:
    name: str        # upper-case
    args: List[Any]
    star: bool = False       # COUNT(*)
    distinct: bool = False   # COUNT(DISTINCT x)


@dataclass
class Star:
    pass


# ---- statements ----

@dataclass
class ColumnDef:
    name: str
    type: str                # INTEGER, REAL, TEXT, ANY
    primary_key: bool = False
    not_null: bool = False
    unique: bool = False
    default: Any = None


@dataclass
class CreateTable:
    name: str
    columns: List[ColumnDef]
    if_not_exists: bool = False
    sql: str = ""


@dataclass
class DropTable:
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex:
    name: str
    table: str
    column: str
    unique: bool = False
    if_not_exists: bool = False
    sql: str = ""


@dataclass
class DropIndex:
    name: str
    if_exists: bool = False


@dataclass
class Insert:
    table: str
    columns: Optional[List[str]]
    rows: List[List[Any]]


@dataclass
class Select:
    items: List[Tuple[Any, Optional[str]]]     # (expression or Star, alias)
    table: Optional[str] = None
    where: Any = None
    group_by: List[Any] = field(default_factory=list)
    having: Any = None
    order_by: List[Tuple[Any, bool]] = field(default_factory=list)   # (expr, descending)
    limit: Any = None
    offset: Any = None
    distinct: bool = False


@dataclass
class Update:
    table: str
    assignments: List[Tuple[str, Any]]
    where: Any = None


@dataclass
class Delete:
    table: str
    where: Any = None


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


@dataclass
class Explain:
    statement: Any


# ---- turning an expression back into text (used for result column names) ----

def to_sql(e):
    if isinstance(e, Literal):
        v = e.value
        if v is None:
            return "NULL"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return str(v)
    if isinstance(e, Column):
        return e.name
    if isinstance(e, Star):
        return "*"
    if isinstance(e, Unary):
        return f"NOT {to_sql(e.operand)}" if e.op == "NOT" else f"{e.op}{to_sql(e.operand)}"
    if isinstance(e, Binary):
        return f"{to_sql(e.left)} {e.op} {to_sql(e.right)}"
    if isinstance(e, IsNull):
        return f"{to_sql(e.operand)} IS {'NOT ' if e.negated else ''}NULL"
    if isinstance(e, Like):
        return f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}LIKE {to_sql(e.pattern)}"
    if isinstance(e, InList):
        return f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}IN ({', '.join(map(to_sql, e.items))})"
    if isinstance(e, Between):
        return f"{to_sql(e.operand)} {'NOT ' if e.negated else ''}BETWEEN {to_sql(e.low)} AND {to_sql(e.high)}"
    if isinstance(e, Func):
        inner = "*" if e.star else ("DISTINCT " if e.distinct else "") + ", ".join(map(to_sql, e.args))
        return f"{e.name}({inner})"
    return "?"
