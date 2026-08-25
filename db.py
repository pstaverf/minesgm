import os
import sys
import json
import asyncio
import logging
import sqlite3
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from db_config import DATABASE_URL

logger = logging.getLogger(__name__)

# Global connection pool
_pg_pool = None
_sqlite_conn = None
_use_sqlite_fallback = False

PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT DEFAULT NULL,
    balance BIGINT DEFAULT 0,
    games BIGINT DEFAULT 0,
    lost BIGINT DEFAULT 0,
    bonus_time TEXT DEFAULT NULL,
    registered_at TEXT DEFAULT NULL,
    mp_balance BIGINT DEFAULT 0,
    mp_daily_transferred BIGINT DEFAULT 0,
    mp_daily_date TEXT DEFAULT NULL,
    ref_earned BIGINT DEFAULT 0,
    ref_count BIGINT DEFAULT 0,
    referred_by BIGINT DEFAULT NULL,
    max_balance BIGINT DEFAULT 0,
    first_name TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS chat_members (
    user_id BIGINT,
    chat_id BIGINT,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS bans (
    user_id BIGINT PRIMARY KEY,
    reason TEXT,
    until TEXT,
    is_permanent INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS games_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    game_type TEXT,
    bet BIGINT,
    result TEXT,
    win_amount BIGINT DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS game_stats (
    user_id BIGINT PRIMARY KEY,
    mines BIGINT DEFAULT 0,
    tower BIGINT DEFAULT 0,
    diamonds BIGINT DEFAULT 0,
    crash BIGINT DEFAULT 0,
    slots BIGINT DEFAULT 0,
    bowling BIGINT DEFAULT 0,
    darts BIGINT DEFAULT 0,
    basketball BIGINT DEFAULT 0,
    football BIGINT DEFAULT 0,
    roulette BIGINT DEFAULT 0,
    twentyone BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transfers_history (
    id BIGSERIAL PRIMARY KEY,
    sender_id BIGINT,
    receiver_id BIGINT,
    amount BIGINT,
    commission BIGINT DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS active_games_state (
    game_id BIGINT PRIMARY KEY,
    owner_id BIGINT,
    chat_id BIGINT,
    game_type TEXT,
    stage TEXT,
    bet BIGINT,
    mine_count INTEGER,
    mine_positions TEXT,
    revealed TEXT,
    level INTEGER,
    game_over INTEGER DEFAULT 0,
    won INTEGER DEFAULT 0,
    exploded_mine INTEGER DEFAULT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS promo_codes (
    code TEXT PRIMARY KEY,
    reward BIGINT,
    total_activations INTEGER,
    remaining_activations INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS promo_activations (
    code TEXT,
    user_id BIGINT,
    activated_at TEXT,
    PRIMARY KEY (code, user_id)
);

CREATE TABLE IF NOT EXISTS top_bans (
    user_id BIGINT PRIMARY KEY,
    banned_at TEXT
);

CREATE TABLE IF NOT EXISTS transfer_bans (
    user_id BIGINT PRIMARY KEY,
    banned_at TEXT
);

CREATE TABLE IF NOT EXISTS mp_transfers_history (
    id BIGSERIAL PRIMARY KEY,
    sender_id BIGINT,
    receiver_id BIGINT,
    amount BIGINT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS time_deposits (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    amount BIGINT,
    days INTEGER,
    percent REAL,
    profit BIGINT,
    is_locked INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at TEXT,
    end_at TEXT,
    created_at_dt TEXT,
    end_at_dt TEXT
);

CREATE TABLE IF NOT EXISTS savings_accounts (
    user_id BIGINT PRIMARY KEY,
    balance BIGINT DEFAULT 0,
    accumulated_interest REAL DEFAULT 0.0,
    total_earned BIGINT DEFAULT 0,
    last_accrual TEXT
);

CREATE TABLE IF NOT EXISTS savings_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    type TEXT,
    amount BIGINT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS bank_settings (
    user_id BIGINT PRIMARY KEY,
    notifications_enabled INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS referrals (
    referrer_id BIGINT,
    referral_id BIGINT PRIMARY KEY,
    is_active INTEGER DEFAULT 0,
    bonus_paid BIGINT DEFAULT 0,
    earned_from_losses BIGINT DEFAULT 0,
    joined_at TEXT
);

CREATE TABLE IF NOT EXISTS p2p_accounts (
    user_id BIGINT PRIMARY KEY,
    mcoin_balance BIGINT DEFAULT 0,
    mp_balance BIGINT DEFAULT 0,
    rating BIGINT DEFAULT 0,
    deals_count INTEGER DEFAULT 0,
    last_deposit_date TEXT DEFAULT NULL,
    sell_order_active INTEGER DEFAULT 0,
    sell_price BIGINT DEFAULT 0,
    buy_order_active INTEGER DEFAULT 0,
    buy_price BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS p2p_deals_history (
    id BIGSERIAL PRIMARY KEY,
    buyer_id BIGINT,
    seller_id BIGINT,
    amount_mp BIGINT,
    price_per_mp BIGINT,
    total_mcoin BIGINT,
    commission BIGINT DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS p2p_deal_ratings (
    id BIGSERIAL PRIMARY KEY,
    deal_id BIGINT,
    rater_id BIGINT,
    target_id BIGINT,
    rating_change INTEGER,
    created_at TEXT,
    UNIQUE(deal_id, rater_id)
);

CREATE TABLE IF NOT EXISTS p2p_bot_stats (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT,
    amount_mp BIGINT,
    amount_mcoin BIGINT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS p2p_settings (
    id BIGINT PRIMARY KEY,
    official_sell_enabled INTEGER DEFAULT 1,
    official_buy_enabled INTEGER DEFAULT 1,
    official_sell_rate BIGINT DEFAULT 10000,
    official_buy_rate BIGINT DEFAULT 10000,
    rate_min BIGINT DEFAULT 7000,
    rate_max BIGINT DEFAULT 29000,
    interval_minutes INTEGER DEFAULT 150,
    last_update TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS arena_history (
    id BIGSERIAL PRIMARY KEY,
    round_id BIGINT UNIQUE,
    total_bank BIGINT,
    winner_id BIGINT,
    winner_name TEXT,
    winner_username TEXT,
    winner_avatar TEXT,
    winner_color TEXT,
    winner_bet BIGINT,
    winner_share REAL,
    players_json TEXT,
    zones_json TEXT,
    ball_trajectory_json TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_users_max_balance ON users(max_balance DESC);
CREATE INDEX IF NOT EXISTS idx_users_games ON users(games DESC);
CREATE INDEX IF NOT EXISTS idx_users_lost ON users(lost DESC);
CREATE INDEX IF NOT EXISTS idx_games_history_user ON games_history(user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_transfers_history_users ON transfers_history(sender_id, receiver_id);
"""


def _get_sqlite_connection():
    global _sqlite_conn
    if _sqlite_conn is None:
        _sqlite_conn = sqlite3.connect("game.db", check_same_thread=False)
        _sqlite_conn.row_factory = sqlite3.Row
    return _sqlite_conn


async def init_db_pool():
    global _pg_pool, _use_sqlite_fallback
    if _pg_pool is not None:
        return _pg_pool
    try:
        import asyncpg
        _pg_pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=2,
            max_size=20,
            command_timeout=15.0,
            timeout=5.0
        )
        async with _pg_pool.acquire() as conn:
            await conn.execute(PG_SCHEMA_SQL)
        logger.info("Connected to PostgreSQL pool and initialized schema successfully!")
        _use_sqlite_fallback = False
        return _pg_pool
    except Exception as e:
        logger.warning(f"PostgreSQL connection failed ({e}). Falling back to SQLite mode.")
        _use_sqlite_fallback = True
        return None


async def execute(query: str, *args):
    global _pg_pool, _use_sqlite_fallback
    if not _use_sqlite_fallback:
        pool = await init_db_pool()
        if pool:
            try:
                # Convert SQLite ? placeholders to PostgreSQL $1, $2, ... if needed
                pg_query = query
                if "?" in pg_query:
                    parts = pg_query.split("?")
                    new_q = []
                    for i, p in enumerate(parts[:-1]):
                        new_q.append(f"{p}${i+1}")
                    new_q.append(parts[-1])
                    pg_query = "".join(new_q)
                async with pool.acquire() as conn:
                    return await conn.execute(pg_query, *args)
            except Exception as e:
                logger.error(f"PG Execute error: {e}")
    # SQLite fallback
    conn = _get_sqlite_connection()
    q = query
    # Convert $1, $2 to ? if query was written for PG
    import re
    q = re.sub(r'\$\d+', '?', q)
    cursor = conn.cursor()
    cursor.execute(q, args)
    conn.commit()
    return cursor.rowcount


async def fetchrow(query: str, *args) -> Optional[Dict[str, Any]]:
    global _pg_pool, _use_sqlite_fallback
    if not _use_sqlite_fallback:
        pool = await init_db_pool()
        if pool:
            try:
                pg_query = query
                if "?" in pg_query:
                    parts = pg_query.split("?")
                    new_q = []
                    for i, p in enumerate(parts[:-1]):
                        new_q.append(f"{p}${i+1}")
                    new_q.append(parts[-1])
                    pg_query = "".join(new_q)
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(pg_query, *args)
                    return dict(row) if row else None
            except Exception as e:
                logger.error(f"PG Fetchrow error: {e}")
    # SQLite fallback
    conn = _get_sqlite_connection()
    import re
    q = re.sub(r'\$\d+', '?', query)
    cursor = conn.cursor()
    cursor.execute(q, args)
    row = cursor.fetchone()
    return dict(row) if row else None


async def fetch(query: str, *args) -> List[Dict[str, Any]]:
    global _pg_pool, _use_sqlite_fallback
    if not _use_sqlite_fallback:
        pool = await init_db_pool()
        if pool:
            try:
                pg_query = query
                if "?" in pg_query:
                    parts = pg_query.split("?")
                    new_q = []
                    for i, p in enumerate(parts[:-1]):
                        new_q.append(f"{p}${i+1}")
                    new_q.append(parts[-1])
                    pg_query = "".join(new_q)
                async with pool.acquire() as conn:
                    rows = await conn.fetch(pg_query, *args)
                    return [dict(r) for r in rows]
            except Exception as e:
                logger.error(f"PG Fetch error: {e}")
    # SQLite fallback
    conn = _get_sqlite_connection()
    import re
    q = re.sub(r'\$\d+', '?', query)
    cursor = conn.cursor()
    cursor.execute(q, args)
    rows = cursor.fetchall()
    return [dict(r) for r in rows]


async def fetchval(query: str, *args):
    row = await fetchrow(query, *args)
    if row:
        return list(row.values())[0]
    return None
