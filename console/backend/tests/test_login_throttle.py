"""Unit tests for console login throttle."""

from __future__ import annotations

import unittest

from kin_console.login_throttle import LoginThrottle


class LoginThrottleTests(unittest.TestCase):
    def test_locks_after_max_failures(self) -> None:
        t = LoginThrottle(max_failures=3, window_sec=60, lockout_sec=30)
        self.assertTrue(t.check("k").allowed)
        self.assertTrue(t.record_failure("k").allowed)
        self.assertTrue(t.record_failure("k").allowed)
        locked = t.record_failure("k")
        self.assertFalse(locked.allowed)
        self.assertGreaterEqual(locked.retry_after_sec, 1)
        self.assertFalse(t.check("k").allowed)

    def test_success_clears_failures(self) -> None:
        t = LoginThrottle(max_failures=2, window_sec=60, lockout_sec=30)
        t.record_failure("k")
        t.record_success("k")
        self.assertTrue(t.check("k").allowed)
        self.assertTrue(t.record_failure("k").allowed)


if __name__ == "__main__":
    unittest.main()
