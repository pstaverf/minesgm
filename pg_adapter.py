import os
import re
import sys
import logging
import threading
from typing import Any, List, Optional, Tuple, Union, Dict

logger = logging.getLogger("pg_adapter")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

from db_config import DATABASE_URL
from db import PG_SCHEMA_SQL


def _convert_sqlite_query_to_pg(query: str) -> str:
    """
    Safely converts SQLite syntax to PostgreSQL:
    1. Replaces '?' with '%s' ONLY outside of quoted string literals.
    2. Translates 'ALTER TABLE ... ADD COLUMN ...' to include 'IF NOT EXISTS'.
    3. Translates 'AUTOINCREMENT' to 'BIGSERIAL' / 'SERIAL'.
    4. Translates 'INSERT OR IGNORE INTO' to 'INSERT INTO ... ON CONFLICT DO NOTHING'.
    """
    q = query.strip()

    # 1. Parameter placeholder replacement (? -> %s outside of single quotes)
    result = []
    in_single_quote = False
    in_double_quote = False
    i = 0
    while i < len(q):
        char = q[i]
        if char == "'" and not in_double_quote:
            # Check for escaped single quote
            if i + 1 < len(q) and q[i+1] == "'":
                result.append("''")
                i += 2
                continue
            in_single_quote = not in_single_quote
            result.append(char)
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result.append(char)
        elif char == '?' and not in_single_quote and not in_double_quote:
            result.append('%s')
        else:
            result.append(char)
        i += 1
    q = "".join(result)

    # 2. ALTER TABLE ... ADD COLUMN -> ADD COLUMN IF NOT EXISTS
    if re.search(r'\bALTER\s+TABLE\b', q, re.IGNORECASE) and re.search(r'\bADD\s+COLUMN\b', q, re.IGNORECASE):
        if not re.search(r'\bIF\s+NOT\s+EXISTS\b', q, re.IGNORECASE):
            q = re.sub(r'(\bADD\s+COLUMN\s+)', r'\1IF NOT EXISTS ', q, flags=re.IGNORECASE)

    # 3. AUTOINCREMENT -> SERIAL in CREATE TABLE (if any new tables are created)
    if "AUTOINCREMENT" in q.upper():
        q = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY', q, flags=re.IGNORECASE)
        q = re.sub(r'AUTOINCREMENT', '', q, flags=re.IGNORECASE)

    # 4. INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    if re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if "ON CONFLICT" not in q.upper():
            q = q + " ON CONFLICT DO NOTHING"

    return q


class SQLiteCompatibleRow:
    """Dual access row wrapper: supports row[0], row['column'], dict(row), iteration, etc."""
    __slots__ = ('_data', '_mapping', '_col_names')

    def __init__(self, data_tuple: tuple, col_names: list):
        self._data = data_tuple
        self._col_names = col_names
        self._mapping = {name.lower(): val for name, val in zip(col_names, data_tuple)}

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._data[item]
        elif isinstance(item, str):
            key = item.lower()
            if key in self._mapping:
                return self._mapping[key]
            raise KeyError(f"Column '{item}' not found in row. Available: {self._col_names}")
        raise TypeError(f"Invalid row key type: {type(item)}")

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        if isinstance(key, str):
            return key.lower() in self._mapping
        return key in self._data

    def __repr__(self):
        return f"<Row {self._mapping}>"

    def keys(self):
        return self._col_names

    def values(self):
        return self._data

    def items(self):
        return list(zip(self._col_names, self._data))

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(str(key).lower(), default)


