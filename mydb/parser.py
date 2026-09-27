"""Recursive-descent parser: tokens -> syntax tree.

Each grammar rule is one method. For expressions, each precedence level is one
method that calls the next-higher level, so  1 + 2 * 3  parses as  1 + (2 * 3):

    expr        := or
    or          := and ( OR and )*
    and         := not ( AND not )*
    not         := NOT not | comparison
    comparison  := additive [ (= | != | < | ...) additive | IS [NOT] NULL
                             | [NOT] LIKE ... | [NOT] IN (...) | [NOT] BETWEEN ... ]
    additive    := multiply ( (+ | - | ||) multiply )*
    multiply    := unary ( (* | / | %) unary )*
    unary       := - unary | + unary | primary
    primary     := number | 'string' | NULL | TRUE | FALSE | ( expr )
                 | name ( args ) | name [. name]
"""
from . import ast
from .tokenizer import SQLSyntaxError, tokenize

# Words that can't be used as bare table/column names.
RESERVED = {
    "SELECT", "FROM", "WHERE", "INSERT", "INTO", "VALUES", "CREATE", "DROP", "UPDATE",
    "SET", "DELETE", "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "BETWEEN", "ORDER",
    "BY", "GROUP", "HAVING", "LIMIT", "OFFSET", "AS", "ON", "TABLE", "INDEX", "DISTINCT",
    "TRUE", "FALSE", "PRIMARY", "UNIQUE", "DEFAULT",
}

TYPE_NAMES = {
    "INT": "INTEGER", "INTEGER": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
    "BOOLEAN": "INTEGER", "BOOL": "INTEGER",
    "REAL": "REAL", "FLOAT": "REAL", "DOUBLE": "REAL", "NUMERIC": "REAL", "DECIMAL": "REAL",
    "TEXT": "TEXT", "VARCHAR": "TEXT", "CHAR": "TEXT", "STRING": "TEXT",
}

