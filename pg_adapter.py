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


def _convert_query_params_to_pg(query: str) -> str:
    """
    Safely converts query syntax for psycopg2:
    1. Replaces '?' with '%s' outside of single/double quotes.
    2. Translates 'INSERT OR IGNORE INTO' to 'INSERT INTO ... ON CONFLICT DO NOTHING'.
    3. Translates scalar 'MAX(COALESCE(...))' to 'GREATEST(COALESCE(...))'.
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

    # 2. INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    if re.search(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', q, re.IGNORECASE):
        q = re.sub(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', 'INSERT INTO', q, flags=re.IGNORECASE)
        if "ON CONFLICT" not in q.upper():
            q = q + " ON CONFLICT DO NOTHING"

    # 3. Scalar MAX(COALESCE(...), ...) -> GREATEST(COALESCE(...), ...) for PostgreSQL
    if re.search(r'\bMAX\s*\(\s*COALESCE', q, re.IGNORECASE):
        q = re.sub(r'\bMAX\s*\((\s*COALESCE)', r'GREATEST(\1', q, flags=re.IGNORECASE)

    return q


class _CaseInsensitiveColList(list):
    def __contains__(self, item):
        if isinstance(item, str):
            item_lower = item.lower()
            return any(isinstance(x, str) and x.lower() == item_lower for x in self)
        return super().__contains__(item)


class PGRow:
    """Dual access row wrapper: supports row[0], row['column'], row[1:3], dict(row.items()), iteration, etc."""
    __slots__ = ('_data', '_mapping', '_col_names')

    def __init__(self, data_tuple: tuple, col_names: list):
        self._data = data_tuple
        self._col_names = _CaseInsensitiveColList(col_names)
        self._mapping = {name.lower(): val for name, val in zip(col_names, data_tuple)}

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._data[item]
        elif isinstance(item, str):
            key = item.lower()
            if key in self._mapping:
                return self._mapping[key]
            raise KeyError(f"Column '{item}' not found in row. Available: {self._col_names}")
        elif isinstance(item, slice):
            return self._data[item]
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
        return f"<PGRow {self._mapping}>"

    def keys(self):
        return self._col_names

    def values(self):
        return self._data

    def items(self):
        return list(zip(self._col_names, self._data))

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(str(key).lower(), default)


# Alias for backward compatibility if referenced
SQLiteCompatibleRow = PGRow


class PGAdapterCursor:
    def __init__(self, adapter_conn):
        self._adapter_conn = adapter_conn
        self._raw_cursor = None
        self.description = None
        self.rowcount = -1
        self.lastrowid = None

    def _ensure_cursor(self):
        if self._raw_cursor is None or self._raw_cursor.closed:
            self._raw_cursor = self._adapter_conn._get_raw_cursor()
        return self._raw_cursor

    def execute(self, query: str, params: Union[Tuple, List, dict] = None):
        converted_query = _convert_query_params_to_pg(query)
        with self._adapter_conn._lock:
            for attempt in range(2):
                cur = self._ensure_cursor()
                try:
                    if params is not None:
                        cur.execute(converted_query, params)
                    else:
                        cur.execute(converted_query)
                    self.description = cur.description
                    self.rowcount = cur.rowcount
                    return self
                except Exception as e:
                    try:
                        self._adapter_conn._conn.rollback()
                    except Exception:
                        pass
                    # If connection dropped, reconnect and retry once
                    if attempt == 0:
                        try:
                            logger.warning(f"Connection lost ({e}), reconnecting to PostgreSQL...")
                            self._adapter_conn._connect()
                            self._raw_cursor = self._adapter_conn._get_raw_cursor()
                            continue
                        except Exception:
                            pass
                    logger.error(f"PostgreSQL Query Error: {e} | Query: {converted_query}")
                    raise

    def executemany(self, query: str, seq_of_params):
        converted_query = _convert_query_params_to_pg(query)
        with self._adapter_conn._lock:
            for attempt in range(2):
                cur = self._ensure_cursor()
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
                    if attempt == 0:
                        try:
                            logger.warning(f"Connection lost ({e}), reconnecting to PostgreSQL...")
                            self._adapter_conn._connect()
                            self._raw_cursor = self._adapter_conn._get_raw_cursor()
                            continue
                        except Exception:
                            pass
                    logger.error(f"PostgreSQL executemany Error: {e} | Query: {converted_query}")
                    raise

    def fetchone(self) -> Optional[PGRow]:
        with self._adapter_conn._lock:
            cur = self._ensure_cursor()
            try:
                row = cur.fetchone()
                if row is None:
                    return None
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    return PGRow(row, cols)
                return row
            except Exception:
                return None

    def fetchall(self) -> List[PGRow]:
        with self._adapter_conn._lock:
            cur = self._ensure_cursor()
            try:
                rows = cur.fetchall()
                if not rows:
                    return []
                if cur.description:
                    cols = [desc[0] for desc in cur.description]
                    return [PGRow(r, cols) for r in rows]
                return rows
            except Exception:
                return []

    def close(self):
        try:
            if self._raw_cursor and not self._raw_cursor.closed:
                self._raw_cursor.close()
        except Exception:
            pass


_schema_initialized = False


class PGAdapterConnection:
    """Thread-safe resilient connection to PostgreSQL."""
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
                self._conn.autocommit = True
                logger.info("Connected to PostgreSQL successfully!")
            except ImportError:
                logger.error("psycopg2 is not installed! Run `pip install psycopg2-binary`")
                raise
            except Exception as e:
                logger.error(f"PostgreSQL connection failed: {e}")
                raise

    def _get_raw_cursor(self):
        try:
            if self._conn is None or self._conn.closed:
                self._connect()
            return self._conn.cursor()
        except Exception:
            self._connect()
            return self._conn.cursor()

    def _init_schema(self):
        global _schema_initialized
        if _schema_initialized:
            return
        with self._lock:
            if _schema_initialized:
                return
            try:
                cur = self._conn.cursor()
                cur.execute(PG_SCHEMA_SQL)
                logger.info("PostgreSQL database tables & indexes verified successfully!")
                self._sync_sequences()
                _schema_initialized = True
            except Exception as e:
                logger.warning(f"Schema initialization note: {e}")

    def _sync_sequences(self):
        """Synchronizes all serial sequences with current MAX(id) in each table."""
        with self._lock:
            try:
                cur = self._conn.cursor()
                cur.execute("""
                    DO $do$
                    DECLARE
                        r RECORD;
                        seq_name TEXT;
                    BEGIN
                        FOR r IN (
                            SELECT table_name, column_name 
                            FROM information_schema.columns 
                            WHERE column_default LIKE 'nextval(%' AND table_schema = 'public'
                        ) LOOP
                            seq_name := pg_get_serial_sequence(r.table_name, r.column_name);
                            IF seq_name IS NOT NULL THEN
                                EXECUTE format('SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I), 0) + 1, false)', seq_name, r.column_name, r.table_name);
                            END IF;
                        END LOOP;
                    END $do$;
                """)
                logger.info("PostgreSQL sequences synchronized successfully!")
            except Exception as e:
                logger.warning(f"Sequence synchronization note: {e}")

    def cursor(self):
        return PGAdapterCursor(self)

    def execute(self, query: str, params: Any = None):
        cur = self.cursor()
        return cur.execute(query, params)

    def commit(self):
        # With autocommit=True, transactions commit immediately
        pass

    def rollback(self):
        with self._lock:
            if self._conn and not self._conn.closed:
                try:
                    self._conn.rollback()
                except Exception:
                    pass

    def create_function(self, name, num_args, func):
        # PostgreSQL has built-in lower/upper functions
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


def get_db_connection(dsn: str = None) -> PGAdapterConnection:
    """Returns singleton PostgreSQL connection."""
    global _global_adapter_conn
    with _global_lock:
        if _global_adapter_conn is None:
            target_dsn = dsn or DATABASE_URL
            _global_adapter_conn = PGAdapterConnection(target_dsn)
        return _global_adapter_conn
