"""Tokenizer: splits SQL text into tokens.

    "SELECT name FROM users WHERE age >= 18;"
 -> WORD(SELECT) WORD(name) WORD(FROM) WORD(users) WORD(WHERE) WORD(age) OP(>=) NUMBER(18) OP(;)
"""
from dataclasses import dataclass


class SQLSyntaxError(Exception):
    pass


@dataclass
class Token:
    kind: str       # WORD, IDENT (quoted), NUMBER, STRING, OP, EOF
    value: object
    pos: int
    end: int


TWO_CHAR_OPS = {"<=", ">=", "!=", "<>", "==", "||"}
ONE_CHAR_OPS = set("=<>+-*/%(),;.")


def tokenize(sql):
    tokens, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):                      # comment to end of line
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            tokens.append(Token("WORD", sql[i:j], i, j))
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            while j < n and sql[j].isdigit():
                j += 1
            is_float = False
            if j < n and sql[j] == "." :
                is_float = True
                j += 1
                while j < n and sql[j].isdigit():
                    j += 1
            if j < n and sql[j] in "eE" and j + 1 < n and (sql[j + 1].isdigit() or sql[j + 1] in "+-"):
                is_float = True
                j += 2
                while j < n and sql[j].isdigit():
                    j += 1
            text = sql[i:j]
            tokens.append(Token("NUMBER", float(text) if is_float else int(text), i, j))
            i = j
        elif c in "'\"":
            quote, j, chars = c, i + 1, []
            while True:
                if j >= n:
                    raise SQLSyntaxError(f"unterminated {'string' if quote == chr(39) else 'identifier'} at position {i}")
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:     # '' is an escaped '
                        chars.append(quote)
                        j += 2
                        continue
                    break
                chars.append(sql[j])
                j += 1
            tokens.append(Token("STRING" if quote == "'" else "IDENT", "".join(chars), i, j + 1))
            i = j + 1
        elif sql[i:i + 2] in TWO_CHAR_OPS:
            tokens.append(Token("OP", sql[i:i + 2], i, i + 2))
            i += 2
        elif c in ONE_CHAR_OPS:
            tokens.append(Token("OP", c, i, i + 1))
            i += 1
        else:
            raise SQLSyntaxError(f"unexpected character {c!r} at position {i}")
    tokens.append(Token("EOF", None, n, n))
    return tokens
