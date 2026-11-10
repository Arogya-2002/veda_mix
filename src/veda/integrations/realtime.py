"""Live gallery updates over Redis pub/sub: the worker publishes, each SSE stream listens.

Messages carry S3 keys, not URLs: the API signs fresh URLs per guest. Pub/sub is
fire-and-forget, so a guest who is offline misses messages; the gallery page refetches
on reconnect (the stream's first message, "ready", is the cue).
"""

import json
from collections.abc import AsyncIterator
from functools import lru_cache

import redis
import redis.asyncio as aioredis

from veda.config.configuration import get_settings
from veda.logging import get_logger

log = get_logger(__name__)


def channel(event_id: int) -> str:
    return f"gallery:{event_id}"


@lru_cache
def _sync_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=5)


def publish_photo_ready(event_id: int, photo: dict) -> None:
    """Worker side. Never raises: a missed live update only delays the photo until the
    guest's next refresh."""
    try:
        _sync_client().publish(channel(event_id), json.dumps(photo, default=str))
    except redis.RedisError:
        log.warning("publish_failed", event_id=event_id, exc_info=True)


async def photo_events(event_id: int) -> AsyncIterator[dict]:
    """API side: {"type": "ready"} once subscribed, then one dict per published photo.

    Own connection per stream (a subscribed connection can't run other commands).
    Raises redis.RedisError if Redis is unreachable; the caller answers 503.
    """
    client = aioredis.Redis.from_url(get_settings().redis_url)
    pubsub = client.pubsub(ignore_subscribe_messages=True)
    try:
        await pubsub.subscribe(channel(event_id))
        yield {"type": "ready"}
        async for message in pubsub.listen():
            yield {"type": "photo", "photo": json.loads(message["data"])}
    finally:
        await pubsub.aclose()
        await client.aclose()


@lru_cache
def _async_client() -> aioredis.Redis:
    return aioredis.Redis.from_url(get_settings().redis_url, socket_timeout=2)


async def live_viewers(event_ids: list[int]) -> dict[int, int] | None:
    """Guests with the gallery open right now, per event: every open stream is one Redis
    subscriber, and Redis drops it when the connection closes. None if Redis is down."""
    if not event_ids:
        return {}
    try:
        counts = await _async_client().pubsub_numsub(*(channel(e) for e in event_ids))
    except redis.RedisError:
        log.warning("live_viewers_unavailable", exc_info=True)
        return None
    return {event_id: n for event_id, (_, n) in zip(event_ids, counts, strict=True)}
