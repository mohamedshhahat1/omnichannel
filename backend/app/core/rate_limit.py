"""Shared fixed-window rate limiting.

`docs/security.md` 2.5 requires login to be rate limited "per account **and**
per IP". Phase 3 shipped only the per-account half: `users.failed_logins` plus
`locked_until`. That leaves a credential-stuffing run free to spread one
attempt across ten thousand accounts from a single host and never trip a single
lockout, because the counter it increments is attached to the victim rather
than to the attacker.

This module is the other half, and is deliberately generic: webhooks, password
reset and AI runs all need the same shape later.

Design notes worth the reader's time:

* **Fixed window, not a token bucket.** Two Redis commands, no Lua, no clock
  skew between processes. A fixed window lets through at most one extra burst
  at a boundary; for a control whose job is to make automation expensive rather
  than to shape traffic precisely, that is an acceptable trade for something an
  operator can reason about at three in the morning.
* **The bucket is hashed.** A source address is personal data and would
  otherwise sit in plaintext in Redis, visible to anyone with `KEYS`. The
  digest is also what keeps IPv6 colons out of the key structure.
* **Failure is a decision, not an accident.** If Redis is unreachable the
  limiter returns `degraded=True` and honours the configured `fail_open`
  choice. Failing open is the default because the per-account lockout still
  stands - the system falls back to exactly the protection it had before this
  module existed - whereas failing closed turns a cache outage into a total
  authentication outage. Deployments that prefer the opposite trade set
  `auth.login_rate_limit_fail_open` to false.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_BUCKET_DIGEST_CHARS: Final = 32


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Whether one attempt may proceed.

    `degraded` is true when the answer did not come from a working shared
    counter, so a caller can log the fact that the control was not actually
    enforced instead of silently believing it was.
    """

    allowed: bool
    retry_after_seconds: int = 0
    degraded: bool = False


class RateLimiter(Protocol):
    """The one operation a caller needs: count this attempt, may it proceed?"""

    async def hit(self, bucket: str) -> RateLimitDecision:
        """Record an attempt against `bucket` and decide on it."""
        ...


def bucket_digest(value: str) -> str:
    """Return the short, stable digest used in place of a raw bucket value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_BUCKET_DIGEST_CHARS]


class FixedWindowRateLimiter:
    """A Redis-backed counter that resets on a fixed window.

    Keys are `oc:{env}:rl:{purpose}:{digest}`. The environment segment keeps a
    staging run from throttling production if the two ever share an instance;
    the purpose segment keeps login attempts from colliding with any future
    limiter. This is not the tenant key namespace from `app.core.redis`, and
    deliberately so: the caller of a login attempt has no tenant yet.
    """

    def __init__(
        self,
        redis: Redis | None,
        *,
        environment: str,
        purpose: str,
        limit: int,
        window_seconds: int,
        fail_open: bool = True,
    ) -> None:
        self._redis = redis
        self._environment = environment
        self._purpose = purpose
        self._limit = limit
        self._window_seconds = window_seconds
        self._fail_open = fail_open

    @property
    def enabled(self) -> bool:
        """False when there is no Redis to count in, or the limit is zero."""
        return self._redis is not None and self._limit > 0

    def _key(self, bucket: str) -> str:
        return f"oc:{self._environment}:rl:{self._purpose}:{bucket_digest(bucket)}"

    async def hit(self, bucket: str) -> RateLimitDecision:
        """Count one attempt and decide whether it may proceed."""
        client = self._redis
        if client is None or self._limit <= 0:
            # No shared counter: say so rather than pretend the control ran.
            return RateLimitDecision(allowed=True, degraded=True)

        key = self._key(bucket)
        try:
            count = int(await client.incr(key))
            if count == 1:
                # Only the first hit sets the expiry, so the window is fixed
                # from the first attempt instead of sliding on every one.
                await client.expire(key, self._window_seconds)
                ttl = self._window_seconds
            else:
                ttl = int(await client.ttl(key))
                if ttl < 0:
                    # A counter with no expiry would throttle forever.
                    await client.expire(key, self._window_seconds)
                    ttl = self._window_seconds
        except RedisError:
            logger.warning(
                "rate_limit.backend_unavailable",
                extra={"purpose": self._purpose, "fail_open": self._fail_open},
                exc_info=True,
            )
            return RateLimitDecision(
                allowed=self._fail_open,
                retry_after_seconds=0 if self._fail_open else self._window_seconds,
                degraded=True,
            )

        if count > self._limit:
            return RateLimitDecision(allowed=False, retry_after_seconds=max(ttl, 1))
        return RateLimitDecision(allowed=True)


class InMemoryFixedWindowRateLimiter:
    """A process-local limiter with the same contract.

    Used by the test suite, and usable in single-process local development. It
    is **not** a substitute for the Redis limiter in a deployment: every worker
    would keep its own counter, so the effective limit would be multiplied by
    the worker count.
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, tuple[float, int]] = {}

    @property
    def enabled(self) -> bool:
        """True whenever a positive limit is configured."""
        return self._limit > 0

    async def hit(self, bucket: str) -> RateLimitDecision:
        """Count one attempt and decide whether it may proceed."""
        if self._limit <= 0:
            return RateLimitDecision(allowed=True, degraded=True)
        now = self._clock()
        started_at, count = self._windows.get(bucket, (now, 0))
        if now - started_at >= self._window_seconds:
            started_at, count = now, 0
        count += 1
        self._windows[bucket] = (started_at, count)
        if count > self._limit:
            remaining = self._window_seconds - (now - started_at)
            return RateLimitDecision(
                allowed=False,
                retry_after_seconds=max(int(remaining), 1),
            )
        return RateLimitDecision(allowed=True)