class PGAdapterCursor:
    def __init__(self, adapter_conn):
        self._adapter_conn = adapter_conn
        self.description = None
        self.rowcount = -1
        self.lastrowid = None

    def execute(self, query: str, params: Union[Tuple, List, dict] = None):
        converted_query = _convert_sqlite_query_to_pg(query)
        with self._adapter_conn._lock:
            cur = self._adapter_conn._get_raw_cursor()
            try:
                if params is not None:
                    cur.execute(converted_query, params)
                else:
                    cur.execute(converted_query)
                self.description = cur.description
                self.rowcount = cur.rowcount
                return self
            except Exception as e:
                # If error occurred, attempt rollback to keep connection healthy
                try:
                    self._adapter_conn._conn.rollback()
                except Exception:
                    pass
                logger.error(f"PostgreSQL Query Error: {e} | Query: {converted_query}")
                raise

    def executemany(self, query: str, seq_of_params):
        converted_query = _convert_sqlite_query_to_pg(query)
        with self._adapter_conn._lock:
            cur = self._adapter_conn._get_raw_cursor()
            try:
                cur.executemany(converted_query, seq_of_params)
                self.description = cur.description
                self.rowcount = cur.rowcount
                return self
            except Exception as e:
                try:
                    self._adapter_conn._conn.rollback()
                except Exception:
                    pass
                logger.error(f"PostgreSQL executemany Error: {e} | Query: {converted_query}")
                raise

    def fetchone(self) -> Optional[SQLiteCompatibleRow]:
        with self._adapter_conn._lock:
            cur = self._adapter_conn._get_raw_cursor()
            try:
                row = cur.fetchone()
                if row is None:
                    return None
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    return SQLiteCompatibleRow(row, cols)
                return row
            except Exception as e:
                # Some queries (like UPDATE/INSERT without RETURNING) have no rows to fetch
                return None

    def fetchall(self) -> List[SQLiteCompatibleRow]:
        with self._adapter_conn._lock:
            cur = self._adapter_conn._get_raw_cursor()
            try:
                rows = cur.fetchall()
                if not rows:
                    return []
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    return [SQLiteCompatibleRow(r, cols) for r in rows]
                return rows
            except Exception as e:
                return []

    def close(self):
        pass


class PGAdapterConnection:
    """Thread-safe resilient connection to PostgreSQL matching sqlite3.Connection API."""
    def __init__(self, dsn: str = DATABASE_URL):
        self.dsn = dsn
        self._conn = None
        self._lock = threading.RLock()
        self.row_factory = None
        self._connect()
        self._init_schema()

    def _connect(self):
        with self._lock:
            try:
                import psycopg2
                self._conn = psycopg2.connect(self.dsn)
                self._conn.autocommit = True  # Autocommit avoids stalled transaction locks
                logger.info("Connected to PostgreSQL successfully! 🚀")
            except ImportError:
                logger.error("psycopg2 is not installed! Run `pip install psycopg2-binary`")
                raise
            except Exception as e:
                logger.error(f"PostgreSQL connection failed: {e}")
                raise

    def _get_raw_cursor(self):
        # Auto-reconnect if connection was dropped
        try:
            if self._conn is None or self._conn.closed:
                self._connect()
            return self._conn.cursor()
        except Exception:
            self._connect()
            return self._conn.cursor()

    def _init_schema(self):
        with self._lock:
            try:
                cur = self._conn.cursor()
                cur.execute(PG_SCHEMA_SQL)
                logger.info("PostgreSQL database tables & performance indexes verified successfully! ✅")
            except Exception as e:
                logger.warning(f"Schema verification note: {e}")

    def cursor(self):
        return PGAdapterCursor(self)

    def execute(self, query: str, params: Any = None):
        """Shortcut matching sqlite3 conn.execute()."""
        cur = self.cursor()
        return cur.execute(query, params)

    def commit(self):
        # With autocommit=True, changes are saved immediately
        pass

    def rollback(self):
        with self._lock:
            if self._conn and not self._conn.closed:
                try:
                    self._conn.rollback()
                except Exception:
                    pass

    def create_function(self, name, num_args, func):
        # Built-in in PostgreSQL (e.g. lower, upper, etc.)
        pass

    def close(self):
        with self._lock:
            if self._conn and not self._conn.closed:
                self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()


_global_adapter_conn = None
_global_lock = threading.Lock()


def get_db_connection(dsn: str = None) -> Union[PGAdapterConnection, Any]:
    """Returns singleton PostgreSQL connection matching sqlite3 API."""
    global _global_adapter_conn
    with _global_lock:
        if _global_adapter_conn is None:
            target_dsn = dsn or DATABASE_URL
            try:
                _global_adapter_conn = PGAdapterConnection(target_dsn)
            except Exception as e:
                logger.warning(f"PostgreSQL unavailable ({e}), using local SQLite fallback.")
                import sqlite3
                conn = sqlite3.connect("game.db", check_same_thread=False)
                conn.create_function("lower", 1, lambda s: s.lower() if s is not None else None)
                return conn
        return _global_adapter_conn
