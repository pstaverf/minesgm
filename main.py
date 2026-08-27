import os
import secrets
import asyncio
import random
import re
import json
import html
import math
import urllib.parse
from arena_engine import arena_engine
from asset_rotator import asset_rotator
from datetime import datetime, timedelta, timezone
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

MSK_TZ = timezone(timedelta(hours=3))


def get_msk_now():
    return datetime.now(MSK_TZ)


def get_msk_today_str():
    return get_msk_now().strftime("%Y-%m-%d")


from db_config import (
    BOT_TOKEN, BOT_USERNAME, ADMINS,
    DATABASE_URL, REDIS_URL,
    WEBHOOK_HOST, WEB_SERVER_PORT
)

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEB_SERVER_HOST = "0.0.0.0"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

from pg_adapter import get_db_connection
conn = get_db_connection()
cursor = conn.cursor()

# PostgreSQL Schema & Performance Indexes are automatically initialized via pg_adapter / db.py
try:
    cursor.execute("UPDATE users SET max_balance = balance WHERE max_balance IS NULL OR max_balance < balance")
    conn.commit()
except Exception:
    pass

# High-Speed In-Memory Caches
_known_usernames = {}
_known_names = {}
_known_chat_members = set()
_banned_users_cache = {}
_top_banned_users = set()
_transfer_banned_users = set()
mp_pending_transfers = {}


def init_caches():
    try:
        cursor.execute("SELECT user_id, reason, until, is_permanent FROM bans")
        for row in cursor.fetchall():
            uid, reason, until_str, is_perm = row[0], row[1], row[2], row[3]
            if is_perm:
                _banned_users_cache[uid] = {"reason": reason, "until_str": "Навсегда", "until_dt": None, "is_perm": True}
            elif until_str:
                try:
                    until_dt = datetime.fromisoformat(until_str)
                    _banned_users_cache[uid] = {"reason": reason, "until_str": until_dt.strftime("%d/%m/%y %H:%M"), "until_dt": until_dt, "is_perm": False}
                except Exception:
                    pass

        cursor.execute("SELECT user_id FROM top_bans")
        for row in cursor.fetchall():
            _top_banned_users.add(row[0])

        cursor.execute("SELECT user_id FROM transfer_bans")
        for row in cursor.fetchall():
            _transfer_banned_users.add(row[0])
    except Exception:
        pass


def is_top_banned(user_id):
    return user_id in _top_banned_users


def ban_user_top(user_id):
    _top_banned_users.add(user_id)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute("INSERT INTO top_bans (user_id, banned_at) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET banned_at = EXCLUDED.banned_at", (user_id, now_str))
        conn.commit()
        return True
    except Exception:
        return False


def unban_user_top(user_id):
    _top_banned_users.discard(user_id)
    try:
        cursor.execute("DELETE FROM top_bans WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception:
        return False


def is_transfer_banned(user_id):
    return user_id in _transfer_banned_users


def ban_user_transfers(user_id):
    _transfer_banned_users.add(user_id)
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute("INSERT INTO transfer_bans (user_id, banned_at) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET banned_at = EXCLUDED.banned_at", (user_id, now_str))
        conn.commit()
        return True
    except Exception:
        return False


def unban_user_transfers(user_id):
    _transfer_banned_users.discard(user_id)
    try:
        cursor.execute("DELETE FROM transfer_bans WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    except Exception:
        return False


def get_user_mp_limit(user_data):
    today_str = get_msk_today_str()
    if user_data.get("mp_daily_date") != today_str:
        return 1000
    daily_used = user_data.get("mp_daily_transferred", 0) or 0
    return max(0, 1000 - daily_used)


def get_user(user_id):
    try:
        cursor.execute('SELECT balance, games, lost, bonus_time, username, registered_at, mp_balance, mp_daily_transferred, mp_daily_date, ref_earned, ref_count, referred_by, max_balance, first_name FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if result:
            return {
                "balance": result[0],
                "games": result[1],
                "lost": result[2],
                "bonus_time": result[3],
                "username": result[4],
                "registered_at": result[5],
                "mp_balance": result[6] if len(result) > 6 and result[6] is not None else 0,
                "mp_daily_transferred": result[7] if len(result) > 7 and result[7] is not None else 0,
                "mp_daily_date": result[8] if len(result) > 8 else None,
                "ref_earned": result[9] if len(result) > 9 and result[9] is not None else 0,
                "ref_count": result[10] if len(result) > 10 and result[10] is not None else 0,
                "referred_by": result[11] if len(result) > 11 else None,
                "max_balance": result[12] if len(result) > 12 and result[12] is not None else result[0],
                "first_name": result[13] if len(result) > 13 and result[13] else None
            }
        reg_time = datetime.now().strftime("%d.%m.%Y %H:%M")
        cursor.execute('INSERT INTO users (user_id, balance, games, lost, bonus_time, username, registered_at, mp_balance, mp_daily_transferred, mp_daily_date, ref_earned, ref_count, referred_by, max_balance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (user_id) DO NOTHING', (user_id, 0, 0, 0, None, None, reg_time, 0, 0, None, 0, 0, None, 0))
        conn.commit()
        return {"balance": 0, "games": 0, "lost": 0, "bonus_time": None, "username": None, "registered_at": reg_time, "mp_balance": 0, "mp_daily_transferred": 0, "mp_daily_date": None, "ref_earned": 0, "ref_count": 0, "referred_by": None, "max_balance": 0, "first_name": None}
    except Exception:
        return {"balance": 0, "games": 0, "lost": 0, "bonus_time": None, "username": None, "registered_at": None, "mp_balance": 0, "mp_daily_transferred": 0, "mp_daily_date": None, "ref_earned": 0, "ref_count": 0, "referred_by": None, "max_balance": 0, "first_name": None}


def update_user(user_id, balance=None, games=None, lost=None, bonus_time=None, username=None, registered_at=None, mp_balance=None, mp_daily_transferred=None, mp_daily_date=None, ref_earned=None, ref_count=None, referred_by=None, max_balance=None, first_name=None):
    try:
        updates = []
        params = []
        if balance is not None:
            updates.append("balance = ?")
            params.append(balance)
            if max_balance is None:
                updates.append("max_balance = GREATEST(COALESCE(max_balance, 0), ?)")
                params.append(balance)
        if max_balance is not None:
            updates.append("max_balance = ?")
            params.append(max_balance)
        if games is not None:
            updates.append("games = ?")
            params.append(games)
        if lost is not None:
            updates.append("lost = ?")
            params.append(lost)
        if bonus_time is not None:
            updates.append("bonus_time = ?")
            params.append(bonus_time)
        if username is not None:
            updates.append("username = ?")
            params.append(username)
        if first_name is not None:
            updates.append("first_name = ?")
            params.append(first_name)
        if registered_at is not None:
            updates.append("registered_at = ?")
            params.append(registered_at)
        if mp_balance is not None:
            updates.append("mp_balance = ?")
            params.append(mp_balance)
        if mp_daily_transferred is not None:
            updates.append("mp_daily_transferred = ?")
            params.append(mp_daily_transferred)
        if mp_daily_date is not None:
            updates.append("mp_daily_date = ?")
            params.append(mp_daily_date)
        if ref_earned is not None:
            updates.append("ref_earned = ?")
            params.append(ref_earned)
        if ref_count is not None:
            updates.append("ref_count = ?")
            params.append(ref_count)
        if referred_by is not None:
            updates.append("referred_by = ?")
            params.append(referred_by)
        if updates:
            params.append(user_id)
            query = f'UPDATE users SET {", ".join(updates)} WHERE user_id = ?'
            cursor.execute(query, params)
            conn.commit()
            return True
        return False
    except Exception:
        return False


def get_user_by_username(username):
    try:
        username_clean = username.lstrip('@').lower()
        cursor.execute('SELECT user_id, balance, games, lost, bonus_time, username, registered_at, mp_balance, mp_daily_transferred, mp_daily_date, ref_earned, ref_count, referred_by FROM users WHERE LOWER(username) = ?', (username_clean,))
        result = cursor.fetchone()
        if result:
            return {
                "user_id": result[0],
                "balance": result[1],
                "games": result[2],
                "lost": result[3],
                "bonus_time": result[4],
                "username": result[5],
                "registered_at": result[6],
                "mp_balance": result[7] if len(result) > 7 and result[7] is not None else 0,
                "mp_daily_transferred": result[8] if len(result) > 8 and result[8] is not None else 0,
                "mp_daily_date": result[9] if len(result) > 9 else None,
                "ref_earned": result[10] if len(result) > 10 and result[10] is not None else 0,
                "ref_count": result[11] if len(result) > 11 and result[11] is not None else 0,
                "referred_by": result[12] if len(result) > 12 else None
            }
        return None
    except Exception:
        return None


def get_user_display_name(user_id):
    cached_name = _known_names.get(user_id)
    if cached_name:
        return cached_name
    try:
        cursor.execute('SELECT first_name, username FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row:
            if row[0]:
                _known_names[user_id] = row[0]
                return row[0]
            if row[1]:
                return row[1]
        return f"ID:{user_id}"
    except Exception:
        return f"ID:{user_id}"


def get_user_mention(user_id, first_name=None):
    if first_name and first_name != "Мины Бот" and not str(first_name).lower().endswith("bot"):
        clean_name = html.escape(str(first_name))
        return f'<a href="tg://user?id={user_id}">{clean_name}</a>'
    cached_uname = _known_usernames.get(user_id)
    if cached_uname:
        return f'<a href="tg://user?id={user_id}">@{html.escape(cached_uname)}</a>'
    try:
        cursor.execute('SELECT username FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row and row[0]:
            _known_usernames[user_id] = row[0]
            return f'<a href="tg://user?id={user_id}">@{html.escape(row[0])}</a>'
    except Exception:
        pass
    return f'<a href="tg://user?id={user_id}">Игрок</a>'


def add_transfer_history(sender_id, receiver_id, amount, commission=0, created_at_str=None):
    try:
        if not created_at_str:
            created_at_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cursor.execute('''
            INSERT INTO transfers_history (sender_id, receiver_id, amount, commission, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (sender_id, receiver_id, amount, commission, created_at_str))
        conn.commit()
        return True
    except Exception:
        return False


def get_user_transfers(user_id, limit=5, offset=0):
    try:
        cursor.execute('''
            SELECT id, sender_id, receiver_id, amount, commission, created_at
            FROM transfers_history
            WHERE sender_id = ? OR receiver_id = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (user_id, user_id, limit, offset))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_transfers_count(user_id):
    try:
        cursor.execute('''
            SELECT COUNT(*) FROM transfers_history
            WHERE sender_id = ? OR receiver_id = ?
        ''', (user_id, user_id))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def get_last_game(user_id):
    try:
        cursor.execute('''
            SELECT game_type, bet, result, win_amount, created_at
            FROM games_history
            WHERE user_id = ?
            ORDER BY id DESC LIMIT 1
        ''', (user_id,))
        row = cursor.fetchone()
        if row:
            return {
                "game_type": row[0],
                "bet": row[1],
                "result": row[2],
                "win_amount": row[3],
                "created_at": row[4]
            }
        return None
    except Exception:
        return None


def save_username(user_id, username=None, first_name=None):
    if first_name and first_name != "Мины Бот" and not str(first_name).lower().endswith("bot"):
        _known_names[user_id] = first_name
    if username:
        _known_usernames[user_id] = username
    try:
        if first_name and username:
            cursor.execute('UPDATE users SET username = ?, first_name = ? WHERE user_id = ?', (username, first_name, user_id))
        elif first_name:
            cursor.execute('UPDATE users SET first_name = ? WHERE user_id = ?', (first_name, user_id))
        elif username:
            cursor.execute('UPDATE users SET username = ? WHERE user_id = ?', (username, user_id))
        conn.commit()
    except Exception:
        pass


def check_user_ban(user_id):
    if user_id not in _banned_users_cache:
        return None
    ban_info = _banned_users_cache[user_id]
    if ban_info.get("is_perm"):
        return ban_info
    until_dt = ban_info.get("until_dt")
    if until_dt and datetime.now() > until_dt:
        _banned_users_cache.pop(user_id, None)
        try:
            cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
            conn.commit()
        except Exception:
            pass
        return None
    return ban_info


def ban_user(user_id, reason, until_dt=None, is_permanent=False):
    try:
        until_str = until_dt.isoformat() if until_dt else None
        cursor.execute('''
            INSERT INTO bans (user_id, reason, until, is_permanent) VALUES (?, ?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                reason = EXCLUDED.reason,
                until = EXCLUDED.until,
                is_permanent = EXCLUDED.is_permanent
        ''', (user_id, reason, until_str, 1 if is_permanent else 0))
        conn.commit()
        if is_permanent:
            _banned_users_cache[user_id] = {"reason": reason, "until_str": "Навсегда", "until_dt": None, "is_perm": True}
        else:
            _banned_users_cache[user_id] = {"reason": reason, "until_str": until_dt.strftime("%d/%m/%y %H:%M") if until_dt else "", "until_dt": until_dt, "is_perm": False}
        return True
    except Exception:
        return False


def unban_user(user_id):
    try:
        _banned_users_cache.pop(user_id, None)
        cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
        conn.commit()
        return True
    except Exception:
        return False


def parse_ban_duration(text):
    text = text.lower().strip()
    if text in ["нг", "навсегда", "перм", "perm", "forever", "вечно", "inf"]:
        return None, True
    m = re.match(r'^(\d+)\s*(мин|м|m|min|минут|минуты|минута|ч|h|hour|часов|часа|час|д|d|day|дней|дня|день|мес|month|месяцев|месяца|месяц)?$', text)
    if not m:
        return None, False
    val = int(m.group(1))
    unit = (m.group(2) or "м").lower()
    if unit in ["мин", "м", "m", "min", "минут", "минуты", "минута"]:
        delta = timedelta(minutes=val)
    elif unit in ["ч", "h", "hour", "часов", "часа", "час"]:
        delta = timedelta(hours=val)
    elif unit in ["д", "d", "day", "дней", "дня", "день"]:
        delta = timedelta(days=val)
    elif unit in ["мес", "month", "месяцев", "месяца", "месяц"]:
        delta = timedelta(days=val * 30)
    else:
        delta = timedelta(minutes=val)
    return delta, False


def add_game_history(user_id, game_type, bet, result, win_amount=0, created_at_str=None):
    try:
        if not created_at_str:
            created_at_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
        cursor.execute('''
            INSERT INTO games_history (user_id, game_type, bet, result, win_amount, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, game_type, bet, result, win_amount, created_at_str))
        
        cursor.execute('INSERT INTO game_stats (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING', (user_id,))
        if game_type in ["mines", "tower", "diamonds", "crash", "slots", "bowling", "darts", "basketball", "football", "twentyone"]:
            cursor.execute(f'UPDATE game_stats SET {game_type} = {game_type} + 1 WHERE user_id = ?', (user_id,))
        conn.commit()

        try:
            asyncio.create_task(activate_referral_if_needed(user_id))
            if result == "lose" and bet > 0:
                asyncio.create_task(process_referral_loss_cashback(user_id, bet))
        except Exception:
            pass
    except Exception:
        pass


def get_user_stats(user_id):
    try:
        cursor.execute('SELECT mines, tower, diamonds, crash, slots, bowling, darts, basketball, football, COALESCE(twentyone, 0) FROM game_stats WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if row:
            mines, tower, diamonds, crash, slots, bowling, darts, basketball, football, twentyone = row
            slots = slots or 0
            bowling = bowling or 0
            darts = darts or 0
            basketball = basketball or 0
            football = football or 0
            twentyone = twentyone or 0
            total = mines + tower + diamonds + crash + slots + bowling + darts + basketball + football + twentyone
            return {
                "mines": mines, "tower": tower, "diamonds": diamonds, "crash": crash,
                "slots": slots, "bowling": bowling, "darts": darts, "basketball": basketball,
                "football": football, "twentyone": twentyone, "total": total
            }
        else:
            cursor.execute('SELECT games FROM users WHERE user_id = ?', (user_id,))
            u_row = cursor.fetchone()
            total_legacy = u_row[0] if u_row else 0
            return {
                "mines": total_legacy, "tower": 0, "diamonds": 0, "crash": 0,
                "slots": 0, "bowling": 0, "darts": 0, "basketball": 0, "football": 0,
                "twentyone": 0, "total": total_legacy
            }
    except Exception:
        return {"mines": 0, "tower": 0, "diamonds": 0, "crash": 0, "slots": 0, "bowling": 0, "darts": 0, "basketball": 0, "football": 0, "twentyone": 0, "total": 0}


def get_user_history(user_id, limit=5, offset=0):
    try:
        cursor.execute('''
            SELECT game_type, bet, result, win_amount, created_at FROM games_history
            WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (user_id, limit, offset))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_history_count(user_id):
    try:
        cursor.execute('SELECT COUNT(*) FROM games_history WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# --- BANK & DEPOSIT SYSTEM HELPERS ---
TIME_DEPOSIT_RATES = {
    1: 0.4,
    3: 1.5,
    7: 4.0,
    15: 10.0,
    30: 25.0,
    60: 65.0
}
BANK_MAX_LIMIT = 100000000


def get_bank_settings(user_id):
    try:
        cursor.execute("SELECT notifications_enabled FROM bank_settings WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row is not None:
            return {"notifications_enabled": bool(row[0])}
        cursor.execute("INSERT INTO bank_settings (user_id, notifications_enabled) VALUES (?, 1) ON CONFLICT (user_id) DO NOTHING", (user_id,))
        conn.commit()
        return {"notifications_enabled": True}
    except Exception:
        return {"notifications_enabled": True}


def toggle_bank_notifications(user_id):
    try:
        cur = get_bank_settings(user_id)["notifications_enabled"]
        new_val = 0 if cur else 1
        cursor.execute("INSERT INTO bank_settings (user_id, notifications_enabled) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET notifications_enabled = EXCLUDED.notifications_enabled", (user_id, new_val))
        conn.commit()
        return bool(new_val)
    except Exception:
        return True


def get_user_bank_total(user_id):
    try:
        cursor.execute("SELECT SUM(amount) FROM time_deposits WHERE user_id = ? AND status = 'active'", (user_id,))
        r1 = cursor.fetchone()
        time_total = r1[0] if (r1 and r1[0]) else 0
        cursor.execute("SELECT balance FROM savings_accounts WHERE user_id = ?", (user_id,))
        r2 = cursor.fetchone()
        savings_bal = r2[0] if (r2 and r2[0]) else 0
        return time_total + savings_bal
    except Exception:
        return 0


def create_time_deposit(user_id, amount, days, is_locked=0):
    try:
        if amount <= 0:
            return False, "Сумма должна быть больше 0!"
        rate = TIME_DEPOSIT_RATES.get(days, 0.4)
        profit = int(amount * rate / 100)
        now = datetime.now()
        end_dt = now + timedelta(days=days)
        now_str = now.strftime("%d-%m-%y %H:%M")
        end_str = end_dt.strftime("%d-%m-%y %H:%M")
        now_iso = now.isoformat()
        end_iso = end_dt.isoformat()

        current_bank_total = get_user_bank_total(user_id)
        if current_bank_total + amount > BANK_MAX_LIMIT:
            max_can_add = max(0, BANK_MAX_LIMIT - current_bank_total)
            return False, f"Превышен лимит банка (100kk mCoin)! Вы можете внести ещё максимум {format_number(max_can_add)} m¢."

        cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, user_id, amount))
        if cursor.rowcount == 0:
            return False, "Недостаточно средств на балансе!"

        cursor.execute('''
            INSERT INTO time_deposits (user_id, amount, days, percent, profit, is_locked, status, created_at, end_at, created_at_dt, end_at_dt)
            VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        ''', (user_id, amount, days, rate, profit, 1 if is_locked else 0, now_str, end_str, now_iso, end_iso))
        conn.commit()
        return True, {"amount": amount, "days": days, "percent": rate, "profit": profit, "is_locked": is_locked, "end_at": end_str}
    except Exception as e:
        return False, str(e)


def get_active_time_deposits(user_id):
    try:
        cursor.execute('''
            SELECT id, amount, days, percent, profit, is_locked, created_at, end_at, created_at_dt, end_at_dt
            FROM time_deposits
            WHERE user_id = ? AND status = 'active'
            ORDER BY id DESC
        ''', (user_id,))
        return cursor.fetchall()
    except Exception:
        return []


def get_active_time_deposits_count(user_id):
    try:
        cursor.execute("SELECT COUNT(*) FROM time_deposits WHERE user_id = ? AND status = 'active'", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def get_user_earned_deposits_total(user_id):
    try:
        cursor.execute("SELECT SUM(profit) FROM time_deposits WHERE user_id = ? AND status = 'completed'", (user_id,))
        r1 = cursor.fetchone()
        dep_profit = r1[0] if (r1 and r1[0]) else 0
        cursor.execute("SELECT total_earned FROM savings_accounts WHERE user_id = ?", (user_id,))
        r2 = cursor.fetchone()
        sav_profit = r2[0] if (r2 and r2[0]) else 0
        return dep_profit + sav_profit
    except Exception:
        return 0


def get_user_time_deposits_history(user_id, limit=5, offset=0):
    try:
        cursor.execute('''
            SELECT id, amount, days, percent, profit, is_locked, status, created_at, end_at
            FROM time_deposits
            WHERE user_id = ?
            ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (user_id, limit, offset))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_time_deposits_history_count(user_id):
    try:
        cursor.execute("SELECT COUNT(*) FROM time_deposits WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def withdraw_time_deposit(dep_id, user_id):
    try:
        cursor.execute("SELECT amount, is_locked, status FROM time_deposits WHERE id = ? AND user_id = ?", (dep_id, user_id))
        row = cursor.fetchone()
        if not row:
            return False, "Депозит не найден!"
        amount, is_locked, status = row
        if status != "active":
            return False, "Депозит уже закрыт!"
        if is_locked:
            return False, "Снятие данного депозита было заблокировано при открытии!"

        cursor.execute("UPDATE time_deposits SET status = 'withdrawn' WHERE id = ? AND user_id = ? AND status = 'active' AND is_locked = 0", (dep_id, user_id))
        if cursor.rowcount == 0:
            return False, "Депозит уже закрыт или заблокирован!"

        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (amount, amount, user_id))
        conn.commit()
        return True, amount
    except Exception as e:
        return False, str(e)


def withdraw_all_time_deposits(user_id):
    try:
        cursor.execute("SELECT id, amount, is_locked FROM time_deposits WHERE user_id = ? AND status = 'active' AND is_locked = 0", (user_id,))
        rows = cursor.fetchall()
        if not rows:
            return False, "Нет доступных для досрочного снятия депозитов (или они заблокированы)!"

        total_withdrawn = 0
        for dep_id, amount, is_locked in rows:
            cursor.execute("UPDATE time_deposits SET status = 'withdrawn' WHERE id = ? AND user_id = ? AND status = 'active' AND is_locked = 0", (dep_id, user_id))
            if cursor.rowcount > 0:
                total_withdrawn += amount

        if total_withdrawn <= 0:
            return False, "Нет доступных для досрочного снятия депозитов!"

        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (total_withdrawn, total_withdrawn, user_id))
        conn.commit()
        return True, total_withdrawn
    except Exception as e:
        return False, str(e)


def get_savings_account(user_id):
    try:
        cursor.execute("SELECT balance, accumulated_interest, total_earned, last_accrual FROM savings_accounts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {
                "balance": row[0],
                "accumulated_interest": row[1],
                "total_earned": row[2],
                "last_accrual": row[3]
            }
        cursor.execute("INSERT INTO savings_accounts (user_id, balance, accumulated_interest, total_earned, last_accrual) VALUES (?, 0, 0.0, 0, ?) ON CONFLICT (user_id) DO NOTHING", (user_id, datetime.now().isoformat()))
        conn.commit()
        return {"balance": 0, "accumulated_interest": 0.0, "total_earned": 0, "last_accrual": datetime.now().isoformat()}
    except Exception:
        return {"balance": 0, "accumulated_interest": 0.0, "total_earned": 0, "last_accrual": None}


def deposit_to_savings(user_id, amount):
    try:
        if amount <= 0:
            return False, "Сумма должна быть больше 0!"
        current_bank_total = get_user_bank_total(user_id)
        if current_bank_total + amount > BANK_MAX_LIMIT:
            max_can_add = max(0, BANK_MAX_LIMIT - current_bank_total)
            return False, f"Превышен лимит банка (100kk mCoin)! Вы можете внести ещё максимум {format_number(max_can_add)} m¢."

        cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (amount, user_id, amount))
        if cursor.rowcount == 0:
            return False, "Недостаточно средств на балансе!"

        get_savings_account(user_id)
        cursor.execute("UPDATE savings_accounts SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        now_str = datetime.now().strftime("%d-%m-%y %H:%M")
        cursor.execute("INSERT INTO savings_history (user_id, type, amount, created_at) VALUES (?, 'deposit', ?, ?)", (user_id, amount, now_str))
        conn.commit()

        acc = get_savings_account(user_id)
        return True, acc["balance"]
    except Exception as e:
        return False, str(e)


def withdraw_from_savings(user_id, amount):
    try:
        if amount <= 0:
            return False, "Сумма должна быть больше 0!"
        acc = get_savings_account(user_id)
        total_available = acc["balance"] + int(acc["accumulated_interest"])
        if total_available <= 0:
            return False, "На накопительном счете нет средств!"
        if amount > total_available:
            amount = total_available

        interest_to_wd = min(int(acc["accumulated_interest"]), amount)
        balance_to_wd = amount - interest_to_wd

        cursor.execute('''
            UPDATE savings_accounts
            SET balance = balance - ?,
                accumulated_interest = GREATEST(0.0, accumulated_interest - ?),
                total_earned = total_earned + ?
            WHERE user_id = ? AND balance >= ?
        ''', (balance_to_wd, interest_to_wd, interest_to_wd, user_id, balance_to_wd))
        if cursor.rowcount == 0:
            return False, "Недостаточно средств на накопительном счете!"

        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (amount, amount, user_id))
        now_str = datetime.now().strftime("%d-%m-%y %H:%M")
        cursor.execute("INSERT INTO savings_history (user_id, type, amount, created_at) VALUES (?, 'withdraw', ?, ?)", (user_id, amount, now_str))
        conn.commit()

        user_data = get_user(user_id)
        return True, {"withdrawn": amount, "balance": user_data["balance"]}
    except Exception as e:
        return False, str(e)


def get_savings_history(user_id, limit=5, offset=0):
    try:
        cursor.execute("SELECT type, amount, created_at FROM savings_history WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?", (user_id, limit, offset))
        return cursor.fetchall()
    except Exception:
        return []


def get_savings_history_count(user_id):
    try:
        cursor.execute("SELECT COUNT(*) FROM savings_history WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


# --- REFERRAL SYSTEM HELPERS ---
def register_referral(referrer_id, referral_id):
    try:
        if referrer_id == referral_id:
            return False
        cursor.execute("SELECT referred_by, games, lost FROM users WHERE user_id = ?", (referral_id,))
        row = cursor.fetchone()
        if row:
            referred_by, games_count, lost_count = row[0], row[1] or 0, row[2] or 0
            if referred_by or games_count > 0 or lost_count > 0:
                return False
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
        cursor.execute('''
            INSERT INTO referrals (referrer_id, referral_id, is_active, bonus_paid, earned_from_losses, joined_at)
            VALUES (?, ?, 0, 0, 0, ?)
            ON CONFLICT (referral_id) DO NOTHING
        ''', (referrer_id, referral_id, now_str))
        cursor.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (referrer_id, referral_id))
        conn.commit()
        return True
    except Exception:
        return False


async def activate_referral_if_needed(user_id):
    try:
        cursor.execute("SELECT referrer_id, is_active FROM referrals WHERE referral_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or row[1] == 1:
            return
        referrer_id = row[0]
        cursor.execute("UPDATE referrals SET is_active = 1, bonus_paid = 100000 WHERE referral_id = ?", (user_id,))
        ref_user = get_user(referrer_id)
        update_user(referrer_id, balance=ref_user["balance"] + 100000, ref_count=ref_user["ref_count"] + 1, ref_earned=ref_user["ref_earned"] + 100000)
        conn.commit()

        try:
            name_val = get_user_display_name(user_id)
            user_link = f'<a href="tg://user?id={user_id}">{html.escape(name_val)}</a>'
            msg_text = (
                f'🎁 <b>Реферальный бонус!</b>\n'
                f'Твой друг {user_link} выполнил условие (сыграл игру или забрал бонус).\n'
                f'Тебе начислено <b>100\'000 mCoin</b>!'
            )
            await bot.send_message(chat_id=referrer_id, text=msg_text, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    except Exception:
        pass


async def process_referral_loss_cashback(user_id, lost_amount):
    if lost_amount <= 0:
        return
    try:
        cursor.execute("SELECT referrer_id, is_active FROM referrals WHERE referral_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or row[1] != 1:
            return
        referrer_id = row[0]
        cashback = int(lost_amount * 0.02)
        if cashback < 1:
            return
        ref_user = get_user(referrer_id)
        update_user(referrer_id, balance=ref_user["balance"] + cashback, ref_earned=ref_user["ref_earned"] + cashback)
        cursor.execute("UPDATE referrals SET earned_from_losses = earned_from_losses + ? WHERE referral_id = ?", (cashback, user_id))
        conn.commit()
    except Exception:
        pass


def get_top_referrers(limit=10):
    try:
        cursor.execute('''
            SELECT user_id, ref_count, ref_earned FROM users
            WHERE ref_count > 0
            ORDER BY ref_count DESC, ref_earned DESC LIMIT ?
        ''', (limit,))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_referrals_list(referrer_id, limit=5, offset=0):
    try:
        cursor.execute('''
            SELECT referral_id, is_active, bonus_paid, earned_from_losses, joined_at
            FROM referrals
            WHERE referrer_id = ?
            ORDER BY referral_id DESC LIMIT ? OFFSET ?
        ''', (referrer_id, limit, offset))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_referrals_count(referrer_id):
    try:
        cursor.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (referrer_id,))
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def is_bot_command(text, chat_type="private"):
    if not text:
        return False
    text = text.strip().lower()
    
    if chat_type == "private":
        return True
        
    if text.startswith("/"):
        return True
    
    parts = text.split()
    first = parts[0]
    
    command_triggers = [
        "мины", "м", "башня", "бш", "игры", "игра", "баланс", "б", "бал",
        "бонус", "топ", "дать", "пер", "раздача", "роздача",
        "история", "недавние", "алмазы", "алмаз", "diamond",
        "краш", "crash", "кр",
        "слоты", "слот", "slots", "slot",
        "боулинг", "bowling", "бо",
        "дартс", "darts", "дс", "dart",
        "баскетбол", "basketball", "баскет", "бс", "бк",
        "футбол", "football", "фб",
        "промо", "promo",
        "гет", "get",
        "поинт", "поинты", "point", "points", "send", "перевод",
        "рекорды", "рекорд", "records",
        "депозит", "депозиты", "деп", "банк", "deposit",
        "реф", "рефералы", "реферал", "ref", "referral"
    ]
    
    if first in command_triggers:
        return True
        
    if text in ["топ чата", "топ ч", "история игр", "недавние игры", "топ рекорды", "топ рекордов", "топ рекорд"]:
        return True
        
    return False


@dp.message.outer_middleware()
async def save_username_middleware(handler, event, data):
    if event.from_user:
        save_username(event.from_user.id, event.from_user.username, event.from_user.first_name)
        if event.chat and event.chat.type in ["group", "supergroup"]:
            add_chat_member(event.from_user.id, event.chat.id)
        ban_info = check_user_ban(event.from_user.id)
        if ban_info:
            chat_type = event.chat.type if event.chat else "private"
            msg_text = event.text or event.caption or ""
            
            if is_bot_command(msg_text, chat_type):
                raw_user_name = event.from_user.first_name or "Игрок"
                clean_user_name = html.escape(raw_user_name)
                user_link = f'<a href="tg://user?id={event.from_user.id}">{clean_user_name}</a>'
                await event.reply(
                    f'<b>{user_link} ты заблокирован! 🚫\n</b>'
                    f'<i>Причина: {ban_info["reason"]}\n</i>'
                    f'<i>До: {ban_info["until_str"]}</i>',
                    parse_mode=ParseMode.HTML
                )
            return
    return await handler(event, data)


@dp.callback_query.outer_middleware()
async def save_username_cb_middleware(handler, event, data):
    if event.from_user:
        save_username(event.from_user.id, event.from_user.username, event.from_user.first_name)
        if event.message and event.message.chat and event.message.chat.type in ["group", "supergroup"]:
            add_chat_member(event.from_user.id, event.message.chat.id)
        ban_info = check_user_ban(event.from_user.id)
        if ban_info:
            await event.answer(
                f'Ты заблокирован! 🚫\nПричина: {ban_info["reason"]}\nДо: {ban_info["until_str"]}',
                show_alert=True
            )
            return
    return await handler(event, data)


def add_chat_member(user_id, chat_id):
    key = (user_id, chat_id)
    if key in _known_chat_members:
        return True
    _known_chat_members.add(key)
    try:
        cursor.execute('INSERT INTO chat_members (user_id, chat_id) VALUES (?, ?) ON CONFLICT (user_id, chat_id) DO NOTHING', (user_id, chat_id))
        conn.commit()
        return True
    except Exception:
        return False


def get_top_users(limit=10, chat_id=None):
    try:
        if chat_id:
            cursor.execute('''
                SELECT user_id, balance FROM users 
                WHERE user_id NOT IN (SELECT user_id FROM top_bans)
                AND user_id IN (
                    SELECT user_id FROM chat_members WHERE chat_id = ?
                    UNION
                    SELECT owner_id AS user_id FROM active_games_state WHERE chat_id = ?
                )
                ORDER BY balance DESC LIMIT ?
            ''', (chat_id, chat_id, limit))
        else:
            cursor.execute('''
                SELECT user_id, balance FROM users 
                WHERE user_id NOT IN (SELECT user_id FROM top_bans)
                ORDER BY balance DESC LIMIT ?
            ''', (limit,))
        return cursor.fetchall()
    except Exception:
        return []


def get_user_rank(user_id, chat_id=None):
    if is_top_banned(user_id):
        return 0
    try:
        if chat_id:
            cursor.execute('''
                SELECT COUNT(*) + 1 FROM users 
                WHERE user_id NOT IN (SELECT user_id FROM top_bans)
                AND balance > (SELECT balance FROM users WHERE user_id = ?)
                AND user_id IN (
                    SELECT user_id FROM chat_members WHERE chat_id = ?
                    UNION
                    SELECT owner_id AS user_id FROM active_games_state WHERE chat_id = ?
                )
            ''', (user_id, chat_id, chat_id))
        else:
            cursor.execute('''
                SELECT COUNT(*) + 1 FROM users 
                WHERE user_id NOT IN (SELECT user_id FROM top_bans)
                AND balance > (SELECT balance FROM users WHERE user_id = ?)
            ''', (user_id,))
        result = cursor.fetchone()
        return result[0] if result else 0
    except Exception:
        return 0


def format_number(num):
    num = int(num)
    if num >= 1000000000000:
        v = num / 1000000000000
        s = f"{v:.2f}".rstrip('0').rstrip('.')
        return f"{s}kkkk"
    elif num >= 1000000000:
        v = num / 1000000000
        s = f"{v:.2f}".rstrip('0').rstrip('.')
        return f"{s}kkk"
    elif num >= 1000000:
        v = num / 1000000
        s = f"{v:.2f}".rstrip('0').rstrip('.')
        return f"{s}kk"

    return f"{num:,}".replace(",", "'")


def parse_amount(text):
    if not text:
        return None
    text = text.lower().strip().replace("'", "").replace(" ", "")

    multiplier = 1
    if text.endswith('кккк') or text.endswith('kkkk'):
        multiplier = 1000000000000
        text = text[:-4]
    elif text.endswith('ккк') or text.endswith('kkk'):
        multiplier = 1000000000
        text = text[:-3]
    elif text.endswith('кк') or text.endswith('kk'):
        multiplier = 1000000
        text = text[:-2]
    elif text.endswith('к') or text.endswith('k'):
        multiplier = 1000
        text = text[:-1]

    if multiplier == 1 and re.match(r'^\d{1,3}([.,]\d{3})+$', text):
        text = re.sub(r'[.,]', '', text)
    else:
        text = text.replace(",", ".")

    try:
        num = float(text)
        return int(num * multiplier)
    except ValueError:
        return None


def resolve_bet_amount(text, balance=0):
    if not text:
        return None
    t = text.lower().strip()
    if t in ["все", "всё", "all", "allin", "вб", "ва-банк", "вабанк", "ва банк"]:
        return balance
    if t in ["пол", "половина", "half", "1/2"]:
        return balance // 2
    return parse_amount(text)


def parse_multiplier(text):
    if not text:
        return None
    text = text.lower().strip()
    text = text.lstrip("xх").rstrip("xх")
    text = text.replace("'", "").replace(" ", "").replace(",", ".")

    multiplier = 1.0
    if text.endswith('кккк') or text.endswith('kkkk'):
        multiplier = 1000000000000.0
        text = text[:-4]
    elif text.endswith('ккк') or text.endswith('kkk'):
        multiplier = 1000000000.0
        text = text[:-3]
    elif text.endswith('кк') or text.endswith('kk'):
        multiplier = 1000000.0
        text = text[:-2]
    elif text.endswith('к') or text.endswith('k'):
        multiplier = 1000.0
        text = text[:-1]

    try:
        num = float(text)
        return round(num * multiplier, 2)
    except ValueError:
        return None


def generate_mines(count):
    rng = secrets.SystemRandom()
    positions = list(range(25))
    rng.shuffle(positions)
    return set(rng.sample(positions, count))


def generate_tower_mines(count):
    rng = secrets.SystemRandom()
    tower = []
    for _ in range(TOWER_ROWS):
        cols = list(range(TOWER_COLS))
        rng.shuffle(cols)
        tower.append(set(rng.sample(cols, count)))
    return tower


def generate_diamonds_mines(count):
    rng = secrets.SystemRandom()
    diamonds_tower = []
    for _ in range(DIAMONDS_ROWS):
        cols = list(range(DIAMONDS_COLS))
        rng.shuffle(cols)
        diamonds_tower.append(set(rng.sample(cols, count)))
    return diamonds_tower


def get_tower_multiplier(mine_count, level):
    multipliers = {
        1: {
            0: 1.00, 1: 1.21, 2: 1.52, 3: 1.89, 4: 2.37, 5: 2.96, 6: 3.70, 7: 4.63, 8: 5.78, 9: 7.23
        },
        2: {
            0: 1.00, 1: 1.62, 2: 2.69, 3: 4.49, 4: 7.48, 5: 12.47, 6: 20.79, 7: 34.65, 8: 57.75, 9: 96.25
        },
        3: {
            0: 1.00, 1: 2.42, 2: 6.06, 3: 15.16, 4: 37.89, 5: 94.73, 6: 238.62, 7: 592.04, 8: 1480.10, 9: 3700.25
        },
        4: {
            0: 1.00, 1: 4.85, 2: 24.25, 3: 121.25, 4: 606.25, 5: 3031.25, 6: 15156.25, 7: 75781.25, 8: 378906.25, 9: 1894531.25
        }
    }
    return multipliers.get(mine_count, {}).get(level, 1.00)


def get_diamonds_multiplier(mine_count, level):
    multipliers = {
        1: {
            0: 1.00, 1: 1.46, 2: 2.18, 3: 3.27, 4: 4.91, 5: 7.37, 6: 11.05,
            7: 16.57, 8: 24.86, 9: 37.29, 10: 55.94, 11: 83.90, 12: 125.86,
            13: 188.79, 14: 283.18, 15: 424.77, 16: 637.16
        },
        2: {
            0: 1.00, 1: 2.91, 2: 8.73, 3: 26.19, 4: 78.57, 5: 235.71, 6: 707.13,
            7: 2121.39, 8: 6364.17, 9: 19092.51, 10: 57277.53, 11: 171832.59,
            12: 515497.77, 13: 1546493.31, 14: 4639479.93, 15: 13918439.79, 16: 41755319.37
        }
    }
    return multipliers.get(mine_count, {}).get(level, 1.00)


def get_multiplier(mine_count, level):
    multipliers = {
        1: {
            0: 1.00, 1: 1.01, 2: 1.05, 3: 1.10, 4: 1.15, 5: 1.21, 6: 1.28, 7: 1.35,
            8: 1.43, 9: 1.52, 10: 1.62, 11: 1.73, 12: 1.87, 13: 2.02, 14: 2.20,
            15: 2.41, 16: 2.66, 17: 2.96, 18: 3.32, 19: 3.76, 20: 4.31, 21: 5.00,
            22: 5.88, 23: 7.04, 24: 8.59
        },
        2: {
            0: 1.00, 1: 1.05, 2: 1.15, 3: 1.26, 4: 1.39, 5: 1.53, 6: 1.70, 7: 1.90,
            8: 2.14, 9: 2.42, 10: 2.77, 11: 3.20, 12: 3.73, 13: 4.41, 14: 5.29,
            15: 6.47, 16: 8.09, 17: 10.37, 18: 13.68, 19: 18.72, 20: 26.74,
            21: 40.43, 22: 65.96, 23: 120.19
        },
        3: {
            0: 1.00, 1: 1.10, 2: 1.26, 3: 1.45, 4: 1.68, 5: 1.96, 6: 2.30, 7: 2.73,
            8: 3.28, 9: 3.98, 10: 4.90, 11: 6.13, 12: 7.80, 13: 10.14, 14: 13.51,
            15: 18.54, 16: 26.34, 17: 38.94, 18: 60.32, 19: 98.76, 20: 172.97,
            21: 330.78, 22: 714.30
        },
        4: {
            0: 1.00, 1: 1.15, 2: 1.39, 3: 1.68, 4: 2.05, 5: 2.53, 6: 3.17, 7: 4.01,
            8: 5.16, 9: 6.74, 10: 8.96, 11: 12.16, 12: 16.91, 13: 24.18, 14: 35.75,
            15: 55.06, 16: 88.93, 17: 151.98, 18: 278.30, 19: 552.47, 20: 1209.71,
            21: 3029.88
        },
        5: {
            0: 1.00, 1: 1.21, 2: 1.53, 3: 1.96, 4: 2.53, 5: 3.32, 6: 4.43, 7: 6.01,
            8: 8.33, 9: 11.80, 10: 17.16, 11: 25.75, 12: 40.13, 13: 65.21, 14: 111.78,
            15: 204.94, 16: 409.88, 17: 922.22, 18: 2459.26, 19: 8607.41, 20: 51644.48
        },
        6: {
            0: 1.00, 1: 1.28, 2: 1.70, 3: 2.30, 4: 3.17, 5: 4.43, 6: 6.33, 7: 9.25,
            8: 13.88, 9: 21.35, 10: 33.93, 11: 56.03, 12: 96.94, 13: 177.72, 14: 350.51,
            15: 764.74, 16: 1891.07, 17: 5673.22, 18: 22692.89, 19: 170196.67
        }
    }
    if mine_count in multipliers and level in multipliers[mine_count]:
        return multipliers[mine_count][level]

    try:
        safe = 25 - mine_count
        if level <= 0 or level > safe:
            return 1.00
        p = math.comb(safe, level) / math.comb(25, level)
        return round(0.97 / p, 2)
    except Exception:
        return 1.00


def get_chat_key(user_id, chat_id):
    return f"{user_id}_{chat_id}"


user_messages = {}
game_messages = {}
games = {}
active_games = {}
game_counter = 0
exit_confirm = {}


def save_active_game_to_db(game_id, g):
    try:
        gtype = g.get("type", "mines")
        if gtype == "twentyone":
            mp_json = json.dumps(g.get("player_cards", []))
            rev_json = json.dumps({
                "dealer_cards": g.get("dealer_cards", []),
                "deck": g.get("deck", []),
                "user_first_name": g.get("user_first_name", "")
            })
        else:
            mp = g.get("mine_positions")
            if isinstance(mp, set):
                mp_json = json.dumps(list(mp))
            elif isinstance(mp, list):
                mp_json = json.dumps([list(s) if isinstance(s, set) else s for s in mp])
            else:
                mp_json = json.dumps(mp)

            rev = list(g.get("revealed", []))
            rev_json = json.dumps(rev)

        dt_str = g.get("created_at_str")
        if not dt_str:
            created_at = g.get("created_at")
            if isinstance(created_at, datetime):
                dt_str = created_at.strftime("%d-%m-%Y %H:%M:%S")
            else:
                dt_str = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

        cursor.execute('''
            INSERT INTO active_games_state
            (game_id, owner_id, chat_id, game_type, stage, bet, mine_count, mine_positions, revealed, level, game_over, won, exploded_mine, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (game_id) DO UPDATE SET
                owner_id = EXCLUDED.owner_id,
                chat_id = EXCLUDED.chat_id,
                game_type = EXCLUDED.game_type,
                stage = EXCLUDED.stage,
                bet = EXCLUDED.bet,
                mine_count = EXCLUDED.mine_count,
                mine_positions = EXCLUDED.mine_positions,
                revealed = EXCLUDED.revealed,
                level = EXCLUDED.level,
                game_over = EXCLUDED.game_over,
                won = EXCLUDED.won,
                exploded_mine = EXCLUDED.exploded_mine,
                created_at = EXCLUDED.created_at
        ''', (
            game_id, g["owner_id"], g["chat_id"], g.get("type", "mines"), g.get("stage", "playing"),
            g.get("bet", 0), g.get("mine_count", 0), mp_json, rev_json, g.get("level", 0),
            1 if g.get("game_over") else 0, 1 if g.get("won") else 0,
            g.get("exploded_mine"), dt_str
        ))
        conn.commit()
        try:
            from redis_client import redis_set_active_game
            asyncio.create_task(redis_set_active_game(g["owner_id"], game_id, g))
        except Exception:
            pass
    except Exception:
        pass


def remove_active_game_from_db(game_id):
    try:
        cursor.execute('DELETE FROM active_games_state WHERE game_id = ?', (game_id,))
        conn.commit()
        try:
            from redis_client import redis_remove_active_game
            g = games.get(game_id)
            owner_id = g["owner_id"] if g else 0
            if owner_id:
                asyncio.create_task(redis_remove_active_game(owner_id, game_id))
        except Exception:
            pass
    except Exception:
        pass


def load_all_active_games_from_db():
    try:
        cursor.execute('SELECT game_id, owner_id, chat_id, game_type, stage, bet, mine_count, mine_positions, revealed, level, game_over, won, exploded_mine, created_at FROM active_games_state')
        rows = cursor.fetchall()
        for r in rows:
            gid, oid, cid, gtype, stage, bet, mcount, mp_json, rev_json, level, gover, won, exp_mine, dt_str = r
            try:
                created_dt = datetime.strptime(dt_str, "%d-%m-%Y %H:%M:%S")
            except Exception:
                created_dt = datetime.now()

            if gtype == "twentyone":
                try:
                    player_cards = [tuple(c) for c in json.loads(mp_json)] if mp_json else []
                    rev_dict = json.loads(rev_json) if rev_json else {}
                    dealer_cards = [tuple(c) for c in rev_dict.get("dealer_cards", [])] if isinstance(rev_dict, dict) else []
                    deck = [tuple(c) for c in rev_dict.get("deck", [])] if isinstance(rev_dict, dict) else []
                    user_first_name = rev_dict.get("user_first_name", "") if isinstance(rev_dict, dict) else ""
                except Exception:
                    player_cards, dealer_cards, deck, user_first_name = [], [], [], ""

                games[gid] = {
                    "type": gtype,
                    "stage": stage,
                    "bet": bet,
                    "deck": deck or create_deck_21(),
                    "player_cards": player_cards,
                    "dealer_cards": dealer_cards,
                    "owner_id": oid,
                    "chat_id": cid,
                    "game_over": bool(gover),
                    "won": bool(won),
                    "created_at": created_dt,
                    "created_at_str": dt_str,
                    "settled": bool(gover) or bool(won),
                    "expired": False,
                    "message_id": None,
                    "user_first_name": user_first_name
                }
                if not bool(gover):
                    active_games[oid] = gid
                continue

            try:
                mp_raw = json.loads(mp_json) if mp_json else []
                if gtype == "mines":
                    mine_positions = set(mp_raw)
                elif gtype in ["tower", "diamonds"]:
                    mine_positions = [set(s) if isinstance(s, list) else s for s in mp_raw]
                else:
                    mine_positions = mp_raw
            except Exception:
                mine_positions = set()

            try:
                revealed = set(json.loads(rev_json)) if rev_json else set()
            except Exception:
                revealed = set()

            games[gid] = {
                "type": gtype,
                "stage": stage,
                "bet": bet,
                "mine_count": mcount,
                "mine_positions": mine_positions,
                "revealed": revealed,
                "level": level,
                "game_over": bool(gover),
                "won": bool(won),
                "exploded_mine": exp_mine,
                "owner_id": oid,
                "chat_id": cid,
                "cashed_out": False,
                "bet_placed": len(revealed) > 0,
                "moves_count": len(revealed),
                "created_at": created_dt,
                "created_at_str": dt_str,
                "settled": False,
                "expired": False,
                "message_id": None,
                "is_edited": False
            }
            if not bool(gover):
                active_games[oid] = gid
    except Exception:
        pass


def get_next_game_id():
    global game_counter
    try:
        cursor.execute('SELECT MAX(game_id) FROM active_games_state')
        row = cursor.fetchone()
        db_max = row[0] if (row and row[0]) else 0
    except Exception:
        db_max = 0
    game_counter = max(game_counter + 1, db_max + 1, int(datetime.now().timestamp()))
    return game_counter


load_all_active_games_from_db()

GRID_SIZE = 5
TOTAL_CELLS = 25
TOWER_ROWS = 9
TOWER_COLS = 5
DIAMONDS_ROWS = 16
DIAMONDS_COLS = 3

event_participants = {}
event_message_id = None
event_chat_id = None
event_number = None
event_prize = None
event_active = False
event_started = False
event_timer_task = None
event_timeout_task = None
event_min_num = 1
event_max_num = 250

EMOJI_IDS_TO_CHECK = [
    "5206607081334906820", "5247011187308140698", "5255703720078879038",
    "5276032951342088188", "5280769763398671636", "5307594157739515229",
    "5309815458990433715", "5350612670435313545", "5372926953978341366",
    "5372981976804366741", "5386367538735104399", "5393194986252542669",
    "5418238674267556907", "5427009714745517609", "5431577498364158238",
    "5436040291507247633", "5436113877181941026", "5442983582882601962",
    "5465368548702446780", "5467671759274661866", "5469654973308476699",
    "5469785308386041323", "5471960722206366390", "5472212780952066876",
    "5472255352667904566", "5472283700884660912", "5836936408681421518",
    "5852868430952144622", "5881948563591666817", "5920090136627908485",
    "5809949600152296075", "5283080528818360566", "544528490978621387",
    "5361741454685256344", "5258203794772085854"
]


async def get_chat_members(chat_id):
    try:
        members = []
        async for member in bot.get_chat_members(chat_id):
            members.append(member.user.id)
        return members
    except Exception:
        return []


async def check_expired_games_task():
    while True:
        try:
            now = datetime.now()
            expired_ids = []
            for gid, gdata in list(games.items()):
                if not gdata.get("game_over", False) and not gdata.get("settled", False) and not gdata.get("expired", False):
                    created_at = gdata.get("created_at")
                    if created_at and (now - created_at) > timedelta(hours=12):
                        expired_ids.append(gid)

            for gid in expired_ids:
                gdata = games.get(gid)
                if not gdata or gdata.get("settled", False) or gdata.get("expired", False):
                    continue

                gdata["game_over"] = True
                gdata["expired"] = True
                gdata["settled"] = True

                uid = gdata["owner_id"]
                bet = gdata.get("bet", 0)
                gtype = gdata.get("type", "mines")
                name_map = {"mines": "мины", "tower": "башня", "diamonds": "алмазы", "twentyone": "21"}
                game_title = name_map.get(gtype, "мины")

                udata = get_user(uid)
                update_user(uid, balance=udata["balance"] + bet)

                dt_str = gdata.get("created_at_str", now.strftime("%d-%m-%Y %H:%M:%S"))
                add_game_history(uid, gtype, bet, "expired", 0, dt_str)
                remove_active_game_from_db(gid)

                msg_text = (
                    f'💾 Игра «{game_title}», начавшаяся {dt_str}, была завершена из-за бездействия. '
                    f'<b>{format_number(bet)} mCoin были возвращены на баланс.</b>'
                )

                try:
                    await bot.send_message(chat_id=uid, text=msg_text, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

                if active_games.get(uid) == gid:
                    del active_games[uid]

        except Exception:
            pass
        await asyncio.sleep(60)


async def show_catalog(message_or_callback, user_id, user_name, chat_id, is_group=False, edit=False):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '<b><tg-emoji emoji-id="5309815458990433715">🎮</tg-emoji>КАТАЛОГ ИГР\n'
        '<code>·····················</code></b>\n'
        '<blockquote> <i>ℹ️ В этом разделе вы можете ознакомиться со всеми играми, их описанием и инструкцией по запуску.</i></blockquote>'
    )

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text="Быстрые",
                callback_data="catalog_fast",
                style="primary",
                icon_custom_emoji_id="5449683594425410231"
            ),
            InlineKeyboardButton(
                text="Режимы",
                callback_data="catalog_modes",
                style="primary",
                icon_custom_emoji_id="5361741454685256344"
            )
        ]
    ]

    if not is_group:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data="back",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if isinstance(message_or_callback, types.Message):
        msg = await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        if edit:
            try:
                msg = await message_or_callback.message.edit_text(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            except Exception:
                msg = await message_or_callback.message.answer(
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
        else:
            msg = await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )

    key = get_chat_key(user_id, chat_id)
    if key not in user_messages:
        user_messages[key] = []
    if msg:
        user_messages[key].append(msg.message_id)


async def show_fast_games_catalog(callback: types.CallbackQuery, user_id, user_name, chat_id, is_group=False):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '<b><tg-emoji emoji-id="5449683594425410231">⚡</tg-emoji>БЫСТРЫЕ ИГРЫ\n'
        '<code>·····················</code></b>\n'
        '<blockquote> <i>ℹ️ Выберите быструю игру из списка ниже:</i></blockquote>'
    )

    keyboard_buttons = [
        [
            InlineKeyboardButton(text="🎰 Слоты", callback_data="info_slots", style="primary"),
            InlineKeyboardButton(text="🎳 Боулинг", callback_data="info_bowling", style="primary")
        ],
        [
            InlineKeyboardButton(text="🎯 Дартс", callback_data="info_darts", style="primary"),
            InlineKeyboardButton(text="🏀 Баскетбол", callback_data="info_basketball", style="primary")
        ],
        [
            InlineKeyboardButton(text="⚽ Футбол", callback_data="info_football", style="primary"),
            InlineKeyboardButton(text="🚀 Краш", callback_data="info_crash", style="primary")
        ],
        [
            InlineKeyboardButton(text="Назад", callback_data="catalog_main", icon_custom_emoji_id="5255703720078879038")
        ]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        msg = await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    key = get_chat_key(user_id, chat_id)
    if key not in user_messages:
        user_messages[key] = []
    if msg:
        user_messages[key].append(msg.message_id)


async def show_modes_games_catalog(callback: types.CallbackQuery, user_id, user_name, chat_id, is_group=False):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '<b><tg-emoji emoji-id="5361741454685256344">🕹</tg-emoji>РЕЖИМЫ ИГР\n'
        '<code>·····················</code></b>\n'
        '<blockquote> <i>ℹ️ Выберите режим игры из списка ниже:</i></blockquote>'
    )

    keyboard_buttons = [
        [
            InlineKeyboardButton(
                text="Мины",
                callback_data="mines",
                style="primary",
                icon_custom_emoji_id="5469654973308476699"
            ),
            InlineKeyboardButton(
                text="🛕 Башня",
                callback_data="tower",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                text="Алмазы",
                callback_data="diamonds",
                style="primary",
                icon_custom_emoji_id="5307594157739515229"
            ),
            InlineKeyboardButton(
                text="🃏 21 (Очко)",
                callback_data="info_twentyone",
                style="primary"
            )
        ],
        [
            InlineKeyboardButton(
                text="⚔️ WebApp: Арена",
                web_app=types.WebAppInfo(url=f"{WEBHOOK_HOST}/app")
            )
        ],
        [
            InlineKeyboardButton(
                text="Назад",
                callback_data="catalog_main",
                icon_custom_emoji_id="5255703720078879038"
            )
        ]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        msg = await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    key = get_chat_key(user_id, chat_id)
    if key not in user_messages:
        user_messages[key] = []
    if msg:
        user_messages[key].append(msg.message_id)


async def show_top(message, chat_id=None):
    user_id = message.from_user.id

    if chat_id:
        add_chat_member(user_id, chat_id)
        top_users = get_top_users(10, chat_id)
        title = "ТОП ИГРОКОВ ЧАТА ПО MCOIN"
    else:
        top_users = get_top_users(10)
        title = "МИРОВОЙ ТОП ПО MCOIN"

    if not top_users:
        await message.answer("<i>Нет данных для топа!</i>", parse_mode=ParseMode.HTML)
        return

    rank_emojis = [
        '<tg-emoji emoji-id="5765089714717596171">🥇</tg-emoji>',
        '<tg-emoji emoji-id="5767304320114498155">🥈</tg-emoji>',
        '<tg-emoji emoji-id="5764928223947267454">🥉</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>',
        '<tg-emoji emoji-id="5334644364280866007">🏅</tg-emoji>'
    ]

    text = f'<tg-emoji emoji-id="5280769763398671636">🏆</tg-emoji><b>{title}</b>\n\n'

    for i, (uid, balance) in enumerate(top_users):
        try:
            user = await bot.get_chat(uid)
            raw_name = user.first_name or "Пользователь"
        except Exception:
            raw_name = get_user_display_name(uid)

        clean_name = html.escape(str(raw_name))
        user_link = f'<a href="tg://user?id={uid}">{clean_name}</a>'
        text += f'{rank_emojis[i]} {i+1}.  {user_link} | <code>{format_number(balance)}</code> m¢\n'

    user_rank = get_user_rank(user_id, chat_id) if chat_id else get_user_rank(user_id)

    if user_rank > 0:
        user_data = get_user(user_id)

        if user_rank <= 10:
            caller_emoji = rank_emojis[user_rank - 1]
        else:
            caller_emoji = "🎖️"

        try:
            user = await bot.get_chat(user_id)
            raw_name = user.first_name or "Пользователь"
        except Exception:
            raw_name = get_user_display_name(user_id)

        clean_name = html.escape(str(raw_name))
        user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
        text += f'\n<blockquote>{user_rank}. {caller_emoji}  {user_link} | <code>{format_number(user_data["balance"])}</code> mCoin</blockquote>'

    await message.answer(
        text=text,
        parse_mode=ParseMode.HTML
    )



def get_active_unplayed_games(user_id):
    result = []
    for gid, g in games.items():
        if g.get("owner_id") == user_id:
            if not g.get("game_over", False) and not g.get("settled", False) and not g.get("expired", False):
                if g.get("stage") in ["playing", "ready"]:
                    result.append((gid, g))
    return sorted(result, key=lambda x: x[0], reverse=True)


async def show_recent_games_menu(message_or_callback, user_id, user_name, page=1):
    clean_name = html.escape(str(user_name))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '🕓 <b>НЕДАВНИЕ ИГРЫ</b>\n'
        '<code>·····················</code>\n'
        '<blockquote>ℹ️ Вы можете просмотреть свои недавние игры и продолжить запущенные. Если вы запускали игру более 12 часов назад, она завершилась, и ставка была возвращена на ваш баланс.</blockquote>'
    )

    unplayed = get_active_unplayed_games(user_id)
    per_page = 5
    total_pages = max(1, (len(unplayed) + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    start_idx = (page - 1) * per_page
    page_games = unplayed[start_idx:start_idx + per_page]

    keyboard_rows = []

    type_names = {
        "mines": "Мины",
        "tower": "Башня",
        "diamonds": "Алмазы",
        "twentyone": "21 (Очко)"
    }

    for gid, gdata in page_games:
        gtype = gdata.get("type", "mines")
        gname = type_names.get(gtype, "Мины")
        bet_str = format_number(gdata.get("bet", 10))
        btn_text = f"{gname} • {bet_str} mCoin"
        keyboard_rows.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"resume_game_{gid}",
                style="success",
                icon_custom_emoji_id="5809949600152296075"
            )
        ])

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"recent_page_{page - 1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"recent_page_{page + 1}"))
        if nav_buttons:
            keyboard_rows.append(nav_buttons)

    keyboard_rows.append([
        InlineKeyboardButton(
            text="История игр",
            callback_data="history_games_1",
            style="primary",
            icon_custom_emoji_id="5309815458990433715"
        ),
        InlineKeyboardButton(
            text="Статистика",
            callback_data="history_stats",
            style="primary",
            icon_custom_emoji_id="5431577498364158238"
        )
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


@dp.message(Command("history", "история"))
@dp.message(lambda message: message.text and message.text.lower() in ["история", "недавние игры", "игры история", "история игр"])
async def cmd_history(message: types.Message):
    await show_recent_games_menu(message, message.from_user.id, message.from_user.first_name, page=1)


@dp.callback_query(lambda c: c.data and c.data.startswith("recent_page_"))
async def process_recent_page(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 1
    await show_recent_games_menu(callback, callback.from_user.id, callback.from_user.first_name, page=page)


@dp.callback_query(lambda c: c.data == "back_to_history_menu")
async def process_back_to_history_menu(callback: types.CallbackQuery):
    await callback.answer()
    await show_recent_games_menu(callback, callback.from_user.id, callback.from_user.first_name, page=1)


@dp.callback_query(lambda c: c.data == "history_stats")
async def process_history_stats(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

    stats = get_user_stats(user_id)

    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji><b>СТАТИСТИКА ПО ИГРАМ</b>\n'
        f'<code>·····················</code>\n'
        f'<blockquote expandable>\n'
        f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> Мины: {stats["mines"]}\n'
        f'<tg-emoji emoji-id="5283080528818360566">🚀</tg-emoji> Краш: {stats["crash"]}\n'
        f'🛕 Башня: {stats["tower"]}\n'
        f'💠 Алмазы: {stats["diamonds"]}\n'
        f'🎰 Слоты: {stats.get("slots", 0)}\n'
        f'🎳 Боулинг: {stats.get("bowling", 0)}\n'
        f'🎯 Дартс: {stats.get("darts", 0)}\n'
        f'🏀 Баскетбол: {stats.get("basketball", 0)}\n'
        f'⚽ Футбол: {stats.get("football", 0)}\n'
        f'🃏 21 (Очко): {stats.get("twentyone", 0)}\n'
        f'<code>·····················</code>\n'
        f'🕹 Сыграно: {stats["total"]} игр\n'
        f'</blockquote>'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data="back_to_history_menu",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ]
        ]
    )

    try:
        await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )


@dp.callback_query(lambda c: c.data and c.data.startswith("history_games_"))
async def process_history_games(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 1

    user_id = callback.from_user.id
    user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

    total_count = get_user_history_count(user_id)
    per_page = 5
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    rows = get_user_history(user_id, limit=per_page, offset=offset)

    type_info = {
        "mines": ("💣", "Мины"),
        "tower": ("🛕", "Башня"),
        "diamonds": ("💠", "Алмазы"),
        "crash": ("🚀", "Краш"),
        "slots": ("🎰", "Слоты"),
        "bowling": ("🎳", "Боулинг"),
        "darts": ("🎯", "Дартс"),
        "basketball": ("🏀", "Баскетбол"),
        "football": ("⚽", "Футбол"),
        "twentyone": ("🃏", "21 (Очко)")
    }

    text = f'{user_link}\n🕓 <b>История игр:</b>\n\n'

    if not rows:
        text += "<i>История игр пуста. Сыграйте в любую игру!</i>\n\n"
    else:
        for idx, (gtype, bet, result, win_amount, dt_str) in enumerate(rows):
            icon, name = type_info.get(gtype, ("🎮", "Игра"))
            text += f'{icon} <b>{name}:</b> {dt_str}\n'
            text += f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            if result == "win":
                text += f'✅ <b>Выигрыш:</b> {format_number(win_amount)} m¢\n'
            elif result == "draw":
                text += f'🤝 <b>Ничья (возврат):</b> {format_number(win_amount)} m¢\n'
            elif result == "expired":
                text += '💾 <b>Игра отменена (бездействие)</b>\n'
            else:
                text += '❌ <b>Игра проиграна!</b>\n'
            
            if idx < len(rows) - 1:
                text += '<code>·····················</code>\n'

        text += f'\n↗️ <i>Страница {page}/{total_pages}</i>'

    keyboard_rows = []
    nav_buttons = []

    if page > 5:
        nav_buttons.append(InlineKeyboardButton(text="⏮️ 5", callback_data=f"history_games_{page - 5}"))
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"history_games_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"history_games_{page + 1}"))
    if total_pages - page >= 5:
        nav_buttons.append(InlineKeyboardButton(text="⏩ 5", callback_data=f"history_games_{page + 5}"))

    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([
        InlineKeyboardButton(
            text="Назад",
            callback_data="back_to_history_menu",
            icon_custom_emoji_id="5255703720078879038"
        )
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    try:
        await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )


@dp.callback_query(lambda c: c.data and c.data.startswith("resume_game_"))
async def process_resume_game(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка игры!", show_alert=True)
        return

    user_id = callback.from_user.id
    game = games.get(game_id)
    if not game:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("settled", False) or game.get("expired", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    active_games[user_id] = game_id
    game["chat_id"] = callback.message.chat.id

    old_msg_id = game.get("message_id")
    if old_msg_id and old_msg_id != callback.message.message_id:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=old_msg_id)
        except Exception:
            pass

    await callback.answer()

    gtype = game.get("type")
    if gtype == "mines":
        await show_mines_grid_from_callback(callback, user_id, game_id)
    elif gtype == "tower":
        await show_tower_grid_from_callback(callback, user_id, game_id)
    elif gtype == "diamonds":
        await show_diamonds_grid_from_callback(callback, user_id, game_id)
    elif gtype == "twentyone":
        await show_twentyone_from_callback(callback, user_id, game_id)
    else:
        await callback.answer("Неизвестный тип игры!", show_alert=True)


# --- START COMMAND ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        await message.answer("<i>Эта команда работает только в личных сообщениях!</i>", parse_mode=ParseMode.HTML)
        return

    user_id = message.from_user.id
    get_user(user_id)

    parts = message.text.split()
    if len(parts) > 1:
        payload = parts[1].strip()
        if payload.startswith("ref_"):
            try:
                referrer_id = int(payload.replace("ref_", ""))
                register_referral(referrer_id, user_id)
            except Exception:
                pass
        elif payload == "p2p":
            await show_p2p_main(message, user_id=user_id, first_name=message.from_user.first_name)
            return

    text = (
        '<b>Привет! <tg-emoji emoji-id="5350612670435313545">👋</tg-emoji> Ты в Мины Бот — место, где время летит незаметно!</b>\n\n'
        '🎮 Много бесплатных игр без скачивания, прямо в Telegram.\n\n'
        'Соревнуйся с друзьями и прокачивай свои каналы и чаты. 🏆'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Играть",
                    callback_data="play",
                    style="success",
                    icon_custom_emoji_id="5350612670435313545"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Добавить бота в чат",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=start",
                    style="primary",
                    icon_custom_emoji_id="5393194986252542669"
                )
            ]
        ]
    )

    photo_url = "https://iili.io/CPpNkSR.md.png"
    msg = await message.answer_photo(
        photo=photo_url,
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    key = get_chat_key(message.from_user.id, message.chat.id)
    if key not in user_messages:
        user_messages[key] = []
    user_messages[key].append(msg.message_id)


@dp.message(Command("add"))
async def cmd_add(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer("<i>Использование: /add [сумма]\nИли в ответ на сообщение пользователя: /add [сумма]</i>", parse_mode=ParseMode.HTML)
        return

    amount = parse_amount(args[1])
    if amount is None or amount < 0:
        await message.answer("<i>Неверная сумма! Пример: 100, 1.4кк, 120кк</i>", parse_mode=ParseMode.HTML)
        return

    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    else:
        target_user_id = user_id
        target_name = "ваш"

    user_data = get_user(target_user_id)
    new_balance = user_data["balance"] + amount

    if update_user(target_user_id, balance=new_balance):
        await message.reply(
            f'<i>✅ Successfully! На баланс пользователя {target_name} добавлено {format_number(amount)} mCoin\n'
            f'Текущий баланс: {format_number(new_balance)} mCoin</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("<i>Ошибка базы данных!</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("set"))
async def cmd_set(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.answer("<i>Использование: /set [сумма]\nИли в ответ на сообщение пользователя: /set [сумма]</i>", parse_mode=ParseMode.HTML)
        return

    amount = parse_amount(args[1])
    if amount is None or amount < 0:
        await message.answer("<i>Неверная сумма! Пример: 100, 1.4кк, 120кк</i>", parse_mode=ParseMode.HTML)
        return

    if message.reply_to_message:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    else:
        target_user_id = user_id
        target_name = "ваш"

    get_user(target_user_id)
    if update_user(target_user_id, balance=amount):
        user_data = get_user(target_user_id)
        await message.reply(
            f'<i>✅ Successfully! Баланс пользователя {target_name} установлен на {format_number(user_data["balance"])} mCoin</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("<i>Ошибка базы данных!</i>", parse_mode=ParseMode.HTML)


def parse_record_field(field_str: str):
    if not field_str:
        return None, None
    f = field_str.lower().strip()
    if f in ["баланс", "balance", "max_balance", "макс", "максбаланс", "bal", "max", "б"]:
        return "max_balance", "Рекорд максимального баланса"
    if f in ["игры", "игры_колво", "games", "game", "игр", "и"]:
        return "games", "Сыграно игр"
    if f in ["слив", "проигрыш", "проигрыши", "lost", "проиграно", "lose", "с", "п"]:
        return "lost", "Проиграно m¢"
    return None, None


@dp.message(Command("setrec", "setrecord", "изменитьрекорд"))
async def cmd_setrec(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    text = (message.text or "").strip()
    parts = text.split()

    target_user_id = None
    target_name = None
    field_arg = None
    val_arg = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or f"ID:{target_user_id}"
        if len(parts) >= 3:
            field_arg = parts[1]
            val_arg = parts[2]
        else:
            await message.reply(
                "<i>Использование в ответ на сообщение:\n"
                "/setrec &lt;тип&gt; &lt;значение&gt;\n\n"
                "Типы:\n"
                "• <code>баланс</code> (максимальный баланс)\n"
                "• <code>игры</code> (количество игр)\n"
                "• <code>слив</code> (сумма проигрышей)\n\n"
                "Пример: <code>/setrec баланс 50кк</code> или <code>/setrec игры 500</code></i>",
                parse_mode=ParseMode.HTML
            )
            return
    elif len(parts) >= 4:
        target_arg = parts[1]
        field_arg = parts[2]
        val_arg = parts[3]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.reply(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.reply(f"<i>Пользователь {target_arg} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        await message.reply(
            "<i>Использование:\n"
            "• <code>/setrec &lt;@user/ID&gt; &lt;тип&gt; &lt;значение&gt;</code>\n"
            "• ответом на сообщение: <code>/setrec &lt;тип&gt; &lt;значение&gt;</code>\n\n"
            "Доступные типы:\n"
            "• <code>баланс</code> (максимальный баланс)\n"
            "• <code>игры</code> (количество сыгранных игр)\n"
            "• <code>слив</code> (сумма проигрышей)\n\n"
            "Пример: <code>/setrec @durov баланс 100кк</code></i>",
            parse_mode=ParseMode.HTML
        )
        return

    col_name, col_title = parse_record_field(field_arg)
    if not col_name:
        await message.reply(
            "<i>Неизвестный тип рекорда! Доступны: <code>баланс</code>, <code>игры</code>, <code>слив</code></i>",
            parse_mode=ParseMode.HTML
        )
        return

    amount = parse_amount(val_arg)
    if amount is None or amount < 0:
        await message.reply("<i>Неверное значение! Пример: 100, 1.4кк, 500</i>", parse_mode=ParseMode.HTML)
        return

    kwargs = {col_name: amount}
    if update_user(target_user_id, **kwargs):
        admin_name = html.escape(message.from_user.first_name or "Админ")
        admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
        target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'
        await message.reply(
            f'<i>{admin_link}, успешно обновлен рекорд для {target_link}!\n'
            f'👑 <b>{col_title}:</b> {format_number(amount)}</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("<i>Ошибка при обновлении базы данных!</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("delrec", "delrecord", "resetrec", "удалитьрекорд", "сброситьрекорд"))
async def cmd_delrec(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    text = (message.text or "").strip()
    parts = text.split()

    target_user_id = None
    target_name = None
    field_arg = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or f"ID:{target_user_id}"
        if len(parts) >= 2:
            field_arg = parts[1]
    elif len(parts) >= 2:
        target_arg = parts[1]
        if len(parts) >= 3:
            field_arg = parts[2]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.reply(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                col, _ = parse_record_field(target_arg)
                if col:
                    target_user_id = user_id
                    target_name = message.from_user.first_name or "Админ"
                    field_arg = target_arg
                else:
                    await message.reply(f"<i>Пользователь {target_arg} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                    return
    else:
        await message.reply(
            "<i>Использование:\n"
            "• <code>/delrec &lt;@user/ID&gt; [тип]</code> — сбросить рекорды пользователя\n"
            "• ответом на сообщение: <code>/delrec [тип]</code>\n\n"
            "Типы: <code>все</code> (по умолчанию) | <code>баланс</code> | <code>игры</code> | <code>слив</code>\n"
            "Пример: <code>/delrec @username</code> или <code>/delrec @username баланс</code></i>",
            parse_mode=ParseMode.HTML
        )
        return

    u_data = get_user(target_user_id)
    cur_balance = u_data.get("balance", 0)

    if not field_arg or field_arg.lower().strip() in ["все", "all", "всё"]:
        success = update_user(target_user_id, max_balance=cur_balance, games=0, lost=0)
        desc = f"Все рекорды сброшены (Макс. баланс: {format_number(cur_balance)} m¢, Игры: 0, Проиграно: 0)"
    else:
        col_name, col_title = parse_record_field(field_arg)
        if not col_name:
            await message.reply(
                "<i>Неизвестный тип рекорда! Доступны: <code>все</code>, <code>баланс</code>, <code>игры</code>, <code>слив</code></i>",
                parse_mode=ParseMode.HTML
            )
            return
        val = cur_balance if col_name == "max_balance" else 0
        success = update_user(target_user_id, **{col_name: val})
        desc = f"Рекорд «{col_title}» сброшен в {format_number(val)}"

    if success:
        admin_name = html.escape(message.from_user.first_name or "Админ")
        admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
        target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'
        await message.reply(
            f'<i>{admin_link}, для пользователя {target_link} успешно выполнено:\n'
            f'🗑 {desc} ✅</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("<i>Ошибка при обновлении базы данных!</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("sna"))
async def cmd_sna(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        cursor.execute("UPDATE users SET balance = 0")
        conn.commit()
        cursor.execute("SELECT COUNT(*) FROM users")
        row = cursor.fetchone()
        total_users = row[0] if row else 0
        await message.reply(
            f"<i>✅ Successfully! Баланс всех игроков обнулен.\n"
            f"👥 Всего пользователей: {total_users}</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.reply(f"<i>Ошибка базы данных: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("sn"))
async def cmd_sn(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        cursor.execute("UPDATE users SET balance = 0, mp_balance = 0, mp_daily_transferred = 0, mp_daily_date = NULL, games = 0, lost = 0, bonus_time = NULL")
        cursor.execute("DELETE FROM games_history")
        cursor.execute("DELETE FROM transfers_history")
        cursor.execute("DELETE FROM mp_transfers_history")
        cursor.execute("DELETE FROM game_stats")
        cursor.execute("DELETE FROM active_games_state")
        cursor.execute("DELETE FROM promo_codes")
        cursor.execute("DELETE FROM promo_activations")
        cursor.execute("DELETE FROM top_bans")
        cursor.execute("DELETE FROM transfer_bans")
        _top_banned_users.clear()
        _transfer_banned_users.clear()
        try:
            cursor.execute("ALTER SEQUENCE IF EXISTS games_history_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS transfers_history_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS mp_transfers_history_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS p2p_deals_history_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS p2p_deal_ratings_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS p2p_bot_stats_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS time_deposits_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS savings_history_id_seq RESTART WITH 1")
            cursor.execute("ALTER SEQUENCE IF EXISTS arena_history_id_seq RESTART WITH 1")
        except Exception:
            pass
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM users")
        row = cursor.fetchone()
        total_users = row[0] if row else 0

        await message.reply(
            f"<i>✅ Successfully! База данных полностью очищена.\n"
            f"👥 Сохранены только профили пользователей ({total_users} шт.).\n"
            f"🗑 Балансы, статистика (сыграно/проиграно), история игр, переводов и промокоды удалены.</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.reply(f"<i>Ошибка базы данных: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("unbd", "унбд"))
@dp.message(lambda message: message.text and message.text.strip().lower().split()[0] in ["/unbd", "unbd", "унбд", "/унбд"])
async def cmd_unbd(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    text = (message.text or "").strip()
    parts = text.split()

    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name or f"ID:{target_user_id}"
    elif len(parts) > 1:
        target_arg = parts[1]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.reply(f"<i>Пользователь @{uname} не найден в базе данных!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.reply(f"<i>Пользователь {target_arg} не найден в базе данных!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        await message.reply("<i>Использование: /unbd &lt;id/@username&gt; или ответом на сообщение пользователя</i>", parse_mode=ParseMode.HTML)
        return

    cursor.execute("SELECT 1 FROM users WHERE user_id = ?", (target_user_id,))
    if not cursor.fetchone():
        cursor.execute("SELECT 1 FROM game_stats WHERE user_id = ?", (target_user_id,))
        if not cursor.fetchone():
            await message.reply(f"<i>Пользователь с ID {target_user_id} не найден в базе данных!</i>", parse_mode=ParseMode.HTML)
            return

    try:
        cursor.execute("DELETE FROM users WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM game_stats WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM bans WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM top_bans WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM transfer_bans WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM promo_activations WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM games_history WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM chat_members WHERE user_id = ?", (target_user_id,))
        cursor.execute("DELETE FROM active_games_state WHERE owner_id = ?", (target_user_id,))
        conn.commit()

        _known_usernames.pop(target_user_id, None)
        _banned_users_cache.pop(target_user_id, None)
        _top_banned_users.discard(target_user_id)
        _transfer_banned_users.discard(target_user_id)
        active_games.pop(target_user_id, None)

        clean_name = html.escape(str(target_name))
        await message.reply(
            f"<i>✅ Successfully! Пользователь <b>{clean_name}</b> (<code>{target_user_id}</code>) полностью удален из базы данных!</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.reply(f"<i>Ошибка при удалении пользователя: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


def clean_promo_string(s):
    if not s:
        return ""
    s = s.strip()
    for q in ['"', "'", "«", "»", "“", "”", "`"]:
        s = s.strip(q)
    return s.strip()


@dp.message(Command("cprom"))
async def cmd_cprom(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("<i>Использование: /cprom [название] [награда] [активации]\nПример: <code>/cprom Новый год 100кк 10</code> или <code>/cprom test 50кк</code></i>", parse_mode=ParseMode.HTML)
        return

    activations = 1
    reward = None
    promo_name = ""

    # Check if last argument is integer activations
    if len(args) >= 4 and args[-1].isdigit():
        activations = int(args[-1])
        if activations <= 0:
            await message.answer("<i>Количество активаций должно быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return
        reward = parse_amount(args[-2])
        if reward is None or reward <= 0:
            await message.answer("<i>Неверная сумма награды! Пример: 100кк, 50000</i>", parse_mode=ParseMode.HTML)
            return
        promo_name = clean_promo_string(" ".join(args[1:-2]))
    else:
        reward = parse_amount(args[-1])
        if reward is None or reward <= 0:
            await message.answer("<i>Неверная сумма награды! Пример: 100кк, 50000</i>", parse_mode=ParseMode.HTML)
            return
        promo_name = clean_promo_string(" ".join(args[1:-1]))
        activations = 1

    if not promo_name:
        await message.answer("<i>Укажите название промокода!</i>", parse_mode=ParseMode.HTML)
        return

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute('''
            INSERT INTO promo_codes (code, reward, total_activations, remaining_activations, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (code) DO UPDATE SET
                reward = EXCLUDED.reward,
                total_activations = EXCLUDED.total_activations,
                remaining_activations = EXCLUDED.remaining_activations,
                created_at = EXCLUDED.created_at
        ''', (promo_name, reward, activations, activations, now_str))
        conn.commit()

        await message.reply(
            f"<i>✅ Промокод «<b>{html.escape(promo_name)}</b>» успешно создан!\n"
            f"💰 Награда: <b>{format_number(reward)}</b> mCoin\n"
            f"👥 Количество активаций: <b>{activations}</b></i>",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.reply(f"<i>Ошибка базы данных: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("listprom"))
async def cmd_listprom(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        cursor.execute("DELETE FROM promo_codes WHERE remaining_activations <= 0")
        conn.commit()

        cursor.execute("SELECT code, reward, remaining_activations, total_activations FROM promo_codes ORDER BY created_at DESC")
        rows = cursor.fetchall()

        if not rows:
            await message.answer("<i>Список доступных промокодов пуст.</i>", parse_mode=ParseMode.HTML)
            return

        lines = ["<b>📋 Доступные промокоды:</b>\n"]
        for code, reward, rem, total in rows:
            lines.append(f"• «<code>{html.escape(code)}</code>» — {format_number(reward)} mCoin (Осталось: {rem}/{total})")

        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"<i>Ошибка базы данных: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("em"))
async def cmd_em(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    status_msg = await message.answer(f"⏳ Проверяю {len(EMOJI_IDS_TO_CHECK)} emoji-id...")

    good_ids = []
    bad_ids = []

    for emoji_id in EMOJI_IDS_TO_CHECK:
        try:
            probe = await message.answer(
                f'<tg-emoji emoji-id="{emoji_id}">✅</tg-emoji>',
                parse_mode=ParseMode.HTML
            )
            good_ids.append(emoji_id)
            try:
                await probe.delete()
            except Exception:
                pass
        except TelegramBadRequest:
            bad_ids.append(emoji_id)
        await asyncio.sleep(0.05)

    report = f"✅ Рабочих: {len(good_ids)}/{len(EMOJI_IDS_TO_CHECK)}\n"
    if bad_ids:
        report += "<i>Битые ID (DOCUMENT_INVALID):\n</i>" + "\n".join(f"<code>{i}</code>" for i in bad_ids)
    else:
        report += "<i>Битых не найдено.</i>"

    await status_msg.edit_text(report, parse_mode=ParseMode.HTML)


@dp.message(Command("event"))
async def cmd_event(message: types.Message):
    user_id = message.from_user.id

    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    global event_active, event_started, event_number, event_prize, event_participants, event_message_id, event_chat_id, event_timer_task, event_timeout_task, event_min_num, event_max_num

    if event_active:
        await message.answer("<i>Ивент уже запущен!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()

    if len(args) < 5:
        await message.answer("<i>Использование: /event [min] [max] [приз] [участники]\nПример: /event 1 250 250kk 5</i>", parse_mode=ParseMode.HTML)
        return

    try:
        min_num = int(args[1])
        max_num = int(args[2])
        if min_num < 1:
            await message.answer("<i>Минимальное число должно быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return
        if max_num <= min_num:
            await message.answer("<i>Максимальное число должно быть больше минимального!</i>", parse_mode=ParseMode.HTML)
            return
        if max_num > 1000000:
            await message.answer("<i>Максимальное число не может превышать 1,000,000!</i>", parse_mode=ParseMode.HTML)
            return
        prize = parse_amount(args[3])
        if prize is None or prize <= 0:
            await message.answer("<i>Неверная сумма приза!</i>", parse_mode=ParseMode.HTML)
            return
        max_participants = int(args[4])
        if max_participants < 1:
            await message.answer("<i>Должен быть хотя бы 1 участник!</i>", parse_mode=ParseMode.HTML)
            return
    except ValueError:
        await message.answer("<i>Неверный формат! Пример: /event 1 250 250kk 5</i>", parse_mode=ParseMode.HTML)
        return

    event_min_num = min_num
    event_max_num = max_num
    event_number = random.randint(min_num, max_num)
    event_prize = prize
    event_participants = {}
    event_active = True
    event_started = False
    event_chat_id = message.chat.id

    text = (
        f'<b><tg-emoji emoji-id="5467671759274661866">🤩</tg-emoji> Ивент на {format_number(prize)} начался!</b>\n\n'
        '<tg-emoji emoji-id="5372926953978341366">👥</tg-emoji> Участники: пока нет\n'
        '<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji> До начала розыгрыша 60 сек.\n'
        '<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Для участия нажми кнопку ниже.'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Участвовать",
                    callback_data="event_join",
                    style="primary",
                    icon_custom_emoji_id="5920090136627908485"
                )
            ]
        ]
    )

    msg = await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    event_message_id = msg.message_id

    await bot.pin_chat_message(chat_id=message.chat.id, message_id=msg.message_id)

    if event_timer_task:
        event_timer_task.cancel()

    if event_timeout_task:
        event_timeout_task.cancel()

    event_timer_task = asyncio.create_task(event_timer(message.chat.id, msg.message_id))


async def event_timer(chat_id, message_id):
    global event_active, event_started, event_participants, event_timeout_task

    for i in range(60, -1, -1):
        if not event_active:
            return

        if i == 0:
            if len(event_participants) < 1:
                await bot.edit_message_text(
                    "<i>Ивент отменен! Недостаточно участников.</i>",
                    chat_id=chat_id,
                    message_id=message_id
                )
                await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
                event_active = False
                return

            event_started = True

            participants_list = []
            for uid in event_participants.keys():
                try:
                    user = await bot.get_chat(uid)
                    clean_first_name = html.escape(user.first_name or "User")
                    participants_list.append(f'<a href="tg://user?id={uid}">{clean_first_name}</a>')
                except Exception:
                    participants_list.append(f'<a href="tg://user?id={uid}">User</a>')

            text = (
                f'<tg-emoji emoji-id="5852868430952144622">🔥</tg-emoji> Конкурс начался!\n\n'
                f'<tg-emoji emoji-id="5372926953978341366">👥</tg-emoji> Участники: {", ".join(participants_list)}\n\n'
                f'У каждого участника будет по 3 попытки чтобы отгадать нужное число!\n'
                f'Чтобы испытать свою удачи напиши <code>/i число</code>\n\n'
                f'⏰ У вас есть 10 минут!'
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Участвовать",
                            callback_data="event_join",
                            style="primary",
                            icon_custom_emoji_id="5920090136627908485"
                        )
                    ]
                ]
            )

            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard
            )

            event_timeout_task = asyncio.create_task(event_timeout(chat_id, message_id))
            return

        participants_count = len(event_participants)
        try:
            await bot.edit_message_text(
                f'<b><tg-emoji emoji-id="5467671759274661866">🤩</tg-emoji> Ивент на {format_number(event_prize)} начался!</b>\n\n'
                f'<tg-emoji emoji-id="5372926953978341366">👥</tg-emoji> Участники: {participants_count} чел.\n'
                f'<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji> До начала розыгрыша {i} сек.\n'
                f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Для участия нажми кнопку ниже.',
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Участвовать",
                                callback_data="event_join",
                                style="primary",
                                icon_custom_emoji_id="5920090136627908485"
                            )
                        ]
                    ]
                )
            )
        except Exception:
            pass

        await asyncio.sleep(1)


async def event_timeout(chat_id, message_id):
    global event_active, event_started, event_number, event_participants

    await asyncio.sleep(600)

    if not event_active or not event_started:
        return

    event_active = False
    event_started = False

    await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)

    participants_list = []
    for uid in event_participants.keys():
        try:
            user = await bot.get_chat(uid)
            clean_first_name = html.escape(user.first_name or "User")
            participants_list.append(f'<a href="tg://user?id={uid}">{clean_first_name}</a>')
        except Exception:
            participants_list.append(f'<a href="tg://user?id={uid}">User</a>')

    text = (
        f'⏰ <b>Время вышло!</b>\n\n'
        f'Загаданное число было: <code>{event_number}</code>\n\n'
        f'<tg-emoji emoji-id="5372926953978341366">👥</tg-emoji> Участники: {", ".join(participants_list)}\n\n'
        f'<i>Никто не угадал число. Ивент завершен!</i>'
    )

    await bot.edit_message_text(
        text=text,
        chat_id=chat_id,
        message_id=message_id,
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(lambda c: c.data == "event_join")
async def process_event_join(callback: types.CallbackQuery):
    global event_active, event_started, event_participants

    if not event_active:
        await callback.answer("Ивент не активен!", show_alert=True)
        return

    if event_started:
        await callback.answer("Розыгрыш уже начался!", show_alert=True)
        return

    user_id = callback.from_user.id

    if user_id in event_participants:
        await callback.answer("Вы уже участвуете в розыгрыше!", show_alert=True)
        return

    event_participants[user_id] = 3
    await callback.answer("✅ Вы стали участником розыгрыша!")

    participants_list = []
    for uid in event_participants.keys():
        try:
            user = await bot.get_chat(uid)
            clean_first_name = html.escape(user.first_name or "User")
            participants_list.append(f'<a href="tg://user?id={uid}">{clean_first_name}</a>')
        except Exception:
            participants_list.append(f'<a href="tg://user?id={uid}">User</a>')

    try:
        await callback.message.edit_text(
            f'<b><tg-emoji emoji-id="5467671759274661866">🤩</tg-emoji> Ивент на {format_number(event_prize)} начался!</b>\n\n'
            f'<tg-emoji emoji-id="5372926953978341366">👥</tg-emoji> Участники: {", ".join(participants_list)}\n'
            f'<tg-emoji emoji-id="5386367538735104399">⌛</tg-emoji> До начала розыгрыша ... сек.\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Для участия нажми кнопку ниже.',
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Участвовать",
                            callback_data="event_join",
                            style="primary",
                            icon_custom_emoji_id="5920090136627908485"
                        )
                    ]
                ]
            )
        )
    except Exception:
        pass


@dp.message(lambda message: message.text and message.text.startswith("/i "))
async def cmd_i(message: types.Message):
    global event_active, event_started, event_number, event_prize, event_participants, event_min_num, event_max_num

    if not event_active or not event_started:
        await message.answer("<i>Конкурс окончен. Ожидайте новый.</i>", parse_mode=ParseMode.HTML)
        return

    user_id = message.from_user.id

    if user_id not in event_participants:
        await message.delete()
        return

    if event_participants[user_id] <= 0:
        await message.reply("<i>У вас закончились попытки!</i>", parse_mode=ParseMode.HTML)
        await message.delete()
        return

    try:
        guess = int(message.text.split()[1])
    except (ValueError, IndexError):
        await message.reply(f"<i>Введите число от {event_min_num} до {event_max_num}! Пример: /i 50</i>", parse_mode=ParseMode.HTML)
        await message.delete()
        return

    if guess < event_min_num or guess > event_max_num:
        await message.reply(f"<i>Число должно быть от {event_min_num} до {event_max_num}!</i>", parse_mode=ParseMode.HTML)
        await message.delete()
        return

    event_participants[user_id] -= 1

    if guess == event_number:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + event_prize)

        await message.reply(
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji> <a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a> <b>угадал загаданное число и получил {format_number(event_prize)} mCoin!\n'
            f'Загаданное число было: <code>{event_number}</code></b>',
            parse_mode=ParseMode.HTML
        )

        await bot.unpin_chat_message(chat_id=message.chat.id, message_id=event_message_id)
        event_active = False

        results_text = "📊 Результаты:\n"
        for uid, attempts in event_participants.items():
            try:
                user = await bot.get_chat(uid)
                name = user.first_name
            except Exception:
                name = "User"
            clean_p_name = html.escape(str(name))
            results_text += f'<a href="tg://user?id={uid}">{clean_p_name}</a>: {3 - attempts} попыток\n'

        await message.answer(
            results_text,
            parse_mode=ParseMode.HTML
        )
        return

    if event_participants[user_id] == 0:
        await message.reply(
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji> <b>Вы не отгадали число!</b>\n'
            f'Вы истратили все попытки!',
            parse_mode=ParseMode.HTML
        )
        await message.delete()
        return

    await message.reply(
        f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji> <b>Вы не отгадали число!</b>\n'
        f'Оставшиеся попытки: <code>{event_participants[user_id]}</code>',
        parse_mode=ParseMode.HTML
    )
    await message.delete()


@dp.message(Command("top"))
@dp.message(lambda message: message.text and message.text.lower() in ["топ", "топ игроков", "мировой топ"])
async def cmd_top(message: types.Message):
    await show_top(message)


@dp.message(Command("top_chat", "topchat"))
@dp.message(lambda message: message.text and message.text.lower() in ["топ чата", "топ ч", "топ чат", "топ беседы", "топбеседы"])
async def cmd_top_chat(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("<i>Эта команда работает только в беседах!</i>", parse_mode=ParseMode.HTML)
        return
    add_chat_member(message.from_user.id, message.chat.id)
    await show_top(message, message.chat.id)


async def show_records(message: types.Message):
    async def get_user_name_and_link(uid: int):
        try:
            user = await bot.get_chat(uid)
            raw_name = user.first_name or "Пользователь"
        except Exception:
            raw_name = get_user_display_name(uid)
        clean_name = html.escape(str(raw_name))
        return f'<a href="tg://user?id={uid}">{clean_name}</a>'

    try:
        cursor.execute('''
            SELECT user_id, max_balance FROM users 
            WHERE user_id NOT IN (SELECT user_id FROM top_bans)
            ORDER BY max_balance DESC LIMIT 1
        ''')
        max_bal_row = cursor.fetchone()

        cursor.execute('''
            SELECT user_id, games FROM users 
            WHERE user_id NOT IN (SELECT user_id FROM top_bans)
            ORDER BY games DESC LIMIT 1
        ''')
        max_games_row = cursor.fetchone()

        cursor.execute('''
            SELECT user_id, lost FROM users 
            WHERE user_id NOT IN (SELECT user_id FROM top_bans)
            ORDER BY lost DESC LIMIT 1
        ''')
        max_lost_row = cursor.fetchone()
    except Exception:
        max_bal_row = None
        max_games_row = None
        max_lost_row = None

    if max_bal_row and max_bal_row[1] is not None:
        bal_user_link = await get_user_name_and_link(max_bal_row[0])
        bal_text = f'— {bal_user_link} {format_number(max_bal_row[1])} m¢'
    else:
        bal_text = '— Нет данных'

    if max_games_row and max_games_row[1] is not None:
        games_user_link = await get_user_name_and_link(max_games_row[0])
        games_text = f'— {games_user_link} сыграно {format_number(max_games_row[1])} игр.'
    else:
        games_text = '— сыграно 0 игр.'

    if max_lost_row and max_lost_row[1] is not None:
        lost_user_link = await get_user_name_and_link(max_lost_row[0])
        lost_text = f'— {lost_user_link} проиграно {format_number(max_lost_row[1])} m¢'
    else:
        lost_text = '— проиграно 0 m¢'

    text = (
        '<tg-emoji emoji-id="5767161061480340249">👑</tg-emoji> <b>Рекорды бота</b>\n\n'
        '<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Самый большой баланс:\n'
        f'{bal_text}\n\n'
        '<tg-emoji emoji-id="5309815458990433715">🎮</tg-emoji> Игроман бота\n'
        f'{games_text}\n\n'
        '<tg-emoji emoji-id="5442983582882601962">🗿</tg-emoji> «Везунчик» бота\n'
        f'{lost_text}'
    )

    await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.message(Command("records", "record", "рекорды", "рекорд"))
@dp.message(lambda message: message.text and message.text.lower().strip() in [
    "рекорды", "рекорд", "топ рекорды", "топ рекордов", "топ рекорд", "records", "/records", "/рекорды", "records_top"
])
async def cmd_records(message: types.Message):
    await show_records(message)


@dp.message(Command("game", "games"))
@dp.message(lambda message: message.text and message.text.lower() in ["игры", "игра", "каталог"])
async def cmd_game(message: types.Message):
    is_group = message.chat.type in ["group", "supergroup"]
    await show_catalog(message, message.from_user.id, message.from_user.first_name, message.chat.id, is_group)


# --- MINES GAME ---

@dp.message(Command("mines"))
@dp.message(lambda message: message.text and re.match(r'^(мины|mines)\s*', message.text, re.IGNORECASE))
async def cmd_mines(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_mines_info(message, user_id)
        return

    unplayed = get_active_unplayed_games(user_id)
    if unplayed:
        await message.answer("<i>У вас уже есть игра! Закончите или доиграйте активную игру.</i>", parse_mode=ParseMode.HTML)
        return

    args = parts[1].strip().split()
    user_data = get_user(user_id)

    try:
        if len(args) == 1:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = 1
        elif len(args) >= 2:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = int(args[1])
            if mine_count < 1 or mine_count > 6:
                await message.answer("<i>Количество мин должно быть от 1 до 6!</i>", parse_mode=ParseMode.HTML)
                return
        else:
            await show_mines_info(message, user_id)
            return

        if bet < 1:
            if user_data["balance"] < 1:
                await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
            else:
                await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return

        if bet > 250000000:
            await message.answer("<i>Максимальная ставка: 250kk m¢!</i>", parse_mode=ParseMode.HTML)
            return

        if user_data["balance"] < bet:
            await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
            return

        await start_mines_game_from_command(message, user_id, chat_id, bet, mine_count)

    except ValueError:
        await show_mines_info(message, user_id)


# --- TOWER GAME ---

@dp.message(Command("tower"))
@dp.message(lambda message: message.text and re.match(r'^(башня|tower)\s*', message.text, re.IGNORECASE))
async def cmd_tower(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_tower_info(message, user_id)
        return

    unplayed = get_active_unplayed_games(user_id)
    if unplayed:
        await message.answer("<i>У вас уже есть игра! Закончите или доиграйте активную игру.</i>", parse_mode=ParseMode.HTML)
        return

    args = parts[1].strip().split()
    user_data = get_user(user_id)

    try:
        if len(args) == 1:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = 1
        elif len(args) >= 2:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = int(args[1])
            if mine_count < 1 or mine_count > 4:
                await message.answer("<i>Количество мин должно быть от 1 до 4!</i>", parse_mode=ParseMode.HTML)
                return
        else:
            await show_tower_info(message, user_id)
            return

        if bet < 1:
            if user_data["balance"] < 1:
                await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
            else:
                await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return

        if bet > 250000000:
            await message.answer("<i>Максимальная ставка: 250kk m¢!</i>", parse_mode=ParseMode.HTML)
            return

        if user_data["balance"] < bet:
            await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
            return

        await start_tower_game_from_command(message, user_id, chat_id, bet, mine_count)

    except ValueError:
        await show_tower_info(message, user_id)


# --- DIAMONDS GAME (АЛМАЗЫ) ---

@dp.message(Command("diamond", "diamonds"))
@dp.message(lambda message: message.text and re.match(r'^(алмазы|алмаз|diamond|diamonds)\s*', message.text, re.IGNORECASE))
async def cmd_diamonds(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_diamonds_info(message, user_id)
        return

    unplayed = get_active_unplayed_games(user_id)
    if unplayed:
        await message.answer("<i>У вас уже есть игра! Закончите или доиграйте активную игру.</i>", parse_mode=ParseMode.HTML)
        return

    args = parts[1].strip().split()
    user_data = get_user(user_id)

    try:
        if len(args) == 1:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = 1
        elif len(args) >= 2:
            bet = resolve_bet_amount(args[0], user_data["balance"])
            if bet is None:
                await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
                return
            mine_count = int(args[1])
            if mine_count < 1 or mine_count > 2:
                await message.answer("<i>Количество мин должно быть 1 или 2!</i>", parse_mode=ParseMode.HTML)
                return
        else:
            await show_diamonds_info(message, user_id)
            return

        if bet < 1:
            if user_data["balance"] < 1:
                await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
            else:
                await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return

        if bet > 250000000:
            await message.answer("<i>Максимальная ставка: 250kk m¢!</i>", parse_mode=ParseMode.HTML)
            return

        if user_data["balance"] < bet:
            await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
            return

        await start_diamonds_game_from_command(message, user_id, chat_id, bet, mine_count)

    except ValueError:
        await show_diamonds_info(message, user_id)


# --- WEBAPP & ARENA COMMAND ---

@dp.message(Command("app", "webapp", "arena", "арена"))
@dp.message(lambda message: message.text and re.match(r'^(арена|arena|мини\s*апп|webapp)\b', message.text, re.IGNORECASE))
async def cmd_webapp_arena(message: types.Message):
    user_id = message.from_user.id
    user_link = get_user_mention(user_id, message.from_user.first_name)
    webapp_url = f"{WEBHOOK_HOST}/app"
    is_private = message.chat.type == "private"

    if is_private:
        btn = InlineKeyboardButton(
            text="⚔️ Открыть Арену (WebApp)",
            web_app=types.WebAppInfo(url=webapp_url)
        )
    else:
        btn = InlineKeyboardButton(
            text="⚔️ Открыть Арену",
            url=f"https://t.me/{BOT_USERNAME}/app" if BOT_USERNAME else webapp_url
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[btn]])
    text = (
        f"{user_link}\n"
        "⚔️ <b>Mines · Мультиплеерная Арена</b>\n"
        "<code>·····················</code>\n"
        "<blockquote><i>🎯 Делайте ставки в mCoin, получайте зоны на интерактивном поле и забирайте весь банк раунда при остановке шара!</i></blockquote>\n\n"
        "<i>Нажмите кнопку ниже, чтобы запустить Mini App:</i>"
    )
    await message.answer(text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)


# --- 21 (ОЧКО) GAME ---

@dp.message(Command("21", "очко", "ochko", "twentyone"))
@dp.message(lambda message: message.text and re.match(r'^(21|очко|ochko)\b', message.text, re.IGNORECASE))
async def cmd_twentyone(message: types.Message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_twentyone_info(message, user_id)
        return

    unplayed = get_active_unplayed_games(user_id)
    if unplayed:
        await message.answer("<i>У вас уже есть игра! Закончите или доиграйте активную игру.</i>", parse_mode=ParseMode.HTML)
        return

    bet_str = parts[1].strip()
    user_data = get_user(user_id)

    try:
        bet = resolve_bet_amount(bet_str, user_data["balance"])
        if bet is None:
            await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, вб, пол</i>", parse_mode=ParseMode.HTML)
            return

        if bet < 1:
            if user_data["balance"] < 1:
                await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
            else:
                await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return

        if bet > 500000000:
            await message.answer("<i>Максимальная ставка: 500kk m¢!</i>", parse_mode=ParseMode.HTML)
            return

        if user_data["balance"] < bet:
            await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
            return

        await start_twentyone_game_from_command(message, user_id, chat_id, bet)

    except ValueError:
        await show_twentyone_info(message, user_id)


# --- CRASH GAME (КРАШ) ---

CRASH_STICKER_ID = "CAACAgIAAxkBAAERvEdqhKGsSq0k-jQQ4fTpDTbyWVntowACGoAAAq1smEp3pQH6UfFOqz0E"


async def process_crash_round(message: types.Message, user_id: int, bet: int, target_mult: float, user_first_name: str = None):
    sticker_msg = None
    try:
        sticker_msg = await message.answer_sticker(CRASH_STICKER_ID)
    except Exception:
        pass

    await asyncio.sleep(2)

    if sticker_msg:
        try:
            await sticker_msg.delete()
        except Exception:
            pass

    # Generate crash point (97% RTP)
    r = secrets.SystemRandom().random()
    if r >= 0.97:
        crash_point = 1.00
    else:
        crash_point = 0.97 / (1.0 - r)

    crash_point = round(crash_point, 2)
    if crash_point < 1.00:
        crash_point = 1.00
    elif crash_point > 100000.00:
        crash_point = 100000.00

    if crash_point == int(crash_point):
        crash_point_str = f"{int(crash_point)}"
    else:
        crash_point_str = f"{crash_point:.2f}".rstrip('0').rstrip('.')

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))

    current_data = get_user(user_id)

    if target_mult <= crash_point:
        # WIN
        win_amount = int(bet * target_mult)
        if win_amount > 2000000000:
            win_amount = 2000000000
        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "crash", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<blockquote> <tg-emoji emoji-id="5283080528818360566">🚀</tg-emoji>Ракета упала на x<b>{crash_point_str}</b><tg-emoji emoji-id="5244837092042750681">📈</tg-emoji>\n\n'
            f'<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji> Ты <b>выиграл! Твой выигрыш составил <code>{format_number(win_amount)}</code> m¢</b> </blockquote>'
        )
    else:
        # LOSE
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "crash", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<blockquote> <tg-emoji emoji-id="5283080528818360566">🚀</tg-emoji>Ракета упала на x<b>{crash_point_str}</b><tg-emoji emoji-id="5246762912428603768">📉</tg-emoji>\n\n'
            f'<tg-emoji emoji-id="5210952531676504517">❌</tg-emoji> Ты <b>проиграл <code>{format_number(bet)}</code> m¢</b> </blockquote>'
        )

    await message.answer(
        text=response_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.message(Command("crash", "краш"))
@dp.message(lambda message: message.text and re.match(r'^(краш|crash|кр)\s*', message.text, re.IGNORECASE))
async def cmd_crash(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_crash_info(message, user_id)
        return

    args = parts[1].strip().split()

    if len(args) < 2:
        await show_crash_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(args[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 200000000:
        await message.answer("<i>Максимальная ставка: 200kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    # Parse multiplier
    target_mult = parse_multiplier(args[1])
    if target_mult is None or target_mult < 1.01 or target_mult > 100000:
        await message.answer("<i>Множитель должен быть от x1.01 до x100k! Пример: 1.1, 4, 100к</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_crash_round(message, user_id, bet, target_mult, user_first_name=message.from_user.first_name))


# --- SLOTS GAME (СЛОТЫ) ---

async def process_slots_outcome(message: types.Message, dice_msg: types.Message, user_id: int, bet: int, user_first_name: str = None):
    await asyncio.sleep(2.7)

    # Telegram dice 🎰 value is 1..64
    # (value - 1) has 6 bits: reel1 (bits 0-1), reel2 (bits 2-3), reel3 (bits 4-5)
    # 0 = BAR (🅱️), 1 = Berries/Cherry (🍒), 2 = Lemon (🍋), 3 = Seven (7️⃣)
    val = (dice_msg.dice.value - 1) if (dice_msg and dice_msg.dice) else 0
    reel1 = val & 3
    reel2 = (val >> 2) & 3
    reel3 = (val >> 4) & 3

    symbols = {0: "🅱️", 1: "🍒", 2: "🍋", 3: "7️⃣"}
    combo_str = f"{symbols.get(reel1, '❓')}{symbols.get(reel2, '❓')}{symbols.get(reel3, '❓')}"

    is_win = (reel1 == reel2 == reel3)

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))
    current_data = get_user(user_id)

    if is_win:
        win_mult = 15.5
        win_amount = int(bet * win_mult)
        if win_amount > 3000000000:
            win_amount = 3000000000

        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "slots", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Слоты · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'💰 <b>Выигрыш:</b> х15.5 / {format_number(win_amount)} m¢\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Выпало: {combo_str} </blockquote>'
        )
    else:
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "slots", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5276032951342088188">💥</tg-emoji><b>Слоты · Проигрыш!</b>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Выпало: {combo_str} </blockquote>'
        )

    await message.answer(
        text=response_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.message(Command("slots", "slot"))
@dp.message(lambda message: message.text and re.match(r'^(слоты|слот|slots|slot)\b', message.text, re.IGNORECASE))
async def cmd_slots(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_slots_info(message, user_id)
        return

    args = parts[1].strip().split()
    if not args:
        await show_slots_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(args[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 300000000:
        await message.answer("<i>Максимальная ставка: 300kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        dice_msg = await message.answer_dice(emoji="🎰")
    except Exception:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + bet)
        await message.answer("<i>Ошибка запуска слотов! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_slots_outcome(message, dice_msg, user_id, bet, user_first_name=message.from_user.first_name))


# --- BOWLING GAME (БОУЛИНГ) ---

def parse_bowling_choice(choice_text: str):
    t = choice_text.lower().strip()
    if t in ["страйк", "strike", "страйк!", "все", "всё", "all", "6", "6 кеглей", "6кеглей"]:
        return 6, "Страйк (6 кеглей)"
    elif t in ["мимо", "miss", "промах", "ни одной", "0", "0 кеглей", "0кеглей"]:
        return 1, "Мимо (0 кеглей)"
    elif t in ["1", "1 кегля", "1 кеглю", "1 кегли", "1кегля", "1кеглю"]:
        return 2, "1 кегля"
    elif t in ["3", "3 кегли", "3 кеглей", "3кегли"]:
        return 3, "3 кегли"
    elif t in ["4", "4 кегли", "4 кеглей", "4кегли"]:
        return 4, "4 кегли"
    elif t in ["5", "5 кеглей", "5 кегли", "5кеглей"]:
        return 5, "5 кеглей"
    elif t in ["2", "2 кегли", "2 кеглей", "2кегли"]:
        return 3, "3 кегли"
    return None, None


def get_bowling_choice_keyboard(bet: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 кегля (х5.8)", callback_data=f"bwl_{bet}_2", style="primary"),
                InlineKeyboardButton(text="3 кегли (х5.8)", callback_data=f"bwl_{bet}_3", style="primary"),
            ],
            [
                InlineKeyboardButton(text="4 кегли (х5.8)", callback_data=f"bwl_{bet}_4", style="primary"),
                InlineKeyboardButton(text="5 кеглей (х5.8)", callback_data=f"bwl_{bet}_5", style="primary"),
            ],
            [
                InlineKeyboardButton(text="Страйк (6 кеглей) (х5.8)", callback_data=f"bwl_{bet}_6", style="primary"),
                InlineKeyboardButton(text="Мимо (0 кеглей) (х5.8)", callback_data=f"bwl_{bet}_1", style="primary"),
            ]
        ]
    )


async def process_bowling_outcome(message: types.Message, dice_msg: types.Message, user_id: int, bet: int, target_choice_code: int, target_choice_name: str, user_first_name: str = None):
    await asyncio.sleep(2.7)

    val = dice_msg.dice.value if (dice_msg and dice_msg.dice) else 1

    outcome_names = {
        1: "Мимо (0 кеглей)",
        2: "1 кегля",
        3: "3 кегли",
        4: "4 кегли",
        5: "5 кеглей",
        6: "Страйк (6 кеглей)"
    }
    outcome_str = outcome_names.get(val, f"{val} кеглей")

    is_win = (val == target_choice_code)

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))
    current_data = get_user(user_id)

    if is_win:
        win_mult = 5.8
        win_amount = int(bet * win_mult)
        if win_amount > 1740000000:
            win_amount = 1740000000

        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "bowling", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Боулинг · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'💰 <b>Выигрыш:</b> х5.8 / {format_number(win_amount)} m¢\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )
    else:
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "bowling", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji><b> Боулинг · Проигрыш!</b>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )

    try:
        await message.answer(
            text=response_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("bwl_"))
async def process_bowling_choice_callback(callback: types.CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    try:
        bet = int(parts[1])
        target_code = int(parts[2])
    except ValueError:
        return

    user_id = callback.from_user.id
    user_data = get_user(user_id)
    if user_data["balance"] < bet:
        await callback.message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await callback.message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    target_names = {
        1: "Мимо (0 кеглей)",
        2: "1 кегля",
        3: "3 кегли",
        4: "4 кегли",
        5: "5 кеглей",
        6: "Страйк (6 кеглей)"
    }
    target_name = target_names.get(target_code, f"{target_code} кеглей")

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        dice_msg = await callback.message.answer_dice(emoji="🎳")
    except Exception:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + bet)
        await callback.message.answer("<i>Ошибка запуска боулинга! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_bowling_outcome(callback.message, dice_msg, user_id, bet, target_code, target_name, user_first_name=callback.from_user.first_name))


@dp.message(Command("bowling", "боулинг", "бо"))
@dp.message(lambda message: message.text and re.match(r'^(боулинг|bowling|бо)\b', message.text, re.IGNORECASE))
async def cmd_bowling(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_bowling_info(message, user_id)
        return

    tokens = parts[1].strip().split()
    if not tokens:
        await show_bowling_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(tokens[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 300000000:
        await message.answer("<i>Максимальная ставка: 300kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # If outcome is specified in arguments (e.g., "боулинг 100 1 кегля", "боулинг 100 страйк")
    if len(tokens) >= 2:
        choice_raw = " ".join(tokens[1:])
        target_code, target_name = parse_bowling_choice(choice_raw)
        if target_code is not None:
            # Deduct bet upfront
            if not update_user(user_id, balance=user_data["balance"] - bet):
                await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
                return
            try:
                dice_msg = await message.answer_dice(emoji="🎳")
            except Exception:
                user_data = get_user(user_id)
                update_user(user_id, balance=user_data["balance"] + bet)
                await message.answer("<i>Ошибка запуска боулинга! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
                return
            asyncio.create_task(process_bowling_outcome(message, dice_msg, user_id, bet, target_code, target_name, user_first_name=message.from_user.first_name))
            return

    # Otherwise prompt user to choose outcome
    user_link = get_user_mention(user_id, message.from_user.first_name)
    prompt_text = (
        f"{user_link}\n"
        f"🎳 <b>Боулинг · Выберите итог:</b>\n"
        f"<code>·····················</code>\n"
        f"💸 <b>Ставка:</b> {format_number(bet)} m¢\n\n"
        f"<blockquote><i>Укажите, сколько кеглей собьет бросок:</i></blockquote>"
    )
    await message.answer(
        text=prompt_text,
        reply_markup=get_bowling_choice_keyboard(bet),
        parse_mode=ParseMode.HTML
    )


# --- DARTS GAME (ДАРТС) ---

def parse_darts_choice(choice_text: str):
    t = choice_text.lower().strip()
    if t in ["красное", "красный", "красная", "red", "к", "крас"]:
        return "red", "🔴 Красное", 1.94
    elif t in ["белое", "белый", "белая", "white", "б", "бел"]:
        return "white", "⚪️ Белое", 2.9
    elif t in ["центр", "яблочко", "булзай", "center", "bullseye", "ц", "в центр"]:
        return "center", "🎯 Центр", 5.8
    elif t in ["мимо", "промах", "miss", "м"]:
        return "miss", "😯 Мимо", 5.8
    return None, None, None


def get_darts_choice_keyboard(bet: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 Красное (х1.94)", callback_data=f"darts_{bet}_red", style="primary"),
                InlineKeyboardButton(text="⚪️ Белое (х2.9)", callback_data=f"darts_{bet}_white", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🎯 Центр (х5.8)", callback_data=f"darts_{bet}_center", style="primary"),
                InlineKeyboardButton(text="😯 Мимо (х5.8)", callback_data=f"darts_{bet}_miss", style="primary"),
            ]
        ]
    )


async def process_darts_outcome(message: types.Message, dice_msg: types.Message, user_id: int, bet: int, target_choice_code: str, target_choice_name: str, win_mult: float, user_first_name: str = None):
    await asyncio.sleep(2.7)

    val = dice_msg.dice.value if (dice_msg and dice_msg.dice) else 1

    outcome_names = {
        1: "😯 Мимо",
        2: "🔴 Красное кольцо",
        3: "⚪️ Белое кольцо",
        4: "🔴 Красное кольцо",
        5: "⚪️ Белое кольцо",
        6: "🎯 Центр (Яблочко)"
    }
    outcome_str = outcome_names.get(val, f"Сектор {val}")

    # Win condition
    is_win = False
    if target_choice_code == "red" and val in [2, 4]:
        is_win = True
    elif target_choice_code == "white" and val in [3, 5]:
        is_win = True
    elif target_choice_code == "center" and val == 6:
        is_win = True
    elif target_choice_code == "miss" and val == 1:
        is_win = True

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))
    current_data = get_user(user_id)

    if is_win:
        win_amount = int(bet * win_mult)
        if win_amount > 1740000000:
            win_amount = 1740000000

        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "darts", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Дартс · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'💰 <b>Выигрыш:</b> х{win_mult} / {format_number(win_amount)} m¢\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )
    else:
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "darts", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji><b> Дартс · Проигрыш!</b>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )

    await message.answer(
        text=response_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("darts_"))
async def process_darts_choice_callback(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    try:
        bet = int(parts[1])
        target_code = parts[2]
    except ValueError:
        return

    code_map = {
        "red": ("🔴 Красное", 1.94),
        "white": ("⚪️ Белое", 2.9),
        "center": ("🎯 Центр", 5.8),
        "miss": ("😯 Мимо", 5.8)
    }
    if target_code not in code_map:
        return
    target_name, win_mult = code_map[target_code]

    user_id = callback.from_user.id
    user_data = get_user(user_id)
    if user_data["balance"] < bet:
        await callback.message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await callback.message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        dice_msg = await callback.message.answer_dice(emoji="🎯")
    except Exception:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + bet)
        await callback.message.answer("<i>Ошибка запуска дартса! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_darts_outcome(callback.message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=callback.from_user.first_name))


@dp.message(Command("darts", "дартс", "дс", "dart"))
@dp.message(lambda message: message.text and re.match(r'^(дартс|darts|дс|dart)\b', message.text, re.IGNORECASE))
async def cmd_darts(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_darts_info(message, user_id)
        return

    tokens = parts[1].strip().split()
    if not tokens:
        await show_darts_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(tokens[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 300000000:
        await message.answer("<i>Максимальная ставка: 300kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # If outcome is specified in arguments (e.g., "дартс 100 центр", "дс 100 мимо")
    if len(tokens) >= 2:
        choice_raw = " ".join(tokens[1:])
        target_code, target_name, win_mult = parse_darts_choice(choice_raw)
        if target_code is not None:
            # Deduct bet upfront
            if not update_user(user_id, balance=user_data["balance"] - bet):
                await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
                return
            try:
                dice_msg = await message.answer_dice(emoji="🎯")
            except Exception:
                user_data = get_user(user_id)
                update_user(user_id, balance=user_data["balance"] + bet)
                await message.answer("<i>Ошибка запуска дартса! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
                return
            asyncio.create_task(process_darts_outcome(message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=message.from_user.first_name))
            return

    # Otherwise prompt user to choose outcome
    user_link = get_user_mention(user_id, message.from_user.first_name)
    prompt_text = (
        f"{user_link}\n"
        f"🎯 <b>Дартс · выбери исход!</b>\n"
        f"<code>·····················</code>\n"
        f"💸 <b>Ставка:</b> {format_number(bet)} m¢\n\n"
        f"🔰 <b>Коэффициенты:</b>\n"
        f"🔴 Красное (х1.94)\n"
        f"⚪️ Белое (х2.9)\n"
        f"🎯 Центр (х5.8)\n"
        f"😯 Мимо (х5.8)"
    )
    await message.answer(
        text=prompt_text,
        reply_markup=get_darts_choice_keyboard(bet),
        parse_mode=ParseMode.HTML
    )


# --- BASKETBALL GAME (БАСКЕТБОЛ) ---

def parse_basketball_choice(choice_text: str):
    t = choice_text.lower().strip()
    if t in ["попадание", "попал", "гол", "hit", "score", "п", "г", "в корзину", "1"]:
        return "hit", "🏀 Попадание", 2.4
    elif t in ["мимо", "промах", "miss", "м", "0"]:
        return "miss", "💨 Мимо", 2.4
    elif t in ["застрял", "застрял мяч", "дужка", "stuck", "з"]:
        return "stuck", "🛑 Застрял мяч", 4.8
    return None, None, None


def get_basketball_choice_keyboard(bet: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏀 Попадание (х2.4)", callback_data=f"bb_{bet}_hit", style="primary"),
                InlineKeyboardButton(text="💨 Мимо (х2.4)", callback_data=f"bb_{bet}_miss", style="primary"),
            ],
            [
                InlineKeyboardButton(text="🛑 Застрял мяч (х4.8)", callback_data=f"bb_{bet}_stuck", style="primary"),
            ]
        ]
    )


async def process_basketball_outcome(message: types.Message, dice_msg: types.Message, user_id: int, bet: int, target_choice_code: str, target_choice_name: str, win_mult: float, user_first_name: str = None):
    await asyncio.sleep(2.7)

    val = dice_msg.dice.value if (dice_msg and dice_msg.dice) else 1

    outcome_names = {
        1: "💨 Мимо",
        2: "💨 Отскок мимо",
        3: "🛑 Застрял мяч",
        4: "🏀 Попадание!",
        5: "🏀 Чистое попадание!"
    }
    outcome_str = outcome_names.get(val, "Мимо")

    # Win condition
    is_win = False
    if target_choice_code == "hit" and val in [4, 5]:
        is_win = True
    elif target_choice_code == "miss" and val in [1, 2]:
        is_win = True
    elif target_choice_code == "stuck" and val == 3:
        is_win = True

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))
    current_data = get_user(user_id)

    if is_win:
        win_amount = int(bet * win_mult)
        if win_amount > 1440000000:
            win_amount = 1440000000

        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "basketball", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Баскетбол · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'💰 <b>Выигрыш:</b> х{win_mult} / {format_number(win_amount)} m¢\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )
    else:
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "basketball", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji><b> Баскетбол · Проигрыш!</b>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )

    await message.answer(
        text=response_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("bb_"))
async def process_basketball_choice_callback(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    try:
        bet = int(parts[1])
        target_code = parts[2]
    except ValueError:
        return

    code_map = {
        "hit": ("🏀 Попадание", 2.4),
        "miss": ("💨 Мимо", 2.4),
        "stuck": ("🛑 Застрял мяч", 4.8)
    }
    if target_code not in code_map:
        return
    target_name, win_mult = code_map[target_code]

    user_id = callback.from_user.id
    user_data = get_user(user_id)
    if user_data["balance"] < bet:
        await callback.message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await callback.message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        dice_msg = await callback.message.answer_dice(emoji="🏀")
    except Exception:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + bet)
        await callback.message.answer("<i>Ошибка запуска баскетбола! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_basketball_outcome(callback.message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=callback.from_user.first_name))


@dp.message(Command("basketball", "баскетбол", "баскет", "бс", "бк"))
@dp.message(lambda message: message.text and re.match(r'^(баскетбол|basketball|баскет|бс|бк)\b', message.text, re.IGNORECASE))
async def cmd_basketball(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_basketball_info(message, user_id)
        return

    tokens = parts[1].strip().split()
    if not tokens:
        await show_basketball_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(tokens[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 300000000:
        await message.answer("<i>Максимальная ставка: 300kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # If outcome is specified in arguments (e.g., "баскетбол 100 гол", "бс 100 мимо")
    if len(tokens) >= 2:
        choice_raw = " ".join(tokens[1:])
        target_code, target_name, win_mult = parse_basketball_choice(choice_raw)
        if target_code is not None:
            # Deduct bet upfront
            if not update_user(user_id, balance=user_data["balance"] - bet):
                await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
                return
            try:
                dice_msg = await message.answer_dice(emoji="🏀")
            except Exception:
                user_data = get_user(user_id)
                update_user(user_id, balance=user_data["balance"] + bet)
                await message.answer("<i>Ошибка запуска баскетбола! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
                return
            asyncio.create_task(process_basketball_outcome(message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=message.from_user.first_name))
            return

    # Otherwise prompt user to choose outcome
    user_link = get_user_mention(user_id, message.from_user.first_name)
    prompt_text = (
        f"{user_link}\n"
        f"🏀 <b>Баскетбол · выбери исход!</b>\n"
        f"<code>·····················</code>\n"
        f"💸 <b>Ставка:</b> {format_number(bet)} m¢\n\n"
        f"🔰 <b>Коэффициенты:</b>\n"
        f"🏀 Попадание (х2.4)\n"
        f"💨 Мимо (х2.4)\n"
        f"🛑 Застрял мяч (х4.8)"
    )
    await message.answer(
        text=prompt_text,
        reply_markup=get_basketball_choice_keyboard(bet),
        parse_mode=ParseMode.HTML
    )


# --- FOOTBALL GAME (ФУТБОЛ) ---

def parse_football_choice(choice_text: str):
    t = choice_text.lower().strip()
    if t in ["гол", "попадание", "забил", "goal", "г", "1"]:
        return "goal", "⚽️ Гол", 1.6
    elif t in ["мимо", "промах", "штанга", "miss", "м", "0"]:
        return "miss", "💨 Мимо", 2.4
    return None, None, None


def get_football_choice_keyboard(bet: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚽️ Гол (х1.6)", callback_data=f"fb_{bet}_goal", style="primary"),
                InlineKeyboardButton(text="💨 Мимо (х2.4)", callback_data=f"fb_{bet}_miss", style="primary"),
            ]
        ]
    )


async def process_football_outcome(message: types.Message, dice_msg: types.Message, user_id: int, bet: int, target_choice_code: str, target_choice_name: str, win_mult: float, user_first_name: str = None):
    await asyncio.sleep(2.7)

    val = dice_msg.dice.value if (dice_msg and dice_msg.dice) else 1

    outcome_names = {
        1: "💨 Мимо ворот",
        2: "🥅 Штанга / Мимо",
        3: "⚽️ Гол!",
        4: "⚽️ Гол в девятку!",
        5: "⚽️ Гол!"
    }
    outcome_str = outcome_names.get(val, "Мимо")

    # Win condition
    is_win = False
    if target_choice_code == "goal" and val in [3, 4, 5]:
        is_win = True
    elif target_choice_code == "miss" and val in [1, 2]:
        is_win = True

    user_link = get_user_mention(user_id, user_first_name or (message.from_user.first_name if message and message.from_user and message.from_user.first_name != "Мины Бот" else None))
    current_data = get_user(user_id)

    if is_win:
        win_amount = int(bet * win_mult)
        if win_amount > 720000000:
            win_amount = 720000000

        new_balance = current_data["balance"] + win_amount
        new_games = current_data["games"] + 1

        update_user(user_id, balance=new_balance, games=new_games)
        add_game_history(user_id, "football", bet, "win", win_amount)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Футбол · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'💰 <b>Выигрыш:</b> х{win_mult} / {format_number(win_amount)} m¢\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )
    else:
        new_games = current_data["games"] + 1
        new_lost = current_data["lost"] + bet

        update_user(user_id, games=new_games, lost=new_lost)
        add_game_history(user_id, "football", bet, "lose", 0)

        response_text = (
            f"{user_link}\n"
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji><b> Футбол · Проигрыш!</b>\n'
            f'<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'🎲 <b>Выбрано:</b> {target_choice_name}\n'
            f'<code>············</code>\n'
            f'<blockquote> <tg-emoji emoji-id="5258203794772085854">⚡️</tg-emoji> Итог: {outcome_str} </blockquote>'
        )

    await message.answer(
        text=response_text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("fb_"))
async def process_football_choice_callback(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        return
    try:
        bet = int(parts[1])
        target_code = parts[2]
    except ValueError:
        return

    code_map = {
        "goal": ("⚽️ Гол", 1.6),
        "miss": ("💨 Мимо", 2.4)
    }
    if target_code not in code_map:
        return
    target_name, win_mult = code_map[target_code]

    user_id = callback.from_user.id
    user_data = get_user(user_id)
    if user_data["balance"] < bet:
        await callback.message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # Deduct bet upfront
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await callback.message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        await callback.message.delete()
    except Exception:
        pass

    try:
        dice_msg = await callback.message.answer_dice(emoji="⚽")
    except Exception:
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + bet)
        await callback.message.answer("<i>Ошибка запуска футбола! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
        return

    asyncio.create_task(process_football_outcome(callback.message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=callback.from_user.first_name))


@dp.message(Command("football", "футбол", "фб"))
@dp.message(lambda message: message.text and re.match(r'^(футбол|football|фб)\b', message.text, re.IGNORECASE))
async def cmd_football(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    parts = re.split(r'\s+', text, maxsplit=1)

    if len(parts) == 1:
        await show_football_info(message, user_id)
        return

    tokens = parts[1].strip().split()
    if not tokens:
        await show_football_info(message, user_id)
        return

    user_data = get_user(user_id)
    bet = resolve_bet_amount(tokens[0], user_data["balance"])
    if bet is None:
        await message.answer("<i>Неверный формат суммы! Пример: 100, 1.4кк, 120кк, все, пол</i>", parse_mode=ParseMode.HTML)
        return

    if bet < 1:
        if user_data["balance"] < 1:
            await message.answer("<i>Ваш баланс равен 0! Пополните баланс чтобы играть.</i>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("<i>Ставка должна быть больше 0!</i>", parse_mode=ParseMode.HTML)
        return

    if bet > 300000000:
        await message.answer("<i>Максимальная ставка: 300kk m¢!</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < bet:
        await message.answer(f"<i>Недостаточно средств! Ваш баланс: {format_number(user_data['balance'])} mCoin</i>", parse_mode=ParseMode.HTML)
        return

    # If outcome is specified in arguments (e.g., "футбол 100 гол", "фб 100 мимо")
    if len(tokens) >= 2:
        choice_raw = " ".join(tokens[1:])
        target_code, target_name, win_mult = parse_football_choice(choice_raw)
        if target_code is not None:
            # Deduct bet upfront
            if not update_user(user_id, balance=user_data["balance"] - bet):
                await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
                return
            try:
                dice_msg = await message.answer_dice(emoji="⚽")
            except Exception:
                user_data = get_user(user_id)
                update_user(user_id, balance=user_data["balance"] + bet)
                await message.answer("<i>Ошибка запуска футбола! Ставка возвращена.</i>", parse_mode=ParseMode.HTML)
                return
            asyncio.create_task(process_football_outcome(message, dice_msg, user_id, bet, target_code, target_name, win_mult, user_first_name=message.from_user.first_name))
            return

    # Otherwise prompt user to choose outcome
    user_link = get_user_mention(user_id, message.from_user.first_name)
    prompt_text = (
        f"{user_link}\n"
        f"⚽️ <b>Футбол · выбери исход!</b>\n"
        f"<code>·····················</code>\n"
        f"💸 <b>Ставка:</b> {format_number(bet)} m¢\n\n"
        f"🔰 <b>Коэффициенты:</b>\n"
        f"⚽️ Гол (х1.6)\n"
        f"💨 Мимо (х2.4)"
    )
    await message.answer(
        text=prompt_text,
        reply_markup=get_football_choice_keyboard(bet),
        parse_mode=ParseMode.HTML
    )


# --- PROMO CODES ---

@dp.message(Command("promo", "промо"))
@dp.message(lambda message: message.text and re.match(r'^(промо|promo)\b', message.text, re.IGNORECASE))
async def cmd_promo(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    user_name = html.escape(message.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

    parts = re.split(r'\s+', text, maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply(
            f'<i>{user_link}, ты не ввел промокод <tg-emoji emoji-id="5474493399996308572">😏</tg-emoji></i>',
            parse_mode=ParseMode.HTML
        )
        return

    code_input = clean_promo_string(parts[1])

    try:
        cursor.execute("SELECT code, reward, remaining_activations FROM promo_codes WHERE code = ?", (code_input,))
        promo = cursor.fetchone()

        if not promo or promo[2] <= 0:
            cursor.execute("DELETE FROM promo_codes WHERE remaining_activations <= 0")
            conn.commit()
            await message.reply(
                f'<i>{user_link}, такого промокода не существует! <tg-emoji emoji-id="5371074117971745503">🤡</tg-emoji></i>',
                parse_mode=ParseMode.HTML
            )
            return

        real_code, reward, remaining = promo

        cursor.execute("SELECT 1 FROM promo_activations WHERE code = ? AND user_id = ?", (real_code, user_id))
        if cursor.fetchone():
            await message.reply(
                f'<i>{user_link}, ты уже активировал этот промокод!</i>',
                parse_mode=ParseMode.HTML
            )
            return

        new_remaining = remaining - 1
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

        cursor.execute("INSERT INTO promo_activations (code, user_id, activated_at) VALUES (?, ?, ?) ON CONFLICT (code, user_id) DO NOTHING", (real_code, user_id, now_str))

        if new_remaining <= 0:
            cursor.execute("DELETE FROM promo_codes WHERE code = ?", (real_code,))
        else:
            cursor.execute("UPDATE promo_codes SET remaining_activations = ? WHERE code = ?", (new_remaining, real_code))
        conn.commit()

        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + reward)

        await message.reply(
            f'<i>{user_link}, ты успешно активировал промокод «{html.escape(real_code)}» и получил {format_number(reward)} mCoin</i>',
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        await message.reply(f"<i>Ошибка при активации промокода: {html.escape(str(e))}</i>", parse_mode=ParseMode.HTML)


# --- BALANCE, BONUS & TRANSFERS ---

@dp.message(lambda message: message.text and message.text.lower() in ["б", "баланс", "бал"])
async def cmd_balance(message: types.Message):
    user_id = message.from_user.id

    user_data = get_user(user_id)

    if not user_data:
        await message.answer("<i>Ошибка получения данных!</i>", parse_mode=ParseMode.HTML)
        return

    balance = user_data["balance"]
    games_count = user_data["games"]
    lost = user_data["lost"]

    text = (
        f'<i><tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: {format_number(balance)} mCoin\n'
        '<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> Сыграно игр: {games_count}\n'
        f'<tg-emoji emoji-id="5442983582882601962">🗿</tg-emoji> Проиграно mCoin: {format_number(lost)}</i>'
    )

    if message.chat.type in ["group", "supergroup"]:
        await message.reply(
            text=text,
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer(
            text=text,
            parse_mode=ParseMode.HTML
        )


@dp.message(lambda message: message.text and message.text.lower() == "бонус" and message.chat.type in ["group", "supergroup"])
async def cmd_bonus(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)

    if not user_data:
        await message.reply("<i>Ошибка получения данных!</i>", parse_mode=ParseMode.HTML)
        return

    try:
        user_info = await bot.get_chat(user_id)
        description = user_info.bio or ""
    except Exception:
        description = ""

    if f"@{BOT_USERNAME}" not in description and BOT_USERNAME not in description:
        text = (
            f'<i>Чтобы получить бонус, нужно выполнить некие условия.\n'
            '<code>·····················</code>\n'
            f'Нужно добавить юзернейм бота в свое описание.\n'
            f'Для продолжения нажмите ниже.</i>'
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Проверить",
                        callback_data="check_bonus",
                        style="success",
                        icon_custom_emoji_id="5465368548702446780"
                    ),
                    InlineKeyboardButton(
                        text="Туториал",
                        callback_data="bonus_tutorial",
                        style="primary",
                        icon_custom_emoji_id="5471960722206366390"
                    )
                ]
            ]
        )

        await message.reply(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard
        )
        return

    if user_data["bonus_time"]:
        try:
            last_bonus = datetime.fromisoformat(user_data["bonus_time"])
            if datetime.now() - last_bonus < timedelta(hours=6):
                time_left = timedelta(hours=6) - (datetime.now() - last_bonus)
                hours = int(time_left.total_seconds() // 3600)
                minutes = int((time_left.total_seconds() % 3600) // 60)
                await message.reply(f"<i>Бонус можно получить только раз в 6 часов. Осталось: {hours}ч {minutes}м</i>", parse_mode=ParseMode.HTML)
                return
        except Exception:
            pass

    bonus = random.randint(70000, 100000)
    if update_user(user_id, balance=user_data["balance"] + bonus, bonus_time=datetime.now().isoformat()):
        try:
            asyncio.create_task(activate_referral_if_needed(user_id))
        except Exception:
            pass
        await message.reply(
            f'<i>🎉 Поздравляем! Вы получили бонус {format_number(bonus)} mCoin!\n'
            f'Текущий баланс: {format_number(user_data["balance"] + bonus)} mCoin</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.reply("<i>Ошибка при начислении бонуса!</i>", parse_mode=ParseMode.HTML)


# --- INFO PROMPTS ---

async def show_mines_info(message, user_id):
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Мины — это игра, в которой вам нужно угадать пустые ячейки. Чем больше ячеек вы откроете, тем больше получите mCoin!\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х5,044,291\n'
        'Макс. ставка: 250kk m¢\n'
        'Макс. выигрыш: 2kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u><tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> /mines [ставка] [мины (1-6)]</u></b>\n\n'
        'Пример: <code>/mines 100 6</code>\n'
        'Пример: <code>Мины 1.4кк</code>'
    )

    await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def show_tower_info(message, user_id):
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Башня — это игра, в которой нужно избежать мин и добраться до вершины.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х1,894,531\n'
        'Макс. ставка: 250kk m¢\n'
        'Макс. выигрыш: 2kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>🛕 /tower [ставка] [мины (1-4)]</u></b>\n\n'
        'Пример: <code>/tower 100 4</code>\n'
        'Пример: <code>Башня 1.4кк</code>'
    )

    await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def show_diamonds_info(message, user_id):
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Алмазная лихорадка — это игра, в которой необходимо угадать, в какой ячейке спрятан алмаз. Вам нужно открывать по одной ячейке на каждом из 16 уровней, чтобы найти алмаз.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х41,755,319\n'
        'Макс. ставка: 250kk m¢\n'
        'Макс. выигрыш: 2kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u><tg-emoji emoji-id="5307594157739515229">💠</tg-emoji> /diamond [ставка] [мины 1-2]</u></b>\n\n'
        'Пример: <code>/diamond 100 2</code>\n'
        'Пример: <code>алмазы 100</code>'
    )

    await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )


async def show_twentyone_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> 21 (Очко) — карточная игра, в которой цель игрока — набрать сумму очков ближе к 21, чем у дилера, не превышая это число. Карты от 2 до 10 считаются по номиналу, валет даёт 2 очка, дама — 3 очка, король — 4 очка, туз — 11 или 1, в зависимости от ситуации. Если сумма больше 21 — перебор.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х1.97\n'
        'Макс. ставка: 500kk m¢\n'
        'Макс. выигрыш: 1kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u><tg-emoji emoji-id="5395325195542078574">🍀</tg-emoji> /21 [ставка]</u></b>\n\n'
        'Пример: <code>/21 100</code>\n'
        'Пример: <code>очко 100</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_modes",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


@dp.callback_query(lambda c: c.data == "info_twentyone")
async def process_info_twentyone(callback: types.CallbackQuery):
    await callback.answer()
    await show_twentyone_info(callback, callback.from_user.id, is_callback=True)


async def show_crash_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Краш — это игра, в которой нужно выбрать множитель от x1.01 до x100k. Бот случайно останавливается от x1 до x100k.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х100,000\n'
        'Макс. ставка: 200kk m¢\n'
        'Макс. выигрыш: 2kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>📈 /crash [ставка] [1.01-100к]</u></b>\n\n'
        'Пример: <code>/crash 100 1.1</code>\n'
        'Пример: <code>краш 100 4</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


async def show_slots_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Слоты — это игра, где цель выбить три одинаковых символа на барабанах, запустив их вращение.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х15.5\n'
        'Макс. ставка: 300kk m¢\n'
        'Макс. выигрыш: 3kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>🎰 /slots [ставка]</u></b>\n\n'
        'Пример: <code>/slots 100</code>\n'
        'Пример: <code>слоты 100</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


async def show_bowling_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Боулинг — это игра, в которой вам нужно сбить кегли, чтобы получить максимальный множитель.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х5.8\n'
        'Макс. ставка: 300kk m¢\n'
        'Макс. выигрыш: 1.74kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>🎳 /bowling [ставка]</u></b>\n\n'
        'Пример: <code>/bowling 100</code>\n'
        'Пример: <code>боулинг 100</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


# --- CATALOG CALLBACK HANDLERS ---

@dp.callback_query(lambda c: c.data == "catalog_main")
async def process_catalog_main(callback: types.CallbackQuery):
    await callback.answer()
    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_catalog(callback, callback.from_user.id, callback.from_user.first_name, callback.message.chat.id, is_group, edit=True)


@dp.callback_query(lambda c: c.data == "catalog_fast")
async def process_catalog_fast(callback: types.CallbackQuery):
    await callback.answer()
    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_fast_games_catalog(callback, callback.from_user.id, callback.from_user.first_name, callback.message.chat.id, is_group)


@dp.callback_query(lambda c: c.data == "catalog_modes")
async def process_catalog_modes(callback: types.CallbackQuery):
    await callback.answer()
    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_modes_games_catalog(callback, callback.from_user.id, callback.from_user.first_name, callback.message.chat.id, is_group)


@dp.callback_query(lambda c: c.data == "info_slots")
async def process_info_slots(callback: types.CallbackQuery):
    await callback.answer()
    await show_slots_info(callback, callback.from_user.id, is_callback=True)


@dp.callback_query(lambda c: c.data == "info_bowling")
async def process_info_bowling(callback: types.CallbackQuery):
    await callback.answer()
    await show_bowling_info(callback, callback.from_user.id, is_callback=True)


async def show_darts_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Дартс — это игра, в которой нужно попасть в центр мишени, чтобы получить максимальный множитель.\n'
        '📊 Лимиты:\n'
        'RTP: ~97%\n'
        'Макс. множитель: х5.8\n'
        'Макс. ставка: 300kk m¢\n'
        'Макс. выигрыш: 1.74kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>🎯 /darts [ставка]</u></b>\n\n'
        'Пример: <code>/darts 100</code>\n'
        'Пример: <code>дартс 100 мимо</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


@dp.callback_query(lambda c: c.data == "info_darts")
async def process_info_darts(callback: types.CallbackQuery):
    await callback.answer()
    await show_darts_info(callback, callback.from_user.id, is_callback=True)


async def show_basketball_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Баскетбол — это игра, где нужно попасть в кольцо, чтобы получить максимальный множитель.\n'
        '📊 Лимиты:\n'
        'RTP: ~96%\n'
        'Макс. множитель: х4.8\n'
        'Макс. ставка: 300kk m¢\n'
        'Макс. выигрыш: 1.44kkk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>🏀 /basketball [ставка]</u></b>\n\n'
        'Пример: <code>/basketball 100</code>\n'
        'Пример: <code>баскетбол 100</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


@dp.callback_query(lambda c: c.data == "info_basketball")
async def process_info_basketball(callback: types.CallbackQuery):
    await callback.answer()
    await show_basketball_info(callback, callback.from_user.id, is_callback=True)


async def show_football_info(message_or_callback, user_id, is_callback=False):
    if hasattr(message_or_callback, "from_user") and message_or_callback.from_user:
        user_name = message_or_callback.from_user.first_name or "Игрок"
    else:
        user_name = "Игрок"
    user_link = f'<a href="tg://user?id={user_id}">{html.escape(user_name)}</a>'

    text = (
        '<blockquote expandable><i><tg-emoji emoji-id="5307594157739515229">ℹ️</tg-emoji> Футбол — это игра, в которой нужно предсказать, попадет ли мяч в ворота или пролетит мимо, чтобы получить выигрыш.\n'
        '📊 Лимиты:\n'
        'RTP: ~96%\n'
        'Макс. множитель: х2.4\n'
        'Макс. ставка: 300kk m¢\n'
        'Макс. выигрыш: 720kk m¢</i></blockquote>\n\n'
        f'<i><tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> {user_link}, чтобы начать игру, используй команду:</i>\n\n'
        '<b><u>⚽️ /football [ставка] [гол/мимо]</u></b>\n\n'
        'Пример: <code>/football 100</code>\n'
        'Пример: <code>футбол 100 гол</code>'
    )

    keyboard = None
    if is_callback:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data="catalog_fast",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        try:
            await message_or_callback.message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            await message_or_callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )


@dp.callback_query(lambda c: c.data == "info_football")
async def process_info_football(callback: types.CallbackQuery):
    await callback.answer()
    await show_football_info(callback, callback.from_user.id, is_callback=True)


@dp.callback_query(lambda c: c.data == "info_crash")
async def process_info_crash(callback: types.CallbackQuery):
    await callback.answer()
    await show_crash_info(callback, callback.from_user.id, is_callback=True)


# --- MINES GAME EXECUTION ---

async def start_mines_game_from_command(message, user_id, chat_id, bet, mine_count):
    key = get_chat_key(user_id, chat_id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    game_id = get_next_game_id()

    user_data = get_user(user_id)
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    now = datetime.now()
    game_data = {
        "type": "mines",
        "stage": "playing",
        "bet": bet,
        "mine_count": mine_count,
        "mine_positions": generate_mines(mine_count),
        "revealed": set(),
        "level": 0,
        "game_over": False,
        "won": False,
        "exploded_mine": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "cashed_out": False,
        "bet_placed": False,
        "moves_count": 0,
        "created_at": now,
        "created_at_str": now.strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    await show_mines_grid_from_message(message, user_id, game_id)


async def show_mines_grid_from_message(message, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await message.answer("<i>Игра не найдена!</i>", parse_mode=ParseMode.HTML)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'
    multiplier = get_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "mines", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "mines", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Мины · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Открыто:</b> {len(revealed) - 1} из {TOTAL_CELLS - mine_count}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Мины · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Открыто:</b> {len(revealed)} из {TOTAL_CELLS - mine_count}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        multiplier = get_multiplier(mine_count, level)

        if level == 0:
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5247011187308140698">🧨</tg-emoji>Мины • начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            win_amount = int(bet * multiplier)
            next_multiplier = get_multiplier(mine_count, level + 1)
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💎</tg-emoji>Мины • игра идёт.</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji><b>Выигрыш:</b> <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{next_multiplier:.2f}x</blockquote>'
            )

    grid_buttons = []
    for row in range(GRID_SIZE):
        row_buttons = []
        for col in range(GRID_SIZE):
            idx = row * GRID_SIZE + col

            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif idx in mine_positions:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"cell_{game_id}_{idx}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_game_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if len(revealed) > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        else:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"exit_game_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ])

    msg = None
    try:
        msg = await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


# --- TOWER GAME EXECUTION ---

async def start_tower_game_from_command(message, user_id, chat_id, bet, mine_count):
    key = get_chat_key(user_id, chat_id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    game_id = get_next_game_id()

    user_data = get_user(user_id)
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    now = datetime.now()
    game_data = {
        "type": "tower",
        "stage": "playing",
        "bet": bet,
        "mine_count": mine_count,
        "mine_positions": generate_tower_mines(mine_count),
        "revealed": set(),
        "level": 0,
        "game_over": False,
        "won": False,
        "exploded_mine": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "cashed_out": False,
        "bet_placed": False,
        "moves_count": 0,
        "created_at": now,
        "created_at_str": now.strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    await show_tower_grid_from_message(message, user_id, game_id)


async def show_tower_grid_from_message(message, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await message.answer("<i>Игра не найдена!</i>", parse_mode=ParseMode.HTML)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'
    multiplier = get_tower_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "tower", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "tower", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Башня · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Башня · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        if level == 0:
            text = (
                f'{user_link}\n'
                '<b>🛕Башня · начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_tower_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            text = (
                f'{user_link}\n'
                '<b>🛕Башня · игра идёт.</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_tower_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )

    grid_buttons = []

    display_max_row = min(level, TOWER_ROWS - 1)
    for row in range(display_max_row, -1, -1):
        row_buttons = []
        for col in range(TOWER_COLS):
            idx = row * TOWER_COLS + col
            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif col in mine_positions[row]:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"tower_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"tower_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    if row == level:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"tower_cell_{game_id}_{row}_{col}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"tower_noop_{game_id}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_tower_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if level > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"tower_cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_tower_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])

    msg = None
    try:
        msg = await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


# --- DIAMONDS GAME EXECUTION ---

async def start_diamonds_game_from_command(message, user_id, chat_id, bet, mine_count):
    key = get_chat_key(user_id, chat_id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    game_id = get_next_game_id()

    user_data = get_user(user_id)
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    now = datetime.now()
    game_data = {
        "type": "diamonds",
        "stage": "playing",
        "bet": bet,
        "mine_count": mine_count,
        "mine_positions": generate_diamonds_mines(mine_count),
        "revealed": set(),
        "level": 0,
        "game_over": False,
        "won": False,
        "exploded_mine": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "cashed_out": False,
        "bet_placed": False,
        "moves_count": 0,
        "created_at": now,
        "created_at_str": now.strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    await show_diamonds_grid_from_message(message, user_id, game_id)


async def show_diamonds_grid_from_message(message, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await message.answer("<i>Игра не найдена!</i>", parse_mode=ParseMode.HTML)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    user_link = f'<a href="tg://user?id={user_id}">{html.escape(message.from_user.first_name or "Игрок")}</a>'
    multiplier = get_diamonds_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "diamonds", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "diamonds", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Алмазы · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {DIAMONDS_ROWS}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Алмазы · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {DIAMONDS_ROWS}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        if level == 0:
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💠</tg-emoji>Алмазы · начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_diamonds_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            win_amount = int(bet * multiplier)
            next_multiplier = get_diamonds_multiplier(mine_count, level + 1)
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💠</tg-emoji>Алмазы · игра идёт!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji><b>Выигрыш:</b> <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{next_multiplier:.2f}x</blockquote>'
            )

    grid_buttons = []

    for row in range(0, level + 1 if not (game_over or won) else min(level + 1, DIAMONDS_ROWS)):
        row_buttons = []
        for col in range(DIAMONDS_COLS):
            idx = row * DIAMONDS_COLS + col
            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif col in mine_positions[row]:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💠",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💠",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    if row == level:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"diamonds_cell_{game_id}_{row}_{col}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"diamonds_noop_{game_id}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_diamonds_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if level > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"diamonds_cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_diamonds_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])

    msg = None
    try:
        msg = await message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


# --- CALLBACK HANDLERS: TOWER ---

@dp.callback_query(lambda c: c.data == "tower")
async def process_tower_start(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    key = get_chat_key(user_id, chat_id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass

    game_id = get_next_game_id()

    game_data = {
        "type": "tower_select",
        "stage": "select_mines",
        "bet": 10,
        "mine_count": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "moves_count": 0,
        "created_at": datetime.now(),
        "created_at_str": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'

    text = (
        f'{user_link}\n'
        '🛕<b>Башня · выбери количество мин!</b>\n'
        '<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: 10 m¢'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1️⃣", callback_data=f"tower_count_{game_id}_1"),
                InlineKeyboardButton(text="2️⃣", callback_data=f"tower_count_{game_id}_2"),
                InlineKeyboardButton(text="3️⃣", callback_data=f"tower_count_{game_id}_3"),
                InlineKeyboardButton(text="4️⃣", callback_data=f"tower_count_{game_id}_4")
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"tower_back_to_catalog_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ]
        ]
    )

    msg = await callback.message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    games[game_id]["message_id"] = msg.message_id

    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("tower_count_"))
async def process_tower_count(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-2])
        count = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "select_mines":
        await callback.answer("Выберите количество мин!", show_alert=True)
        return

    await callback.answer()

    now = datetime.now()
    game["mine_count"] = count
    game["stage"] = "playing"
    game["type"] = "tower"
    game["mine_positions"] = generate_tower_mines(count)
    game["revealed"] = set()
    game["level"] = 0
    game["game_over"] = False
    game["won"] = False
    game["exploded_mine"] = None
    game["cashed_out"] = False
    game["bet_placed"] = False
    game["moves_count"] = 0
    game["created_at"] = now
    game["created_at_str"] = now.strftime("%d-%m-%Y %H:%M:%S")
    game["settled"] = False
    game["expired"] = False
    save_active_game_to_db(game_id, game)

    key = get_chat_key(user_id, game["chat_id"])
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=game["chat_id"], message_id=callback.message.message_id)
    except Exception:
        pass

    await show_tower_grid_from_callback(callback, user_id, game_id)


@dp.callback_query(lambda c: c.data.startswith("tower_cell_"))
async def process_tower_cell_click(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 4:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-3])
        target_row = int(data[-2])
        cell_idx = int(data[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["stage"] != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    current_level = game["level"]
    if target_row != current_level:
        await callback.answer()
        return

    global_idx = current_level * TOWER_COLS + cell_idx

    if global_idx in game["revealed"]:
        await callback.answer("Эта клетка уже открыта!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        is_mine = cell_idx in game["mine_positions"][current_level]

        game["revealed"].add(global_idx)
        game["level"] += 1
        game["moves_count"] = game.get("moves_count", 0) + 1
        game["bet_placed"] = True

        if is_mine:
            game["game_over"] = True
            game["exploded_mine"] = global_idx
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_tower_grid_from_callback(callback, user_id, game_id, game_over=True)
            return

        if game["level"] >= TOWER_ROWS:
            game["game_over"] = True
            game["won"] = True
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_tower_grid_from_callback(callback, user_id, game_id, won=True)
            return

        save_active_game_to_db(game_id, game)
        await show_tower_grid_from_callback(callback, user_id, game_id)
    finally:
        game["is_processing"] = False


async def show_tower_grid_from_callback(callback, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'
    multiplier = get_tower_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "tower", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "tower", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Башня · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Башня · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        if level == 0:
            text = (
                f'{user_link}\n'
                '<b>🛕Башня · начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_tower_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            text = (
                f'{user_link}\n'
                '<b>🛕Башня · игра идёт.</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {TOWER_ROWS}\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_tower_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )

    grid_buttons = []

    display_max_row = min(level, TOWER_ROWS - 1)
    for row in range(display_max_row, -1, -1):
        row_buttons = []
        for col in range(TOWER_COLS):
            idx = row * TOWER_COLS + col
            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif col in mine_positions[row]:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"tower_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"tower_noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"tower_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    if row == level:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"tower_cell_{game_id}_{row}_{col}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"tower_noop_{game_id}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_tower_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if level > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"tower_cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_tower_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])

    msg = None
    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("tower_cashout_"))
async def process_tower_cashout(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена!", show_alert=True)
        return

    if game.get("stage") != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    if game["level"] == 0:
        await callback.answer("Откройте хотя бы одну клетку!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        game["game_over"] = True
        game["won"] = True
        game["cashed_out"] = True
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

        await show_tower_grid_from_callback(callback, user_id, game_id, won=True)
    finally:
        game["is_processing"] = False


@dp.callback_query(lambda c: c.data.startswith("exit_tower_"))
async def process_exit_tower(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[2])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game.get("bet_placed", False) or len(game.get("revealed", set())) > 0 or game.get("level", 0) > 0:
        await callback.answer("Нельзя отменить начатую игру! Нажмите «Забрать выигрыш».", show_alert=True)
        return

    key = f"exit_{user_id}_{game_id}"
    current_time = datetime.now().timestamp()

    if key in exit_confirm:
        if current_time - exit_confirm[key] < 10:
            user_data = get_user(user_id)
            update_user(user_id, balance=user_data["balance"] + game["bet"])

            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=game["message_id"])
            except Exception:
                pass

            if game_id in games:
                del games[game_id]
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            if key in exit_confirm:
                del exit_confirm[key]
            remove_active_game_from_db(game_id)

            await callback.message.edit_text(
                f'<i>✅ Игра отменена. Ставка {format_number(game["bet"])} mCoin возвращена на баланс.</i>',
                parse_mode=ParseMode.HTML
            )

            await callback.answer("Игра отменена!")
            return
        else:
            del exit_confirm[key]

    exit_confirm[key] = current_time
    await callback.answer("⚠️ Если вы хотите отменить игру, нажмите ещё раз на кнопку назад.", show_alert=True)


@dp.callback_query(lambda c: c.data.startswith("tower_back_to_catalog_"))
async def process_tower_back_to_catalog(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if game and game["owner_id"] == user_id:
        if game_id in games:
            del games[game_id]
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

    await callback.answer()

    key = get_chat_key(user_id, callback.message.chat.id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
    except Exception:
        pass

    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_catalog(callback, user_id, callback.from_user.first_name, callback.message.chat.id, is_group)


# --- CALLBACK HANDLERS: MINES ---

@dp.callback_query(lambda c: c.data.startswith("cell_"))
async def process_cell_click(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[1])
        cell_idx = int(data[2])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["stage"] != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    if cell_idx in game["revealed"]:
        await callback.answer("Эта клетка уже открыта!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        game["revealed"].add(cell_idx)
        game["level"] += 1
        game["moves_count"] = game.get("moves_count", 0) + 1
        game["bet_placed"] = True

        if cell_idx in game["mine_positions"]:
            game["game_over"] = True
            game["exploded_mine"] = cell_idx
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_mines_grid_from_callback(callback, user_id, game_id, game_over=True)
            return

        if len(game["revealed"]) == TOTAL_CELLS - game["mine_count"]:
            game["game_over"] = True
            game["won"] = True
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_mines_grid_from_callback(callback, user_id, game_id, won=True)
            return

        save_active_game_to_db(game_id, game)
        await show_mines_grid_from_callback(callback, user_id, game_id)
    finally:
        game["is_processing"] = False


async def show_mines_grid_from_callback(callback, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'
    multiplier = get_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "mines", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "mines", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Мины · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Открыто:</b> {len(revealed) - 1} из {TOTAL_CELLS - mine_count}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Мины · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Открыто:</b> {len(revealed)} из {TOTAL_CELLS - mine_count}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        multiplier = get_multiplier(mine_count, level)

        if level == 0:
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5247011187308140698">🧨</tg-emoji>Мины • начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            win_amount = int(bet * multiplier)
            next_multiplier = get_multiplier(mine_count, level + 1)
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💎</tg-emoji>Мины • игра идёт.</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji><b>Выигрыш:</b> <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{next_multiplier:.2f}x</blockquote>'
            )

    grid_buttons = []
    for row in range(GRID_SIZE):
        row_buttons = []
        for col in range(GRID_SIZE):
            idx = row * GRID_SIZE + col

            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif idx in mine_positions:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💎",
                            callback_data=f"noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"cell_{game_id}_{idx}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_game_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if len(revealed) > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        else:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"exit_game_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ])

    msg = None
    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("cashout_"))
async def process_cashout(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 2:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена!", show_alert=True)
        return

    if game.get("stage") != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    if len(game["revealed"]) == 0:
        await callback.answer("Откройте хотя бы одну клетку!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        game["game_over"] = True
        game["won"] = True
        game["cashed_out"] = True
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

        await show_mines_grid_from_callback(callback, user_id, game_id, won=True)
    finally:
        game["is_processing"] = False


@dp.callback_query(lambda c: c.data.startswith("exit_game_"))
async def process_exit_game(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[2])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game.get("bet_placed", False) or len(game.get("revealed", set())) > 0 or game.get("level", 0) > 0:
        await callback.answer("Нельзя отменить начатую игру! Нажмите «Забрать выигрыш».", show_alert=True)
        return

    key = f"exit_{user_id}_{game_id}"
    current_time = datetime.now().timestamp()

    if key in exit_confirm:
        if current_time - exit_confirm[key] < 10:
            user_data = get_user(user_id)
            update_user(user_id, balance=user_data["balance"] + game["bet"])

            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=game["message_id"])
            except Exception:
                pass

            if game_id in games:
                del games[game_id]
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            if key in exit_confirm:
                del exit_confirm[key]
            remove_active_game_from_db(game_id)

            await callback.message.edit_text(
                f'<i>✅ Игра отменена. Ставка {format_number(game["bet"])} mCoin возвращена на баланс.</i>',
                parse_mode=ParseMode.HTML
            )

            await callback.answer("Игра отменена!")
            return
        else:
            del exit_confirm[key]

    exit_confirm[key] = current_time
    await callback.answer("⚠️ Если вы хотите отменить игру, нажмите ещё раз на кнопку назад.", show_alert=True)


@dp.callback_query(lambda c: c.data.startswith("mines_count_"))
async def process_mines_count(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-2])
        count = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "select_mines":
        await callback.answer("Выберите количество мин!", show_alert=True)
        return

    await callback.answer()

    now = datetime.now()
    game["mine_count"] = count
    game["stage"] = "playing"
    game["type"] = "mines"
    game["mine_positions"] = generate_mines(count)
    game["revealed"] = set()
    game["level"] = 0
    game["game_over"] = False
    game["won"] = False
    game["exploded_mine"] = None
    game["cashed_out"] = False
    game["bet_placed"] = False
    game["moves_count"] = 0
    game["created_at"] = now
    game["created_at_str"] = now.strftime("%d-%m-%Y %H:%M:%S")
    game["settled"] = False
    game["expired"] = False
    save_active_game_to_db(game_id, game)

    key = get_chat_key(user_id, game["chat_id"])
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=game["chat_id"], message_id=callback.message.message_id)
    except Exception:
        pass

    await show_mines_grid_from_callback(callback, user_id, game_id)


@dp.callback_query(lambda c: c.data.startswith("back_to_catalog_"))
async def process_back_to_catalog_from_select(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if game and game["owner_id"] == user_id:
        if game_id in games:
            del games[game_id]
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

    await callback.answer()

    key = get_chat_key(user_id, callback.message.chat.id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
    except Exception:
        pass

    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_catalog(callback, user_id, callback.from_user.first_name, callback.message.chat.id, is_group)


@dp.callback_query(lambda c: c.data == "mines")
async def process_mines_start(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    key = get_chat_key(user_id, chat_id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass

    game_id = get_next_game_id()

    game_data = {
        "type": "mines_select",
        "stage": "select_mines",
        "bet": 10,
        "mine_count": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "moves_count": 0,
        "created_at": datetime.now(),
        "created_at_str": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'

    text = (
        f'{user_link}\n'
        '<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji><b>Мины • выбери мины!</b>\n'
        '<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: 10 m¢'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1️⃣", callback_data=f"mines_count_{game_id}_1"),
                InlineKeyboardButton(text="2️⃣", callback_data=f"mines_count_{game_id}_2"),
                InlineKeyboardButton(text="3️⃣", callback_data=f"mines_count_{game_id}_3")
            ],
            [
                InlineKeyboardButton(text="4️⃣", callback_data=f"mines_count_{game_id}_4"),
                InlineKeyboardButton(text="5️⃣", callback_data=f"mines_count_{game_id}_5"),
                InlineKeyboardButton(text="6️⃣", callback_data=f"mines_count_{game_id}_6")
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"back_to_catalog_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ]
        ]
    )

    msg = await callback.message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    games[game_id]["message_id"] = msg.message_id

    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


# --- CALLBACK HANDLERS: DIAMONDS ---

@dp.callback_query(lambda c: c.data == "diamonds")
async def process_diamonds_start(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    key = get_chat_key(user_id, chat_id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass

    game_id = get_next_game_id()

    game_data = {
        "type": "diamonds_select",
        "stage": "select_mines",
        "bet": 10,
        "mine_count": None,
        "owner_id": user_id,
        "chat_id": chat_id,
        "moves_count": 0,
        "created_at": datetime.now(),
        "created_at_str": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "is_edited": False
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'

    text = (
        f'{user_link}\n'
        '<tg-emoji emoji-id="5307594157739515229">💠</tg-emoji> <b>Алмазы · выбери количество мин!</b>\n'
        '<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: 10 m¢'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1️⃣", callback_data=f"diamonds_count_{game_id}_1"),
                InlineKeyboardButton(text="2️⃣", callback_data=f"diamonds_count_{game_id}_2")
            ],
            [
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"diamonds_back_to_catalog_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ]
        ]
    )

    msg = await callback.message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

    games[game_id]["message_id"] = msg.message_id

    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("diamonds_count_"))
async def process_diamonds_count(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-2])
        count = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "select_mines":
        await callback.answer("Выберите количество мин!", show_alert=True)
        return

    await callback.answer()

    now = datetime.now()
    game["mine_count"] = count
    game["stage"] = "playing"
    game["type"] = "diamonds"
    game["mine_positions"] = generate_diamonds_mines(count)
    game["revealed"] = set()
    game["level"] = 0
    game["game_over"] = False
    game["won"] = False
    game["exploded_mine"] = None
    game["cashed_out"] = False
    game["bet_placed"] = False
    game["moves_count"] = 0
    game["created_at"] = now
    game["created_at_str"] = now.strftime("%d-%m-%Y %H:%M:%S")
    game["settled"] = False
    game["expired"] = False
    save_active_game_to_db(game_id, game)

    key = get_chat_key(user_id, game["chat_id"])
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=game["chat_id"], message_id=callback.message.message_id)
    except Exception:
        pass

    await show_diamonds_grid_from_callback(callback, user_id, game_id)


@dp.callback_query(lambda c: c.data.startswith("diamonds_cell_"))
async def process_diamonds_cell_click(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 4:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-3])
        target_row = int(data[-2])
        cell_idx = int(data[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game["stage"] != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    current_level = game["level"]
    if target_row != current_level:
        await callback.answer()
        return

    global_idx = current_level * DIAMONDS_COLS + cell_idx

    if global_idx in game["revealed"]:
        await callback.answer("Эта клетка уже открыта!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        is_mine = cell_idx in game["mine_positions"][current_level]

        game["revealed"].add(global_idx)
        game["level"] += 1
        game["moves_count"] = game.get("moves_count", 0) + 1
        game["bet_placed"] = True

        if is_mine:
            game["game_over"] = True
            game["exploded_mine"] = global_idx
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_diamonds_grid_from_callback(callback, user_id, game_id, game_over=True)
            return

        if game["level"] >= DIAMONDS_ROWS:
            game["game_over"] = True
            game["won"] = True
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            remove_active_game_from_db(game_id)
            await show_diamonds_grid_from_callback(callback, user_id, game_id, won=True)
            return

        save_active_game_to_db(game_id, game)
        await show_diamonds_grid_from_callback(callback, user_id, game_id)
    finally:
        game["is_processing"] = False


async def show_diamonds_grid_from_callback(callback, user_id, game_id, game_over=False, won=False):
    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        return

    mine_count = game["mine_count"]
    bet = game["bet"]
    revealed = game["revealed"]
    mine_positions = game["mine_positions"]
    level = game["level"]
    exploded_mine = game.get("exploded_mine")

    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'
    multiplier = get_diamonds_multiplier(mine_count, level)
    win_amount = int(bet * multiplier)

    if (game_over or won) and not game.get("settled", False):
        game["settled"] = True
        if game_over and exploded_mine is not None:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
            add_game_history(user_id, "diamonds", bet, "lose", 0, game.get("created_at_str"))
        elif won:
            user_data = get_user(user_id)
            update_user(user_id, games=user_data["games"] + 1, balance=user_data["balance"] + win_amount)
            add_game_history(user_id, "diamonds", bet, "win", win_amount, game.get("created_at_str"))

    if game_over and exploded_mine is not None:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5469785308386041323">💥</tg-emoji> <b>Алмазы · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {DIAMONDS_ROWS}\n\n'
            f'<blockquote><i><tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>Мог забрать: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢</i></blockquote>'
        )
    elif won:
        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji><b>Алмазы · Победа!</b> <tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
            f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
            f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> <b>Уровень:</b> {level} из {DIAMONDS_ROWS}\n\n'
            f'<tg-emoji emoji-id="5472212780952066876">🤑</tg-emoji> Выигрыш: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢'
        )
    else:
        if level == 0:
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💠</tg-emoji>Алмазы · начни игру!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{get_diamonds_multiplier(mine_count, level + 1):.2f}x</blockquote>'
            )
        else:
            win_amount = int(bet * multiplier)
            next_multiplier = get_diamonds_multiplier(mine_count, level + 1)
            text = (
                f'{user_link}\n'
                '<b><tg-emoji emoji-id="5307594157739515229">💠</tg-emoji>Алмазы · игра идёт!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5469654973308476699">💣</tg-emoji> <b>Мин:</b> {mine_count}\n'
                f'<tg-emoji emoji-id="5881948563591666817">💸</tg-emoji><b>Ставка</b>: {format_number(bet)} m¢\n'
                f'<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji><b>Выигрыш:</b> <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{multiplier:.2f}x / {format_number(win_amount)} m¢\n\n'
                f'<blockquote>Следующий множитель: <tg-emoji emoji-id="5836936408681421518">❌</tg-emoji>{next_multiplier:.2f}x</blockquote>'
            )

    grid_buttons = []

    for row in range(0, level + 1 if not (game_over or won) else min(level + 1, DIAMONDS_ROWS)):
        row_buttons = []
        for col in range(DIAMONDS_COLS):
            idx = row * DIAMONDS_COLS + col
            if game_over or won:
                if idx == exploded_mine:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5276032951342088188"
                        )
                    )
                elif col in mine_positions[row]:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="danger",
                            icon_custom_emoji_id="5469654973308476699"
                        )
                    )
                elif idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💠",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text=" ",
                            callback_data=f"diamonds_noop_{game_id}",
                            icon_custom_emoji_id="5436113877181941026"
                        )
                    )
            else:
                if idx in revealed:
                    row_buttons.append(
                        InlineKeyboardButton(
                            text="💠",
                            callback_data=f"diamonds_noop_{game_id}",
                            style="success"
                        )
                    )
                else:
                    if row == level:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"diamonds_cell_{game_id}_{row}_{col}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
                    else:
                        row_buttons.append(
                            InlineKeyboardButton(
                                text=" ",
                                callback_data=f"diamonds_noop_{game_id}",
                                icon_custom_emoji_id="5436113877181941026"
                            )
                        )
        grid_buttons.append(row_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=grid_buttons)

    if game_over or won:
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_diamonds_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])
    else:
        if level > 0:
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text="Забрать выигрыш",
                    callback_data=f"diamonds_cashout_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5427009714745517609"
                )
            ])
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(
                text="Назад",
                callback_data=f"exit_diamonds_{game_id}",
                icon_custom_emoji_id="5255703720078879038"
            )
        ])

    msg = None
    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = True
    except Exception:
        msg = await callback.message.answer(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
        game["is_edited"] = False

    game["message_id"] = msg.message_id

    key = get_chat_key(user_id, game["chat_id"])
    if key not in game_messages:
        game_messages[key] = []
    game_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("diamonds_cashout_"))
async def process_diamonds_cashout(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена!", show_alert=True)
        return

    if game.get("stage") != "playing":
        await callback.answer("Игра неактивна!", show_alert=True)
        return

    if game["level"] == 0:
        await callback.answer("Откройте хотя бы одну клетку!", show_alert=True)
        return

    if game.get("is_processing", False):
        await callback.answer()
        return

    game["is_processing"] = True

    try:
        await callback.answer()

        game["game_over"] = True
        game["won"] = True
        game["cashed_out"] = True
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

        await show_diamonds_grid_from_callback(callback, user_id, game_id, won=True)
    finally:
        game["is_processing"] = False


@dp.callback_query(lambda c: c.data.startswith("exit_diamonds_"))
async def process_exit_diamonds(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[2])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("game_over", False) or game.get("won", False) or game.get("cashed_out", False):
        await callback.answer("Игра уже завершена.", show_alert=True)
        return

    if game.get("bet_placed", False) or len(game.get("revealed", set())) > 0 or game.get("level", 0) > 0:
        await callback.answer("Нельзя отменить начатую игру! Нажмите «Забрать выигрыш».", show_alert=True)
        return

    key = f"exit_{user_id}_{game_id}"
    current_time = datetime.now().timestamp()

    if key in exit_confirm:
        if current_time - exit_confirm[key] < 10:
            user_data = get_user(user_id)
            update_user(user_id, balance=user_data["balance"] + game["bet"])

            try:
                await callback.bot.delete_message(chat_id=game["chat_id"], message_id=game["message_id"])
            except Exception:
                pass

            if game_id in games:
                del games[game_id]
            if user_id in active_games and active_games.get(user_id) == game_id:
                del active_games[user_id]
            if key in exit_confirm:
                del exit_confirm[key]
            remove_active_game_from_db(game_id)

            await callback.message.edit_text(
                f'<i>✅ Игра отменена. Ставка {format_number(game["bet"])} mCoin возвращена на баланс.</i>',
                parse_mode=ParseMode.HTML
            )

            await callback.answer("Игра отменена!")
            return
        else:
            del exit_confirm[key]

    exit_confirm[key] = current_time
    await callback.answer("⚠️ Если вы хотите отменить игру, нажмите ещё раз на кнопку назад.", show_alert=True)


@dp.callback_query(lambda c: c.data.startswith("diamonds_back_to_catalog_"))
async def process_diamonds_back_to_catalog(callback: types.CallbackQuery):
    data = callback.data.split("_")

    if len(data) < 3:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    try:
        game_id = int(data[-1])
    except ValueError:
        await callback.answer("Ошибка! Попробуйте заново.", show_alert=True)
        return

    user_id = callback.from_user.id

    game = games.get(game_id)
    if game and game["owner_id"] == user_id:
        if game_id in games:
            del games[game_id]
        if user_id in active_games and active_games.get(user_id) == game_id:
            del active_games[user_id]
        remove_active_game_from_db(game_id)

    await callback.answer()

    key = get_chat_key(user_id, callback.message.chat.id)
    if key in game_messages:
        for msg_id in game_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
            except Exception:
                pass
        game_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
    except Exception:
        pass

    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_catalog(callback, user_id, callback.from_user.first_name, callback.message.chat.id, is_group)


# --- 21 (ОЧКО) GAME EXECUTION & CALLBACKS ---

RANKS_21 = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
SUITS_21 = ['♠️', '♥️', '♦️', '♣️']


def create_deck_21():
    deck = [(r, s) for r in RANKS_21 for s in SUITS_21]
    secrets.SystemRandom().shuffle(deck)
    return deck


def calculate_21_score(cards):
    if not cards:
        return 0
    if len(cards) == 2 and cards[0][0] == 'A' and cards[1][0] == 'A':
        return 21
    total = 0
    aces = 0
    for rank, _ in cards:
        if rank in ['2', '3', '4', '5', '6', '7', '8', '9', '10']:
            total += int(rank)
        elif rank == 'J':
            total += 2
        elif rank == 'Q':
            total += 3
        elif rank == 'K':
            total += 4
        elif rank == 'A':
            total += 11
            aces += 1
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total


def format_21_cards(cards):
    cards_str = " • ".join(f"{r}{s}" for r, s in cards)
    score = calculate_21_score(cards)
    return f"{cards_str} | {score}"


async def start_twentyone_game_from_command(message, user_id, chat_id, bet):
    key = get_chat_key(user_id, chat_id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    if user_id in active_games:
        old_game_id = active_games[user_id]
        old_game = games.get(old_game_id)
        if old_game and not old_game.get("settled", False) and not old_game.get("game_over", False):
            if old_game.get("stage") == "ready":
                cur_u = get_user(user_id)
                update_user(user_id, balance=cur_u["balance"] + old_game.get("bet", 0))
            remove_active_game_from_db(old_game_id)
            if old_game_id in games:
                del games[old_game_id]

    user_data = get_user(user_id)
    if not update_user(user_id, balance=user_data["balance"] - bet):
        await message.answer("<i>Ошибка списания средств!</i>", parse_mode=ParseMode.HTML)
        return

    game_id = get_next_game_id()
    now = datetime.now()
    user_first_name = message.from_user.first_name or "Игрок"

    game_data = {
        "type": "twentyone",
        "stage": "ready",
        "bet": bet,
        "deck": create_deck_21(),
        "player_cards": [],
        "dealer_cards": [],
        "owner_id": user_id,
        "chat_id": chat_id,
        "game_over": False,
        "won": False,
        "created_at": now,
        "created_at_str": now.strftime("%d-%m-%Y %H:%M:%S"),
        "settled": False,
        "expired": False,
        "message_id": None,
        "user_first_name": user_first_name
    }

    games[game_id] = game_data
    active_games[user_id] = game_id
    save_active_game_to_db(game_id, game_data)

    user_link = get_user_mention(user_id, user_first_name)

    text = (
        f'{user_link}\n<tg-emoji emoji-id="5395325195542078574">🍀</tg-emoji>21 · начни игру!\n'
        '<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji><b>Ставка:</b> {format_number(bet)} m¢'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Запустить",
                    callback_data=f"to_start_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5809949600152296075"
                ),
                InlineKeyboardButton(
                    text="Назад",
                    callback_data=f"to_cancel_{game_id}",
                    icon_custom_emoji_id="5255703720078879038"
                )
            ]
        ]
    )

    msg = await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )
    game_data["message_id"] = msg.message_id
    if key not in user_messages:
        user_messages[key] = []
    user_messages[key].append(msg.message_id)


async def show_twentyone_from_callback(callback: types.CallbackQuery, user_id: int, game_id: int):
    game = games.get(game_id)
    if not game:
        await callback.answer("Игра не найдена!", show_alert=True)
        return

    if game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    bet = game["bet"]
    user_first_name = game.get("user_first_name") or callback.from_user.first_name or "Игрок"
    user_link = get_user_mention(user_id, user_first_name)
    stage = game.get("stage", "ready")

    if stage == "ready":
        text = (
            f'{user_link}\n<tg-emoji emoji-id="5395325195542078574">🍀</tg-emoji>21 · начни игру!\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji><b>Ставка:</b> {format_number(bet)} m¢'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Запустить",
                        callback_data=f"to_start_{game_id}",
                        style="success",
                        icon_custom_emoji_id="5809949600152296075"
                    ),
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data=f"to_cancel_{game_id}",
                        icon_custom_emoji_id="5255703720078879038"
                    )
                ]
            ]
        )
    else:
        dealer_cards = game.get("dealer_cards", [])
        player_cards = game.get("player_cards", [])
        dealer_str = format_21_cards(dealer_cards)
        player_str = format_21_cards(player_cards)
        text = (
            f'{user_link}\n'
            '♠️<b> 21 · игра идёт.</b> <code>·····················</code>\n'
            f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji> Ставка: {format_number(bet)} m¢\n\n'
            '🤵♂ Дилер: \n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты: \n'
            f'<blockquote>{player_str}</blockquote>'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="СТОП",
                        callback_data=f"to_stand_{game_id}",
                        style="danger",
                        icon_custom_emoji_id="5445350865776941647"
                    ),
                    InlineKeyboardButton(
                        text="ЕЩЁ",
                        callback_data=f"to_hit_{game_id}",
                        style="success",
                        icon_custom_emoji_id="5809949600152296075"
                    )
                ]
            ]
        )

    msg = None
    try:
        msg = await callback.message.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        try:
            msg = await callback.message.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            pass

    if msg:
        game["message_id"] = msg.message_id


async def update_twentyone_game_view(message_obj, user_id, game_id, user_first_name="Игрок"):
    game = games.get(game_id)
    if not game:
        return

    bet = game["bet"]
    user_link = get_user_mention(user_id, game.get("user_first_name") or user_first_name)

    dealer_cards = game["dealer_cards"]
    player_cards = game["player_cards"]

    dealer_str = format_21_cards(dealer_cards)
    player_str = format_21_cards(player_cards)

    text = (
        f'{user_link}\n'
        '♠️<b> 21 · игра идёт.</b> <code>·····················</code>\n'
        f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji> Ставка: {format_number(bet)} m¢\n\n'
        '🤵♂ Дилер: \n'
        f'<blockquote>{dealer_str}</blockquote>\n'
        '<code>············</code>\n'
        '🫵 Ты: \n'
        f'<blockquote>{player_str}</blockquote>'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="СТОП",
                    callback_data=f"to_stand_{game_id}",
                    style="danger",
                    icon_custom_emoji_id="5445350865776941647"
                ),
                InlineKeyboardButton(
                    text="ЕЩЁ",
                    callback_data=f"to_hit_{game_id}",
                    style="success",
                    icon_custom_emoji_id="5809949600152296075"
                )
            ]
        ]
    )

    msg = None
    try:
        msg = await message_obj.edit_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        try:
            msg = await message_obj.answer(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception:
            pass

    if msg:
        game["message_id"] = msg.message_id


@dp.callback_query(lambda c: c.data and c.data.startswith("to_start_"))
async def process_twentyone_start(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    game = games.get(game_id)
    user_id = callback.from_user.id
    if not game or game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "ready":
        await callback.answer()
        return

    game["stage"] = "playing"
    if callback.from_user.first_name:
        game["user_first_name"] = callback.from_user.first_name
    deck = game["deck"]
    game["player_cards"] = [deck.pop(), deck.pop()]
    game["dealer_cards"] = [deck.pop(), deck.pop()]

    dealer_score = calculate_21_score(game["dealer_cards"])
    player_score = calculate_21_score(game["player_cards"])
    bet = game["bet"]
    user_data = get_user(user_id)

    # If dealer has 21 immediately on deal -> automatic loss (or tie if player also has 21)
    if dealer_score == 21:
        game["settled"] = True
        game["game_over"] = True
        await callback.answer()

        if active_games.get(user_id) == game_id:
            del active_games[user_id]
        if game_id in games:
            del games[game_id]
        remove_active_game_from_db(game_id)

        user_link = get_user_mention(user_id, game.get("user_first_name") or callback.from_user.first_name)
        dealer_str = format_21_cards(game["dealer_cards"])
        player_str = format_21_cards(game["player_cards"])

        if player_score == 21:
            # Both have 21 -> Tie
            update_user(user_id, balance=user_data["balance"] + bet, games=user_data["games"] + 1)
            add_game_history(user_id, "twentyone", bet, "draw", bet, game.get("created_at_str"))

            text = (
                f'{user_link}\n'
                '🤝 <b>21 · Ничья!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>  Ставка: {format_number(bet)} m¢\n\n'
                '🤵♂ Дилер:\n'
                f'<blockquote>{dealer_str}</blockquote>\n'
                '<code>············</code>\n'
                '🫵 Ты:\n'
                f'<blockquote>{player_str}</blockquote>\n\n'
                '<i>🤝 Ничья! У обоих 21. Ставка возвращена на баланс.</i>'
            )
        else:
            # Dealer has 21 -> Automatic loss
            update_user(user_id, lost=user_data["lost"] + bet, games=user_data["games"] + 1)
            add_game_history(user_id, "twentyone", bet, "lose", 0, game.get("created_at_str"))

            text = (
                f'{user_link}\n'
                '<tg-emoji emoji-id="5213090867044162716">😭</tg-emoji> <b>21 · Проигрыш!</b>\n'
                '<code>·····················</code>\n'
                f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>  Ставка: {format_number(bet)} m¢\n\n'
                '🤵♂ Дилер:\n'
                f'<blockquote>{dealer_str}</blockquote>\n'
                '<code>············</code>\n'
                '🫵 Ты:\n'
                f'<blockquote>{player_str}</blockquote>\n\n'
                f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji> <i>Не повезло! У дилера 21.</i>'
            )

        try:
            await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass
        return

    save_active_game_to_db(game_id, game)

    await callback.answer()
    await update_twentyone_game_view(callback.message, user_id, game_id, game.get("user_first_name"))


@dp.callback_query(lambda c: c.data and c.data.startswith("to_hit_"))
async def process_twentyone_hit(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    game = games.get(game_id)
    user_id = callback.from_user.id
    if not game or game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "playing" or game.get("settled", False):
        await callback.answer()
        return

    if not game["deck"]:
        game["deck"] = create_deck_21()
    new_card = game["deck"].pop()
    game["player_cards"].append(new_card)

    player_score = calculate_21_score(game["player_cards"])
    await callback.answer()

    if player_score > 21:
        # Bust -> Player loses
        game["settled"] = True
        game["game_over"] = True
        game["won"] = False
        bet = game["bet"]
        user_data = get_user(user_id)
        update_user(user_id, games=user_data["games"] + 1, lost=user_data["lost"] + bet)
        add_game_history(user_id, "twentyone", bet, "lose", 0, game.get("created_at_str"))

        if active_games.get(user_id) == game_id:
            del active_games[user_id]
        if game_id in games:
            del games[game_id]
        remove_active_game_from_db(game_id)

        user_link = get_user_mention(user_id, game.get("user_first_name") or callback.from_user.first_name)
        dealer_str = format_21_cards(game["dealer_cards"])
        player_str = format_21_cards(game["player_cards"])

        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5213090867044162716">😭</tg-emoji> <b>21 · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>  Ставка: {format_number(bet)} m¢\n\n'
            '🤵♂ Дилер:\n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты:\n'
            f'<blockquote>{player_str}</blockquote>\n\n'
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji> <i>Не повезло! У вас перебор ({player_score}).</i>'
        )

        try:
            await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception:
            pass
    else:
        save_active_game_to_db(game_id, game)
        await update_twentyone_game_view(callback.message, user_id, game_id, game.get("user_first_name"))


@dp.callback_query(lambda c: c.data and c.data.startswith("to_stand_"))
async def process_twentyone_stand(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    game = games.get(game_id)
    user_id = callback.from_user.id
    if not game or game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") != "playing" or game.get("settled", False):
        await callback.answer()
        return

    game["settled"] = True
    game["game_over"] = True
    await callback.answer()

    dealer_cards = game["dealer_cards"]
    deck = game["deck"]
    while calculate_21_score(dealer_cards) < 17:
        if not deck:
            deck = create_deck_21()
        dealer_cards.append(deck.pop())

    dealer_score = calculate_21_score(dealer_cards)
    player_score = calculate_21_score(game["player_cards"])
    bet = game["bet"]
    user_data = get_user(user_id)

    user_link = get_user_mention(user_id, game.get("user_first_name") or callback.from_user.first_name)
    dealer_str = format_21_cards(dealer_cards)
    player_str = format_21_cards(game["player_cards"])

    if active_games.get(user_id) == game_id:
        del active_games[user_id]
    if game_id in games:
        del games[game_id]
    remove_active_game_from_db(game_id)

    if dealer_score > 21:
        # Dealer busts -> Player wins
        win_amount = min(int(bet * 1.97), 1000000000)
        update_user(user_id, balance=user_data["balance"] + win_amount, games=user_data["games"] + 1)
        add_game_history(user_id, "twentyone", bet, "win", win_amount, game.get("created_at_str"))

        text = (
            f'{user_link}\n'
            '🤯 <b>21 · Победа! ✅</b>\n'
            '<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'💰 <b>Выигрыш: </b>x1,97 / {format_number(win_amount)} m¢\n\n'
            '🤵♂ Дилер:\n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты:\n'
            f'<blockquote>{player_str}</blockquote>\n\n'
            '<i>🎉 Ты победил! У дилера перебор.</i>'
        )
    elif player_score > dealer_score:
        # Player wins by higher score
        win_amount = min(int(bet * 1.97), 1000000000)
        update_user(user_id, balance=user_data["balance"] + win_amount, games=user_data["games"] + 1)
        add_game_history(user_id, "twentyone", bet, "win", win_amount, game.get("created_at_str"))

        text = (
            f'{user_link}\n'
            '🤯 <b>21 · Победа! ✅</b>\n'
            '<code>·····················</code>\n'
            f'💸 <b>Ставка:</b> {format_number(bet)} m¢\n'
            f'💰 <b>Выигрыш: </b>x1,97 / {format_number(win_amount)} m¢\n\n'
            '🤵♂ Дилер:\n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты:\n'
            f'<blockquote>{player_str}</blockquote>\n\n'
            f'<i>🎉 Ты победил! У тебя {player_score} против {dealer_score} у дилера.</i>'
        )
    elif player_score == dealer_score:
        # Push (Tie)
        update_user(user_id, balance=user_data["balance"] + bet, games=user_data["games"] + 1)
        add_game_history(user_id, "twentyone", bet, "draw", bet, game.get("created_at_str"))

        text = (
            f'{user_link}\n'
            '🤝 <b>21 · Ничья!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>  Ставка: {format_number(bet)} m¢\n\n'
            '🤵♂ Дилер:\n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты:\n'
            f'<blockquote>{player_str}</blockquote>\n\n'
            '<i>🤝 Ничья! Ставка возвращена на баланс.</i>'
        )
    else:
        # Dealer higher score -> Player loses
        update_user(user_id, lost=user_data["lost"] + bet, games=user_data["games"] + 1)
        add_game_history(user_id, "twentyone", bet, "lose", 0, game.get("created_at_str"))

        text = (
            f'{user_link}\n'
            '<tg-emoji emoji-id="5213090867044162716">😭</tg-emoji> <b>21 · Проигрыш!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5224257782013769471">💰</tg-emoji>  Ставка: {format_number(bet)} m¢\n\n'
            '🤵♂ Дилер:\n'
            f'<blockquote>{dealer_str}</blockquote>\n'
            '<code>············</code>\n'
            '🫵 Ты:\n'
            f'<blockquote>{player_str}</blockquote>\n\n'
            f'<tg-emoji emoji-id="5472255352667904566">😔</tg-emoji> <i>Не повезло! У дилера {dealer_score}.</i>'
        )

    try:
        await callback.message.edit_text(text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("to_cancel_"))
async def process_twentyone_cancel(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    game = games.get(game_id)
    user_id = callback.from_user.id
    if not game or game["owner_id"] != user_id:
        await callback.answer("Это не ваша игра!", show_alert=True)
        return

    if game.get("stage") == "ready" and not game.get("settled", False):
        game["settled"] = True
        game["game_over"] = True
        user_data = get_user(user_id)
        update_user(user_id, balance=user_data["balance"] + game["bet"])
        if active_games.get(user_id) == game_id:
            del active_games[user_id]
        if game_id in games:
            del games[game_id]
        remove_active_game_from_db(game_id)
        await callback.answer("Игра отменена. Ставка возвращена.")
        try:
            await callback.message.edit_text("<i>Игра отменена. Ставка возвращена на баланс.</i>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    else:
        await callback.answer("Игру нельзя отменить во время раунда!", show_alert=True)


# --- GENERAL PLAY / BACK & NOOP HANDLERS ---

@dp.callback_query(lambda c: c.data == "play")
async def process_play(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id

    key = get_chat_key(user_id, callback.message.chat.id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
    except Exception:
        pass

    is_group = callback.message.chat.type in ["group", "supergroup"]
    await show_catalog(callback, user_id, callback.from_user.first_name, callback.message.chat.id, is_group)


@dp.callback_query(lambda c: c.data == "back")
async def process_back(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    key = get_chat_key(user_id, chat_id)
    if key in user_messages:
        for msg_id in user_messages[key]:
            try:
                await callback.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
        user_messages[key] = []

    try:
        await callback.bot.delete_message(chat_id=chat_id, message_id=callback.message.message_id)
    except Exception:
        pass

    text = (
        '<b>Привет! <tg-emoji emoji-id="5350612670435313545">👋</tg-emoji> Ты в Мины Бот — место, где время летит незаметно!</b>\n\n'
        '🎮 Много бесплатных игр без скачивания, прямо в Telegram.\n\n'
        'Соревнуйся с друзьями и прокачивай свои каналы и чаты. 🏆'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Играть",
                    callback_data="play",
                    style="success",
                    icon_custom_emoji_id="5350612670435313545"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Добавить бота в чат",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=start",
                    style="primary",
                    icon_custom_emoji_id="5393194986252542669"
                )
            ]
        ]
    )

    photo_url = "https://iili.io/CPpNkSR.md.png"
    msg = await callback.message.answer_photo(
        photo=photo_url,
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    if key not in user_messages:
        user_messages[key] = []
    user_messages[key].append(msg.message_id)


@dp.callback_query(lambda c: c.data.startswith("noop_") or c.data.startswith("tower_noop_") or c.data.startswith("diamonds_noop_"))
async def process_noop(callback: types.CallbackQuery):
    try:
        game_id = int(callback.data.split("_")[-1])
        game = games.get(game_id)
        if game and not game.get("game_over", False) and not game.get("settled", False) and not game.get("expired", False):
            await callback.answer()
            return
    except Exception:
        pass
    await callback.answer("Игра завершена.")


@dp.callback_query(lambda c: c.data == "check_bonus")
async def process_check_bonus(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    user_data = get_user(user_id)

    if not user_data:
        await callback.answer("Ошибка получения данных!", show_alert=True)
        return

    try:
        user_info = await bot.get_chat(user_id)
        description = user_info.bio or ""
    except Exception:
        description = ""

    if f"@{BOT_USERNAME}" not in description and BOT_USERNAME not in description:
        await callback.answer("Не все условия выполнены.", show_alert=True)
        return

    if user_data["bonus_time"]:
        try:
            last_bonus = datetime.fromisoformat(user_data["bonus_time"])
            if datetime.now() - last_bonus < timedelta(hours=6):
                time_left = timedelta(hours=6) - (datetime.now() - last_bonus)
                hours = int(time_left.total_seconds() // 3600)
                minutes = int((time_left.total_seconds() % 3600) // 60)
                await callback.answer(f"Бонус можно получить только раз в 6 часов. Осталось: {hours}ч {minutes}м", show_alert=True)
                return
        except Exception:
            pass

    bonus = random.randint(70000, 100000)
    if update_user(user_id, balance=user_data["balance"] + bonus, bonus_time=datetime.now().isoformat()):
        try:
            asyncio.create_task(activate_referral_if_needed(user_id))
        except Exception:
            pass
        await callback.message.edit_text(
            f'<i>🎉 Поздравляем! Вы получили бонус {format_number(bonus)} mCoin!\n'
            f'Текущий баланс: {format_number(user_data["balance"] + bonus)} mCoin</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await callback.answer("Ошибка при начислении бонуса!", show_alert=True)


@dp.callback_query(lambda c: c.data == "bonus_tutorial")
async def process_bonus_tutorial(callback: types.CallbackQuery):
    await callback.answer()

    user_id = callback.from_user.id
    clean_user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{clean_user_name}</a>'

    text = (
        f'{user_link}, бонус получить достаточно просто, нужно просто добавить юзернейм бота - <code>@{BOT_USERNAME}</code> в описание профиля.'
    )

    photo_url = "https://iili.io/CiK2iKJ.md.png"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Продолжить",
                    callback_data="bonus_continue"
                )
            ]
        ]
    )

    await callback.message.delete()

    await callback.message.answer_photo(
        photo=photo_url,
        caption=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


@dp.callback_query(lambda c: c.data == "bonus_continue")
async def process_bonus_continue(callback: types.CallbackQuery):
    await callback.answer()

    photo_url = "https://iili.io/CidJB9e.md.jpg"

    await callback.message.delete()

    await callback.message.answer_photo(
        photo=photo_url,
        caption="Выполните так как на фото",
        parse_mode=ParseMode.HTML
    )


# --- TRANSFERS ---

@dp.message(lambda message: message.text and (
    message.text.lower().startswith("дать") or 
    message.text.lower().startswith("пер")
))
async def cmd_transfer(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)

    if is_transfer_banned(user_id):
        await message.reply("<i>Вам заблокированы переводы! 🚫</i>", parse_mode=ParseMode.HTML)
        return

    text = message.text.strip()

    parts = text.split()
    if len(parts) < 2 and not message.reply_to_message:
        await message.reply(
            "<i>Укажите сумму и получателя!\n"
            "Пример: ответом на сообщение <code>дать 10кк</code> или <code>дать 10кк @username</code></i>",
            parse_mode=ParseMode.HTML
        )
        return

    cmd = parts[0].lower()
    if cmd not in ["дать", "пер"]:
        return

    args_parts = parts[1:]

    target_user_id = None
    target_name = None
    amount = None
    comment = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Игрок"
        if args_parts:
            amt = resolve_bet_amount(args_parts[0], user_data["balance"])
            if amt is not None:
                amount = amt
                if len(args_parts) > 1:
                    comment = " ".join(args_parts[1:]).strip()
            else:
                amount = parse_amount(" ".join(args_parts))
    else:
        username_part = None
        amount_tokens = []

        for part in args_parts:
            if part.startswith("@") and not username_part:
                username_part = part.lstrip("@")
            else:
                amount_tokens.append(part)

        if username_part:
            found = get_user_by_username(username_part)
            if not found:
                await message.reply(
                    f"<i>Игрок @{username_part} не найден в базе бота. Он должен хотя бы раз написать боту.</i>",
                    parse_mode=ParseMode.HTML
                )
                return
            target_user_id = found["user_id"]
            target_name = f"@{username_part}"

            if amount_tokens:
                amt = resolve_bet_amount(amount_tokens[0], user_data["balance"])
                if amt is not None:
                    amount = amt
                    if len(amount_tokens) > 1:
                        comment = " ".join(amount_tokens[1:]).strip()
                else:
                    amount = parse_amount(" ".join(amount_tokens))
        else:
            await message.reply(
                "<i>Укажите получателя!\n"
                "Напишите в ответ на сообщение игрока: <code>дать 10кк</code> или укажите юзернейм: <code>дать 10кк @username</code></i>",
                parse_mode=ParseMode.HTML
            )
            return

    if amount is None or amount <= 0:
        await message.reply("<i>Неверная сумма! Пример: <code>дать 10кк</code></i>", parse_mode=ParseMode.HTML)
        return

    if target_user_id == user_id:
        await message.reply("<i>Нельзя переводить самому себе!</i>", parse_mode=ParseMode.HTML)
        return

    if is_transfer_banned(target_user_id):
        await message.reply("<i>Этому пользователю заблокированы переводы! 🚫</i>", parse_mode=ParseMode.HTML)
        return

    if user_data["balance"] < amount:
        await message.reply(
            f"<i>Недостаточно средств!\n"
            f"<tg-emoji emoji-id=\"5418238674267556907\">⭐</tg-emoji> Баланс: <b>{format_number(user_data['balance'])} m¢</b></i>",
            parse_mode=ParseMode.HTML
        )
        return

    commission = int(amount * 0.1)
    amount_after_commission = amount - commission

    sender_balance_before = user_data["balance"]
    receiver_data = get_user(target_user_id)
    receiver_balance_before = receiver_data["balance"]

    new_sender_balance = sender_balance_before - amount
    update_user(user_id, balance=new_sender_balance)

    new_receiver_balance = receiver_balance_before + amount_after_commission
    update_user(target_user_id, balance=new_receiver_balance)

    add_transfer_history(
        sender_id=user_id,
        receiver_id=target_user_id,
        amount=amount_after_commission,
        commission=commission
    )

    sender_name = html.escape(message.from_user.first_name or "Игрок")
    sender_link = f'<a href="tg://user?id={user_id}">{sender_name}</a>'
    clean_target_name = html.escape(str(target_name))
    target_link = f'<a href="tg://user?id={target_user_id}">{clean_target_name}</a>'

    clean_comment = html.escape(comment) if comment else ""
    comment_block = f'\n<blockquote> <tg-emoji emoji-id="5465300082628763143">💬</tg-emoji> Комментарий: {clean_comment}</blockquote>' if comment else ""

    sender_msg = (
        f'<tg-emoji emoji-id="5472250091332993630">💳</tg-emoji> {sender_link} передал(-а) <b>{format_number(amount_after_commission)} m¢</b> игроку {target_link}.'
        f'\n<blockquote> <tg-emoji emoji-id="5287231198098117669">💰</tg-emoji>Комиссия: {format_number(commission)} m¢</blockquote>'
        f'{comment_block}'
        f'\n<code>·····················</code>'
        f'\n<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: <s>{format_number(sender_balance_before)}</s> → <b>{format_number(new_sender_balance)} m¢</b>'
    )

    await message.reply(sender_msg, parse_mode=ParseMode.HTML)

    try:
        receiver_msg = (
            f'<tg-emoji emoji-id="5472250091332993630">💳</tg-emoji> {sender_link} передал(-а) вам <b>{format_number(amount_after_commission)} m¢</b>'
            f'{comment_block}'
            f'\n<code>·····················</code>'
            f'\n<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: {format_number(receiver_balance_before)} → <b>{format_number(new_receiver_balance)} m¢</b>'
        )
        await bot.send_message(
            chat_id=target_user_id,
            text=receiver_msg,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# --- MP (MINESPOINTS) CURRENCY & TRANSFERS ---

@dp.message(Command("point", "points", "mp", "поинт", "поинты"))
@dp.message(lambda message: message.text and message.text.strip().lower() in ["поинт", "поинты", "point", "points", "/point", "/points", "мп", "/mp", "mp"])
async def cmd_point(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    mp_balance = user_data.get("mp_balance", 0) or 0
    rem_limit = get_user_mp_limit(user_data)

    text = (
        f"💎 Баланс: {format_number(mp_balance)} MP\n"
        f"<code>·····················</code>\n"
        f"<blockquote>Лимит: {format_number(rem_limit)} MP/д.</blockquote>"
    )

    if message.chat.type in ["group", "supergroup"]:
        await message.reply(text, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("send", "mpsend", "sendmp"))
@dp.message(lambda message: message.text and (
    message.text.strip().lower() == "/send" or
    message.text.strip().lower() == "send" or
    message.text.strip().lower().startswith("/send ") or
    message.text.strip().lower().startswith("send ")
))
async def cmd_send_mp(message: types.Message):
    user_id = message.from_user.id
    user_data = get_user(user_id)
    sender_name = html.escape(message.from_user.first_name or "Игрок")
    sender_link = f'<a href="tg://user?id={user_id}">{sender_name}</a>'

    help_text = (
        f"{sender_link}, ты не ввел сколько MPOINT хочешь перевести!\n"
        f"Комиссия на перевод - 0%\n"
        f"<blockquote>Пример: /send сумма «в ответ» </blockquote>\n"
        f"<blockquote>Пример: /send 10 6025818386</blockquote>\n"
        f"<blockquote>Пример: /send 10 @username</blockquote>"
    )

    if is_transfer_banned(user_id):
        await message.reply("<i>Вам заблокированы переводы! 🚫</i>", parse_mode=ParseMode.HTML)
        return

    text = message.text.strip()
    parts = text.split()

    target_user_id = None
    target_name = None
    amount = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Игрок"
        if len(parts) >= 2:
            amount = parse_amount(parts[1])
        else:
            await message.reply(help_text, parse_mode=ParseMode.HTML)
            return
    else:
        if len(parts) < 2:
            await message.reply(help_text, parse_mode=ParseMode.HTML)
            return
        elif len(parts) == 2:
            await message.reply(help_text, parse_mode=ParseMode.HTML)
            return
        else:
            arg1 = parts[1]
            arg2 = parts[2]

            parsed_amt1 = parse_amount(arg1)
            parsed_amt2 = parse_amount(arg2)

            if parsed_amt1 is not None and not (arg1.isdigit() and len(arg1) >= 8 and parsed_amt2 is not None):
                amount = parsed_amt1
                target_arg = arg2
            elif parsed_amt2 is not None:
                amount = parsed_amt2
                target_arg = arg1
            else:
                await message.reply(help_text, parse_mode=ParseMode.HTML)
                return

            if target_arg.startswith("@"):
                uname = target_arg.lstrip("@")
                found = get_user_by_username(uname)
                if not found:
                    await message.reply(
                        f"<i>Игрок @{uname} не найден в базе бота. Он должен хотя бы раз написать боту.</i>",
                        parse_mode=ParseMode.HTML
                    )
                    return
                target_user_id = found["user_id"]
                target_name = f"@{uname}"
            elif target_arg.isdigit():
                target_user_id = int(target_arg)
                target_name = get_user_display_name(target_user_id)
            else:
                found = get_user_by_username(target_arg)
                if found:
                    target_user_id = found["user_id"]
                    target_name = f"@{target_arg}"
                else:
                    await message.reply(
                        f"<i>Игрок {target_arg} не найден в базе бота.</i>",
                        parse_mode=ParseMode.HTML
                    )
                    return

    if amount is None or amount <= 0:
        await message.reply(help_text, parse_mode=ParseMode.HTML)
        return

    if target_user_id == user_id:
        await message.reply("<i>Нельзя переводить самому себе!</i>", parse_mode=ParseMode.HTML)
        return

    if is_transfer_banned(target_user_id):
        await message.reply("<i>Этому пользователю заблокированы переводы! 🚫</i>", parse_mode=ParseMode.HTML)
        return

    sender_mp = user_data.get("mp_balance", 0) or 0
    if sender_mp < amount:
        await message.reply(
            f"<i>Недостаточно средств!\n"
            f"💎 Баланс: <b>{format_number(sender_mp)} MP</b></i>",
            parse_mode=ParseMode.HTML
        )
        return

    rem_limit = get_user_mp_limit(user_data)
    if amount > rem_limit:
        await message.reply(
            f"<i>Превышен суточный лимит переводов!\n"
            f"📮 Доступно сегодня: <b>{format_number(rem_limit)} MP/д.</b></i>",
            parse_mode=ParseMode.HTML
        )
        return

    # Ensure target user exists in DB
    get_user(target_user_id)

    confirm_token = secrets.token_hex(8)
    mp_pending_transfers[confirm_token] = {
        "sender_id": user_id,
        "receiver_id": target_user_id,
        "amount": amount,
        "target_name": target_name,
        "chat_id": message.chat.id
    }

    target_display = html.escape(target_name or get_user_display_name(target_user_id))
    target_link = f'<a href="tg://user?id={target_user_id}">{target_display}</a>'

    confirm_text = (
        f"❓ {sender_link}, точно хочешь перевести <b>{format_number(amount)} MP </b>игроку {target_link}?"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да ✅",
                    callback_data=f"mp_yes_{confirm_token}",
                    style="success"
                ),
                InlineKeyboardButton(
                    text="Нет 💢",
                    callback_data=f"mp_no_{confirm_token}",
                    style="danger"
                )
            ]
        ]
    )

    await message.reply(
        text=confirm_text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )


@dp.callback_query(lambda c: c.data and c.data.startswith("mp_no_"))
async def process_mp_no(callback: types.CallbackQuery):
    token = callback.data.split("_")[-1]
    transfer = mp_pending_transfers.get(token)
    if transfer and callback.from_user.id != transfer["sender_id"]:
        await callback.answer("Это действие не для вас!", show_alert=True)
        return

    mp_pending_transfers.pop(token, None)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(lambda c: c.data and c.data.startswith("mp_yes_"))
async def process_mp_yes(callback: types.CallbackQuery):
    token = callback.data.split("_")[-1]
    transfer = mp_pending_transfers.get(token)
    if not transfer:
        await callback.answer("Перевод устарел или уже выполнен!", show_alert=True)
        return

    if callback.from_user.id != transfer["sender_id"]:
        await callback.answer("Это действие не для вас!", show_alert=True)
        return

    sender_id = transfer["sender_id"]
    receiver_id = transfer["receiver_id"]
    amount = transfer["amount"]

    sender_data = get_user(sender_id)
    receiver_data = get_user(receiver_id)

    sender_mp_before = sender_data.get("mp_balance", 0) or 0
    if sender_mp_before < amount:
        await callback.answer("Недостаточно MP на балансе!", show_alert=True)
        return

    rem_limit = get_user_mp_limit(sender_data)
    if amount > rem_limit:
        await callback.answer("Превышен суточный лимит переводов!", show_alert=True)
        return

    if is_transfer_banned(sender_id) or is_transfer_banned(receiver_id):
        await callback.answer("Переводы заблокированы!", show_alert=True)
        return

    mp_pending_transfers.pop(token, None)

    sender_mp_after = sender_mp_before - amount
    receiver_mp_before = receiver_data.get("mp_balance", 0) or 0
    receiver_mp_after = receiver_mp_before + amount

    today_str = get_msk_today_str()
    if sender_data.get("mp_daily_date") != today_str:
        new_daily_transferred = amount
    else:
        new_daily_transferred = (sender_data.get("mp_daily_transferred", 0) or 0) + amount

    new_rem_limit = max(0, 1000 - new_daily_transferred)

    update_user(sender_id, mp_balance=sender_mp_after, mp_daily_transferred=new_daily_transferred, mp_daily_date=today_str)
    update_user(receiver_id, mp_balance=receiver_mp_after)

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute("INSERT INTO mp_transfers_history (sender_id, receiver_id, amount, created_at) VALUES (?, ?, ?, ?)",
                       (sender_id, receiver_id, amount, now_str))
        conn.commit()
    except Exception:
        pass

    sender_name = html.escape(callback.from_user.first_name or "Игрок")
    sender_link = f'<a href="tg://user?id={sender_id}">{sender_name}</a>'
    target_raw_name = transfer.get("target_name") or get_user_display_name(receiver_id)
    target_name = html.escape(target_raw_name)
    target_link = f'<a href="tg://user?id={receiver_id}">{target_name}</a>'

    success_msg = (
        f"🔹 {sender_link} перевел(-а) <b>{format_number(amount)} MP </b> {target_link}. \n"
        f"<blockquote>📮 Лимит: {format_number(new_rem_limit)} MP</blockquote>\n"
        f"<code>·····················</code>\n"
        f"💎 Баланс: <s>{format_number(sender_mp_before)}</s> → {format_number(sender_mp_after)} MP"
    )

    try:
        await callback.message.edit_text(
            text=success_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=None
        )
    except Exception:
        pass

    await callback.answer("Перевод выполнен!")

    try:
        receiver_pm = f"✅ Поступил перевод в размере {format_number(amount)} MP от {sender_link}."
        await bot.send_message(
            chat_id=receiver_id,
            text=receiver_pm,
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


@dp.message(Command("addmp"))
async def cmd_addmp(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    if not args:
        await message.answer("<i>Использование: /addmp [сумма] или в ответ на сообщение</i>", parse_mode=ParseMode.HTML)
        return

    amount = parse_amount(args[0])
    if amount is None:
        await message.answer("<i>Неверная сумма!</i>", parse_mode=ParseMode.HTML)
        return

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    elif len(args) > 1:
        target_arg = args[1]
        if target_arg.startswith("@"):
            found = get_user_by_username(target_arg.lstrip("@"))
            if not found:
                await message.answer("<i>Пользователь не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = target_arg
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = f"ID:{target_user_id}"
        else:
            found = get_user_by_username(target_arg)
            if not found:
                await message.answer("<i>Пользователь не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{target_arg}"
    else:
        target_user_id = user_id
        target_name = "ваш"

    u_data = get_user(target_user_id)
    new_bal = u_data.get("mp_balance", 0) + amount
    update_user(target_user_id, mp_balance=new_bal)
    await message.reply(
        f"<i>✅ На баланс {target_name} добавлено <b>{format_number(amount)} MP</b>\n"
        f"💎 Текущий баланс: <b>{format_number(new_bal)} MP</b></i>",
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("setmp"))
async def cmd_setmp(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    if not args:
        await message.answer("<i>Использование: /setmp [сумма] или в ответ на сообщение</i>", parse_mode=ParseMode.HTML)
        return

    amount = parse_amount(args[0])
    if amount is None or amount < 0:
        await message.answer("<i>Неверная сумма!</i>", parse_mode=ParseMode.HTML)
        return

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.first_name
    elif len(args) > 1:
        target_arg = args[1]
        if target_arg.startswith("@"):
            found = get_user_by_username(target_arg.lstrip("@"))
            if not found:
                await message.answer("<i>Пользователь не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = target_arg
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = f"ID:{target_user_id}"
        else:
            found = get_user_by_username(target_arg)
            if not found:
                await message.answer("<i>Пользователь не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{target_arg}"
    else:
        target_user_id = user_id
        target_name = "ваш"

    get_user(target_user_id)
    update_user(target_user_id, mp_balance=amount)
    await message.reply(
        f"<i>✅ Баланс MP {target_name} установлен на <b>{format_number(amount)} MP</b></i>",
        parse_mode=ParseMode.HTML
    )


# --- GIVEAWAYS ---

giveaways = {}
active_giveaways = {}
giveaway_counter = 0


@dp.message(lambda message: message.text and (
    message.text.lower().startswith("раздача") or
    message.text.lower().startswith("/раздача") or
    message.text.lower().startswith("роздача") or
    message.text.lower().startswith("/giveaway")
))
async def cmd_giveaway(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "<i>Использование: раздача [сумма] [акт]\n"
            "Пример: <code>раздача 10кк 10</code></i>",
            parse_mode=ParseMode.HTML
        )
        return

    amount = parse_amount(args[1])
    if amount is None or amount <= 0:
        await message.answer("<i>Неверная сумма! Пример: <code>раздача 10кк 10</code></i>", parse_mode=ParseMode.HTML)
        return

    try:
        activations = int(args[2])
        if activations <= 0:
            await message.answer("<i>Количество активаций должно быть больше 0!</i>", parse_mode=ParseMode.HTML)
            return
    except ValueError:
        await message.answer("<i>Количество активаций должно быть числом! Пример: <code>раздача 10кк 10</code></i>", parse_mode=ParseMode.HTML)
        return

    global giveaway_counter
    giveaway_counter += 1
    gid = giveaway_counter
    chat_id = message.chat.id

    old_gid = active_giveaways.get(chat_id)
    if old_gid and old_gid in giveaways:
        giveaways[old_gid]["is_active"] = False

    giveaways[gid] = {
        "chat_id": chat_id,
        "amount": amount,
        "total_activations": activations,
        "remaining_activations": activations,
        "claimed_users": set(),
        "is_active": True,
        "message_id": None
    }
    active_giveaways[chat_id] = gid

    text = (
        f'<tg-emoji emoji-id="5287231198098117669">💰</tg-emoji>Новая раздача на <b>{activations}</b> акт.\n'
        f'Сумма награды: <b>{format_number(amount)} m¢</b>\n\n'
        f'<i>Чтобы забрать награду, нажми на кнопку ниже!</i>'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Взять",
                    callback_data=f"giveaway_claim_{gid}",
                    style="success",
                    icon_custom_emoji_id="5278467510604160626"
                )
            ]
        ]
    )

    msg = await message.answer(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

    giveaways[gid]["message_id"] = msg.message_id

    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id)
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("giveaway_claim_"))
async def process_giveaway_claim(callback: types.CallbackQuery):
    try:
        gid = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка!", show_alert=True)
        return

    user_id = callback.from_user.id
    chat_id = callback.message.chat.id

    giveaway = giveaways.get(gid)
    if not giveaway:
        try:
            await callback.answer("Эта раздача устарела!", show_alert=True)
        except Exception:
            pass
        return

    current_gid = active_giveaways.get(chat_id)
    if current_gid != gid or not giveaway["is_active"]:
        try:
            await callback.answer("Эта раздача устарела!", show_alert=True)
        except Exception:
            pass
        return

    if user_id in giveaway["claimed_users"]:
        try:
            await callback.answer("Вы уже забрали эту награду!", show_alert=True)
        except Exception:
            pass
        return

    if giveaway["remaining_activations"] <= 0:
        try:
            await callback.answer("Раздача окончена! Активации закончились.", show_alert=True)
        except Exception:
            pass
        return

    giveaway["remaining_activations"] -= 1
    giveaway["claimed_users"].add(user_id)

    amount = giveaway["amount"]
    user_data = get_user(user_id)
    update_user(user_id, balance=user_data["balance"] + amount)

    user_name = html.escape(callback.from_user.first_name or "Игрок")
    user_link = f'<a href="tg://user?id={user_id}">{user_name}</a>'

    try:
        await callback.message.reply(
            f'<i>{user_link}, вы получили <b>{format_number(amount)} m¢</b>!</i>',
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    try:
        await callback.answer(f"Вы получили {format_number(amount)} m¢!")
    except Exception:
        pass

    if giveaway["remaining_activations"] <= 0:
        giveaway["is_active"] = False
        try:
            finished_text = (
                f'<tg-emoji emoji-id="5287231198098117669">💰</tg-emoji><b>Раздача окончена!</b>\n'
                f'Сумма награды: <b>{format_number(amount)} m¢</b>\n'
                f'Все {giveaway["total_activations"]} активаций были забраны.'
            )
            await callback.message.edit_text(
                text=finished_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None
            )
        except Exception:
            pass


# --- BAN & UNBAN ADMIN COMMANDS ---

@dp.message(Command("ban", "бан"))
async def cmd_ban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]

    target_user_id = None
    target_name = None
    reason = "Не указана"
    duration_str = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"

        if len(args) == 0:
            await message.answer("<i>Использование: /ban &lt;причина&gt; &lt;время&gt;\nПример: <code>/ban спам 1ч</code> или <code>/ban нг</code></i>", parse_mode=ParseMode.HTML)
            return
        elif len(args) == 1:
            duration_str = args[0]
        else:
            duration_str = args[-1]
            reason = " ".join(args[:-1])
    else:
        if len(args) < 2:
            await message.answer("<i>Использование: /ban &lt;username/id&gt; &lt;причина&gt; &lt;время&gt;\nПример: <code>/ban @username спам 1ч</code> или <code>/ban @username нг</code></i>", parse_mode=ParseMode.HTML)
            return

        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = f"ID:{target_user_id}"
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return

        if len(args) == 2:
            duration_str = args[1]
        else:
            duration_str = args[-1]
            reason = " ".join(args[1:-1])

    if target_user_id == user_id:
        await message.answer("<i>Вы не можете забанить самого себя!</i>", parse_mode=ParseMode.HTML)
        return

    if target_user_id in ADMINS:
        await message.answer("<i>Нельзя забанить администратора!</i>", parse_mode=ParseMode.HTML)
        return

    delta, is_permanent = parse_ban_duration(duration_str)

    if not is_permanent:
        if delta is None:
            await message.answer("<i>Неверный формат времени! Пример: <code>5мин</code>, <code>1ч</code>, <code>24ч</code>, <code>7д</code>, <code>нг</code></i>", parse_mode=ParseMode.HTML)
            return
        if delta < timedelta(minutes=5):
            await message.answer("<i>Минимальный бан — 5 минут!</i>", parse_mode=ParseMode.HTML)
            return
        until_dt = datetime.now() + delta
        until_text = until_dt.strftime("%d/%m/%Y")
    else:
        until_dt = None
        until_text = "навсегда"

    if ban_user(target_user_id, reason, until_dt, is_permanent):
        admin_name = html.escape(message.from_user.first_name or "Админ")
        admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
        target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'

        if is_permanent:
            await message.answer(
                f'<i>{admin_link}, пользователь {target_link} успешно забанен навсегда по причине: {reason}</i>',
                parse_mode=ParseMode.HTML
            )
        else:
            await message.answer(
                f'<i>{admin_link}, пользователь {target_link} успешно забанен до {until_text} по причине: {reason}</i>',
                parse_mode=ParseMode.HTML
            )
    else:
        await message.answer("<i>Ошибка при блокировке пользователя!</i>", parse_mode=ParseMode.HTML)


@dp.message(Command("unban", "разбан"))
async def cmd_unban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = f"ID:{target_user_id}"
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        await message.answer("<i>Использование: /unban <username/id> или в ответ на сообщение</i>", parse_mode=ParseMode.HTML)
        return

    if unban_user(target_user_id):
        admin_name = html.escape(message.from_user.first_name or "Админ")
        admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
        target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'
        await message.answer(
            f'<i>{admin_link}, пользователь {target_link} успешно разбанен.</i>',
            parse_mode=ParseMode.HTML
        )
    else:
        await message.answer("<i>Ошибка при разбане пользователя!</i>", parse_mode=ParseMode.HTML)


# --- TOP BAN & TRANSFER BAN ADMIN COMMANDS ---

@dp.message(Command("tban"))
async def cmd_tban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_name = message.from_user.first_name or "Администратор"

    ban_user_top(target_user_id)
    admin_name = html.escape(message.from_user.first_name or "Админ")
    admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
    target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'

    await message.answer(
        f'<i>{admin_link}, пользователь {target_link} успешно заблокирован в топе! 🚫</i>',
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("untban", "untopban"))
async def cmd_untban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_name = message.from_user.first_name or "Администратор"

    unban_user_top(target_user_id)
    admin_name = html.escape(message.from_user.first_name or "Админ")
    admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
    target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'

    await message.answer(
        f'<i>{admin_link}, пользователь {target_link} успешно разблокирован в топе! ✅</i>',
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("trban"))
async def cmd_trban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_name = message.from_user.first_name or "Администратор"

    ban_user_transfers(target_user_id)
    admin_name = html.escape(message.from_user.first_name or "Админ")
    admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
    target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'

    await message.answer(
        f'<i>{admin_link}, пользователю {target_link} успешно заблокированы переводы! 🚫</i>',
        parse_mode=ParseMode.HTML
    )


@dp.message(Command("untrban"))
async def cmd_untrban(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_name = message.from_user.first_name or "Администратор"

    unban_user_transfers(target_user_id)
    admin_name = html.escape(message.from_user.first_name or "Админ")
    admin_link = f'<a href="tg://user?id={user_id}">{admin_name}</a>'
    target_link = f'<a href="tg://user?id={target_user_id}">{html.escape(str(target_name))}</a>'

    await message.answer(
        f'<i>{admin_link}, пользователю {target_link} успешно разблокированы переводы! ✅</i>',
        parse_mode=ParseMode.HTML
    )


# --- GET USER INFO ---

@dp.message(Command("get", "гет"))
@dp.message(lambda message: message.text and message.text.strip().lower().split()[0] in ["гет", "get", "/get", "/гет"])
async def cmd_get(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    text = (message.text or "").strip()
    parts = text.split()

    target_user_id = None
    target_user_name = None
    target_username = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_user_name = target_user.first_name or "Игрок"
        target_username = target_user.username
    elif len(parts) > 1:
        arg = parts[1]
        if arg.startswith("@"):
            uname = arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.reply(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_username = uname
            target_user_name = f"@{uname}"
        elif arg.isdigit():
            target_user_id = int(arg)
            target_user_name = get_user_display_name(target_user_id)
            target_username = target_user_name.lstrip("@") if target_user_name.startswith("@") else None
        else:
            found = get_user_by_username(arg)
            if found:
                target_user_id = found["user_id"]
                target_username = arg
                target_user_name = f"@{arg}"
            else:
                await message.reply(f"<i>Пользователь {arg} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_user_name = message.from_user.first_name or "Игрок"
        target_username = message.from_user.username

    u_data = get_user(target_user_id)
    balance = u_data.get("balance", 0)
    reg_date = u_data.get("registered_at")
    if not reg_date:
        try:
            cursor.execute('SELECT created_at FROM games_history WHERE user_id = ? ORDER BY id ASC LIMIT 1', (target_user_id,))
            first_game = cursor.fetchone()
            if first_game and first_game[0]:
                reg_date = first_game[0]
            else:
                reg_date = "Неизвестно"
        except Exception:
            reg_date = "Неизвестно"

    ban_info = check_user_ban(target_user_id)
    if ban_info:
        ban_status = f'🚫 <b>Заблокирован</b>\n<blockquote>Причина: {ban_info["reason"]}\nСрок: {ban_info["until_str"]}</blockquote>'
    else:
        ban_status = '✅ <b>Активен (не заблокирован)</b>'

    top_ban_status = '🚫 <b>Заблокирован в топе</b>' if is_top_banned(target_user_id) else '✅ <b>В топе доступен</b>'
    tr_ban_status = '🚫 <b>Переводы заблокированы</b>' if is_transfer_banned(target_user_id) else '✅ <b>Переводы доступны</b>'

    last_game = get_last_game(target_user_id)
    game_names = {
        "mines": ("💣", "Мины"),
        "tower": ("🛕", "Башня"),
        "diamonds": ("💠", "Алмазы"),
        "crash": ("🚀", "Краш"),
        "slots": ("🎰", "Слоты"),
        "bowling": ("🎳", "Боулинг"),
        "darts": ("🎯", "Дартс"),
        "basketball": ("🏀", "Баскетбол"),
        "football": ("⚽", "Футбол"),
        "twentyone": ("🃏", "21 (Очко)")
    }
    if last_game:
        gicon, gname = game_names.get(last_game["game_type"], ("🎮", last_game["game_type"]))
        if last_game["result"] == "win":
            res_str = f'✅ Выигрыш (<b>+{format_number(last_game["win_amount"])} m¢</b>)'
        elif last_game["result"] == "draw":
            res_str = f'🤝 Ничья (<b>{format_number(last_game["win_amount"])} m¢</b>)'
        elif last_game["result"] == "expired":
            res_str = '💾 Отменена (таймаут)'
        else:
            res_str = '❌ Проигрыш'
        last_game_str = (
            f'{gicon} <b>{gname}</b> | <i>{last_game["created_at"]}</i>\n'
            f'<blockquote>Ставка: <b>{format_number(last_game["bet"])} m¢</b>\n'
            f'Результат: {res_str}</blockquote>'
        )
    else:
        last_game_str = '<blockquote><i>Нет сыгранных игр</i></blockquote>'

    clean_target_name = html.escape(str(target_user_name or "Игрок"))
    if target_username:
        clean_target_uname = html.escape(str(target_username))
        user_mention = f'<a href="tg://user?id={target_user_id}">{clean_target_name}</a> (@{clean_target_uname})'
    else:
        user_mention = f'<a href="tg://user?id={target_user_id}">{clean_target_name}</a>'

    mp_balance = u_data.get("mp_balance", 0) or 0
    mp_limit = get_user_mp_limit(u_data)

    max_bal = u_data.get("max_balance") if u_data.get("max_balance") is not None else balance
    games_count = u_data.get("games", 0) or 0
    lost_amount = u_data.get("lost", 0) or 0

    info_text = (
        f'<tg-emoji emoji-id="5465665476971471368">👤</tg-emoji> <b>Информация о пользователе:</b>\n'
        f'<blockquote>Игрок: {user_mention}\n'
        f'ID: <code>{target_user_id}</code></blockquote>\n'
        f'<code>·····················</code>\n'
        f'<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> <b>Баланс:</b> <b>{format_number(balance)} m¢</b>\n'
        f'💎 <b>MP Баланс:</b> <b>{format_number(mp_balance)} MP</b> (Лимит: {format_number(mp_limit)}/д.)\n'
        f'📅 <b>Регистрация:</b> <i>{reg_date}</i>\n'
        f'👑 <b>Рекорды:</b>\n'
        f'<blockquote>• Макс. баланс: <b>{format_number(max_bal)} m¢</b>\n'
        f'• Сыграно игр: <b>{format_number(games_count)}</b>\n'
        f'• Проиграно: <b>{format_number(lost_amount)} m¢</b></blockquote>\n'
        f'🛡 <b>Статус бана:</b> {ban_status}\n'
        f'🏆 <b>Статус топа:</b> {top_ban_status}\n'
        f'💳 <b>Статус переводов:</b> {tr_ban_status}\n'
        f'<code>·····················</code>\n'
        f'🕹 <b>Последняя игра:</b>\n{last_game_str}'
    )

    get_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Сбросить рекорды",
                    callback_data=f"adm_reset_rec_{target_user_id}"
                ),
                InlineKeyboardButton(
                    text="📜 История переводов",
                    callback_data=f"thist_{target_user_id}_1"
                )
            ]
        ]
    )

    await message.reply(info_text, reply_markup=get_keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("adm_reset_rec_"))
async def process_adm_reset_rec(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    try:
        target_user_id = int(callback.data.split("_")[-1])
    except Exception:
        await callback.answer("Ошибка пользователя!", show_alert=True)
        return

    u_data = get_user(target_user_id)
    cur_bal = u_data.get("balance", 0)
    update_user(target_user_id, max_balance=cur_bal, games=0, lost=0)
    await callback.answer("✅ Рекорды пользователя успешно сброшены!", show_alert=True)
    target_name = get_user_display_name(target_user_id)
    try:
        await callback.message.reply(
            f"<i>✅ Рекорды пользователя <b>{html.escape(str(target_name))}</b> (<code>{target_user_id}</code>) сброшены!\n"
            f"• Макс. баланс: {format_number(cur_bal)} m¢\n"
            f"• Сыграно игр: 0\n"
            f"• Проиграно: 0 m¢</i>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# --- TRANSFERS HISTORY (ADMIN) ---

def build_thistory_message(target_user_id, target_name, page=1):
    total_count = get_user_transfers_count(target_user_id)
    per_page = 5
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    rows = get_user_transfers(target_user_id, limit=per_page, offset=offset)

    clean_target_name = html.escape(str(target_name))
    target_link = f'<a href="tg://user?id={target_user_id}">{clean_target_name}</a>'
    text = (
        f'💳 <b>История переводов:</b> {target_link} (<code>{target_user_id}</code>)\n'
        f'<code>·····················</code>\n'
    )

    if not rows:
        text += "<i>Переводов не найдено.</i>\n"
    else:
        for idx, (tid, sender_id, receiver_id, amount, commission, dt_str) in enumerate(rows):
            if sender_id == target_user_id:
                other_name = html.escape(get_user_display_name(receiver_id))
                other_link = f'<a href="tg://user?id={receiver_id}">{other_name}</a>'
                text += (
                    f'📤 <b>Перевод игроку:</b> {other_link}\n'
                    f'<blockquote>💰 Сумма: <b>{format_number(amount)} m¢</b> (ком.: {format_number(commission)} m¢)\n'
                    f'📅 Дата: <i>{dt_str}</i></blockquote>\n'
                )
            else:
                other_name = html.escape(get_user_display_name(sender_id))
                other_link = f'<a href="tg://user?id={sender_id}">{other_name}</a>'
                text += (
                    f'📥 <b>Получено от:</b> {other_link}\n'
                    f'<blockquote>💰 Сумма: <b>{format_number(amount)} m¢</b>\n'
                    f'📅 Дата: <i>{dt_str}</i></blockquote>\n'
                )
            if idx < len(rows) - 1:
                text += '<code>·····················</code>\n'

        text += f'\n↗️ <i>Страница {page}/{total_pages} (Всего: {total_count})</i>'

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"thist_{target_user_id}_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"thist_{target_user_id}_{page + 1}"))

    keyboard_rows = []
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows) if keyboard_rows else None
    return text, keyboard


@dp.message(Command("thistory", "th", "история_переводов"))
async def cmd_thistory(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        await message.answer("<i>У вас нет прав для этой команды!</i>", parse_mode=ParseMode.HTML)
        return

    args = message.text.split()[1:]
    target_user_id = None
    target_name = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        target_user_id = target_user.id
        target_name = target_user.first_name or "Пользователь"
    elif args:
        target_arg = args[0]
        if target_arg.startswith("@"):
            uname = target_arg.lstrip("@")
            found = get_user_by_username(uname)
            if not found:
                await message.answer(f"<i>Пользователь @{uname} не найден в базе бота!</i>", parse_mode=ParseMode.HTML)
                return
            target_user_id = found["user_id"]
            target_name = f"@{uname}"
        elif target_arg.isdigit():
            target_user_id = int(target_arg)
            target_name = get_user_display_name(target_user_id)
        else:
            found = get_user_by_username(target_arg)
            if found:
                target_user_id = found["user_id"]
                target_name = f"@{target_arg}"
            else:
                await message.answer(f"<i>Пользователь {target_arg} не найден!</i>", parse_mode=ParseMode.HTML)
                return
    else:
        target_user_id = user_id
        target_name = message.from_user.first_name or "Администратор"

    text, keyboard = build_thistory_message(target_user_id, target_name, page=1)
    await message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("thist_"))
async def process_thistory_page(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        await callback.answer("У вас нет прав администратора!", show_alert=True)
        return

    await callback.answer()
    parts = callback.data.split("_")
    try:
        target_user_id = int(parts[1])
        page = int(parts[2])
    except Exception:
        return

    target_name = get_user_display_name(target_user_id)
    text, keyboard = build_thistory_message(target_user_id, target_name, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


# --- BANK (DEPOSITS & SAVINGS) & REFERRAL SYSTEM HANDLERS ---

def get_day_declension(days: int) -> str:
    if 11 <= days % 100 <= 19:
        return "дней"
    last_digit = days % 10
    if last_digit == 1:
        return "день"
    elif 2 <= last_digit <= 4:
        return "дня"
    else:
        return "дней"


def build_deposit_main_menu(user_id, user_name):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '<tg-emoji emoji-id="5264895611517300926">🏦</tg-emoji><b>ДЕПОЗИТ БАНК</b>\n'
        '<code>·····················</code>\n'
        '<tg-emoji emoji-id="5390875094027344872">💲</tg-emoji> <b>Временные депозиты:</b>\n'
        '<blockquote>├ 1 день — 0,4%\n'
        '├ 3 дня — 1,5% (0,5%/день)\n'
        '├ 7 дней — 4% (0,57%/день)\n'
        '├ 15 дней — 10% (0,66%/день)\n'
        '├ 30 дней — 25% (0,83%/день) <tg-emoji emoji-id="5420315771991497307">🔥</tg-emoji>\n'
        '└ 60 дней — 65% (1,08%/день) <tg-emoji emoji-id="5274225173837394638">👑</tg-emoji> </blockquote>\n'
        '<b> <tg-emoji emoji-id="5296355151743838259">🪙</tg-emoji> Накопительный счет</b>\n'
        '<blockquote>└ 0.2%/день, вывод в любой момент</blockquote>\n'
        '<code>·····················</code>\n'
        'Куда хочешь <b>вложиться? 👇</b>'
    )

    keyboard_rows = [
        [
            InlineKeyboardButton(
                text="Временные депозиты",
                callback_data="dep_time",
                style="primary",
                icon_custom_emoji_id="5390875094027344872"
            ),
            InlineKeyboardButton(
                text="Накопительный счет",
                callback_data="dep_savings",
                style="success",
                icon_custom_emoji_id="5296355151743838259"
            )
        ]
    ]

    total_deps = get_user_time_deposits_history_count(user_id)
    if total_deps > 0:
        keyboard_rows.append([
            InlineKeyboardButton(
                text="Мои депозиты",
                callback_data="dep_my",
                style="primary"
            )
        ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@dp.message(Command("deposit", "депозит", "банк", "депозиты"))
@dp.message(lambda message: message.text and message.text.strip().lower() in ["депозит", "депозиты", "деп", "банк", "/deposit", "/депозит", "/банк"])
async def cmd_deposit(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    text, keyboard = build_deposit_main_menu(user_id, user_name)
    await message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data == "dep_main")
async def process_dep_main(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = build_deposit_main_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_time_deposits_menu(user_id, user_name):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    text = (
        f'{user_link}\n'
        '<tg-emoji emoji-id="5390875094027344872">💲</tg-emoji> <b>ВРЕМЕННЫЕ ДЕПОЗИТЫ</b>\n'
        '<code>·····················</code>\n'
        '<blockquote>ℹ️ Здесь вы можете вложить свои mCoin под проценты на фиксированный срок. При досрочном снятии возвращается только сумма без процентов.</blockquote>\n'
        'Выбери <b>срок депозита 👇</b>'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1д.", callback_data="dep_term_1_0", style="primary"),
                InlineKeyboardButton(text="3д.", callback_data="dep_term_3_0", style="primary"),
                InlineKeyboardButton(text="7д.", callback_data="dep_term_7_0", style="primary")
            ],
            [
                InlineKeyboardButton(text="15д.", callback_data="dep_term_15_0", style="primary"),
                InlineKeyboardButton(text="30д.", callback_data="dep_term_30_0", style="primary"),
                InlineKeyboardButton(text="60д.", callback_data="dep_term_60_0", style="primary")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="dep_main", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    return text, keyboard


@dp.callback_query(lambda c: c.data == "dep_time")
async def process_dep_time(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = build_time_deposits_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_deposit_term_input_menu(user_id, user_name, days: int, is_locked: int = 0):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)

    decl = get_day_declension(days).upper()
    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5264895611517300926">🏦</tg-emoji><b>ДЕПОЗИТ НА {days} {decl}</b>\n'
        '<code>·····················</code>\n'
        '<blockquote>ℹ️ Чтобы внести другое количество mCoin, ответьте на это сообщение, указав нужное число.</blockquote>\n'
        '<blockquote> <tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Лимит банка: 100kk mCoin.</blockquote>\n'
        '<tg-emoji emoji-id="5465347885614788367">💴</tg-emoji> Сколько <b>хочешь внести?</b>'
    )

    b10 = max(1, int(balance * 0.10)) if balance > 0 else 0
    b25 = max(1, int(balance * 0.25)) if balance > 0 else 0
    b50 = max(1, int(balance * 0.50)) if balance > 0 else 0

    lock_icon = "✅" if is_locked else "❌"
    next_lock = 0 if is_locked else 1

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"Все • {format_number(balance)} m¢", callback_data=f"dep_make_{days}_{is_locked}_all")
            ],
            [
                InlineKeyboardButton(text=f"{format_number(b10)} m¢", callback_data=f"dep_make_{days}_{is_locked}_10"),
                InlineKeyboardButton(text=f"{format_number(b25)} m¢", callback_data=f"dep_make_{days}_{is_locked}_25"),
                InlineKeyboardButton(text=f"{format_number(b50)} m¢", callback_data=f"dep_make_{days}_{is_locked}_50")
            ],
            [
                InlineKeyboardButton(text=f"Заблокировать снятие {lock_icon}", callback_data=f"dep_term_{days}_{next_lock}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="dep_time", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    return text, keyboard


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_term_"))
async def process_dep_term(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    days = int(parts[2])
    is_locked = int(parts[3]) if len(parts) > 3 else 0
    text, keyboard = build_deposit_term_input_menu(callback.from_user.id, callback.from_user.first_name, days, is_locked)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_make_"))
async def process_dep_make(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    days = int(parts[2])
    is_locked = int(parts[3])
    pct_mode = parts[4]

    user_id = callback.from_user.id
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)

    if pct_mode == "all":
        amount = balance
    elif pct_mode == "10":
        amount = max(1, int(balance * 0.10))
    elif pct_mode == "25":
        amount = max(1, int(balance * 0.25))
    elif pct_mode == "50":
        amount = max(1, int(balance * 0.50))
    else:
        try:
            amount = int(pct_mode)
        except Exception:
            amount = balance

    if amount <= 0:
        await callback.answer("У вас нет средств для внесения депозита!", show_alert=True)
        return

    ok, res = create_time_deposit(user_id, amount, days, is_locked)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    await callback.answer("Депозит успешно открыт!")
    clean_name = html.escape(str(callback.from_user.first_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    end_str = res["end_at"]
    rate_str = str(res["percent"]).replace(".", ",")
    text = (
        f'{user_link}\n'
        '<tg-emoji emoji-id="5264895611517300926">🏦</tg-emoji><b>ДЕПОЗИТ ПРИНЯТ</b>\n'
        '<code>·····················</code>\n'
        f'💰 Сумма: <b>{format_number(amount)} m¢</b>\n'
        f'🔘 Процент: <b>{rate_str}% • {format_number(res["profit"])} m¢</b>\n'
        '<code>············</code>\n'
        f'⏳ До: <b>{end_str} ({days}д.)</b>'
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Положить ещё", callback_data="dep_main", style="primary")
            ]
        ]
    )

    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_my_deposits_menu(user_id, user_name):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    active_deps = get_active_time_deposits(user_id)
    total_earned = get_user_earned_deposits_total(user_id)
    notif_enabled = get_bank_settings(user_id)["notifications_enabled"]

    active_count = len(active_deps)

    text = (
        f'{user_link}\n'
        f'🏦 <b>МОИ ДЕПОЗИТЫ • {active_count}</b>\n'
        '<code>·····················</code>\n'
        '❇️ <b>Активные:</b>\n'
    )

    if active_deps:
        for idx, dep in enumerate(active_deps[:3]):
            dep_id, amount, days, percent, profit, is_locked, c_at, end_at, c_dt, e_dt = dep
            p_str = str(percent).replace(".", ",")
            lock_badge = " 🔒" if is_locked else ""
            text += f'До {end_at} • {format_number(amount)} m¢ ({p_str}%){lock_badge}\n'
        if len(active_deps) > 3:
            text += f'<i>...и ещё {len(active_deps) - 3} активных вкладов</i>\n'
    else:
        text += '<i>Нет активных депозитов</i>\n'

    text += (
        '\n<code>············</code>\n'
        f'<blockquote>💰 Заработано: <b>{format_number(total_earned)} m¢</b></blockquote>'
    )

    notif_icon = "✅" if notif_enabled else "❌"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❇️ Все активные депозиты", callback_data="dep_active_all_1", style="success")
            ],
            [
                InlineKeyboardButton(text="Снять ⤵️", callback_data="dep_withdraw_menu_1", style="primary"),
                InlineKeyboardButton(text="История 🕐", callback_data="dep_history_1", style="primary")
            ],
            [
                InlineKeyboardButton(text=f"Уведомления от банка {notif_icon}", callback_data="dep_notif_toggle")
            ],
            [
                InlineKeyboardButton(text="Положить ещё", callback_data="dep_main", style="primary")
            ]
        ]
    )
    return text, keyboard


@dp.callback_query(lambda c: c.data == "dep_my")
async def process_dep_my(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = build_my_deposits_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data == "dep_notif_toggle")
async def process_dep_notif_toggle(callback: types.CallbackQuery):
    toggle_bank_notifications(callback.from_user.id)
    await callback.answer("Настройки уведомлений обновлены!")
    text, keyboard = build_my_deposits_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


def build_active_deposits_menu(user_id, user_name, page=1):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    active_deps = get_active_time_deposits(user_id)
    total_count = len(active_deps)
    per_page = 5
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    start_idx = (page - 1) * per_page
    page_items = active_deps[start_idx:start_idx + per_page]

    text = (
        f'{user_link}\n'
        '❇️ <b>АКТИВНЫЕ ДЕПОЗИТЫ</b>\n'
        '<code>·····················</code>\n'
    )

    if not page_items:
        text += "<i>Нет активных депозитов.</i>\n"
    else:
        for dep in page_items:
            dep_id, amount, days, percent, profit, is_locked, c_at, end_at, c_dt, e_dt = dep
            p_str = str(percent).replace(".", ",")
            lock_badge = " 🔒" if is_locked else ""
            text += f'До {end_at} • <b>{format_number(amount)} m¢</b> ({p_str}%){lock_badge}\n'

    text += (
        '<code>············</code>\n'
        f'↗️ страница {page}/{total_pages}'
    )

    keyboard_rows = []
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"dep_active_all_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"dep_active_all_{page + 1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([
        InlineKeyboardButton(text="Снять ⤵️", callback_data="dep_withdraw_menu_1", style="primary"),
        InlineKeyboardButton(text="История 🕐", callback_data="dep_history_1", style="primary")
    ])
    keyboard_rows.append([
        InlineKeyboardButton(text="Положить ещё", callback_data="dep_main", style="primary"),
        InlineKeyboardButton(text="Назад", callback_data="dep_my", icon_custom_emoji_id="5255703720078879038")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_active_all_"))
async def process_dep_active_all(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 1
    text, keyboard = build_active_deposits_menu(callback.from_user.id, callback.from_user.first_name, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_withdraw_deposits_menu(user_id, user_name, page=1):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    active_deps = get_active_time_deposits(user_id)
    unlocked_deps = [d for d in active_deps if not d[5]]  # is_locked == 0

    total_count = len(unlocked_deps)
    per_page = 4
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    start_idx = (page - 1) * per_page
    page_items = unlocked_deps[start_idx:start_idx + per_page]

    text = (
        f'{user_link}\n'
        '🏦 <b>СНЯТЬ ДЕПОЗИТ</b>\n'
        '<code>·····················</code>\n'
        '<blockquote>ℹ️ Депозит снимается автоматически при нажатии на кнопку. Процент, начисленный на депозит, теряется в полном объеме при его снятии.</blockquote>\n'
        f'↘️ страница {page}/{total_pages}'
    )

    keyboard_rows = []
    if unlocked_deps:
        keyboard_rows.append([
            InlineKeyboardButton(text="❇️ снять всё", callback_data="dep_wd_all", style="danger")
        ])
        for dep in page_items:
            dep_id, amount, days, percent, profit, is_locked, c_at, end_at, c_dt, e_dt = dep
            keyboard_rows.append([
                InlineKeyboardButton(text=f"до {end_at} — {format_number(amount)}", callback_data=f"dep_wd_one_{dep_id}")
            ])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"dep_withdraw_menu_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"dep_withdraw_menu_{page + 1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([
        InlineKeyboardButton(text="Назад", callback_data="dep_my", icon_custom_emoji_id="5255703720078879038")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_withdraw_menu_"))
async def process_dep_withdraw_menu(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 1
    text, keyboard = build_withdraw_deposits_menu(callback.from_user.id, callback.from_user.first_name, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data == "dep_wd_all")
async def process_dep_wd_all(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    ok, res = withdraw_all_time_deposits(user_id)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return
    await callback.answer(f"Снято {format_number(res)} mCoin на баланс!")
    text, keyboard = build_my_deposits_menu(user_id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_wd_one_"))
async def process_dep_wd_one(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        dep_id = int(callback.data.split("_")[-1])
    except Exception:
        return
    ok, res = withdraw_time_deposit(dep_id, user_id)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return
    await callback.answer(f"Депозит на {format_number(res)} mCoin возвращен на баланс!")
    text, keyboard = build_withdraw_deposits_menu(user_id, callback.from_user.first_name, page=1)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        pass


def build_deposits_history_menu(user_id, user_name, page=1):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'

    total_count = get_user_time_deposits_history_count(user_id)
    per_page = 5
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    if page < 1:
        page = 1

    offset = (page - 1) * per_page
    rows = get_user_time_deposits_history(user_id, limit=per_page, offset=offset)

    text = (
        f'{user_link}\n'
        '🕐 <b>ИСТОРИЯ ДЕПОЗИТОВ</b>\n'
        '<code>·····················</code>\n'
    )

    status_names = {
        "active": "❇️ Активен",
        "completed": "✅ Завершен",
        "withdrawn": "⤵️ Снят досрочно"
    }

    if not rows:
        text += "<i>История депозитов пуста.</i>\n"
    else:
        for dep in rows:
            dep_id, amount, days, percent, profit, is_locked, status, c_at, end_at = dep
            st_text = status_names.get(status, status)
            p_str = str(percent).replace(".", ",")
            text += (
                f'🏦 <b>Депозит #{dep_id}</b> ({days}д., {p_str}%)\n'
                f'<blockquote>💰 Сумма: <b>{format_number(amount)} m¢</b> | Прибыль: <b>+{format_number(profit)} m¢</b>\n'
                f'📅 Создан: <i>{c_at}</i> | До: <i>{end_at}</i>\n'
                f'Статус: {st_text}</blockquote>\n'
            )

    text += (
        '<code>············</code>\n'
        f'↗️ страница {page}/{total_pages}'
    )

    keyboard_rows = []
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"dep_history_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"dep_history_{page + 1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([
        InlineKeyboardButton(text="Назад", callback_data="dep_my", icon_custom_emoji_id="5255703720078879038")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_history_"))
async def process_dep_history(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split("_")[-1])
    except Exception:
        page = 1
    text, keyboard = build_deposits_history_menu(callback.from_user.id, callback.from_user.first_name, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# --- SAVINGS ACCOUNT (НАКОПИТЕЛЬНЫЙ СЧЕТ) ---

def build_savings_menu(user_id, user_name):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
    sav = get_savings_account(user_id)
    sav_bal = sav.get("balance", 0)
    accum = int(sav.get("accumulated_interest", 0.0))

    text = (
        f'{user_link}\n'
        '💸 <b>НАКОПИТЕЛЬНЫЙ СЧЕТ</b>\n'
        '<code>·····················</code>\n'
        '<blockquote>ℹ️ Здесь вы можете хранить свои mCoin и получать ежедневные проценты. Проценты начисляются каждый час. Снятие доступно в любой момент без потери начисленных процентов.</blockquote>\n'
        '<blockquote> <tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji>  Лимит банка: 100kk mCoin.</blockquote>\n'
        f'💰 <b>На счету:</b> {format_number(sav_bal)} m¢\n'
        '🔘 <b>Ставка:</b> 0.2% дневных\n'
        f'💸 <b>Накоплено:</b> {format_number(accum)} m¢'
    )

    action_buttons = [
        InlineKeyboardButton(text="Пополнить", callback_data="dep_sav_dep", style="success")
    ]
    if (sav_bal + accum) > 0:
        action_buttons.append(
            InlineKeyboardButton(text="Снять 💰", callback_data="dep_sav_wd", style="primary")
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            action_buttons,
            [
                InlineKeyboardButton(text="История пополнений 🕐", callback_data="dep_sav_hist_1", style="primary")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="dep_main", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    return text, keyboard


@dp.callback_query(lambda c: c.data == "dep_savings")
async def process_dep_savings(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = build_savings_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_savings_deposit_input_menu(user_id, user_name):
    clean_name = html.escape(str(user_name or "Игрок"))
    user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
    user_data = get_user(user_id)
    balance = user_data.get("balance", 0)

    text = (
        f'{user_link}\n'
        '💳 <b>ПОПОЛНИТЬ НАКОП. СЧЕТ</b>\n'
        '<code>·····················</code>\n'
        f'💰 Доступно: <b>{format_number(balance)} m¢</b>\n'
        '<i>Напишите в ответ на это сообщение сумму, которую хотите положить на накопительный счет:</i>'
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="dep_savings", icon_custom_emoji_id="5255703720078879038")]
        ]
    )
    return text, keyboard


@dp.callback_query(lambda c: c.data == "dep_sav_dep")
async def process_dep_sav_dep(callback: types.CallbackQuery):
    await callback.answer()
    text, keyboard = build_savings_deposit_input_menu(callback.from_user.id, callback.from_user.first_name)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "dep_sav_wd")
async def process_dep_sav_wd(callback: types.CallbackQuery):
    await callback.answer()
    clean_name = html.escape(str(callback.from_user.first_name or "Игрок"))
    user_link = f'<a href="tg://user?id={callback.from_user.id}">{clean_name}</a>'
    sav = get_savings_account(callback.from_user.id)
    sav_bal = sav.get("balance", 0)
    accum = int(sav.get("accumulated_interest", 0.0))
    total_avail = sav_bal + accum
    text = (
        f'{user_link}\n'
        '💸 <b>СНЯТЬ С НАКОП. СЧЕТА</b>\n'
        '<code>·····················</code>\n'
        f'💰 Доступно к снятию: <b>{format_number(total_avail)} m¢</b>\n'
        '<i>Напишите в ответ на это сообщение сумму для снятия:</i>'
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="dep_savings", icon_custom_emoji_id="5255703720078879038")]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data and c.data.startswith("dep_sav_hist_"))
async def process_dep_sav_hist(callback: types.CallbackQuery):
    await callback.answer()
    page = 1
    try:
        page = int(callback.data.replace("dep_sav_hist_", ""))
    except Exception:
        page = 1
    uid = callback.from_user.id
    history = get_savings_history(uid, limit=5, offset=(page-1)*5)
    total_cnt = get_savings_history_count(uid)
    total_pages = max(1, math.ceil(total_cnt / 5))
    clean_name = html.escape(str(callback.from_user.first_name or "Игрок"))
    user_link = f'<a href="tg://user?id={uid}">{clean_name}</a>'

    text = f'{user_link}\n📜 <b>История накопительного счета</b>\n<code>·····················</code>\n'
    if not history:
        text += '<i>История операций пуста</i>'
    else:
        for op_type, amount, dt in history:
            op_name = "Пополнение" if op_type == "deposit" else "Снятие"
            text += f'├ {op_name}: <b>{format_number(amount)} m¢</b> ({dt})\n'

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"dep_sav_hist_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop_p2p"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"dep_sav_hist_{page+1}"))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[nav, [InlineKeyboardButton(text="Назад", callback_data="dep_savings", icon_custom_emoji_id="5255703720078879038")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- REPLY TO BANK PROMPTS HANDLER (CUSTOM AMOUNT) ---

@dp.message(lambda message: message.reply_to_message and message.text and (
    "ДЕПОЗИТ НА" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
    "ПОПОЛНИТЬ НАКОП. СЧЕТ" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
    "СНЯТЬ С НАКОП. СЧЕТА" in (message.reply_to_message.text or message.reply_to_message.caption or "")
))
async def process_bank_reply_message(message: types.Message):
    rep_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    user_id = message.from_user.id
    raw_amount = resolve_bet_amount(message.text.strip(), get_user(user_id)["balance"])

    if raw_amount is None or raw_amount <= 0:
        return

    # Check if replied to "ДЕПОЗИТ НА X ДНЕЙ"
    if "ДЕПОЗИТ НА" in rep_text:
        m = re.search(r'ДЕПОЗИТ НА (\d+)', rep_text)
        if m:
            days = int(m.group(1))
            is_locked = 1 if "Заблокировать снятие ✅" in str(message.reply_to_message.reply_markup) else 0
            ok, res = create_time_deposit(user_id, raw_amount, days, is_locked)
            if not ok:
                await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
                return
            clean_name = html.escape(str(message.from_user.first_name or "Игрок"))
            user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
            rate_str = str(res["percent"]).replace(".", ",")
            succ_text = (
                f'{user_link}\n'
                '<tg-emoji emoji-id="5264895611517300926">🏦</tg-emoji><b>ДЕПОЗИТ ПРИНЯТ</b>\n'
                '<code>·····················</code>\n'
                f'💰 Сумма: <b>{format_number(raw_amount)} m¢</b>\n'
                f'🔘 Процент: <b>{rate_str}% • {format_number(res["profit"])} m¢</b>\n'
                '<code>············</code>\n'
                f'⏳ До: <b>{res["end_at"]} ({days}д.)</b>'
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Положить ещё", callback_data="dep_main", style="primary")]]
            )
            await message.reply(succ_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            return

    # Check if replied to "ПОПОЛНИТЬ НАКОП. СЧЕТ"
    if "ПОПОЛНИТЬ НАКОП. СЧЕТ" in rep_text:
        ok, res = deposit_to_savings(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        clean_name = html.escape(str(message.from_user.first_name or "Игрок"))
        user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
        succ_text = (
            f'{user_link}\n'
            '💸 <b>НАКОП. СЧЕТ ПОПОЛНЕН</b>\n'
            '<code>·····················</code>\n'
            f'💰 Сумма: <b>{format_number(raw_amount)} m¢</b>\n'
            '🔘 Ставка: 0.2% дневных\n'
            '<code>············</code>\n'
            f'<blockquote>🏦 На счету: <b>{format_number(res)} m¢</b></blockquote>'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Накопительный счет", callback_data="dep_savings", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    # Check if replied to "СНЯТЬ С НАКОП. СЧЕТА"
    if "СНЯТЬ С НАКОП. СЧЕТА" in rep_text:
        ok, res = withdraw_from_savings(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        clean_name = html.escape(str(message.from_user.first_name or "Игрок"))
        user_link = f'<a href="tg://user?id={user_id}">{clean_name}</a>'
        succ_text = (
            f'🟢 {user_link}, ты вывел с накопительного счета <b>{format_number(res["withdrawn"])} mCoin!</b>\n'
            '<code>·····················</code>\n'
            f'<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> <b>Баланс:</b> <b>{format_number(res["balance"])} mCoin</b>'
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Накопительный счет", callback_data="dep_savings", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return


# ==========================================
# --- P2P EXCHANGE SYSTEM (MPOINT ⇄ mCoin) ---
# ==========================================

p2p_user_action = {}
admin_sexchange_state = {}


def format_deal_date(dt_str):
    if not dt_str:
        return ""
    try:
        if " " in dt_str:
            d_part, t_part = dt_str.split(" ", 1)
            d_sub = d_part.split(".")
            if len(d_sub) == 3:
                day, month, year = d_sub
                yy = year[-2:]
                time_short = ":".join(t_part.split(":")[:2])
                return f"{day}.{month}.{yy} {time_short}"
    except Exception:
        pass
    return dt_str


def get_p2p_settings():
    try:
        cursor.execute("SELECT id, official_sell_enabled, official_buy_enabled, official_sell_rate, official_buy_rate, rate_min, rate_max, interval_minutes, last_update FROM p2p_settings WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            now_iso = datetime.now().isoformat()
            cursor.execute("INSERT INTO p2p_settings (id, official_sell_enabled, official_buy_enabled, official_sell_rate, official_buy_rate, rate_min, rate_max, interval_minutes, last_update) VALUES (1, 1, 1, 10000, 10000, 7000, 29000, 150, ?) ON CONFLICT (id) DO NOTHING", (now_iso,))
            conn.commit()
            return {
                "official_sell_enabled": 1,
                "official_buy_enabled": 1,
                "official_sell_rate": 10000,
                "official_buy_rate": 10000,
                "rate_min": 7000,
                "rate_max": 29000,
                "interval_minutes": 150,
                "last_update": now_iso
            }
        return {
            "official_sell_enabled": row[1] if row[1] is not None else 1,
            "official_buy_enabled": row[2] if row[2] is not None else 1,
            "official_sell_rate": row[3] or 10000,
            "official_buy_rate": row[4] or 10000,
            "rate_min": row[5] or 7000,
            "rate_max": row[6] or 29000,
            "interval_minutes": row[7] or 150,
            "last_update": row[8]
        }
    except Exception:
        return {
            "official_sell_enabled": 1,
            "official_buy_enabled": 1,
            "official_sell_rate": 10000,
            "official_buy_rate": 10000,
            "rate_min": 7000,
            "rate_max": 29000,
            "interval_minutes": 150,
            "last_update": None
        }


def update_p2p_rate_fluctuation(force=False):
    settings = get_p2p_settings()
    now = datetime.now()
    if not force and settings.get("last_update"):
        try:
            last_dt = datetime.fromisoformat(settings["last_update"])
            if (now - last_dt).total_seconds() < settings.get("interval_minutes", 150) * 60:
                return False, settings
        except Exception:
            pass

    r_min = settings.get("rate_min", 7000) or 7000
    r_max = settings.get("rate_max", 29000) or 29000
    if r_min >= r_max:
        r_min, r_max = 7000, 29000

    new_rate = random.randint(r_min // 100, r_max // 100) * 100
    now_iso = now.isoformat()
    try:
        cursor.execute('''
            UPDATE p2p_settings 
            SET official_sell_rate = ?, official_buy_rate = ?, last_update = ? 
            WHERE id = 1
        ''', (new_rate, new_rate, now_iso))
        conn.commit()
        return True, get_p2p_settings()
    except Exception:
        return False, settings


def get_p2p_account(user_id):
    try:
        cursor.execute("SELECT user_id, mcoin_balance, mp_balance, rating, deals_count, last_deposit_date, sell_order_active, sell_price, buy_order_active, buy_price FROM p2p_accounts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("INSERT INTO p2p_accounts (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING", (user_id,))
            conn.commit()
            cursor.execute("SELECT user_id, mcoin_balance, mp_balance, rating, deals_count, last_deposit_date, sell_order_active, sell_price, buy_order_active, buy_price FROM p2p_accounts WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()

        if row:
            return {
                "user_id": row[0],
                "mcoin_balance": row[1] or 0,
                "mp_balance": row[2] or 0,
                "rating": row[3] if row[3] is not None else 100,
                "deals_count": row[4] or 0,
                "last_deposit_date": row[5],
                "sell_order_active": row[6] or 0,
                "sell_price": row[7] or 0,
                "buy_order_active": row[8] or 0,
                "buy_price": row[9] or 0
            }
    except Exception:
        pass
    return {
        "user_id": user_id,
        "mcoin_balance": 0,
        "mp_balance": 0,
        "rating": 100,
        "deals_count": 0,
        "last_deposit_date": None,
        "sell_order_active": 0,
        "sell_price": 0,
        "buy_order_active": 0,
        "buy_price": 0
    }


def get_p2p_bot_24h_stats():
    cutoff = (datetime.now() - timedelta(hours=24)).strftime("%d.%m.%Y %H:%M:%S")
    cursor.execute("SELECT COUNT(*), COALESCE(SUM(amount_mp), 0) FROM p2p_bot_stats WHERE created_at >= ?", (cutoff,))
    row = cursor.fetchone()
    return row[0], row[1]


def deposit_to_p2p_mcoin(user_id, amount):
    amount = int(amount)
    if amount <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    p2p = get_p2p_account(user_id)
    today_str = get_msk_today_str()
    is_first_today = (p2p["last_deposit_date"] != today_str)

    fee = 0 if is_first_today else int(amount * 0.015)
    total_deduct = amount + fee

    user_data = get_user(user_id)
    if user_data["balance"] < total_deduct:
        return False, f"Недостаточно mCoin! Нужно {format_number(total_deduct)} mCoin (с учетом комиссии {format_number(fee)} mCoin)."

    try:
        cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (total_deduct, user_id, total_deduct))
        if cursor.rowcount == 0:
            return False, "Недостаточно mCoin на основном балансе!"

        credited = amount
        cursor.execute("UPDATE p2p_accounts SET mcoin_balance = mcoin_balance + ?, last_deposit_date = ? WHERE user_id = ?", (credited, today_str, user_id))
        conn.commit()

        updated_p2p = get_p2p_account(user_id)
        return True, {
            "amount": amount,
            "fee": fee,
            "is_first": is_first_today,
            "mcoin_balance": updated_p2p["mcoin_balance"],
            "mp_balance": updated_p2p["mp_balance"]
        }
    except Exception as e:
        return False, f"Ошибка пополнения: {e}"


def deposit_to_p2p_mpoint(user_id, amount):
    amount = int(amount)
    if amount <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    user_data = get_user(user_id)
    if user_data.get("mp_balance", 0) < amount:
        return False, f"Недостаточно MPOINT! На балансе: {format_number(user_data.get('mp_balance', 0))} MP."

    try:
        cursor.execute("UPDATE users SET mp_balance = mp_balance - ? WHERE user_id = ? AND mp_balance >= ?", (amount, user_id, amount))
        if cursor.rowcount == 0:
            return False, "Недостаточно MPOINT на основном балансе!"

        cursor.execute("UPDATE p2p_accounts SET mp_balance = mp_balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

        updated_p2p = get_p2p_account(user_id)
        return True, {
            "amount": amount,
            "mcoin_balance": updated_p2p["mcoin_balance"],
            "mp_balance": updated_p2p["mp_balance"]
        }
    except Exception as e:
        return False, f"Ошибка пополнения: {e}"


def withdraw_from_p2p_mcoin(user_id, amount):
    amount = int(amount)
    if amount <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    try:
        cursor.execute("UPDATE p2p_accounts SET mcoin_balance = mcoin_balance - ? WHERE user_id = ? AND mcoin_balance >= ?", (amount, user_id, amount))
        if cursor.rowcount == 0:
            return False, "Недостаточно mCoin на балансе обменника!"

        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (amount, amount, user_id))
        conn.commit()

        updated_p2p = get_p2p_account(user_id)
        return True, {
            "amount": amount,
            "mcoin_balance": updated_p2p["mcoin_balance"]
        }
    except Exception as e:
        return False, f"Ошибка вывода: {e}"


def withdraw_from_p2p_mpoint(user_id, amount):
    amount = int(amount)
    if amount <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    try:
        cursor.execute("UPDATE p2p_accounts SET mp_balance = mp_balance - ? WHERE user_id = ? AND mp_balance >= ?", (amount, user_id, amount))
        if cursor.rowcount == 0:
            return False, "Недостаточно MPOINT на балансе обменника!"

        cursor.execute("UPDATE users SET mp_balance = mp_balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

        updated_p2p = get_p2p_account(user_id)
        return True, {
            "amount": amount,
            "mp_balance": updated_p2p["mp_balance"]
        }
    except Exception as e:
        return False, f"Ошибка вывода: {e}"


def execute_p2p_buy_deal(buyer_id, seller_id, amount_mp):
    amount_mp = int(amount_mp)
    if amount_mp <= 0:
        return False, "Сумма должна быть больше 0!"
    if buyer_id == seller_id:
        return False, "Нельзя покупать у самого себя!"
    if is_transfer_banned(buyer_id) or is_transfer_banned(seller_id):
        return False, "Переводы заблокированы!"

    seller_p2p = get_p2p_account(seller_id)
    if not seller_p2p["sell_order_active"] or seller_p2p["sell_price"] <= 0:
        return False, "У продавца не активна заявка на продажу!"

    total_cost = amount_mp * seller_p2p["sell_price"]
    buyer_u = get_user(buyer_id)
    if buyer_u["balance"] < total_cost:
        return False, f"Недостаточно mCoin! Нужно {format_number(total_cost)} m¢."

    try:
        cursor.execute("UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?", (total_cost, buyer_id, total_cost))
        if cursor.rowcount == 0:
            return False, "Недостаточно mCoin на балансе!"

        cursor.execute("UPDATE p2p_accounts SET mp_balance = mp_balance - ?, deals_count = deals_count + 1 WHERE user_id = ? AND mp_balance >= ? AND sell_order_active = 1", (amount_mp, seller_id, amount_mp))
        if cursor.rowcount == 0:
            cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (total_cost, buyer_id))
            conn.commit()
            return False, "У продавца изменился баланс или заявка отключена!"

        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (total_cost, total_cost, seller_id))
        cursor.execute("UPDATE users SET mp_balance = mp_balance + ? WHERE user_id = ?", (amount_mp, buyer_id))
        cursor.execute("UPDATE p2p_accounts SET deals_count = deals_count + 1 WHERE user_id = ?", (buyer_id,))
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cursor.execute('''
            INSERT INTO p2p_deals_history (buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            RETURNING id
        ''', (buyer_id, seller_id, amount_mp, seller_p2p["sell_price"], total_cost, now_str))
        res_row = cursor.fetchone()
        deal_id = res_row[0] if res_row else 0
        conn.commit()
        return True, {
            "deal_id": deal_id,
            "amount_mp": amount_mp,
            "total_mcoin": total_cost,
            "rate": seller_p2p["sell_price"],
            "seller_id": seller_id
        }
    except Exception as e:
        return False, f"Ошибка проведения сделки: {e}"


def execute_p2p_buy_from_bot(user_id, amount_mp):
    amount_mp = int(amount_mp)
    if amount_mp <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    if not settings.get("official_buy_enabled", 1):
        return False, "Официальная покупка сейчас отключена!"

    rate = settings.get("official_buy_rate", 10000)
    total_mcoin = amount_mp * rate
    try:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cursor.execute("UPDATE users SET balance = balance - ?, mp_balance = mp_balance + ? WHERE user_id = ? AND balance >= ?", (total_mcoin, amount_mp, user_id, total_mcoin))
        if cursor.rowcount == 0:
            return False, "Недостаточно mCoin на основном балансе!"

        cursor.execute("INSERT INTO p2p_bot_stats (user_id, amount_mp, amount_mcoin, created_at) VALUES (?, ?, ?, ?)", (user_id, amount_mp, total_mcoin, now_str))
        cursor.execute("INSERT INTO p2p_deals_history (buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at) VALUES (?, 0, ?, ?, ?, 0, ?) RETURNING id", (user_id, amount_mp, rate, total_mcoin, now_str))
        res_row = cursor.fetchone()
        deal_id = res_row[0] if res_row else 0
        conn.commit()
        return True, {
            "deal_id": deal_id,
            "amount_mp": amount_mp,
            "total_mcoin": total_mcoin,
            "rate": rate
        }
    except Exception as e:
        return False, f"Ошибка сделки: {e}"


def execute_p2p_sell_to_bot(user_id, amount_mp):
    amount_mp = int(amount_mp)
    if amount_mp <= 0:
        return False, "Сумма должна быть больше 0!"
    if is_transfer_banned(user_id):
        return False, "Вам заблокированы переводы!"

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    if not settings.get("official_sell_enabled", 1):
        return False, "Официальная продажа сейчас отключена!"

    rate = settings.get("official_sell_rate", 10000)
    total_mcoin = amount_mp * rate
    try:
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cursor.execute("UPDATE users SET mp_balance = mp_balance - ?, balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ? AND mp_balance >= ?", (amount_mp, total_mcoin, total_mcoin, user_id, amount_mp))
        if cursor.rowcount == 0:
            return False, "Недостаточно MP на балансе!"

        cursor.execute("INSERT INTO p2p_bot_stats (user_id, amount_mp, amount_mcoin, created_at) VALUES (?, ?, ?, ?)", (user_id, amount_mp, total_mcoin, now_str))
        cursor.execute("INSERT INTO p2p_deals_history (buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at) VALUES (0, ?, ?, ?, ?, 0, ?) RETURNING id", (user_id, amount_mp, rate, total_mcoin, now_str))
        res_row = cursor.fetchone()
        deal_id = res_row[0] if res_row else 0
        conn.commit()
        return True, {
            "deal_id": deal_id,
            "amount_mp": amount_mp,
            "total_mcoin": total_mcoin,
            "rate": rate
        }
    except Exception as e:
        return False, f"Ошибка сделки: {e}"


def execute_p2p_sell_to_buyer(seller_id, buyer_id, amount_mp):
    amount_mp = int(amount_mp)
    if amount_mp <= 0:
        return False, "Сумма должна быть больше 0!"
    if seller_id == buyer_id:
        return False, "Нельзя совершать сделку с самим собой!"
    if is_transfer_banned(seller_id) or is_transfer_banned(buyer_id):
        return False, "Переводы заблокированы!"

    buyer_p2p = get_p2p_account(buyer_id)
    if not buyer_p2p["buy_order_active"] or buyer_p2p["buy_price"] <= 0:
        return False, "У покупателя не активна заявка на покупку!"

    total_mcoin = amount_mp * buyer_p2p["buy_price"]

    try:
        cursor.execute("UPDATE users SET mp_balance = mp_balance - ?, balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ? AND mp_balance >= ?", (amount_mp, total_mcoin, total_mcoin, seller_id, amount_mp))
        if cursor.rowcount == 0:
            return False, "Недостаточно MP на основном балансе!"

        cursor.execute("UPDATE p2p_accounts SET mcoin_balance = mcoin_balance - ?, deals_count = deals_count + 1 WHERE user_id = ? AND mcoin_balance >= ? AND buy_order_active = 1", (total_mcoin, buyer_id, total_mcoin))
        if cursor.rowcount == 0:
            cursor.execute("UPDATE users SET mp_balance = mp_balance + ?, balance = balance - ? WHERE user_id = ?", (amount_mp, total_mcoin, seller_id))
            conn.commit()
            return False, "У покупателя изменился баланс или заявка отключена!"

        cursor.execute("UPDATE users SET mp_balance = mp_balance + ? WHERE user_id = ?", (amount_mp, buyer_id))
        cursor.execute("UPDATE p2p_accounts SET deals_count = deals_count + 1 WHERE user_id = ?", (seller_id,))
        now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cursor.execute('''
            INSERT INTO p2p_deals_history (buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            RETURNING id
        ''', (buyer_id, seller_id, amount_mp, buyer_p2p["buy_price"], total_mcoin, now_str))
        res_row = cursor.fetchone()
        deal_id = res_row[0] if res_row else 0
        conn.commit()
        return True, {
            "deal_id": deal_id,
            "amount_mp": amount_mp,
            "total_mcoin": total_mcoin,
            "rate": buyer_p2p["buy_price"],
            "buyer_id": buyer_id
        }
    except Exception as e:
        return False, f"Ошибка проведения сделки: {e}"


# --- P2P UI RENDERING HELPERS ---

async def show_p2p_main(event, user_id=None, first_name=None, edit=False):
    if hasattr(event, "from_user") and event.from_user:
        uid = user_id or event.from_user.id
        fname = first_name or event.from_user.first_name or "Игрок"
    else:
        uid = user_id
        fname = first_name or "Игрок"

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    user_link = get_user_mention(uid, fname)
    sell_rate = settings.get("official_sell_rate", 10000)

    text = (
        '<tg-emoji emoji-id="5402186569006210455">💱</tg-emoji><b>P2P ОБМЕННИК</b>\n'
        '<code>·····················</code>\n'
        '<blockquote><i>ℹ️ Здесь ты можешь купить и продать MPOINT.</i></blockquote>\n'
        f'<blockquote><tg-emoji emoji-id="5431577498364158238">📊</tg-emoji> <b>Оф. курс:</b> 1 MP ≈ {format_number(sell_rate)} mCoin</blockquote>\n'
        f'<tg-emoji emoji-id="5470177992950946662">👇</tg-emoji> {user_link}, что ты хочешь?'
    )

    # Buttons layout: КУПИТЬ on top, ПРОДАТЬ below, Manage & History at bottom
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="КУПИТЬ", callback_data="p2p_buy_list", style="success", icon_custom_emoji_id="5287231198098117669")
            ],
            [
                InlineKeyboardButton(text="ПРОДАТЬ", callback_data="p2p_sell_list", style="primary", icon_custom_emoji_id="5445353829304387411")
            ],
            [
                InlineKeyboardButton(text=" ", callback_data="p2p_manage", style="primary", icon_custom_emoji_id="5334882760735598374"),
                InlineKeyboardButton(text=" ", callback_data="p2p_hist_1", style="primary", icon_custom_emoji_id="5956561916573782596")
            ]
        ]
    )

    if isinstance(event, types.CallbackQuery):
        if edit and event.message:
            try:
                await event.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
                return
            except Exception:
                pass
        if event.message:
            try:
                await event.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
            except Exception:
                pass
    elif isinstance(event, types.Message):
        await event.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


def render_p2p_manage(user_id):
    p2p = get_p2p_account(user_id)

    sell_price = p2p["sell_price"]
    sell_active = p2p["sell_order_active"]
    mp_bal = p2p["mp_balance"]

    if sell_price <= 0:
        sell_status = "⚠️"
    elif not sell_active:
        sell_status = "⭕️"
    elif mp_bal <= 0:
        sell_status = "❗️"
    else:
        sell_status = "✅"

    sell_amount = mp_bal if sell_active else 0
    sell_total = sell_amount * sell_price

    buy_price = p2p["buy_price"]
    buy_active = p2p["buy_order_active"]
    mcoin_bal = p2p["mcoin_balance"]

    if buy_price <= 0:
        buy_status = "⚠️"
    elif not buy_active:
        buy_status = "⭕️"
    elif mcoin_bal < buy_price:
        buy_status = "❗️"
    else:
        buy_status = "✅"

    buy_amount = (mcoin_bal // buy_price) if (buy_price > 0 and buy_active) else 0
    buy_total = mcoin_bal if buy_active else 0

    text = (
        '📝 <b>Управление обменом</b>\n'
        '<code>·····················</code>\n'
        f'💵 <b>Баланс:</b> {format_number(mcoin_bal)} m¢ | {format_number(mp_bal)} MP\n'
        '<blockquote><i>ℹ️ Основной и обменный балансы не связаны. Используйте кнопки ниже для пополнения и вывода.</i></blockquote>\n'
        '<b>💰 Заявка на продажу:</b>\n'
        f'├ <b>Курс:</b> 1 MP = {format_number(sell_price)} m¢ {sell_status}\n'
        '├ <b>Налог:</b> 0 m¢ (за 1 ед.)\n'
        f'├ <b>В продаже:</b> {format_number(sell_amount)} MP\n'
        f'└ <b>Получите:</b> {format_number(sell_total)} m¢\n\n'
        '💳 <b>Заявка на покупку:</b>\n'
        f'├ <b>Курс:</b> {format_number(buy_price)} m¢ = 1 MP {buy_status}\n'
        '├ <b>Сбор:</b> 0 m¢ (за 1 ед.)\n'
        f'├ <b>Будет куплено:</b> {format_number(buy_amount)} MP\n'
        f'└ <b>На сумму:</b> {format_number(buy_total)} m¢\n\n'
        '<blockquote><b>Статусы ордеров:</b>\n'
        '<i>✅ — Активен\n'
        '⭕️ — Отключён\n'
        '❗️ — Недостаточно средств\n'
        '⚠️ — Курс не установлен</i></blockquote>\n'
        f'💞 Рейтинг: {p2p["rating"]}'
    )

    sell_btn_text = "🟢 Продажа" if sell_active else "🔴 Продажа"
    buy_btn_text = "🟢 Покупка" if buy_active else "🔴 Покупка"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Изменить курс продажи", callback_data="p2p_set_sell_price")],
            [InlineKeyboardButton(text="Изменить курс покупки", callback_data="p2p_set_buy_price")],
            [
                InlineKeyboardButton(text="📥 Пополнить", callback_data="p2p_deposit"),
                InlineKeyboardButton(text="📤 Вывести", callback_data="p2p_withdraw")
            ],
            [
                InlineKeyboardButton(text=sell_btn_text, callback_data="p2p_toggle_sell"),
                InlineKeyboardButton(text=buy_btn_text, callback_data="p2p_toggle_buy")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_main", icon_custom_emoji_id="5255703720078879038"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="p2p_manage")
            ]
        ]
    )

    return text, keyboard


def render_p2p_history(user_id, page=1):
    per_page = 5
    cursor.execute("SELECT COUNT(*) FROM p2p_deals_history WHERE buyer_id = ? OR seller_id = ?", (user_id, user_id))
    total_cnt = cursor.fetchone()[0]
    total_pages = max(1, math.ceil(total_cnt / per_page))
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    cursor.execute('''
        SELECT buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at 
        FROM p2p_deals_history 
        WHERE buyer_id = ? OR seller_id = ?
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    ''', (user_id, user_id, per_page, offset))
    rows = cursor.fetchall()

    lines = [
        '🧾 <b>ИСТОРИЯ ОБМЕНОВ</b>',
        '<code>·····················</code>'
    ]

    if not rows:
        lines.append('<i>У вас пока нет истории обменов.</i>')
    else:
        for buyer_id, seller_id, amount_mp, price_per_mp, total_mcoin, commission, created_at in rows:
            date_short = format_deal_date(created_at)
            if buyer_id == user_id:
                # Bought (К)
                partner = "Оф. Покупка" if seller_id == 0 else get_user_display_name(seller_id)
                lines.append(f'💰(К) {format_number(amount_mp)} GMP за {format_number(total_mcoin)} m¢')
                lines.append(f'⤷ <i>{partner} · {date_short}</i>')
            else:
                # Sold (П)
                partner = "Оф. Продажа" if buyer_id == 0 else get_user_display_name(buyer_id)
                lines.append(f'💳(П) {format_number(amount_mp)} GMP за {format_number(total_mcoin)} m¢')
                lines.append(f'⤷ <i>{partner} · {date_short}</i>')

    lines.append('<code>·····················</code>')
    lines.append(f'↗️ Показано: {page}/{total_pages}')
    lines.append('<blockquote>ℹ️ (П) —> Продал, (К) —> Купил</blockquote>')

    text = "\n".join(lines)

    buttons = []
    nav_row = []
    if page <= 1:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"p2p_hist_{page-1}"))

    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop_p2p"))

    if page >= total_pages:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"p2p_hist_{page+1}"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="p2p_main", icon_custom_emoji_id="5255703720078879038")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, keyboard


def render_sexchange_menu():
    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    sell_en = settings.get("official_sell_enabled", 1)
    buy_en = settings.get("official_buy_enabled", 1)
    sell_rate = settings.get("official_sell_rate", 10000)
    buy_rate = settings.get("official_buy_rate", 10000)
    r_min = settings.get("rate_min", 7000)
    r_max = settings.get("rate_max", 29000)
    interval = settings.get("interval_minutes", 150)
    last_upd = settings.get("last_update")

    time_left_str = "скоро"
    if last_upd:
        try:
            last_dt = datetime.fromisoformat(last_upd)
            elapsed = (datetime.now() - last_dt).total_seconds()
            remaining = int((interval * 60) - elapsed)
            if remaining > 0:
                mins = remaining // 60
                secs = remaining % 60
                time_left_str = f"{mins}м {secs}с"
            else:
                time_left_str = "сейчас"
        except Exception:
            pass

    sell_status_str = "🟢 ВКЛ" if sell_en else "🔴 ВЫКЛ"
    buy_status_str = "🟢 ВКЛ" if buy_en else "🔴 ВЫКЛ"

    text = (
        '⚙️ <b>НАСТРОЙКИ ОБМЕННИКА (/sexchange)</b>\n'
        '<code>································</code>\n'
        '📈 <b>Официальные курсы (1 MP):</b>\n'
        f'├ 💰 <b>Оф. Продажа (бот выкупает):</b> <b>{format_number(sell_rate)} m¢</b> ({sell_status_str})\n'
        f'└ 🛍 <b>Оф. Покупка (бот продает):</b> <b>{format_number(buy_rate)} m¢</b> ({buy_status_str})\n\n'
        '📊 <b>Параметры авто-курса:</b>\n'
        f'├ <b>Диапазон:</b> <b>{format_number(r_min)} — {format_number(r_max)} m¢</b>\n'
        f'├ <b>Интервал:</b> <b>{interval} мин. ({interval // 60}ч {interval % 60}м)</b>\n'
        f'└ <b>След. обновление:</b> <b>{time_left_str}</b>'
    )

    btn_sell_tog = "🔴 Отключить оф. продажу" if sell_en else "🟢 Включить оф. продажу"
    btn_buy_tog = "🔴 Отключить оф. покупку" if buy_en else "🟢 Включить оф. покупку"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=btn_sell_tog, callback_data="sexch_tog_sell")
            ],
            [
                InlineKeyboardButton(text=btn_buy_tog, callback_data="sexch_tog_buy")
            ],
            [
                InlineKeyboardButton(text="🎲 Сгенерировать курс сейчас", callback_data="sexch_reroll")
            ],
            [
                InlineKeyboardButton(text="✏️ Курс продажи", callback_data="sexch_set_sell"),
                InlineKeyboardButton(text="✏️ Курс покупки", callback_data="sexch_set_buy")
            ],
            [
                InlineKeyboardButton(text="📊 Диапазон", callback_data="sexch_set_range"),
                InlineKeyboardButton(text="⏱ Интервал", callback_data="sexch_set_interval")
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить меню", callback_data="sexch_refresh")
            ]
        ]
    )
    return text, keyboard


# --- P2P COMMAND HANDLERS ---

@dp.message(Command("exchange", "обменник"))
@dp.message(lambda message: message.text and message.text.strip().lower() in ["обменник", "/exchange", "exchange", "/обменник"])
async def cmd_exchange(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Перейти в обменник",
                        url=f"https://t.me/{BOT_USERNAME}?start=p2p"
                    )
                ]
            ]
        )
        await message.reply("<i>Эта команда работает только в личных сообщениях!</i>", reply_markup=keyboard, parse_mode=ParseMode.HTML)
        return

    await show_p2p_main(message, user_id=message.from_user.id, first_name=message.from_user.first_name)


@dp.message(Command("sexchange", "setexchange"))
@dp.message(lambda message: message.text and message.text.strip().lower() in ["/sexchange", "sexchange", "/setexchange", "setexchange"])
async def cmd_sexchange(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    text, keyboard = render_sexchange_menu()
    await message.reply(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


# --- P2P CALLBACK QUERIES ---

@dp.callback_query(lambda c: c.data == "p2p_main")
async def cb_p2p_main(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer()
    await show_p2p_main(callback, user_id=callback.from_user.id, first_name=callback.from_user.first_name, edit=True)


@dp.callback_query(lambda c: c.data in ["p2p_history", "p2p_hist_1"] or c.data.startswith("p2p_hist_"))
async def cb_p2p_history(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer()
    page = 1
    if callback.data.startswith("p2p_hist_"):
        try:
            page = int(callback.data.replace("p2p_hist_", ""))
        except Exception:
            page = 1
    text, kb = render_p2p_history(callback.from_user.id, page=page)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_manage")
async def cb_p2p_manage(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer()
    text, kb = render_p2p_manage(callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_toggle_sell")
async def cb_p2p_toggle_sell(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    p2p = get_p2p_account(user_id)
    new_val = 0 if p2p["sell_order_active"] else 1
    cursor.execute("UPDATE p2p_accounts SET sell_order_active = ? WHERE user_id = ?", (new_val, user_id))
    conn.commit()
    text, kb = render_p2p_manage(user_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_toggle_buy")
async def cb_p2p_toggle_buy(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    p2p = get_p2p_account(user_id)
    new_val = 0 if p2p["buy_order_active"] else 1
    cursor.execute("UPDATE p2p_accounts SET buy_order_active = ? WHERE user_id = ?", (new_val, user_id))
    conn.commit()
    text, kb = render_p2p_manage(user_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- BUY MP HANDLERS ---

@dp.callback_query(lambda c: c.data in ["p2p_buy_list"] or c.data.startswith("p2p_buy_page_"))
async def cb_p2p_buy_list(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer()
    page = 1
    if callback.data.startswith("p2p_buy_page_"):
        try:
            page = int(callback.data.replace("p2p_buy_page_", ""))
        except Exception:
            page = 1

    current_uid = callback.from_user.id
    cursor.execute('''
        SELECT p.user_id, p.sell_price, p.mp_balance, p.rating 
        FROM p2p_accounts p 
        WHERE p.sell_order_active = 1 AND p.sell_price > 0 AND p.mp_balance > 0 AND p.user_id != ?
        ORDER BY p.sell_price ASC
    ''', (current_uid,))
    sellers = cursor.fetchall()

    per_page = 8
    total_pages = max(1, math.ceil(len(sellers) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    page_sellers = sellers[start_idx:start_idx + per_page]

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    buy_enabled = settings.get("official_buy_enabled", 1)
    buy_rate = settings.get("official_buy_rate", 10000)

    text = (
        '<b>⚡️ КУПИТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        '<blockquote expandable><i> ℹ️ MPOINT можно приобрести как у официального бота, так и у других игроков. Выгодные предложения находятся в верхней части списка.</i></blockquote>\n'
        f'<blockquote>📊 Оф. курс покупки: <b>1 MP</b> ≈ {format_number(buy_rate)} mCoin</blockquote>'
    )

    buttons = []
    if page == 1 and buy_enabled:
        buttons.append([
            InlineKeyboardButton(
                text=f"🏛 Оф. покупка • {format_number(buy_rate)} m¢",
                callback_data="p2p_buy_bot",
                icon_custom_emoji_id="5206607081334906820"
            )
        ])

    for s_uid, s_price, s_mp, s_rating in page_sellers:
        display_name = get_user_display_name(s_uid)
        buttons.append([
            InlineKeyboardButton(
                text=f"{display_name} • {format_number(s_price)} m¢",
                callback_data=f"p2p_buy_seller_{s_uid}"
            )
        ])

    nav_row = []
    if page <= 1:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"p2p_buy_page_{page-1}"))

    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop_p2p"))

    if page >= total_pages:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"p2p_buy_page_{page+1}"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="p2p_main", icon_custom_emoji_id="5255703720078879038")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


# --- OFFICIAL BUY FROM BOT ---

@dp.callback_query(lambda c: c.data == "p2p_buy_bot")
async def cb_p2p_buy_bot(callback: types.CallbackQuery):
    await callback.answer()
    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    if not settings.get("official_buy_enabled", 1):
        await callback.answer("Официальная покупка сейчас отключена!", show_alert=True)
        return

    buy_rate = settings.get("official_buy_rate", 10000)
    user_u = get_user(callback.from_user.id)
    user_balance = user_u.get("balance", 0) or 0
    max_affordable_mp = user_balance // buy_rate if buy_rate > 0 else 0

    text = (
        '<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji><b>КУПИТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        '<i>👤 Продавец: 🏛 Оф. Покупка\n'
        '💞 Рейтинг: 100%\n'
        '❇️ Доступно: ∞ MP\n'
        f'💸 Курс: 1 MP = {format_number(buy_rate)} mCoin\n'
        '·····················\n'
        '🔘 Комиссия: 0%</i>\n'
        f'<blockquote>💰 Твой баланс: <b>{format_number(user_balance)} mCoin</b> (хватит на <b>{format_number(max_affordable_mp)} MP</b>)</blockquote>\n'
        '<blockquote><i>ℹ️ Выберите количество MP или напишите в чат.</i></blockquote>'
    )

    p2p_user_action[callback.from_user.id] = "buy_bot_custom"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 MP", callback_data="p2p_bbot_1"),
                InlineKeyboardButton(text="5 MP", callback_data="p2p_bbot_5"),
                InlineKeyboardButton(text="10 MP", callback_data="p2p_bbot_10"),
            ],
            [
                InlineKeyboardButton(text="25 MP", callback_data="p2p_bbot_25"),
                InlineKeyboardButton(text="50 MP", callback_data="p2p_bbot_50"),
                InlineKeyboardButton(text="100 MP", callback_data="p2p_bbot_100"),
            ],
            [
                InlineKeyboardButton(text="Ввести сумму ✏️", callback_data="p2p_bbot_custom")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_buy_list", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_bbot_custom")
async def cb_p2p_bbot_custom(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "buy_bot_custom"
    text = (
        '💎 <b>КУПИТЬ MPOINT (ОФ. ПОКУПКА)</b>\n'
        '<code>·····················</code>\n'
        '<i>Напишите в ответ или в чат количество MPOINT, которое хотите купить:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_buy_bot", icon_custom_emoji_id="5255703720078879038")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_bbot_"))
async def cb_p2p_buy_bot_amount(callback: types.CallbackQuery):
    try:
        amount_mp = int(callback.data.replace("p2p_bbot_", ""))
    except Exception:
        return

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    buy_rate = settings.get("official_buy_rate", 10000)
    user_u = get_user(callback.from_user.id)
    total_cost = amount_mp * buy_rate

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    if user_u["balance"] < total_cost:
        shortage = total_cost - user_u["balance"]
        err_text = f"🚫 <i>{user_link}, не хватает <b>{format_number(shortage)} mCoin!</b></i>"
        err_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_buy_bot", icon_custom_emoji_id="5255703720078879038")]]
        )
        try:
            await callback.message.edit_text(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        except Exception:
            await callback.message.answer(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        return

    conf_text = (
        '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ ПОКУПКИ</b>\n'
        '<code>·····················</code>\n'
        f'<i>{user_link}, ты точно <b>хочешь купить {format_number(amount_mp)} MPOINT</b> за <b>{format_number(total_cost)} mCoin?</b></i>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Оф. курс: {format_number(buy_rate)} mCoin</blockquote>\n'
        '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
    )
    conf_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_bbot_{amount_mp}", style="success"),
                InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
            ]
        ]
    )
    try:
        await callback.message.edit_text(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_conf_bbot_"))
async def cb_p2p_confirm_buy_bot(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount_mp = int(callback.data.replace("p2p_conf_bbot_", ""))
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    ok, res = execute_p2p_buy_from_bot(callback.from_user.id, amount_mp)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    succ_text = (
        '🎉 <b>УСПЕШНАЯ ПОКУПКА MPOINT</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, ты успешно купил <b>{format_number(res["amount_mp"])} MPOINT</b> за <b>{format_number(res["total_mcoin"])} mCoin!</b>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(res["rate"])} mCoin</blockquote>\n'
        f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> MPOINT зачислены на твой основной баланс.'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_0_1", icon_custom_emoji_id="5393194986252542669", style="success"),
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_0_-1", icon_custom_emoji_id="5382261056078881010", style="danger")
            ],
            [
                InlineKeyboardButton(text="В меню обмена", callback_data="p2p_main", style="primary")
            ]
        ]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_buy_seller_"))
async def cb_p2p_buy_seller(callback: types.CallbackQuery):
    try:
        seller_id = int(callback.data.replace("p2p_buy_seller_", ""))
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    seller_p2p = get_p2p_account(seller_id)
    if not seller_p2p["sell_order_active"] or seller_p2p["mp_balance"] <= 0 or seller_p2p["sell_price"] <= 0:
        await callback.answer("У этого продавца больше нет активных заявок!", show_alert=True)
        return

    display_name = get_user_display_name(seller_id)
    text = (
        '<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji><b>КУПИТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        f'<i>👤 Продавец: <a href="tg://user?id={seller_id}">{display_name}</a>\n'
        f'💞 Рейтинг: {seller_p2p["rating"]}\n'
        f'❇️ Доступно: {format_number(seller_p2p["mp_balance"])} MPOINT\n'
        f'💸 Курс: 1 MP = {format_number(seller_p2p["sell_price"])} mCoin\n'
        '·····················\n'
        '🔘 Комиссия: 0%</i>\n'
        '<blockquote><i>ℹ️ Напишите в ответ на это сообщение, сколько вы хотите купить, или выберите один из вариантов ниже.</i></blockquote>'
    )

    p2p_user_action[callback.from_user.id] = f"buy_seller_{seller_id}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 MP", callback_data=f"p2p_bamt_{seller_id}_1"),
                InlineKeyboardButton(text="5 MP", callback_data=f"p2p_bamt_{seller_id}_5"),
                InlineKeyboardButton(text="10 MP", callback_data=f"p2p_bamt_{seller_id}_10"),
            ],
            [
                InlineKeyboardButton(text="25 MP", callback_data=f"p2p_bamt_{seller_id}_25"),
                InlineKeyboardButton(text="50 MP", callback_data=f"p2p_bamt_{seller_id}_50"),
                InlineKeyboardButton(text="100 MP", callback_data=f"p2p_bamt_{seller_id}_100"),
            ],
            [
                InlineKeyboardButton(text="Все MP", callback_data=f"p2p_bamt_{seller_id}_{seller_p2p['mp_balance']}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_buy_list", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_bamt_"))
async def cb_p2p_buy_amount_choice(callback: types.CallbackQuery):
    parts = callback.data.replace("p2p_bamt_", "").split("_")
    if len(parts) < 2:
        return
    seller_id = int(parts[0])
    amount_mp = int(parts[1])

    seller_p2p = get_p2p_account(seller_id)
    buyer_id = callback.from_user.id
    buyer_u = get_user(buyer_id)
    buyer_link = get_user_mention(buyer_id, callback.from_user.first_name)

    total_cost = amount_mp * seller_p2p["sell_price"]
    if buyer_u["balance"] < total_cost:
        shortage = total_cost - buyer_u["balance"]
        err_text = f"🚫 <i>{buyer_link}, не хватает <b>{format_number(shortage)} mCoin!</b></i>"
        err_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=f"p2p_buy_seller_{seller_id}", icon_custom_emoji_id="5255703720078879038")]]
        )
        try:
            await callback.message.edit_text(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        except Exception:
            await callback.message.answer(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        return

    conf_text = (
        '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
        '<code>·····················</code>\n'
        f'<i>{buyer_link}, ты точно <b>хочешь купить {format_number(amount_mp)} MPOINT</b> за <b>{format_number(total_cost)} mCoin?</b></i>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(seller_p2p["sell_price"])} mCoin</blockquote>\n'
        '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
    )
    conf_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_buy_{seller_id}_{amount_mp}", style="success"),
                InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
            ]
        ]
    )
    try:
        await callback.message.edit_text(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_cancel_buy")
async def cb_p2p_cancel_buy(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer("Сделка отменена!")
    await show_p2p_main(callback, user_id=callback.from_user.id, first_name=callback.from_user.first_name, edit=True)


@dp.callback_query(lambda c: c.data.startswith("p2p_conf_buy_"))
async def cb_p2p_confirm_buy(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    parts = callback.data.replace("p2p_conf_buy_", "").split("_")
    if len(parts) < 2:
        return
    seller_id = int(parts[0])
    amount_mp = int(parts[1])

    buyer_id = callback.from_user.id
    ok, res = execute_p2p_buy_deal(buyer_id, seller_id, amount_mp)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    seller_name = get_user_display_name(seller_id)
    buyer_link = get_user_mention(buyer_id, callback.from_user.first_name)
    succ_text = (
        '🎉 <b>УСПЕШНАЯ СДЕЛКА</b>\n'
        '<code>·····················</code>\n'
        f'{buyer_link}, ты успешно купил <b>{format_number(res["amount_mp"])} MPOINT</b> у <a href="tg://user?id={seller_id}">{seller_name}</a> за <b>{format_number(res["total_mcoin"])} mCoin!</b>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(res["rate"])} mCoin</blockquote>\n'
        f'<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji> MPOINT зачислены на твой основной баланс.'
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_{seller_id}_1", icon_custom_emoji_id="5393194986252542669", style="success"),
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_{seller_id}_-1", icon_custom_emoji_id="5382261056078881010", style="danger")
            ],
            [
                InlineKeyboardButton(text="В меню обмена", callback_data="p2p_main", style="primary")
            ]
        ]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_rate_"))
async def cb_p2p_rate_deal(callback: types.CallbackQuery):
    parts = callback.data.replace("p2p_rate_", "").split("_")
    if len(parts) < 3:
        return
    deal_id = int(parts[0])
    target_id = int(parts[1])
    delta = int(parts[2])
    rater_id = callback.from_user.id

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute('''
            INSERT INTO p2p_deal_ratings (deal_id, rater_id, target_id, rating_change, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (deal_id, rater_id, target_id, delta, now_str))
        if target_id == 0:
            cursor.execute("INSERT INTO p2p_accounts (user_id, rating) VALUES (0, 100) ON CONFLICT (user_id) DO UPDATE SET rating = p2p_accounts.rating + ?", (delta,))
        else:
            cursor.execute("UPDATE p2p_accounts SET rating = rating + ? WHERE user_id = ?", (delta, target_id))
        conn.commit()
        await callback.answer("Спасибо за отзыв!", show_alert=True)
    except Exception:
        await callback.answer("Вы уже оставили отзыв по этой сделке!", show_alert=True)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="В меню обмена", callback_data="p2p_main", style="primary")]]
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass


# --- SELL MP HANDLERS ---

@dp.callback_query(lambda c: c.data in ["p2p_sell_list"] or c.data.startswith("p2p_sell_page_"))
async def cb_p2p_sell_list(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    await callback.answer()
    page = 1
    if callback.data.startswith("p2p_sell_page_"):
        try:
            page = int(callback.data.replace("p2p_sell_page_", ""))
        except Exception:
            page = 1

    current_uid = callback.from_user.id
    cursor.execute('''
        SELECT p.user_id, p.buy_price, p.mcoin_balance, p.rating 
        FROM p2p_accounts p 
        WHERE p.buy_order_active = 1 AND p.buy_price > 0 AND p.mcoin_balance >= p.buy_price AND p.user_id != ?
        ORDER BY p.buy_price DESC
    ''', (current_uid,))
    buyers = cursor.fetchall()

    per_page = 8
    total_pages = max(1, math.ceil(len(buyers) / per_page))
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    page_buyers = buyers[start_idx:start_idx + per_page]

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    sell_enabled = settings.get("official_sell_enabled", 1)
    sell_rate = settings.get("official_sell_rate", 10000)

    text = (
        '<b><tg-emoji emoji-id="5834775782433492415">🔥</tg-emoji> ПРОДАТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        '<blockquote expandable><i> ℹ️ Вы можете продать MPOINT как официальному боту, так и другим игрокам. Цена отображается уже с учётом всех параметров.</i></blockquote>\n'
        f'<blockquote><tg-emoji emoji-id="5431577498364158238">📊</tg-emoji> Оф. курс продажи: <b>1 MP</b> ≈ {format_number(sell_rate)} mCoin</blockquote>'
    )

    buttons = []
    if page == 1 and sell_enabled:
        buttons.append([
            InlineKeyboardButton(
                text=f"🏛 Оф. продажа • {format_number(sell_rate)} m¢",
                callback_data="p2p_sell_bot",
                icon_custom_emoji_id="5206607081334906820"
            )
        ])

    for b_uid, b_price, b_mcoin, b_rating in page_buyers:
        display_name = get_user_display_name(b_uid)
        buttons.append([
            InlineKeyboardButton(
                text=f"{display_name} • {format_number(b_price)} m¢",
                callback_data=f"p2p_sell_buyer_{b_uid}"
            )
        ])

    nav_row = []
    if page <= 1:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"p2p_sell_page_{page-1}"))

    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop_p2p"))

    if page >= total_pages:
        nav_row.append(InlineKeyboardButton(text="🚫", callback_data="noop_p2p"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"p2p_sell_page_{page+1}"))

    buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="p2p_main", icon_custom_emoji_id="5255703720078879038")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_sell_bot")
async def cb_p2p_sell_bot(callback: types.CallbackQuery):
    await callback.answer()
    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    if not settings.get("official_sell_enabled", 1):
        await callback.answer("Официальная продажа сейчас отключена!", show_alert=True)
        return

    deals_24h, mp_sold_24h = get_p2p_bot_24h_stats()
    user_u = get_user(callback.from_user.id)
    user_mp = user_u.get("mp_balance", 0) or 0
    sell_rate = settings.get("official_sell_rate", 10000)

    text = (
        '<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji><b>ПРОДАТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        '<i>👤 Покупатель: 🏛 Оф. Продажа\n'
        '💞 Рейтинг: 100%\n'
        '❇️ Доступно: ∞ mCoin\n'
        f'💸 Курс: 1 MP = {format_number(sell_rate)} mCoin\n'
        '·····················\n'
        '🔘 Комиссия: 0%</i>\n'
        f'<blockquote><tg-emoji emoji-id="5332455502917949981">🏦</tg-emoji> <b>Продано за 24ч:</b> {deals_24h} / {format_number(mp_sold_24h)} MP</blockquote>\n'
        f'<blockquote>💰 Твой баланс: <b>{format_number(user_mp)} MP</b></blockquote>\n'
        '<blockquote><i>ℹ️ Выберите количество MP или напишите в чат.</i></blockquote>'
    )

    p2p_user_action[callback.from_user.id] = "sell_bot_custom"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 MP", callback_data="p2p_sbot_1"),
                InlineKeyboardButton(text="5 MP", callback_data="p2p_sbot_5"),
                InlineKeyboardButton(text="10 MP", callback_data="p2p_sbot_10"),
            ],
            [
                InlineKeyboardButton(text="25 MP", callback_data="p2p_sbot_25"),
                InlineKeyboardButton(text="50 MP", callback_data="p2p_sbot_50"),
                InlineKeyboardButton(text="100 MP", callback_data="p2p_sbot_100"),
            ],
            [
                InlineKeyboardButton(text="Все MP", callback_data=f"p2p_sbot_{user_mp}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_sell_list", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_sbot_"))
async def cb_p2p_sell_bot_amount(callback: types.CallbackQuery):
    try:
        amount_mp = int(callback.data.replace("p2p_sbot_", ""))
    except Exception:
        return

    update_p2p_rate_fluctuation(force=False)
    settings = get_p2p_settings()
    sell_rate = settings.get("official_sell_rate", 10000)
    user_u = get_user(callback.from_user.id)
    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)

    if user_u.get("mp_balance", 0) < amount_mp:
        shortage = amount_mp - user_u.get("mp_balance", 0)
        err_text = f"🚫 <i>{user_link}, не хватает <b>{format_number(shortage)} MP!</b></i>"
        err_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_sell_bot", icon_custom_emoji_id="5255703720078879038")]]
        )
        try:
            await callback.message.edit_text(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        except Exception:
            await callback.message.answer(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        return

    total_mcoin = amount_mp * sell_rate
    conf_text = (
        '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
        '<code>·····················</code>\n'
        f'<i>{user_link}, ты точно <b>хочешь продать {format_number(amount_mp)} MPOINT</b> за <b>{format_number(total_mcoin)} mCoin?</b></i>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Оф. курс: {format_number(sell_rate)} mCoin</blockquote>\n'
        '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
    )
    conf_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_sbot_{amount_mp}", style="success"),
                InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
            ]
        ]
    )
    try:
        await callback.message.edit_text(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_conf_sbot_"))
async def cb_p2p_confirm_sell_bot(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount_mp = int(callback.data.replace("p2p_conf_sbot_", ""))
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    ok, res = execute_p2p_sell_to_bot(callback.from_user.id, amount_mp)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    succ_text = (
        '🎉 <b>УСПЕШНАЯ ПРОДАЖА</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, ты успешно продал <b>{format_number(res["amount_mp"])} MPOINT</b> за <b>{format_number(res["total_mcoin"])} mCoin!</b>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(res["rate"])} mCoin</blockquote>\n'
        f'<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: <b>{format_number(get_user(callback.from_user.id)["balance"])} mCoin</b>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_0_1", icon_custom_emoji_id="5393194986252542669", style="success"),
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_0_-1", icon_custom_emoji_id="5382261056078881010", style="danger")
            ],
            [
                InlineKeyboardButton(text="В меню обмена", callback_data="p2p_main", style="primary")
            ]
        ]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_sell_buyer_"))
async def cb_p2p_sell_buyer(callback: types.CallbackQuery):
    try:
        buyer_id = int(callback.data.replace("p2p_sell_buyer_", ""))
    except Exception:
        await callback.answer("Ошибка!", show_alert=True)
        return

    buyer_p2p = get_p2p_account(buyer_id)
    if not buyer_p2p["buy_order_active"] or buyer_p2p["mcoin_balance"] <= 0 or buyer_p2p["buy_price"] <= 0:
        await callback.answer("У этого покупателя больше нет активных заявок!", show_alert=True)
        return

    display_name = get_user_display_name(buyer_id)
    max_mp_buyer_can_buy = buyer_p2p["mcoin_balance"] // buyer_p2p["buy_price"]
    seller_u = get_user(callback.from_user.id)
    seller_mp = seller_u.get("mp_balance", 0) or 0

    text = (
        '<tg-emoji emoji-id="5307594157739515229">💎</tg-emoji><b>ПРОДАТЬ MPOINT</b>\n'
        '<code>·····················</code>\n'
        f'<i>👤 Покупатель: <a href="tg://user?id={buyer_id}">{display_name}</a>\n'
        f'💞 Рейтинг: {buyer_p2p["rating"]}\n'
        f'❇️ Готов купить: до {format_number(max_mp_buyer_can_buy)} MP\n'
        f'💸 Курс: 1 MP = {format_number(buyer_p2p["buy_price"])} mCoin\n'
        '·····················\n'
        '🔘 Комиссия: 0%</i>\n'
        '<blockquote><i>ℹ️ Напишите в ответ на это сообщение, сколько вы хотите продать, или выберите один из вариантов ниже.</i></blockquote>'
    )

    p2p_user_action[callback.from_user.id] = f"sell_buyer_{buyer_id}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 MP", callback_data=f"p2p_sbuy_{buyer_id}_1"),
                InlineKeyboardButton(text="5 MP", callback_data=f"p2p_sbuy_{buyer_id}_5"),
                InlineKeyboardButton(text="10 MP", callback_data=f"p2p_sbuy_{buyer_id}_10"),
            ],
            [
                InlineKeyboardButton(text="25 MP", callback_data=f"p2p_sbuy_{buyer_id}_25"),
                InlineKeyboardButton(text="50 MP", callback_data=f"p2p_sbuy_{buyer_id}_50"),
                InlineKeyboardButton(text="100 MP", callback_data=f"p2p_sbuy_{buyer_id}_100"),
            ],
            [
                InlineKeyboardButton(text="Все мои MP", callback_data=f"p2p_sbuy_{buyer_id}_{min(seller_mp, max_mp_buyer_can_buy)}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_sell_list", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_sbuy_"))
async def cb_p2p_sell_buyer_amount(callback: types.CallbackQuery):
    parts = callback.data.replace("p2p_sbuy_", "").split("_")
    if len(parts) < 2:
        return
    buyer_id = int(parts[0])
    amount_mp = int(parts[1])

    buyer_p2p = get_p2p_account(buyer_id)
    seller_id = callback.from_user.id
    seller_u = get_user(seller_id)
    seller_link = get_user_mention(seller_id, callback.from_user.first_name)

    if seller_u.get("mp_balance", 0) < amount_mp:
        shortage = amount_mp - seller_u.get("mp_balance", 0)
        err_text = f"🚫 <i>{seller_link}, не хватает <b>{format_number(shortage)} MP!</b></i>"
        err_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=f"p2p_sell_buyer_{buyer_id}", icon_custom_emoji_id="5255703720078879038")]]
        )
        try:
            await callback.message.edit_text(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        except Exception:
            await callback.message.answer(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
        return

    total_mcoin = amount_mp * buyer_p2p["buy_price"]
    conf_text = (
        '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
        '<code>·····················</code>\n'
        f'<i>{seller_link}, ты точно <b>хочешь продать {format_number(amount_mp)} MPOINT</b> за <b>{format_number(total_mcoin)} mCoin?</b></i>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(buyer_p2p["buy_price"])} mCoin</blockquote>\n'
        '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
    )
    conf_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_sbuy_{buyer_id}_{amount_mp}", style="success"),
                InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
            ]
        ]
    )
    try:
        await callback.message.edit_text(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_conf_sbuy_"))
async def cb_p2p_confirm_sell_buyer(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    parts = callback.data.replace("p2p_conf_sbuy_", "").split("_")
    if len(parts) < 2:
        return
    buyer_id = int(parts[0])
    amount_mp = int(parts[1])

    seller_id = callback.from_user.id
    ok, res = execute_p2p_sell_to_buyer(seller_id, buyer_id, amount_mp)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    buyer_name = get_user_display_name(buyer_id)
    seller_link = get_user_mention(seller_id, callback.from_user.first_name)
    succ_text = (
        '🎉 <b>УСПЕШНАЯ СДЕЛКА</b>\n'
        '<code>·····················</code>\n'
        f'{seller_link}, ты успешно продал <b>{format_number(res["amount_mp"])} MPOINT</b> покупателю <a href="tg://user?id={buyer_id}">{buyer_name}</a> за <b>{format_number(res["total_mcoin"])} mCoin!</b>\n'
        f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(res["rate"])} mCoin</blockquote>\n'
        f'<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: <b>{format_number(get_user(seller_id)["balance"])} mCoin</b>'
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_{buyer_id}_1", icon_custom_emoji_id="5393194986252542669", style="success"),
                InlineKeyboardButton(text="1", callback_data=f"p2p_rate_{res['deal_id']}_{buyer_id}_-1", icon_custom_emoji_id="5382261056078881010", style="danger")
            ],
            [
                InlineKeyboardButton(text="В меню обмена", callback_data="p2p_main", style="primary")
            ]
        ]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- P2P DEPOSIT & WITHDRAW HANDLERS ---

@dp.callback_query(lambda c: c.data == "p2p_deposit")
async def cb_p2p_deposit(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        '📥 <b>ПОПОЛНЕНИЕ БАЛАНСА ОБМЕННИКА</b>\n'
        '<code>·····················</code>\n'
        '<i>Выберите, какую валюту вы хотите перевести с основного баланса на баланс обменника:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="mCoin 💰", callback_data="p2p_dep_mcoin"),
                InlineKeyboardButton(text="MPOINT 💎", callback_data="p2p_dep_mpoint")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_manage", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_dep_mcoin")
async def cb_p2p_dep_mcoin(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "dep_mcoin"
    user_u = get_user(callback.from_user.id)
    bal = user_u.get("balance", 0)
    p2p = get_p2p_account(callback.from_user.id)
    today_str = get_msk_today_str()
    is_first = (p2p["last_deposit_date"] != today_str)
    comm_str = "<b>0% (1-е пополнение за день)</b>" if is_first else "<b>1.5%</b>"

    text = (
        '📥 <b>ПОПОЛНЕНИЕ mCoin</b>\n'
        '<code>·····················</code>\n'
        f'💰 Основной баланс: <b>{format_number(bal)} m¢</b>\n'
        f'🔘 Комиссия: {comm_str}\n\n'
        '<i>Напишите в ответ или в чат сумму mCoin для пополнения, или выберите вариант:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="100k", callback_data="p2p_dmc_100000"),
                InlineKeyboardButton(text="500k", callback_data="p2p_dmc_500000"),
                InlineKeyboardButton(text="1M", callback_data="p2p_dmc_1000000")
            ],
            [
                InlineKeyboardButton(text="5M", callback_data="p2p_dmc_5000000"),
                InlineKeyboardButton(text="10M", callback_data="p2p_dmc_10000000"),
                InlineKeyboardButton(text="Все m¢", callback_data=f"p2p_dmc_{bal}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_deposit", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_dmc_"))
async def cb_p2p_dep_mcoin_amount(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount = int(callback.data.replace("p2p_dmc_", ""))
    except Exception:
        return
    ok, res = deposit_to_p2p_mcoin(callback.from_user.id, amount)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    comm_str = "<blockquote><b>🆓 Комиссия не списана (1-е пополнение за день)</b></blockquote>" if res["is_first"] else f"<blockquote><b>🔘 Комиссия: 1,5% ({format_number(res['fee'])} mCoin)</b></blockquote>"
    succ_text = (
        '✅ <b>УСПЕШНОЕ ПОПОЛНЕНИЕ</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, твои <b>{format_number(amount)} mCoin</b> зачислены на баланс обменника.\n'
        f'{comm_str}\n'
        f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>\n'
        f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_dep_mpoint")
async def cb_p2p_dep_mpoint(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "dep_mpoint"
    user_u = get_user(callback.from_user.id)
    bal = user_u.get("mp_balance", 0)

    text = (
        '📥 <b>ПОПОЛНЕНИЕ MPOINT</b>\n'
        '<code>·····················</code>\n'
        f'💎 Основной баланс: <b>{format_number(bal)} MP</b>\n'
        '🔘 Комиссия: <b>0%</b>\n\n'
        '<i>Напишите в ответ или в чат количество MPOINT для пополнения, или выберите вариант:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5 MP", callback_data="p2p_dmp_5"),
                InlineKeyboardButton(text="10 MP", callback_data="p2p_dmp_10"),
                InlineKeyboardButton(text="25 MP", callback_data="p2p_dmp_25")
            ],
            [
                InlineKeyboardButton(text="50 MP", callback_data="p2p_dmp_50"),
                InlineKeyboardButton(text="100 MP", callback_data="p2p_dmp_100"),
                InlineKeyboardButton(text="Все MP", callback_data=f"p2p_dmp_{bal}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_deposit", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_dmp_"))
async def cb_p2p_dep_mpoint_amount(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount = int(callback.data.replace("p2p_dmp_", ""))
    except Exception:
        return
    ok, res = deposit_to_p2p_mpoint(callback.from_user.id, amount)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    succ_text = (
        '✅ <b>УСПЕШНОЕ ПОПОЛНЕНИЕ</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, твои <b>{format_number(amount)} MPOINT</b> зачислены на баланс обменника.\n'
        '<blockquote><b>🆓 Комиссия не списана</b></blockquote>\n'
        f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>\n'
        f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_withdraw")
async def cb_p2p_withdraw(callback: types.CallbackQuery):
    await callback.answer()
    text = (
        '📤 <b>ВЫВОД С БАЛАНСА ОБМЕННИКА</b>\n'
        '<code>·····················</code>\n'
        '<i>Выберите, какую валюту вы хотите вывести с обменника на основной баланс:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="mCoin 💰", callback_data="p2p_wth_mcoin"),
                InlineKeyboardButton(text="MPOINT 💎", callback_data="p2p_wth_mpoint")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_manage", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_wth_mcoin")
async def cb_p2p_wth_mcoin(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "wth_mcoin"
    p2p = get_p2p_account(callback.from_user.id)
    bal = p2p["mcoin_balance"]

    text = (
        '📤 <b>ВЫВОД mCoin</b>\n'
        '<code>·····················</code>\n'
        f'💰 На балансе обменника: <b>{format_number(bal)} m¢</b>\n\n'
        '<i>Напишите в ответ или в чат сумму mCoin для вывода, или выберите вариант:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="100k", callback_data="p2p_wmc_100000"),
                InlineKeyboardButton(text="500k", callback_data="p2p_wmc_500000"),
                InlineKeyboardButton(text="1M", callback_data="p2p_wmc_1000000")
            ],
            [
                InlineKeyboardButton(text="5M", callback_data="p2p_wmc_5000000"),
                InlineKeyboardButton(text="10M", callback_data="p2p_wmc_10000000"),
                InlineKeyboardButton(text="Все m¢", callback_data=f"p2p_wmc_{bal}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_withdraw", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_wmc_"))
async def cb_p2p_wth_mcoin_amount(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount = int(callback.data.replace("p2p_wmc_", ""))
    except Exception:
        return
    ok, res = withdraw_from_p2p_mcoin(callback.from_user.id, amount)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    succ_text = (
        '✅ <b>УСПЕШНЫЙ ВЫВОД</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, твои <b>{format_number(amount)} mCoin</b> выведены на основной баланс.\n'
        f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_wth_mpoint")
async def cb_p2p_wth_mpoint(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "wth_mpoint"
    p2p = get_p2p_account(callback.from_user.id)
    bal = p2p["mp_balance"]

    text = (
        '📤 <b>ВЫВОД MPOINT</b>\n'
        '<code>·····················</code>\n'
        f'💎 На балансе обменника: <b>{format_number(bal)} MP</b>\n\n'
        '<i>Напишите в ответ или в чат количество MPOINT для вывода, или выберите вариант:</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5 MP", callback_data="p2p_wmp_5"),
                InlineKeyboardButton(text="10 MP", callback_data="p2p_wmp_10"),
                InlineKeyboardButton(text="25 MP", callback_data="p2p_wmp_25")
            ],
            [
                InlineKeyboardButton(text="50 MP", callback_data="p2p_wmp_50"),
                InlineKeyboardButton(text="100 MP", callback_data="p2p_wmp_100"),
                InlineKeyboardButton(text="Все MP", callback_data=f"p2p_wmp_{bal}")
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="p2p_withdraw", icon_custom_emoji_id="5255703720078879038")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("p2p_wmp_"))
async def cb_p2p_wth_mpoint_amount(callback: types.CallbackQuery):
    p2p_user_action.pop(callback.from_user.id, None)
    try:
        amount = int(callback.data.replace("p2p_wmp_", ""))
    except Exception:
        return
    ok, res = withdraw_from_p2p_mpoint(callback.from_user.id, amount)
    if not ok:
        await callback.answer(f"Ошибка: {res}", show_alert=True)
        return

    user_link = get_user_mention(callback.from_user.id, callback.from_user.first_name)
    succ_text = (
        '✅ <b>УСПЕШНЫЙ ВЫВОД</b>\n'
        '<code>·····················</code>\n'
        f'{user_link}, твои <b>{format_number(amount)} MPOINT</b> выведены на основной баланс.\n'
        f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
    )
    try:
        await callback.message.edit_text(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- SET PRICE HANDLERS ---

@dp.callback_query(lambda c: c.data == "p2p_set_sell_price")
async def cb_p2p_set_sell_price(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "set_sell_price"
    text = (
        '💰 <b>ИЗМЕНИТЬ КУРС ПРОДАЖИ</b>\n'
        '<code>·····················</code>\n'
        '<i>Напишите новый курс продажи (сколько mCoin вы хотите получать за 1 MP, например: <b>15000</b> или <b>15.000</b>):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_manage", icon_custom_emoji_id="5255703720078879038")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "p2p_set_buy_price")
async def cb_p2p_set_buy_price(callback: types.CallbackQuery):
    await callback.answer()
    p2p_user_action[callback.from_user.id] = "set_buy_price"
    text = (
        '💳 <b>ИЗМЕНИТЬ КУРС ПОКУПКИ</b>\n'
        '<code>·····················</code>\n'
        '<i>Напишите новый курс покупки (сколько mCoin вы готовы платить за 1 MP, например: <b>12000</b> или <b>12.000</b>):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_manage", icon_custom_emoji_id="5255703720078879038")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "noop_p2p")
async def cb_noop_p2p(callback: types.CallbackQuery):
    await callback.answer()


# --- ADMIN SEXCHANGE CALLBACKS ---

@dp.callback_query(lambda c: c.data == "sexch_refresh")
async def cb_sexch_refresh(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    admin_sexchange_state.pop(callback.from_user.id, None)
    await callback.answer("Обновлено!")
    text, kb = render_sexchange_menu()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_tog_sell")
async def cb_sexch_tog_sell(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    settings = get_p2p_settings()
    new_val = 0 if settings.get("official_sell_enabled", 1) else 1
    cursor.execute("UPDATE p2p_settings SET official_sell_enabled = ? WHERE id = 1", (new_val,))
    conn.commit()
    await callback.answer("Статус оф. продажи изменен!")
    text, kb = render_sexchange_menu()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_tog_buy")
async def cb_sexch_tog_buy(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    settings = get_p2p_settings()
    new_val = 0 if settings.get("official_buy_enabled", 1) else 1
    cursor.execute("UPDATE p2p_settings SET official_buy_enabled = ? WHERE id = 1", (new_val,))
    conn.commit()
    await callback.answer("Статус оф. покупки изменен!")
    text, kb = render_sexchange_menu()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_reroll")
async def cb_sexch_reroll(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    ok, res = update_p2p_rate_fluctuation(force=True)
    await callback.answer(f"Новый курс: {format_number(res['official_sell_rate'])} mCoin!", show_alert=True)
    text, kb = render_sexchange_menu()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_set_sell")
async def cb_sexch_set_sell(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    admin_sexchange_state[callback.from_user.id] = "set_sell_rate"
    await callback.answer()
    text = (
        '✏️ <b>ЗАДАТЬ КУРС ОФ. ПРОДАЖИ</b>\n'
        '<code>································</code>\n'
        '<i>Напишите новый фиксированный курс продажи для бота (например: <b>15000</b> или <b>15.000</b>):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в /sexchange", callback_data="sexch_refresh")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_set_buy")
async def cb_sexch_set_buy(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    admin_sexchange_state[callback.from_user.id] = "set_buy_rate"
    await callback.answer()
    text = (
        '✏️ <b>ЗАДАТЬ КУРС ОФ. ПОКУПКИ</b>\n'
        '<code>································</code>\n'
        '<i>Напишите новый фиксированный курс покупки для бота (например: <b>15000</b> или <b>15.000</b>):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в /sexchange", callback_data="sexch_refresh")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_set_range")
async def cb_sexch_set_range(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    admin_sexchange_state[callback.from_user.id] = "set_range"
    await callback.answer()
    text = (
        '📊 <b>ИЗМЕНИТЬ ДИАПАЗОН СКАЧКОВ КУРСА</b>\n'
        '<code>································</code>\n'
        '<i>Напишите минимальный и максимальный курс через дефис или пробел (например: <b>7000-29000</b> или <b>7.000-29.000</b>):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в /sexchange", callback_data="sexch_refresh")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "sexch_set_interval")
async def cb_sexch_set_interval(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMINS:
        return
    admin_sexchange_state[callback.from_user.id] = "set_interval"
    await callback.answer()
    text = (
        '⏱ <b>ИЗМЕНИТЬ ИНТЕРВАЛ СМЕНЫ КУРСА</b>\n'
        '<code>································</code>\n'
        '<i>Напишите интервал в минутах (например: <b>150</b> для 2ч 30мин):</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад в /sexchange", callback_data="sexch_refresh")]]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- P2P & ADMIN EXCHANGE TEXT / REPLY HANDLER ---

@dp.message(lambda message: message.text and (
    message.from_user.id in admin_sexchange_state or
    message.from_user.id in p2p_user_action or
    (message.reply_to_message and (
        "КУПИТЬ MPOINT" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ПРОДАТЬ MPOINT" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "Сколько mCoin пополняем?" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "Сколько MPOINT пополняем?" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ПОПОЛНЕНИЕ mCoin" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ПОПОЛНЕНИЕ MPOINT" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ВЫВОД mCoin" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ВЫВОД MPOINT" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "Сколько mCoin выводим?" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "Сколько MPOINT выводим?" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ИЗМЕНИТЬ КУРС ПРОДАЖИ" in (message.reply_to_message.text or message.reply_to_message.caption or "") or
        "ИЗМЕНИТЬ КУРС ПОКУПКИ" in (message.reply_to_message.text or message.reply_to_message.caption or "")
    ))
))
async def process_p2p_and_admin_reply_message(message: types.Message):
    user_id = message.from_user.id
    raw_text = message.text.strip()
    rep_text = (message.reply_to_message.text or message.reply_to_message.caption or "") if message.reply_to_message else ""
    user_action = p2p_user_action.get(user_id)
    admin_action = admin_sexchange_state.get(user_id)

    # 1. Admin Sexchange State Actions
    if admin_action and user_id in ADMINS:
        if admin_action == "set_sell_rate":
            val = parse_amount(raw_text)
            if val is None or val <= 0:
                await message.reply("<i>Курс должен быть положительным числом!</i>", parse_mode=ParseMode.HTML)
                return
            cursor.execute("UPDATE p2p_settings SET official_sell_rate = ? WHERE id = 1", (val,))
            conn.commit()
            admin_sexchange_state.pop(user_id, None)
            succ_text = f"✅ <i>Официальный курс продажи установлен: <b>1 MP = {format_number(val)} mCoin</b></i>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню /sexchange", callback_data="sexch_refresh")]])
            await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return

        if admin_action == "set_buy_rate":
            val = parse_amount(raw_text)
            if val is None or val <= 0:
                await message.reply("<i>Курс должен быть положительным числом!</i>", parse_mode=ParseMode.HTML)
                return
            cursor.execute("UPDATE p2p_settings SET official_buy_rate = ? WHERE id = 1", (val,))
            conn.commit()
            admin_sexchange_state.pop(user_id, None)
            succ_text = f"✅ <i>Официальный курс покупки установлен: <b>1 MP = {format_number(val)} mCoin</b></i>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню /sexchange", callback_data="sexch_refresh")]])
            await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return

        if admin_action == "set_range":
            cleaned = raw_text.replace("–", "-").replace("—", "-").replace(" ", "-")
            parts = [p for p in cleaned.split("-") if p]
            if len(parts) >= 2:
                r_min = parse_amount(parts[0])
                r_max = parse_amount(parts[1])
                if r_min and r_max and r_min > 0 and r_max > r_min:
                    cursor.execute("UPDATE p2p_settings SET rate_min = ?, rate_max = ? WHERE id = 1", (r_min, r_max))
                    conn.commit()
                    admin_sexchange_state.pop(user_id, None)
                    succ_text = f"✅ <i>Диапазон скачков курса успешно установлен: <b>{format_number(r_min)} — {format_number(r_max)} mCoin</b></i>"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню /sexchange", callback_data="sexch_refresh")]])
                    await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
                    return
            await message.reply("<i>Введите диапазон в формате: <b>7000-29000</b> или <b>7.000-29.000</b> (мин < макс)!</i>", parse_mode=ParseMode.HTML)
            return

        if admin_action == "set_interval":
            try:
                mins = int(raw_text)
                if mins < 1:
                    raise ValueError()
                cursor.execute("UPDATE p2p_settings SET interval_minutes = ? WHERE id = 1", (mins,))
                conn.commit()
                admin_sexchange_state.pop(user_id, None)
                succ_text = f"✅ <i>Интервал смены курса установлен: <b>{mins} мин. ({mins // 60}ч {mins % 60}м)</b></i>"
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню /sexchange", callback_data="sexch_refresh")]])
                await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
            except Exception:
                await message.reply("<i>Интервал должен быть положительным числом минут (например: 150)!</i>", parse_mode=ParseMode.HTML)
                return

    raw_amount = parse_amount(raw_text)

    # 2. Change sell price
    if user_action == "set_sell_price" or "ИЗМЕНИТЬ КУРС ПРОДАЖИ" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Курс должен быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        cursor.execute("UPDATE p2p_accounts SET sell_price = ? WHERE user_id = ?", (raw_amount, user_id))
        conn.commit()
        succ_text = f"✅ <i>Курс продажи успешно установлен: <b>1 MP = {format_number(raw_amount)} mCoin</b></i>"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 3. Change buy price
    if user_action == "set_buy_price" or "ИЗМЕНИТЬ КУРС ПОКУПКИ" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Курс должен быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        cursor.execute("UPDATE p2p_accounts SET buy_price = ? WHERE user_id = ?", (raw_amount, user_id))
        conn.commit()
        succ_text = f"✅ <i>Курс покупки успешно установлен: <b>{format_number(raw_amount)} mCoin = 1 MP</b></i>"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 4. Deposit mCoin
    if user_action == "dep_mcoin" or "ПОПОЛНЕНИЕ mCoin" in rep_text or "Сколько mCoin пополняем?" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Сумма должна быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        ok, res = deposit_to_p2p_mcoin(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        user_link = get_user_mention(user_id, message.from_user.first_name)
        comm_str = "<blockquote><b>🆓 Комиссия не списана (1-е пополнение за день)</b></blockquote>" if res["is_first"] else f"<blockquote><b>🔘 Комиссия: 1,5% ({format_number(res['fee'])} mCoin)</b></blockquote>"
        succ_text = (
            '✅ <b>УСПЕШНОЕ ПОПОЛНЕНИЕ</b>\n'
            '<code>·····················</code>\n'
            f'{user_link}, твои <b>{format_number(raw_amount)} mCoin</b> зачислены на баланс обменника.\n'
            f'{comm_str}\n'
            f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>\n'
            f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 5. Deposit MPOINT
    if user_action == "dep_mpoint" or "ПОПОЛНЕНИЕ MPOINT" in rep_text or "Сколько MPOINT пополняем?" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Сумма должна быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        ok, res = deposit_to_p2p_mpoint(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        user_link = get_user_mention(user_id, message.from_user.first_name)
        succ_text = (
            '✅ <b>УСПЕШНОЕ ПОПОЛНЕНИЕ</b>\n'
            '<code>·····················</code>\n'
            f'{user_link}, твои <b>{format_number(raw_amount)} MPOINT</b> зачислены на баланс обменника.\n'
            '<blockquote><b>🆓 Комиссия не списана</b></blockquote>\n'
            f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>\n'
            f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 6. Withdraw mCoin
    if user_action == "wth_mcoin" or "ВЫВОД mCoin" in rep_text or "Сколько mCoin выводим?" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Сумма должна быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        ok, res = withdraw_from_p2p_mcoin(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        user_link = get_user_mention(user_id, message.from_user.first_name)
        succ_text = (
            '✅ <b>УСПЕШНЫЙ ВЫВОД</b>\n'
            '<code>·····················</code>\n'
            f'{user_link}, твои <b>{format_number(raw_amount)} mCoin</b> выведены на основной баланс.\n'
            f'<blockquote>💰 mCoin (обменник): <b>{format_number(res["mcoin_balance"])}</b></blockquote>'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 7. Withdraw MPOINT
    if user_action == "wth_mpoint" or "ВЫВОД MPOINT" in rep_text or "Сколько MPOINT выводим?" in rep_text:
        if raw_amount is None or raw_amount <= 0:
            await message.reply("<i>Сумма должна быть положительным числом!</i>", parse_mode=ParseMode.HTML)
            return
        p2p_user_action.pop(user_id, None)
        ok, res = withdraw_from_p2p_mpoint(user_id, raw_amount)
        if not ok:
            await message.reply(f"<i>Ошибка: {res}</i>", parse_mode=ParseMode.HTML)
            return
        user_link = get_user_mention(user_id, message.from_user.first_name)
        succ_text = (
            '✅ <b>УСПЕШНЫЙ ВЫВОД</b>\n'
            '<code>·····················</code>\n'
            f'{user_link}, твои <b>{format_number(raw_amount)} MPOINT</b> выведены на основной баланс.\n'
            f'<blockquote>💎 MPOINT (обменник): <b>{format_number(res["mp_balance"])}</b></blockquote>'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Управление обменом", callback_data="p2p_manage", style="primary")]]
        )
        await message.reply(succ_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    # 8. Buy MPOINT from Bot (Official Buy custom amount)
    if user_action == "buy_bot_custom" or ("КУПИТЬ MPOINT" in rep_text and "Оф. Покупка" in rep_text):
        if raw_amount is None or raw_amount <= 0:
            return
        p2p_user_action.pop(user_id, None)
        update_p2p_rate_fluctuation(force=False)
        settings = get_p2p_settings()
        buy_rate = settings.get("official_buy_rate", 10000)
        user_u = get_user(user_id)
        user_link = get_user_mention(user_id, message.from_user.first_name)
        total_cost = raw_amount * buy_rate

        if user_u["balance"] < total_cost:
            shortage = total_cost - user_u["balance"]
            err_text = f"🚫 <i>{user_link}, не хватает <b>{format_number(shortage)} mCoin!</b></i>"
            err_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_buy_bot", icon_custom_emoji_id="5255703720078879038")]]
            )
            await message.reply(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
            return

        conf_text = (
            '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ ПОКУПКИ</b>\n'
            '<code>·····················</code>\n'
            f'<i>{user_link}, ты точно <b>хочешь купить {format_number(raw_amount)} MPOINT</b> за <b>{format_number(total_cost)} mCoin?</b></i>\n'
            f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Оф. курс: {format_number(buy_rate)} mCoin</blockquote>\n'
            '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
        )
        conf_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_bbot_{raw_amount}", style="success"),
                    InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
                ]
            ]
        )
        await message.reply(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
        return

    # 9. Buy MPOINT from Seller Card
    if (user_action and user_action.startswith("buy_seller_")) or ("КУПИТЬ MPOINT" in rep_text and "Продавец:" in rep_text):
        if raw_amount is None or raw_amount <= 0:
            return
        if user_action and user_action.startswith("buy_seller_"):
            seller_id = int(user_action.replace("buy_seller_", ""))
        else:
            m = re.search(r'tg://user\?id=(\d+)', rep_text)
            if not m:
                return
            seller_id = int(m.group(1))

        p2p_user_action.pop(user_id, None)
        seller_p2p = get_p2p_account(seller_id)
        buyer_u = get_user(user_id)
        buyer_link = get_user_mention(user_id, message.from_user.first_name)

        total_cost = raw_amount * seller_p2p["sell_price"]
        if buyer_u["balance"] < total_cost:
            shortage = total_cost - buyer_u["balance"]
            err_text = f"🚫 <i>{buyer_link}, не хватает <b>{format_number(shortage)} mCoin!</b></i>"
            err_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=f"p2p_buy_seller_{seller_id}", icon_custom_emoji_id="5255703720078879038")]]
            )
            await message.reply(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
            return

        conf_text = (
            '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
            '<code>·····················</code>\n'
            f'<i>{buyer_link}, ты точно <b>хочешь купить {format_number(raw_amount)} MPOINT</b> за <b>{format_number(total_cost)} mCoin?</b></i>\n'
            f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(seller_p2p["sell_price"])} mCoin</blockquote>\n'
            '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
        )
        conf_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_buy_{seller_id}_{raw_amount}", style="success"),
                    InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
                ]
            ]
        )
        await message.reply(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
        return

    # 10. Sell MPOINT to Bot (Official Sell custom amount)
    if user_action == "sell_bot_custom" or ("ПРОДАТЬ MPOINT" in rep_text and "Оф. Продажа" in rep_text):
        if raw_amount is None or raw_amount <= 0:
            return
        p2p_user_action.pop(user_id, None)
        update_p2p_rate_fluctuation(force=False)
        settings = get_p2p_settings()
        sell_rate = settings.get("official_sell_rate", 10000)
        user_u = get_user(user_id)
        user_link = get_user_mention(user_id, message.from_user.first_name)
        if user_u.get("mp_balance", 0) < raw_amount:
            shortage = raw_amount - user_u.get("mp_balance", 0)
            err_text = f"🚫 <i>{user_link}, не хватает <b>{format_number(shortage)} MP!</b></i>"
            err_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="p2p_sell_bot", icon_custom_emoji_id="5255703720078879038")]]
            )
            await message.reply(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
            return

        total_mcoin = raw_amount * sell_rate
        conf_text = (
            '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
            '<code>·····················</code>\n'
            f'<i>{user_link}, ты точно <b>хочешь продать {format_number(raw_amount)} MPOINT</b> за <b>{format_number(total_mcoin)} mCoin?</b></i>\n'
            f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Оф. курс: {format_number(sell_rate)} mCoin</blockquote>\n'
            '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
        )
        conf_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_sbot_{raw_amount}", style="success"),
                    InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
                ]
            ]
        )
        await message.reply(conf_text, reply_markup=conf_kb, parse_mode=ParseMode.HTML)
        return

    # 11. Sell MPOINT to Player Buyer
    if (user_action and user_action.startswith("sell_buyer_")) or ("ПРОДАТЬ MPOINT" in rep_text and "Покупатель:" in rep_text):
        if raw_amount is None or raw_amount <= 0:
            return
        if user_action and user_action.startswith("sell_buyer_"):
            buyer_id = int(user_action.replace("sell_buyer_", ""))
        else:
            m = re.search(r'tg://user\?id=(\d+)', rep_text)
            if not m:
                return
            buyer_id = int(m.group(1))

        p2p_user_action.pop(user_id, None)
        buyer_p2p = get_p2p_account(buyer_id)
        seller_u = get_user(user_id)
        seller_link = get_user_mention(user_id, message.from_user.first_name)

        if seller_u.get("mp_balance", 0) < raw_amount:
            shortage = raw_amount - seller_u.get("mp_balance", 0)
            err_text = f"🚫 <i>{seller_link}, не хватает <b>{format_number(shortage)} MP!</b></i>"
            err_kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data=f"p2p_sell_buyer_{buyer_id}", icon_custom_emoji_id="5255703720078879038")]]
            )
            await message.reply(err_text, reply_markup=err_kb, parse_mode=ParseMode.HTML)
            return

        total_mcoin = raw_amount * buyer_p2p["buy_price"]
        conf_text = (
            '<tg-emoji emoji-id="5452069934089641166">❓</tg-emoji> <b>ПОДТВЕРЖДЕНИЕ СДЕЛКИ</b>\n'
            '<code>·····················</code>\n'
            f'<i>{seller_link}, ты точно <b>хочешь продать {format_number(raw_amount)} MPOINT</b> за <b>{format_number(total_mcoin)} mCoin?</b></i>\n'
            f'<blockquote><tg-emoji emoji-id="5402186569006210455">💱</tg-emoji> Курс: {format_number(buyer_p2p["buy_price"])} mCoin</blockquote>\n'
            '<blockquote>🔘 Комиссия: 0% = 0 mCoin</blockquote>'
        )
        conf_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да ✅", callback_data=f"p2p_conf_sbuy_{buyer_id}_{raw_amount}", style="success"),
                    InlineKeyboardButton(text="Отмена 💢", callback_data="p2p_cancel_buy", style="danger")
                ]
            ]
        )
# ==============================================================================
# --- ПОДАРОЧНЫЙ ХРАМ (GIFT TEMPLE) ---
# ==============================================================================

TEMPLE_GIFTS = {
    "heart": {
        "name": "Сердце",
        "emoji": "💝",
        "stars": 15,
        "tg_gift_id": "5129992984518328325"
    },
    "bear": {
        "name": "Мишка",
        "emoji": "🧸",
        "stars": 15,
        "tg_gift_id": "5130095818987405325"
    },
    "box": {
        "name": "Подарок",
        "emoji": "🎁",
        "stars": 25,
        "tg_gift_id": "5130104271835365389"
    },
    "champagne": {
        "name": "Шампанское",
        "emoji": "🍾",
        "stars": 50,
        "tg_gift_id": "5130113849503383565"
    },
    "bouquet": {
        "name": "Букет",
        "emoji": "💐",
        "stars": 50,
        "tg_gift_id": "5130122439832240141"
    },
    "ring": {
        "name": "Кольцо",
        "emoji": "💍",
        "stars": 100,
        "tg_gift_id": "5130131030169403405"
    }
}

_temple_user_target = {}
_temple_user_comment = {}
_temple_waiting_comment = {}
_temple_waiting_target = set()


def get_temple_stats(user_id: int) -> dict:
    try:
        cursor.execute("SELECT gifts_sent, stars_spent FROM temple_user_stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return {"gifts_sent": row[0] or 0, "stars_spent": row[1] or 0}
        return {"gifts_sent": 0, "stars_spent": 0}
    except Exception:
        return {"gifts_sent": 0, "stars_spent": 0}


def record_temple_gift(sender_id: int, receiver_id: int, gift_key: str, gift_name: str, stars_cost: int, comment: str = ""):
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    try:
        cursor.execute("""
            INSERT INTO temple_user_stats (user_id, gifts_sent, stars_spent, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT (user_id) DO UPDATE SET
                gifts_sent = temple_user_stats.gifts_sent + 1,
                stars_spent = temple_user_stats.stars_spent + EXCLUDED.stars_spent,
                updated_at = EXCLUDED.updated_at
        """, (sender_id, stars_cost, now_str))
        
        cursor.execute("""
            INSERT INTO temple_gifts_history (sender_id, receiver_id, gift_key, gift_name, stars_cost, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sender_id, receiver_id, gift_key, gift_name, stars_cost, comment or "", now_str))
        conn.commit()
    except Exception as e:
        logger.error(f"Error recording temple gift: {e}")


async def deliver_telegram_gift(user_id: int, gift_key: str, comment: str = ""):
    gift_info = TEMPLE_GIFTS.get(gift_key)
    if not gift_info:
        return False
    try:
        if hasattr(bot, "get_available_gifts"):
            gifts_res = await bot.get_available_gifts()
            gifts_list = getattr(gifts_res, "gifts", [])
            for g in gifts_list:
                if getattr(g, "star_count", 0) == gift_info["stars"]:
                    if hasattr(bot, "send_gift"):
                        await bot.send_gift(user_id=user_id, gift_id=g.id, text=comment if comment else None)
                        return True
    except Exception as e:
        logger.warning(f"deliver_telegram_gift note: {e}")

    try:
        if hasattr(bot, "send_gift"):
            await bot.send_gift(user_id=user_id, gift_id=gift_info["tg_gift_id"], text=comment if comment else None)
            return True
    except Exception as e:
        logger.warning(f"direct send_gift note: {e}")

    return False


async def resolve_temple_target(message: types.Message, raw_target: str) -> Optional[int]:
    if not raw_target:
        if message.reply_to_message and message.reply_to_message.from_user:
            return message.reply_to_message.from_user.id
        return None
    
    clean_target = raw_target.strip()
    if clean_target.startswith("@"):
        clean_uname = clean_target[1:].lower()
        u = get_user_by_username(clean_uname)
        if u:
            return u["user_id"]
        try:
            chat = await bot.get_chat(clean_target)
            if chat and chat.id:
                get_user(chat.id)
                return chat.id
        except Exception:
            pass
        return None
    elif clean_target.isdigit():
        uid = int(clean_target)
        get_user(uid)
        return uid
    else:
        u = get_user_by_username(clean_target)
        if u:
            return u["user_id"]
        try:
            chat = await bot.get_chat(f"@{clean_target}")
            if chat and chat.id:
                get_user(chat.id)
                return chat.id
        except Exception:
            pass
        return None


async def show_temple_target_selected(event, sender_id: int, sender_name: str, target_id: int, edit: bool = False):
    sender_link = get_user_mention(sender_id, sender_name)
    target_link = get_user_mention(target_id)
    if sender_id == target_id:
        target_str = "<b>самого себя</b>"
    else:
        target_str = target_link
    text = (
        f'{sender_link}\n'
        f'<tg-emoji emoji-id="5472096095280569232">🎁</tg-emoji> Получатель {target_str} успешно выбран!'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Отправить подарок",
                    callback_data=f"temple_gifts_{target_id}",
                    style="success",
                    icon_custom_emoji_id="5846184826184408721"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Сменить получателя",
                    callback_data="temple_change_target",
                    style="danger",
                    icon_custom_emoji_id="5264727218734524899"
                )
            ]
        ]
    )
    if edit and isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await event.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


# --- TEMPLE COMMANDS & CALLBACKS ---

@dp.message(Command("temple", "храм"))
@dp.message(lambda message: message.text and re.match(r'^[!/.]?(храм|temple)(\s+|$)', message.text, re.IGNORECASE))
async def cmd_temple(message: types.Message):
    user_id = message.from_user.id
    raw_text = message.text.strip()
    parts = re.split(r'\s+', raw_text, maxsplit=1)
    
    # If target specified (e.g. "храм @kleymorf" or reply)
    if len(parts) > 1 or (message.reply_to_message and message.reply_to_message.from_user):
        arg = parts[1].strip() if len(parts) > 1 else ""
        target_id = await resolve_temple_target(message, arg)
        if not target_id:
            await message.reply("<i>Пользователь не найден! Укажите корректный @username или ID.</i>", parse_mode=ParseMode.HTML)
            return
        _temple_user_target[user_id] = target_id
        await show_temple_target_selected(message, user_id, message.from_user.first_name, target_id)
        return

    # No target: show main temple intro
    text = (
        '[NEW] <tg-emoji emoji-id="5834781387365818180">🎁</tg-emoji><b> Подарочный храм</b>\n\n'
        '<tg-emoji emoji-id="5942877472163892475">👥</tg-emoji> В подарочном храме ты сможешь:\n'
        '<i>Отправлять подарки своим друзьям.\n'
        'Разыгрывать подарки среди игроков.</i>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Войти",
                    callback_data="temple_enter",
                    style="success",
                    icon_custom_emoji_id="5258084656674250503"
                )
            ]
        ]
    )
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "temple_enter")
async def cb_temple_enter(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    user_link = get_user_mention(user_id, callback.from_user.first_name)
    
    # Send sticker and delete after 2 seconds
    sticker_id = "CAACAgUAAxkBAAERq7xqdMNBP-oO2yCEC4DAChXoLQVFjgACCgQAAjHHqVblSt_BbuOy8D0E"
    try:
        st_msg = await callback.message.answer_sticker(sticker_id)
        await asyncio.sleep(2)
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=st_msg.message_id)
        except Exception:
            pass
    except Exception:
        pass

    stats = get_temple_stats(user_id)
    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5951637899777677904">👋</tg-emoji> Добро пожаловать в подарочный храм!\n\n'
        f'<tg-emoji emoji-id="5422360266618707867">📊</tg-emoji> Твоя статистика\n'
        f'<blockquote> <tg-emoji emoji-id="5357134848857241750">⬜️</tg-emoji>Отправлено подарков: {stats["gifts_sent"]}</blockquote>\n'
        f'<blockquote><tg-emoji emoji-id="5463289097336405244">⭐️</tg-emoji>Потрачено звезд: {stats["stars_spent"]}</blockquote>\n\n'
        f'<b>Для того чтобы отправить подарок пользователю введи команда <code>храм &lt;username/id&gt;</code></b>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🎁 Подарить себе",
                    callback_data=f"temple_gifts_{user_id}",
                    style="success"
                )
            ]
        ]
    )
    await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data == "temple_change_target")
async def cb_temple_change_target(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    _temple_waiting_target.add(user_id)
    user_link = get_user_mention(user_id, callback.from_user.first_name)
    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5472096095280569232">🎁</tg-emoji> Отправьте @username или ID получателя (или введите <code>храм &lt;username/id&gt;</code>):'
    )
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("temple_back_target_"))
async def cb_temple_back_target(callback: types.CallbackQuery):
    await callback.answer()
    target_id = int(callback.data.replace("temple_back_target_", ""))
    user_id = callback.from_user.id
    await show_temple_target_selected(callback, user_id, callback.from_user.first_name, target_id, edit=True)


@dp.callback_query(lambda c: c.data.startswith("temple_gifts_"))
async def cb_temple_gifts(callback: types.CallbackQuery):
    await callback.answer()
    target_id = int(callback.data.replace("temple_gifts_", ""))
    user_id = callback.from_user.id
    user_link = get_user_mention(user_id, callback.from_user.first_name)
    target_link = "самому себе" if target_id == user_id else get_user_mention(target_id)
    
    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5474371208176737086">✉️</tg-emoji> Выберите подарок которой вы хотите подарить {target_link}'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💝", callback_data=f"temple_sel_{target_id}_heart"),
                InlineKeyboardButton(text="🧸", callback_data=f"temple_sel_{target_id}_bear"),
                InlineKeyboardButton(text="🎁", callback_data=f"temple_sel_{target_id}_box"),
            ],
            [
                InlineKeyboardButton(text="🍾", callback_data=f"temple_sel_{target_id}_champagne"),
                InlineKeyboardButton(text="💐", callback_data=f"temple_sel_{target_id}_bouquet"),
                InlineKeyboardButton(text="💍", callback_data=f"temple_sel_{target_id}_ring"),
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data=f"temple_back_target_{target_id}")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("temple_sel_"))
async def cb_temple_select_gift(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("temple_sel_", "").split("_")
    target_id = int(parts[0])
    gift_key = parts[1]
    user_id = callback.from_user.id
    
    gift = TEMPLE_GIFTS.get(gift_key, TEMPLE_GIFTS["heart"])
    user_link = get_user_mention(user_id, callback.from_user.first_name)
    target_link = "самому себе" if target_id == user_id else get_user_mention(target_id)
    
    comment = _temple_user_comment.get(user_id, "")
    comment_block = f'\n<blockquote>💬 <b>Комментарий:</b> <i>{html.escape(comment)}</i></blockquote>\n' if comment else '\n'

    text = (
        f'{user_link}\n'
        f'<tg-emoji emoji-id="5472248119942979457">🤔</tg-emoji> Вы хотите отправить подарок <b>{gift["emoji"]} {gift["name"]} ({gift["stars"]}⭐️)</b> пользователю {target_link}?\n'
        f'{comment_block}\n'
        f'<blockquote><i>Если хотите добавить комментарий к подарку, то нажмите кнопку ниже, если хотите без комментария просто проигнорируйте данное сообщение</i></blockquote>'
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Отправить", callback_data=f"temple_pay_{target_id}_{gift_key}", style="primary"),
                InlineKeyboardButton(text="Добавить комментарий" if not comment else "Изменить комментарий", callback_data=f"temple_com_{target_id}_{gift_key}", style="primary")
            ],
            [
                InlineKeyboardButton(text="« Назад", callback_data=f"temple_gifts_{target_id}")
            ]
        ]
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("temple_com_"))
async def cb_temple_add_comment(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("temple_com_", "").split("_")
    target_id = int(parts[0])
    gift_key = parts[1]
    user_id = callback.from_user.id
    
    _temple_waiting_comment[user_id] = (target_id, gift_key)
    user_link = get_user_mention(user_id, callback.from_user.first_name)
    text = (
        f'{user_link}\n'
        f'✍️ <b>Напишите комментарий к подарку в чат:</b>'
    )
    await callback.message.answer(text, parse_mode=ParseMode.HTML)


@dp.callback_query(lambda c: c.data.startswith("temple_pay_"))
async def cb_temple_pay(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.replace("temple_pay_", "").split("_")
    target_id = int(parts[0])
    gift_key = parts[1]
    user_id = callback.from_user.id
    
    gift = TEMPLE_GIFTS.get(gift_key, TEMPLE_GIFTS["heart"])
    target_name = "себе" if target_id == user_id else get_user_display_name(target_id)
    
    payload = f"temple_gift_{user_id}_{target_id}_{gift_key}_{secrets.token_hex(4)}"
    title = f"Подарок {gift['emoji']} {gift['name']}"
    description = f"Оплата подарка {gift['name']} пользователю {target_name}"
    
    try:
        await callback.message.answer_invoice(
            title=title,
            description=description,
            payload=payload,
            currency="XTR",
            prices=[types.LabeledPrice(label=f"{gift['emoji']} {gift['name']}", amount=gift["stars"])],
            provider_token=""
        )
    except Exception as e:
        await callback.answer(f"Ошибка создания счета: {e}", show_alert=True)


@dp.pre_checkout_query(lambda q: q.invoice_payload and q.invoice_payload.startswith("temple_gift_"))
async def cb_temple_pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)


@dp.message(lambda m: m.successful_payment and m.successful_payment.invoice_payload and m.successful_payment.invoice_payload.startswith("temple_gift_"))
async def cb_temple_successful_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    if len(parts) < 6:
        return
    sender_id = int(parts[2])
    target_id = int(parts[3])
    gift_key = parts[4]
    
    gift = TEMPLE_GIFTS.get(gift_key, TEMPLE_GIFTS["heart"])
    comment = _temple_user_comment.pop(sender_id, "")
    
    record_temple_gift(sender_id, target_id, gift_key, gift["name"], gift["stars"], comment)
    
    # Attempt real Telegram gift delivery
    asyncio.create_task(deliver_telegram_gift(target_id, gift_key, comment))
    
    sender_link = get_user_mention(sender_id, message.from_user.first_name)
    succ_text = (
        f'{sender_link}\n'
        f'<tg-emoji emoji-id="5472180551517477902">✅</tg-emoji> Ваша заявка обрабатывается!'
    )
    await message.answer(succ_text, parse_mode=ParseMode.HTML)
    
    # Notify recipient
    if sender_id == target_id:
        try:
            self_notify = (
                f'🎉 <b>Вы успешно подарили себе подарок {gift["emoji"]} {gift["name"]}!</b>\n'
            )
            if comment:
                self_notify += f'💬 <i>«{html.escape(comment)}»</i>'
            await message.answer(self_notify, parse_mode=ParseMode.HTML)
        except Exception:
            pass
    else:
        try:
            target_notify = (
                f'🎉 <b>Вам пришёл подарок {gift["emoji"]} {gift["name"]}!</b>\n'
                f'👤 От: {sender_link}\n'
            )
            if comment:
                target_notify += f'💬 <i>«{html.escape(comment)}»</i>'
            await bot.send_message(chat_id=target_id, text=target_notify, parse_mode=ParseMode.HTML)
        except Exception:
            pass


@dp.message(lambda message: message.text and (
    message.from_user.id in _temple_waiting_comment or
    message.from_user.id in _temple_waiting_target
))
async def process_temple_text_input(message: types.Message):
    user_id = message.from_user.id
    raw_text = message.text.strip()

    # If user sent a command, cancel waiting state and let other handlers process
    if raw_text.startswith("/") and not raw_text.lower().startswith(("/храм", "/temple")):
        _temple_waiting_comment.pop(user_id, None)
        _temple_waiting_target.discard(user_id)
        return
    
    if user_id in _temple_waiting_comment:
        target_id, gift_key = _temple_waiting_comment.pop(user_id)
        _temple_user_comment[user_id] = raw_text
        gift = TEMPLE_GIFTS.get(gift_key, TEMPLE_GIFTS["heart"])
        user_link = get_user_mention(user_id, message.from_user.first_name)
        target_link = "самому себе" if target_id == user_id else get_user_mention(target_id)
        text = (
            f'{user_link}\n'
            f'<tg-emoji emoji-id="5472248119942979457">🤔</tg-emoji> Вы хотите отправить подарок <b>{gift["emoji"]} {gift["name"]} ({gift["stars"]}⭐️)</b> пользователю {target_link}?\n\n'
            f'<blockquote>💬 <b>Комментарий:</b> <i>{html.escape(raw_text)}</i></blockquote>\n\n'
            f'<blockquote><i>Если хотите изменить комментарий, нажмите кнопку ниже</i></blockquote>'
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Отправить", callback_data=f"temple_pay_{target_id}_{gift_key}", style="primary"),
                    InlineKeyboardButton(text="Изменить комментарий", callback_data=f"temple_com_{target_id}_{gift_key}", style="primary")
                ],
                [
                    InlineKeyboardButton(text="« Назад", callback_data=f"temple_gifts_{target_id}")
                ]
            ]
        )
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if user_id in _temple_waiting_target:
        _temple_waiting_target.discard(user_id)
        # Strip command prefix if user wrote "храм @username"
        lookup_target = raw_text
        if lookup_target.lower().startswith(("храм ", "/храм ", "!храм ", "temple ", "/temple ")):
            lookup_target = lookup_target.split(None, 1)[1].strip()
        
        target_user = await resolve_temple_target(message, lookup_target)
        if not target_user:
            await message.reply("<i>Пользователь не найден! Укажите корректный @username или ID.</i>", parse_mode=ParseMode.HTML)
            return
        _temple_user_target[user_id] = target_user
        await show_temple_target_selected(message, user_id, message.from_user.first_name, target_user)
        return


# --- BANK BACKGROUND TASKS ---

async def check_time_deposits_task():
    while True:
        try:
            now = datetime.now()
            cursor.execute("SELECT id, user_id, amount, days, percent, profit, end_at_dt FROM time_deposits WHERE status = 'active'")
            rows = cursor.fetchall()
            for r in rows:
                dep_id, uid, amount, days, percent, profit, end_at_dt_str = r
                try:
                    end_dt = datetime.fromisoformat(end_at_dt_str)
                except Exception:
                    continue
                if now >= end_dt:
                    cursor.execute("UPDATE time_deposits SET status = 'completed' WHERE id = ? AND status = 'active'", (dep_id,))
                    if cursor.rowcount > 0:
                        payout = amount + profit
                        cursor.execute("UPDATE users SET balance = balance + ?, max_balance = GREATEST(COALESCE(max_balance, 0), balance + ?) WHERE user_id = ?", (payout, payout, uid))
                        conn.commit()

                        settings = get_bank_settings(uid)
                        if settings.get("notifications_enabled", True):
                            try:
                                u_data = get_user(uid)
                                msg_text = (
                                    f'🏦 <b>Депозит завершен!</b>\n'
                                    f'<code>·····················</code>\n'
                                    f'Срок депозита на {days}д. подошел к концу.\n'
                                    f'💰 Выплата: <b>+{format_number(payout)} m¢</b> (прибыль: <b>+{format_number(profit)} m¢</b>)\n'
                                    f'<tg-emoji emoji-id="5418238674267556907">⭐</tg-emoji> Баланс: <b>{format_number(u_data["balance"])} m¢</b>'
                                )
                                await bot.send_message(chat_id=uid, text=msg_text, parse_mode=ParseMode.HTML)
                            except Exception:
                                pass
        except Exception:
            pass
        await asyncio.sleep(30)


async def accrue_savings_task():
    while True:
        try:
            await asyncio.sleep(3600)
            cursor.execute("SELECT user_id, balance, accumulated_interest FROM savings_accounts WHERE balance > 0")
            rows = cursor.fetchall()
            now_iso = datetime.now().isoformat()
            for uid, balance, accumulated in rows:
                if balance > 0:
                    hourly_accrual = balance * (0.002 / 24.0)
                    new_accum = (accumulated or 0.0) + hourly_accrual
                    cursor.execute("UPDATE savings_accounts SET accumulated_interest = ?, last_accrual = ? WHERE user_id = ?", (new_accum, now_iso, uid))
            conn.commit()
        except Exception:
            await asyncio.sleep(60)


async def reset_daily_mp_limits_task():
    while True:
        try:
            now_msk = get_msk_now()
            tomorrow_msk = (now_msk + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (tomorrow_msk - now_msk).total_seconds()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds + 1)
            else:
                await asyncio.sleep(1)

            today_str = get_msk_today_str()
            cursor.execute("UPDATE users SET mp_daily_transferred = 0, mp_daily_date = ?", (today_str,))
            conn.commit()
        except Exception:
            await asyncio.sleep(60)


async def p2p_rate_updater_task():
    while True:
        try:
            update_p2p_rate_fluctuation(force=False)
        except Exception:
            pass
        await asyncio.sleep(30)


# --- WEBAPP & ARENA HTTP API HANDLERS ---

def parse_telegram_init_data(init_data: str, bot_token: str):
    if not init_data:
        return None
    try:
        vals = dict(urllib.parse.parse_qsl(init_data))
        if "hash" not in vals:
            return None
        hash_val = vals.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if calculated_hash == hash_val:
            user_raw = vals.get("user", "{}")
            return json.loads(user_raw)
    except Exception:
        pass
    return None


async def handle_webapp_index(request: web.Request):
    asset_rotator.check_and_reload()
    return web.Response(text=asset_rotator.current_html_content, content_type="text/html", charset="utf-8")


async def handle_webapp_js(request: web.Request):
    asset_rotator.check_and_reload()
    return web.Response(text=asset_rotator.current_js_content, content_type="application/javascript", charset="utf-8")


async def handle_api_avatar(request: web.Request):
    try:
        user_id = int(request.match_info.get("user_id", 0))
    except Exception:
        user_id = 0

    if user_id:
        try:
            photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
            if photos and photos.total_count > 0 and photos.photos:
                photo = photos.photos[0][-1]
                file = await bot.get_file(photo.file_id)
                if file and file.file_path:
                    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}"
                    async with aiohttp.ClientSession() as session:
                        async with session.get(file_url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                return web.Response(body=data, content_type="image/jpeg")
        except Exception:
            pass

    user = get_user(user_id) if user_id else None
    name = (user.get("first_name") if user else f"U{user_id}") or "User"
    clean_n = urllib.parse.quote(name.strip()[:10])
    return web.HTTPFound(f"https://ui-avatars.com/api/?name={clean_n}&background=2563eb&color=fff&bold=true&size=128&rounded=true")


async def fetch_user_telegram_avatar(user_id: int, fallback: str = "") -> str:
    if fallback and fallback.startswith("http") and "dicebear" not in fallback and "ui-avatars" not in fallback:
        return fallback
    return f"/api/avatar/{user_id}"


def parse_telegram_init_data(init_data: str, bot_token: str = ""):
    if not init_data:
        return None
    try:
        raw = init_data
        if "%" in raw:
            try:
                raw = urllib.parse.unquote(raw)
            except Exception:
                pass
        parsed = dict(urllib.parse.parse_qsl(raw))
        if "user" in parsed:
            return json.loads(parsed["user"])
    except Exception:
        pass
    return None


async def handle_api_me(request: web.Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user_info = parse_telegram_init_data(init_data, BOT_TOKEN) if init_data else None

    if user_info:
        user_id = user_info.get("id")
        first_name = user_info.get("first_name", "Игрок")
        username = user_info.get("username", "")
        avatar = user_info.get("photo_url", "")
    else:
        try:
            user_id = int(request.headers.get("X-User-Id", 0))
        except Exception:
            user_id = 0
        first_name = urllib.parse.unquote(request.headers.get("X-User-Name", "")) or "Игрок"
        username = urllib.parse.unquote(request.headers.get("X-User-Username", "")) or ""
        avatar = ""

    if not user_id:
        return web.json_response({"error": "User not authorized"}, status=401)

    user = get_user(user_id)
    if user:
        if first_name and first_name != "Игрок" and (not user.get("first_name") or user.get("first_name") == "Игрок"):
            update_user(user_id, first_name=first_name, username=username or user.get("username"))
            user = get_user(user_id)
        elif not first_name or first_name == "Игрок":
            if user.get("first_name"):
                first_name = user.get("first_name")
            if user.get("username") and not username:
                username = user.get("username")

    db_first_name = user.get("first_name") or first_name or "Игрок"
    db_username = user.get("username") or username or ""

    arena_engine.touch_user(user_id)
    real_avatar = await fetch_user_telegram_avatar(user_id, avatar)

    return web.json_response({
        "user": {
            "id": user_id,
            "first_name": db_first_name,
            "username": db_username,
            "balance": user.get("balance", 0) if user else 0,
            "avatar": real_avatar
        }
    })


async def handle_rounds_active(request: web.Request):
    try:
        user_id = int(request.headers.get("X-User-Id", 0))
        if user_id:
            arena_engine.touch_user(user_id)
    except Exception:
        pass
    data = arena_engine.get_public_state()
    return web.json_response({"round": data})


async def handle_rounds_join(request: web.Request):
    round_id = int(request.match_info.get("round_id", 0))
    try:
        body = await request.json()
    except Exception:
        body = {}

    amount = int(body.get("amount", 0))
    client_avatar = body.get("avatar", "")
    if amount < 1000:
        return web.json_response({"error": "Минимальная ставка 1 000 m¢"}, status=400)

    init_data = request.headers.get("X-Telegram-Init-Data")
    user_info = parse_telegram_init_data(init_data, BOT_TOKEN) if init_data else None

    if user_info:
        user_id = user_info.get("id")
        first_name = user_info.get("first_name", "Игрок")
        username = user_info.get("username", "")
        avatar = user_info.get("photo_url", "") or client_avatar
    else:
        try:
            user_id = int(body.get("user_id") or request.headers.get("X-User-Id", 0))
        except Exception:
            user_id = 0
        first_name = body.get("first_name") or urllib.parse.unquote(request.headers.get("X-User-Name", "")) or "Игрок"
        username = body.get("username") or urllib.parse.unquote(request.headers.get("X-User-Username", "")) or ""
        avatar = client_avatar

    if not user_id:
        return web.json_response({"error": "User not authorized"}, status=401)

    user = get_user(user_id)
    if user:
        if (not first_name or first_name == "Игрок") and user.get("first_name"):
            first_name = user.get("first_name")
        if not username and user.get("username"):
            username = user.get("username")
        if first_name and first_name != "Игрок" and not user.get("first_name"):
            update_user(user_id, first_name=first_name, username=username or user.get("username"))

    if not user or user["balance"] < amount:
        return web.json_response({"error": "Недостаточно mCoin на балансе!"}, status=400)

    # Deduct balance
    if not update_user(user_id, balance=user["balance"] - amount):
        return web.json_response({"error": "Ошибка списания средств"}, status=500)

    real_avatar = await fetch_user_telegram_avatar(user_id, avatar)
    success, msg = arena_engine.add_bet(user_id, first_name, username, amount, real_avatar)
    if not success:
        # Refund
        update_user(user_id, balance=user["balance"])
        return web.json_response({"error": msg}, status=400)

    await broadcast_arena_state()
    return web.json_response({"success": True, "round": arena_engine.get_public_state()})


arena_websockets = set()


async def broadcast_arena_state():
    if not arena_websockets:
        return
    data_str = json.dumps({"type": "round_update", "round": arena_engine.get_public_state()}, ensure_ascii=False)
    dead = set()
    for ws in list(arena_websockets):
        try:
            if not ws.closed:
                await ws.send_str(data_str)
            else:
                dead.add(ws)
        except Exception:
            dead.add(ws)
    for ws in dead:
        arena_websockets.discard(ws)


async def handle_ws_arena(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    arena_websockets.add(ws)
    try:
        init_data = json.dumps({"type": "round_update", "round": arena_engine.get_public_state()}, ensure_ascii=False)
        await ws.send_str(init_data)
    except Exception:
        pass

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                if msg.data == "ping":
                    await ws.send_str("pong")
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
    finally:
        arena_websockets.discard(ws)

    return ws


async def handle_api_arena_history(request: web.Request):
    return web.json_response({"history": arena_engine.history})


async def handle_api_arena_replay(request: web.Request):
    round_id = int(request.match_info.get("round_id", 0))
    round_data = next((h for h in arena_engine.history if h["roundId"] == round_id), None)
    if not round_data:
        try:
            with arena_engine.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                SELECT round_id, total_bank, winner_id, winner_name, winner_username,
                       winner_avatar, winner_color, winner_bet, winner_share,
                       players_json, zones_json, ball_trajectory_json, created_at
                FROM arena_history WHERE round_id = ?
                ''', (round_id,))
                r = cursor.fetchone()
                if r:
                    round_data = {
                        "roundId": r["round_id"],
                        "totalBank": r["total_bank"],
                        "winner": {
                            "id": r["winner_id"],
                            "name": r["winner_name"],
                            "username": r["winner_username"],
                            "avatar": r["winner_avatar"],
                            "color": r["winner_color"],
                            "bet": r["winner_bet"],
                            "share": r["winner_share"]
                        },
                        "players": json.loads(r["players_json"]) if r["players_json"] else [],
                        "zones": json.loads(r["zones_json"]) if r["zones_json"] else [],
                        "ballTrajectory": json.loads(r["ball_trajectory_json"]) if r["ball_trajectory_json"] else {},
                        "createdAt": r["created_at"]
                    }
        except Exception:
            pass

    if not round_data:
        return web.json_response({"error": "Replay not found"}, status=404)
    return web.json_response({"round": round_data})


async def arena_ticker_task():
    while True:
        try:
            arena_engine.tick()
            await broadcast_arena_state()
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def asset_rotator_task():
    while True:
        try:
            await asyncio.sleep(60)
            asset_rotator.rotate_assets()
        except Exception:
            pass


async def on_startup(bot: Bot) -> None:
    try:
        from db import init_db_pool
        await init_db_pool()
    except Exception as e:
        logging.warning(f"Database pool init warning: {e}")
    try:
        from redis_client import get_redis
        await get_redis()
    except Exception as e:
        logging.warning(f"Redis init warning: {e}")

    init_caches()
    asyncio.create_task(check_expired_games_task())
    asyncio.create_task(check_time_deposits_task())
    asyncio.create_task(accrue_savings_task())
    asyncio.create_task(reset_daily_mp_limits_task())
    asyncio.create_task(p2p_rate_updater_task())
    asyncio.create_task(arena_ticker_task())
    asyncio.create_task(asset_rotator_task())

    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True
    )


async def on_shutdown(bot: Bot) -> None:
    await bot.delete_webhook()


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()

    # WebApp routes & APIs
    app.router.add_get('/', handle_webapp_index)
    app.router.add_get('/app', handle_webapp_index)
    app.router.add_get('/2minesg.html', handle_webapp_index)
    app.router.add_get('/indexapp_mines.js', handle_webapp_js)
    app.router.add_get('/{filename:indexapp_[a-f0-9]+\\.js}', handle_webapp_js)
    app.router.add_get('/static/app.js', handle_webapp_js)
    app.router.add_get('/app.js', handle_webapp_js)
    app.router.add_get('/api/me', handle_api_me)
    app.router.add_get('/api/avatar/{user_id}', handle_api_avatar)
    app.router.add_get('/rounds/active', handle_rounds_active)
    app.router.add_post('/rounds/{round_id}/join', handle_rounds_join)
    app.router.add_get('/ws/arena', handle_ws_arena)
    app.router.add_get('/api/arena/history', handle_api_arena_history)
    app.router.add_get('/api/arena/replay/{round_id}', handle_api_arena_replay)

    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    web.run_app(app, host=WEB_SERVER_HOST, port=WEB_SERVER_PORT)


if __name__ == "__main__":
    main()

