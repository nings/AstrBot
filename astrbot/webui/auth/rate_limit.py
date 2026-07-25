"""Login throttling and per-user concurrency limiting for the webui module.

**Everything in this file is PROCESS-LOCAL in-memory state** (plain dicts
guarded by ``asyncio.Lock``s). There is no cross-process/cross-replica
coordination. That is an explicit, accepted MVP limitation, not an
oversight: the plan documents (section 6, risk table) that
``ChatService``'s own ``chat_runs``/``chat_runs_by_session`` registry is
*also* an in-process dict with no cross-restart/cross-replica persistence,
and ties the whole webui module's Phase 1 deployment topology to
``replicas: 1`` for exactly that reason. If AstrBot is ever deployed with
more than one webui-serving process, the throttling below becomes
per-replica rather than global (an attacker could get ``N x capacity``
attempts by hitting different replicas), and the concurrency guard's
in-process half becomes purely advisory. The one piece of state here that
*does* survive a restart/replica boundary is ``WebUiUsageCounter`` itself
(a real DB row) -- see ``can_start_run``/``can_start_run_for_counter``,
which is why the per-user concurrency limiter is described as "backed by
``WebUiUsageCounter`` and/or in-process state": the DB-backed check is the
authoritative one, the in-process ``InProcessConcurrencyGuard`` is a
same-process fast-path/defense-in-depth addition, never a replacement.

Covers two independent mechanisms:

1. ``LoginThrottle`` -- per-IP and per-account request throttling for the
   register/login/refresh endpoints. Two layers, deliberately redundant:

   * A token bucket per dimension (shape mirrors, but does not import,
     ``astrbot.dashboard.server._AuthRateLimiter``/``_RateLimiterRegistry``
     -- this file must not import ``astrbot.dashboard.*``, capability
     wall rule 5), consumed on *every* attempt regardless of outcome
     (matching how the core dashboard's own rate limiter gates
     ``_RATE_LIMITED_ENDPOINTS`` unconditionally). With the default
     settings (``auth_rate_limit_capacity=5``), this alone reproduces the
     plan's exit criterion ("6 次错误密码后触发限流": attempts 1-5 each
     consume one of the 5 starting tokens, the 6th finds the bucket
     empty).
   * An explicit consecutive-*failure* counter with exponential backoff
     lockout (``record_failure``/``record_success``), independent of the
     bucket, matching the plan's literal "lock/backoff after ~6 failures"
     phrasing -- this one only reacts to failures (not successful
     attempts), and its lockout window grows the longer the attacker
     keeps failing, rather than the bucket's fixed linear refill rate.

2. ``can_start_run`` / ``can_start_run_for_counter`` (pure) and
   ``InProcessConcurrencyGuard`` (stateful, in-process) -- the per-user
   concurrent-run limiter referenced in the plan's exit criteria.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from astrbot.webui.config import WebUiSettings, get_webui_settings
from astrbot.webui.db.models import WebUiUsageCounter

# ---------------------------------------------------------------------------
# Consecutive-failure lockout tuning. Not part of `WebUiSettings` (that
# dataclass belongs to `astrbot/webui/config.py`, which this assignment does
# not own -- see notes_for_integrator) so these live as local constants
# instead of inventing new config-file fields out of scope.
# ---------------------------------------------------------------------------

FAILURE_LOCKOUT_THRESHOLD = 6
"""Consecutive failures (per IP, or per normalized account) before lockout."""
FAILURE_LOCKOUT_BASE_SECONDS = 30.0
"""Lockout duration once the threshold is first reached."""
FAILURE_LOCKOUT_MAX_SECONDS = 15 * 60.0
"""Cap on the exponentially-growing lockout duration for repeat offenders."""


# ---------------------------------------------------------------------------
# Token bucket -- reimplementation (not import) of the same algorithm as
# astrbot.dashboard.server._AuthRateLimiter / _RateLimiterRegistry. Kept
# structurally close to that shape on purpose (this file's assignment calls
# it out as "仿 server.py _AuthRateLimiter"), but this file must not import
# astrbot.dashboard.* (capability wall, rule 5), so it is a fresh
# implementation rather than a subclass/reuse.
# ---------------------------------------------------------------------------


class _TokenBucket:
    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.last_accessed = self.last_refill
        self.lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_per_second
            )
            self.last_refill = now
            self.last_accessed = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


class _TokenBucketRegistry:
    """Per-key token bucket registry with idle-entry eviction."""

    _ENTRY_TTL: float = 3600.0
    _EVICT_INTERVAL: float = 1800.0

    def __init__(self) -> None:
        self._buckets: dict[str, _TokenBucket] = {}
        self._last_eviction = time.monotonic()

    def get_or_create(
        self, key: str, capacity: float, refill_per_second: float
    ) -> _TokenBucket:
        self._evict_expired()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(capacity, refill_per_second)
            self._buckets[key] = bucket
        return bucket

    def _evict_expired(self) -> None:
        now = time.monotonic()
        if now - self._last_eviction < self._EVICT_INTERVAL:
            return
        self._last_eviction = now
        cutoff = now - self._ENTRY_TTL
        stale = [k for k, v in self._buckets.items() if v.last_accessed < cutoff]
        for k in stale:
            del self._buckets[k]

    def __len__(self) -> int:
        return len(self._buckets)


@dataclass(frozen=True)
class ThrottleDecision:
    """Result of :meth:`LoginThrottle.check`."""

    allowed: bool
    reason: str | None = None
    """One of ``None``, ``"ip_locked"``, ``"ip_rate_limited"``,
    ``"account_locked"``, ``"account_rate_limited"`` -- useful for
    logging/metrics; callers should return a generic 429 to the client
    regardless of which dimension tripped, to avoid leaking whether an
    account exists."""


class LoginThrottle:
    """Combined per-IP + per-account throttle for register/login/refresh.

    Usage from ``auth_routes.py``::

        throttle = get_login_throttle()
        decision = await throttle.check(ip=client_ip, account=email)
        if not decision.allowed:
            return 429
        if not credentials_are_valid:
            throttle.record_failure(ip=client_ip, account=email)
            await apply_failure_delay()
            return 401
        throttle.record_success(ip=client_ip, account=email)
        ...
    """

    def __init__(self, settings: WebUiSettings | None = None) -> None:
        self._settings = settings or get_webui_settings()
        self._capacity = float(self._settings.auth_rate_limit_capacity)
        self._refill_per_second = (
            self._settings.auth_rate_limit_refill_per_minute / 60.0
        )
        self._ip_buckets = _TokenBucketRegistry()
        self._account_buckets = _TokenBucketRegistry()
        self._failure_counts: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def _normalize_account_key(account: str) -> str:
        return account.strip().lower()

    def _failure_keys(self, ip: str | None, account: str | None) -> list[str]:
        keys: list[str] = []
        if ip:
            keys.append(f"ip:{ip}")
        if account:
            keys.append(f"acct:{self._normalize_account_key(account)}")
        return keys

    def _is_locked(self, key: str) -> bool:
        locked_until = self._locked_until.get(key)
        if locked_until is None:
            return False
        if time.monotonic() >= locked_until:
            del self._locked_until[key]
            self._failure_counts.pop(key, None)
            return False
        return True

    async def check(
        self, *, ip: str | None, account: str | None
    ) -> ThrottleDecision:
        """Consume one token from each configured dimension; report whether the attempt is allowed.

        Call this **before** verifying credentials on every
        register/login/refresh attempt (consumed regardless of outcome --
        see module docstring for why that reproduces the plan's "6th
        attempt gets throttled" exit criterion).
        """
        if ip:
            ip_key = f"ip:{ip}"
            if self._is_locked(ip_key):
                return ThrottleDecision(False, "ip_locked")
            bucket = self._ip_buckets.get_or_create(
                ip, self._capacity, self._refill_per_second
            )
            if not await bucket.acquire():
                return ThrottleDecision(False, "ip_rate_limited")

        if account:
            account_key = f"acct:{self._normalize_account_key(account)}"
            if self._is_locked(account_key):
                return ThrottleDecision(False, "account_locked")
            bucket = self._account_buckets.get_or_create(
                self._normalize_account_key(account),
                self._capacity,
                self._refill_per_second,
            )
            if not await bucket.acquire():
                return ThrottleDecision(False, "account_rate_limited")

        return ThrottleDecision(True, None)

    def record_failure(self, *, ip: str | None, account: str | None) -> None:
        """Record one failed attempt against both the IP and account dimensions.

        Once a key accumulates ``FAILURE_LOCKOUT_THRESHOLD`` (default 6)
        consecutive failures, it is locked out for
        ``FAILURE_LOCKOUT_BASE_SECONDS * 2 ** excess`` seconds (capped at
        ``FAILURE_LOCKOUT_MAX_SECONDS``), independent of -- and in
        addition to -- the token-bucket throttling in :meth:`check`.
        """
        for key in self._failure_keys(ip, account):
            count = self._failure_counts.get(key, 0) + 1
            self._failure_counts[key] = count
            if count >= FAILURE_LOCKOUT_THRESHOLD:
                excess = count - FAILURE_LOCKOUT_THRESHOLD
                backoff = min(
                    FAILURE_LOCKOUT_BASE_SECONDS * (2**excess),
                    FAILURE_LOCKOUT_MAX_SECONDS,
                )
                self._locked_until[key] = time.monotonic() + backoff

    def record_success(self, *, ip: str | None, account: str | None) -> None:
        """Clear failure/lockout state for both dimensions after a successful attempt."""
        for key in self._failure_keys(ip, account):
            self._failure_counts.pop(key, None)
            self._locked_until.pop(key, None)


async def apply_failure_delay(settings: WebUiSettings | None = None) -> None:
    """Sleep for the fixed post-failure delay (timing-attack/brute-force mitigation).

    Mirrors ``astrbot.dashboard.services.auth_service.AuthService.login``'s
    ``await asyncio.sleep(3)`` on a failed attempt (reimplemented, not
    imported, per the capability wall). Call this from ``auth_routes.py``
    immediately after a failed credential check, before returning the
    error response.
    """
    settings = settings or get_webui_settings()
    await asyncio.sleep(settings.auth_failure_delay_seconds)


_default_login_throttle: LoginThrottle | None = None


def get_login_throttle() -> LoginThrottle:
    """Return the process-wide default :class:`LoginThrottle` (lazily constructed)."""
    global _default_login_throttle
    if _default_login_throttle is None:
        _default_login_throttle = LoginThrottle()
    return _default_login_throttle


# ---------------------------------------------------------------------------
# Per-user concurrent-run limiter.
# ---------------------------------------------------------------------------


def can_start_run(
    current_concurrent_runs: int,
    *,
    settings: WebUiSettings | None = None,
) -> bool:
    """Pure decision: may a user with ``current_concurrent_runs`` in-flight chat runs start one more?

    ``current_concurrent_runs`` must be the caller's own up-to-date read
    of ``WebUiUsageCounter.concurrent_runs`` for that user's current
    accounting window. This function performs no DB I/O; the
    authoritative check-then-increment must happen inside a single DB
    transaction in the service layer (this file cannot import
    ``astrbot.webui.services.*`` without creating a layering cycle) --
    e.g. an ``UPDATE ... WHERE concurrent_runs < :max`` / rowcount pattern,
    so the guarantee holds even under concurrent requests within one
    process.
    """
    settings = settings or get_webui_settings()
    return current_concurrent_runs < settings.max_concurrent_runs_per_user


def can_start_run_for_counter(
    counter: WebUiUsageCounter | None,
    *,
    settings: WebUiSettings | None = None,
) -> bool:
    """Same decision as :func:`can_start_run`, taking the ``WebUiUsageCounter`` row directly.

    ``counter=None`` (no row yet created for the current window) is
    equivalent to zero in-flight runs.
    """
    current = counter.concurrent_runs if counter is not None else 0
    return can_start_run(current, settings=settings)


class InProcessConcurrencyGuard:
    """Best-effort, PROCESS-LOCAL fallback/defense-in-depth for the per-user concurrent-run cap.

    This is **not** a substitute for the DB-backed ``WebUiUsageCounter``
    check (:func:`can_start_run`/:func:`can_start_run_for_counter`) -- it
    is single-process, in-memory state that resets on every restart and
    is invisible across replicas (see module docstring). It exists as a
    fast, zero-DB-round-trip guard for code paths that want to reject an
    obviously-over-limit request before ever touching the database, and
    as a safety net alongside the DB-backed counter. Acceptable for the
    Phase 1 MVP because the whole module -- including the ``chat_runs``
    in-memory registry ``ChatService`` itself already relies on -- is
    documented as single-process-only (plan section 6).
    """

    def __init__(self, settings: WebUiSettings | None = None) -> None:
        self._settings = settings or get_webui_settings()
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(self, user_id: str) -> bool:
        async with self._lock:
            current = self._counts.get(user_id, 0)
            if current >= self._settings.max_concurrent_runs_per_user:
                return False
            self._counts[user_id] = current + 1
            return True

    async def release(self, user_id: str) -> None:
        async with self._lock:
            current = self._counts.get(user_id, 0)
            if current <= 1:
                self._counts.pop(user_id, None)
            else:
                self._counts[user_id] = current - 1


_default_concurrency_guard: InProcessConcurrencyGuard | None = None


def get_concurrency_guard() -> InProcessConcurrencyGuard:
    """Return the process-wide default :class:`InProcessConcurrencyGuard` (lazily constructed)."""
    global _default_concurrency_guard
    if _default_concurrency_guard is None:
        _default_concurrency_guard = InProcessConcurrencyGuard()
    return _default_concurrency_guard
