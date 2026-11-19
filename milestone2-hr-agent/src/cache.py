from __future__ import annotations

import json
import logging
from typing import Any

import redis


logger = logging.getLogger(__name__)


class ResilientCache:
    """Redis cache that degrades safely to cache misses."""

    def __init__(self, redis_url: str) -> None:
        self.client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    def health(self) -> bool:
        if self.client is None:
            return False

        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False

    def get(self, key: str) -> Any | None:
        if self.client is None:
            return None

        try:
            value = self.client.get(key)
            if value is None:
                return None
            return json.loads(value)
        except (redis.RedisError, json.JSONDecodeError, TypeError):
            # Redis is an optimization, not the source of truth.
            logger.warning(
                "Cache read failed; continuing as a cache miss.",
                extra={"cache_key": key[:80]},
            )
            return None

    def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: int,
    ) -> None:
        if self.client is None or ttl_seconds <= 0:
            return

        try:
            payload = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            self.client.setex(key, ttl_seconds, payload)
        except (redis.RedisError, TypeError, ValueError):
            logger.warning(
                "Cache write failed; continuing without cache.",
                extra={"cache_key": key[:80]},
            )


class NullCache(ResilientCache):
    def __init__(self) -> None:
        self.client = None