COMPARISON_OPS = {"=": "=", "==": "=", "!=": "!=", "<>": "!=", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def parse(sql):
    """Parse one or more ';'-separated statements."""
    return Parser(sql).parse_all()


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.i = 0

    # ---- token helpers ----

    def peek(self, ahead=0):
        return self.tokens[min(self.i + ahead, len(self.tokens) - 1)]

    def advance(self):
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def is_kw(self, word, ahead=0):
        tok = self.peek(ahead)
        return tok.kind == "WORD" and tok.value.upper() == word

    def accept_kw(self, *words):
        for w in words:
            if self.is_kw(w):
                self.advance()
                return w
        return None

    def expect_kw(self, word):
        if not self.accept_kw(word):
            self.error(f"expected {word}")

    def is_op(self, op):
        tok = self.peek()
        return tok.kind == "OP" and tok.value == op

    def accept_op(self, op):
        if self.is_op(op):
            self.advance()
            return True
        return False

    def expect_op(self, op):
        if not self.accept_op(op):
            self.error(f"expected '{op}'")

    def ident(self, what="name"):
        tok = self.peek()
        if tok.kind == "IDENT" or (tok.kind == "WORD" and tok.value.upper() not in RESERVED):
            self.advance()
            return tok.value
        self.error(f"expected {what}")

    def error(self, msg):
        tok = self.peek()
        near = "end of input" if tok.kind == "EOF" else repr(self.sql[tok.pos:tok.end])
        raise SQLSyntaxError(f"{msg} near {near}")

    # ---- statements ----

    def parse_all(self):
        statements = []
        while self.peek().kind != "EOF":
            if self.accept_op(";"):
                continue
            statements.append(self.statement())
            if self.peek().kind != "EOF":
                self.expect_op(";")
        return statements

    def statement(self):
        start = self.peek().pos
        if self.accept_kw("EXPLAIN"):
            return ast.Explain(self.statement())
        if self.is_kw("SELECT"):
            return self.select()
        if self.accept_kw("INSERT"):
            return self.insert()
        if self.accept_kw("UPDATE"):
            return self.update()
        if self.accept_kw("DELETE"):
            return self.delete()
        if self.accept_kw("CREATE"):
            stmt = self.create()
            stmt.sql = self.sql[start:self.tokens[self.i - 1].end]
            return stmt
        if self.accept_kw("DROP"):
            return self.drop()
        if self.accept_kw("BEGIN"):
            self.accept_kw("TRANSACTION")
            return ast.Begin()
        if self.accept_kw("COMMIT", "END"):
            self.accept_kw("TRANSACTION")
            return ast.Commit()
        if self.accept_kw("ROLLBACK"):
            self.accept_kw("TRANSACTION")
            return ast.Rollback()
        self.error("expected a statement")

    def if_exists(self):
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            return True
        return False

    def if_not_exists(self):
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            return True
        return False

    def create(self):
        if self.accept_kw("TABLE"):
            return self.create_table()
        unique = bool(self.accept_kw("UNIQUE"))
        if self.accept_kw("INDEX"):
            ine = self.if_not_exists()
            name = self.ident("index name")
            self.expect_kw("ON")
            table = self.ident("table name")
            self.expect_op("(")
            column = self.ident("column name")
            if self.is_op(","):
                self.error("multi-column indexes are not supported")
            self.expect_op(")")
            return ast.CreateIndex(name, table, column, unique, ine)
        self.error("expected TABLE or INDEX")

    def create_table(self):
        ine = self.if_not_exists()
        name = self.ident("table name")
        self.expect_op("(")
        columns = [self.column_def()]
        while self.accept_op(","):
            columns.append(self.column_def())
        self.expect_op(")")
        return ast.CreateTable(name, columns, ine)

    def column_def(self):
        name = self.ident("column name")
        col_type = "ANY"
        tok = self.peek()
        if tok.kind == "WORD" and tok.value.upper() in TYPE_NAMES:
            self.advance()
            col_type = TYPE_NAMES[tok.value.upper()]
            if self.accept_op("("):                  # VARCHAR(255): size is ignored
                self.advance()
                if self.accept_op(","):
                    self.advance()
                self.expect_op(")")
        col = ast.ColumnDef(name, col_type)
        while True:
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                col.primary_key = True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                col.not_null = True
            elif self.accept_kw("UNIQUE"):
                col.unique = True
            elif self.accept_kw("DEFAULT"):
                col.default = self.primary()
            else:
                return col

    def drop(self):
        if self.accept_kw("TABLE"):
            ie = self.if_exists()
            return ast.DropTable(self.ident("table name"), ie)
        if self.accept_kw("INDEX"):
            ie = self.if_exists()
            return ast.DropIndex(self.ident("index name"), ie)
        self.error("expected TABLE or INDEX")

    def insert(self):
        self.expect_kw("INTO")
        table = self.ident("table name")
        columns = None
        if self.accept_op("("):
            columns = [self.ident("column name")]
            while self.accept_op(","):
                columns.append(self.ident("column name"))
            self.expect_op(")")
        self.expect_kw("VALUES")
        rows = [self.value_row()]
        while self.accept_op(","):
            rows.append(self.value_row())
        return ast.Insert(table, columns, rows)

    def value_row(self):
        self.expect_op("(")
        row = [self.expr()]
        while self.accept_op(","):
            row.append(self.expr())
        self.expect_op(")")
        return row

    def update(self):
        table = self.ident("table name")
        self.expect_kw("SET")
        assignments = []
        while True:
            col = self.ident("column name")
            self.expect_op("=")
            assignments.append((col, self.expr()))
            if not self.accept_op(","):
                break
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Update(table, assignments, where)

    def delete(self):
        self.expect_kw("FROM")
        table = self.ident("table name")
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Delete(table, where)

    def select(self):
        self.expect_kw("SELECT")
        s = ast.Select(items=[])
        s.distinct = bool(self.accept_kw("DISTINCT"))
        s.items.append(self.select_item())
        while self.accept_op(","):
            s.items.append(self.select_item())
        if self.accept_kw("FROM"):
            s.table = self.ident("table name")
        if self.accept_kw("WHERE"):
            s.where = self.expr()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            s.group_by = [self.expr()]
            while self.accept_op(","):
                s.group_by.append(self.expr())
        if self.accept_kw("HAVING"):
            s.having = self.expr()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            while True:
                e = self.expr()
                desc = self.accept_kw("DESC", "ASC") == "DESC"
                s.order_by.append((e, desc))
                if not self.accept_op(","):
                    break
        if self.accept_kw("LIMIT"):
            s.limit = self.expr()
            if self.accept_kw("OFFSET"):
                s.offset = self.expr()
        return s

    def select_item(self):
        if self.accept_op("*"):
            return (ast.Star(), None)
        e = self.expr()
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident("alias")
        elif self.peek().kind == "IDENT" or (self.peek().kind == "WORD" and self.peek().value.upper() not in RESERVED):
            alias = self.ident("alias")
        return (e, alias)

    # ---- expressions ----

    def expr(self):
        return self.or_expr()

    def or_expr(self):
        left = self.and_expr()
        while self.accept_kw("OR"):
            left = ast.Binary("OR", left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept_kw("AND"):
            left = ast.Binary("AND", left, self.not_expr())
        return left

    def not_expr(self):
        if self.accept_kw("NOT"):
            return ast.Unary("NOT", self.not_expr())
        return self.comparison()

    def comparison(self):
        left = self.additive()
        tok = self.peek()
        if tok.kind == "OP" and tok.value in COMPARISON_OPS:
            self.advance()
            return ast.Binary(COMPARISON_OPS[tok.value], left, self.additive())
        if self.accept_kw("IS"):
            negated = bool(self.accept_kw("NOT"))
            self.expect_kw("NULL")
            return ast.IsNull(left, negated)
        negated = False
        if self.is_kw("NOT") and (self.is_kw("LIKE", 1) or self.is_kw("IN", 1) or self.is_kw("BETWEEN", 1)):
            self.advance()
            negated = True
        if self.accept_kw("LIKE"):
            return ast.Like(left, self.additive(), negated)
        if self.accept_kw("IN"):
            self.expect_op("(")
            items = [self.expr()]
            while self.accept_op(","):
                items.append(self.expr())
            self.expect_op(")")
            return ast.InList(left, items, negated)
        if self.accept_kw("BETWEEN"):
            low = self.additive()
            self.expect_kw("AND")
            return ast.Between(left, low, self.additive(), negated)
        return left

    def additive(self):
        left = self.multiplicative()
        while self.peek().kind == "OP" and self.peek().value in ("+", "-", "||"):
            op = self.advance().value
            left = ast.Binary(op, left, self.multiplicative())
        return left

    def multiplicative(self):
        left = self.unary()
        while self.peek().kind == "OP" and self.peek().value in ("*", "/", "%"):
            op = self.advance().value
            left = ast.Binary(op, left, self.unary())
        return left

    def unary(self):
        if self.accept_op("-"):
            operand = self.unary()
            if isinstance(operand, ast.Literal) and isinstance(operand.value, (int, float)):
                return ast.Literal(-operand.value)
            return ast.Unary("-", operand)
        if self.accept_op("+"):
            return self.unary()
        return self.primary()

    def primary(self):
        tok = self.peek()
        if tok.kind == "NUMBER" or tok.kind == "STRING":
            self.advance()
            return ast.Literal(tok.value)
        if self.accept_kw("NULL"):
            return ast.Literal(None)
        if self.accept_kw("TRUE"):
            return ast.Literal(1)
        if self.accept_kw("FALSE"):
            return ast.Literal(0)
        if self.accept_op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        if tok.kind in ("WORD", "IDENT"):
            name = self.ident("expression")
            if tok.kind == "WORD" and self.accept_op("("):        # function call
                f = ast.Func(name.upper(), [])
                if self.accept_op("*"):
                    f.star = True
                elif not self.is_op(")"):
                    f.distinct = bool(self.accept_kw("DISTINCT"))
                    f.args.append(self.expr())
                    while self.accept_op(","):
                        f.args.append(self.expr())
                self.expect_op(")")
                return f
            if self.accept_op("."):                              # table.column
                name = self.ident("column name")
            return ast.Column(name)
        self.error("expected an expression")
