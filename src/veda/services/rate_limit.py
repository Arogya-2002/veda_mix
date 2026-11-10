"""Fixed-window counters in Redis, e.g. wrong PIN guesses."""

from functools import lru_cache

import redis.asyncio as aioredis

from veda.config.configuration import get_settings


@lru_cache
def _client() -> aioredis.Redis:
    return aioredis.Redis.from_url(get_settings().redis_url, socket_timeout=2)


async def over_limit(keys: dict[str, int]) -> bool:
    """True if any key has reached its limit. Raises redis.RedisError if Redis is down."""
    counts = await _client().mget(list(keys))
    return any(int(c or 0) >= limit for c, limit in zip(counts, keys.values(), strict=True))


async def hit(keys: list[str], window_s: int) -> None:
    """Count one more; a key's window starts at its first hit."""
    async with _client().pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.incr(key)
            pipe.expire(key, window_s, nx=True)
        await pipe.execute()
