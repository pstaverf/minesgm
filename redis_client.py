import json
import logging
from db_config import REDIS_URL

logger = logging.getLogger(__name__)

_redis_pool = None
_in_memory_cache = {}


async def get_redis():
    global _redis_pool
    if _redis_pool is not None:
        return _redis_pool
    try:
        import redis.asyncio as aioredis
        _redis_pool = aioredis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3.0)
        await _redis_pool.ping()
        logger.info("Connected to Redis successfully!")
        return _redis_pool
    except Exception as e:
        logger.warning(f"Redis unavailable ({e}), using in-memory fallback cache.")
        _redis_pool = None
        return None


async def redis_set(key: str, value, expire_sec: int = None):
    r = await get_redis()
    val_str = json.dumps(value, ensure_ascii=False) if not isinstance(value, (str, int, float)) else str(value)
    if r:
        try:
            if expire_sec:
                await r.setex(key, expire_sec, val_str)
            else:
                await r.set(key, val_str)
            return True
        except Exception as e:
            logger.warning(f"Redis set failed: {e}")
    _in_memory_cache[key] = val_str
    return True


async def redis_get(key: str, default=None):
    r = await get_redis()
    if r:
        try:
            val = await r.get(key)
            if val is not None:
                try:
                    return json.loads(val)
                except Exception:
                    return val
        except Exception as e:
            logger.warning(f"Redis get failed: {e}")
    val = _in_memory_cache.get(key)
    if val is not None:
        try:
            return json.loads(val)
        except Exception:
            return val
    return default


async def redis_delete(key: str):
    r = await get_redis()
    if r:
        try:
            await r.delete(key)
        except Exception:
            pass
    _in_memory_cache.pop(key, None)
    return True


async def redis_set_active_game(user_id: int, game_id: int, game_data: dict):
    await redis_set(f"active_game:{user_id}", game_id, expire_sec=86400)
    await redis_set(f"game_data:{game_id}", game_data, expire_sec=86400)


async def redis_get_active_game(user_id: int):
    return await redis_get(f"active_game:{user_id}")


async def redis_remove_active_game(user_id: int, game_id: int = None):
    await redis_delete(f"active_game:{user_id}")
    if game_id:
        await redis_delete(f"game_data:{game_id}")
