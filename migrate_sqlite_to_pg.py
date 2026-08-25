import asyncio
import sqlite3
import os
import sys
import logging
from db_config import DATABASE_URL

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")

TABLES_TO_MIGRATE = [
    "users",
    "chat_members",
    "bans",
    "games_history",
    "game_stats",
    "transfers_history",
    "active_games_state",
    "promo_codes",
    "promo_activations",
    "top_bans",
    "transfer_bans",
    "mp_transfers_history",
    "time_deposits",
    "savings_accounts",
    "savings_history",
    "bank_settings",
    "referrals",
    "p2p_accounts",
    "p2p_deals_history",
    "p2p_deal_ratings",
    "p2p_bot_stats",
    "p2p_settings",
    "arena_history"
]


async def run_migration():
    if not os.path.exists("game.db"):
        logger.error("game.db not found in current directory!")
        return

    logger.info(f"Connecting to SQLite (game.db)...")
    s_conn = sqlite3.connect("game.db")
    s_conn.row_factory = sqlite3.Row
    s_cur = s_conn.cursor()

    try:
        import asyncpg
    except ImportError:
        logger.error("asyncpg is not installed! Run `pip install asyncpg`")
        return

    logger.info(f"Connecting to PostgreSQL ({DATABASE_URL})...")
    try:
        pg_conn = await asyncpg.connect(DATABASE_URL, timeout=10.0)
    except Exception as e:
        logger.error(f"Failed to connect to PostgreSQL: {e}")
        return

    from db import PG_SCHEMA_SQL
    logger.info("Ensuring PostgreSQL schema exists...")
    await pg_conn.execute(PG_SCHEMA_SQL)

    total_migrated = 0
    for tbl in TABLES_TO_MIGRATE:
        try:
            s_cur.execute(f"SELECT * FROM {tbl}")
            rows = s_cur.fetchall()
            if not rows:
                logger.info(f"Table '{tbl}': 0 rows (skipped)")
                continue

            col_names = [d[0] for d in s_cur.description]
            cols_str = ", ".join(col_names)
            placeholders = ", ".join(f"${i+1}" for i in range(len(col_names)))

            insert_sql = f"INSERT INTO {tbl} ({cols_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            
            data = [tuple(r) for r in rows]
            await pg_conn.executemany(insert_sql, data)
            logger.info(f"Table '{tbl}': successfully migrated {len(data)} rows ✅")
            total_migrated += len(data)
        except Exception as e:
            logger.warning(f"Error migrating table '{tbl}': {e}")

    await pg_conn.close()
    s_conn.close()
    logger.info(f"🎉 Migration completed! Total records migrated: {total_migrated}")


if __name__ == "__main__":
    asyncio.run(run_migration())
