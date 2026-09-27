"""Turning Python values into bytes (and back), plus the sort order of values.

A disk only stores bytes, so every row and every key gets encoded like this:

    [count: 2 bytes] then for each value: [type: 1 byte][payload]

    NULL     type 0, no payload
    INTEGER  type 1, 8-byte signed integer
    REAL     type 2, 8-byte float
    TEXT     type 3, 4-byte length + UTF-8 bytes
"""
import struct

NULL, INTEGER, REAL, TEXT = 0, 1, 2, 3


def encode(values) -> bytes:
    parts = [struct.pack("<H", len(values))]
    for v in values:
        if v is None:
            parts.append(bytes([NULL]))
        elif isinstance(v, int):                     # bool is an int too
            parts.append(bytes([INTEGER]) + struct.pack("<q", int(v)))
        elif isinstance(v, float):
            parts.append(bytes([REAL]) + struct.pack("<d", v))
        elif isinstance(v, str):
            b = v.encode("utf-8")
            parts.append(bytes([TEXT]) + struct.pack("<I", len(b)) + b)
        else:
            raise TypeError(f"cannot store value of type {type(v).__name__}")
    return b"".join(parts)


def decode(buf, pos=0):
    """Returns (tuple_of_values, position_after_record)."""
    (n,) = struct.unpack_from("<H", buf, pos)
    pos += 2
    out = []
    for _ in range(n):
        t = buf[pos]
        pos += 1
        if t == NULL:
            out.append(None)
        elif t == INTEGER:
            out.append(struct.unpack_from("<q", buf, pos)[0])
            pos += 8
        elif t == REAL:
            out.append(struct.unpack_from("<d", buf, pos)[0])
            pos += 8
        elif t == TEXT:
            (length,) = struct.unpack_from("<I", buf, pos)
            pos += 4
            out.append(bytes(buf[pos:pos + length]).decode("utf-8"))
            pos += length
        else:
            raise ValueError(f"corrupt record: unknown type tag {t}")
    return tuple(out), pos


def sort_key(v):
    """One ordering for all values, like SQLite: NULL < numbers < text."""
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)):
        return (1, v)
    return (2, v)


def key_order(key):
    """Sort key for a tuple of values (B-tree keys are tuples)."""
    return tuple(sort_key(v) for v in key)
