"""In-memory login throttle for the console HTTP API.

Keyed by client IPv4 (and optionally username) so online brute force of local
admin accounts slows down after a few failures. Process-local only - fine for
the single Host-A console; resets on service restart.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class ThrottleDecision:
    allowed: bool
    retry_after_sec: int = 0


class LoginThrottle:
    def __init__(
        self,
        *,
        max_failures: int = 5,
        window_sec: float = 300.0,
        lockout_sec: float = 300.0,
    ) -> None:
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> None:
        until = self._locked_until.get(key)
        if until is not None and until <= now:
            self._locked_until.pop(key, None)
        stamps = self._failures.get(key) or []
        kept = [t for t in stamps if now - t <= self.window_sec]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def check(self, key: str) -> ThrottleDecision:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            until = self._locked_until.get(key)
            if until is not None and until > now:
                return ThrottleDecision(
                    allowed=False, retry_after_sec=max(1, int(until - now + 0.999))
                )
            return ThrottleDecision(allowed=True)

    def record_failure(self, key: str) -> ThrottleDecision:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            stamps = self._failures.setdefault(key, [])
            stamps.append(now)
            if len(stamps) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_sec
                self._failures[key] = []
                return ThrottleDecision(
                    allowed=False, retry_after_sec=max(1, int(self.lockout_sec))
                )
            return ThrottleDecision(allowed=True)

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


LOGIN_THROTTLE = LoginThrottle()
