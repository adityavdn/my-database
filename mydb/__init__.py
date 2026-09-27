"""MyDB — a SQL database built from scratch in pure Python."""
from .engine import Database, DatabaseError, Result

__version__ = "0.1.0"
__all__ = ["Database", "DatabaseError", "Result"]
