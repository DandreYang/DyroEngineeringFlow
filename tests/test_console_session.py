from __future__ import annotations

from datetime import datetime, timezone
import unittest

from dyro.console.session import (
    BOOTSTRAP_MAX_FAILURES,
    BOOTSTRAP_TTL_SECONDS,
    SESSION_IDLE_TTL_SECONDS,
    ConsoleSessionStore,
    SessionRejected,
)


class ConsoleSessionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [0.0]
        self.wall = [datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)]
        self.store = ConsoleSessionStore(
            bootstrap_secret="a" * 43,
            monotonic_clock=lambda: self.now[0],
            wall_clock=lambda: self.wall[0],
        )

    def test_bootstrap_expiry_and_failure_limit_fail_closed(self) -> None:
        self.now[0] = BOOTSTRAP_TTL_SECONDS
        with self.assertRaises(SessionRejected):
            self.store.exchange("a" * 43)

        limited = ConsoleSessionStore(
            bootstrap_secret="b" * 43,
            monotonic_clock=lambda: 0.0,
            wall_clock=lambda: self.wall[0],
        )
        for _ in range(BOOTSTRAP_MAX_FAILURES):
            with self.assertRaises(SessionRejected):
                limited.exchange("c" * 43)
        with self.assertRaises(SessionRejected):
            limited.exchange("b" * 43)

    def test_authorization_refreshes_idle_only_and_remains_memory_local(self) -> None:
        session = self.store.exchange("a" * 43)
        with self.assertRaises(SessionRejected):
            self.store.bootstrap_secret

        self.now[0] = SESSION_IDLE_TTL_SECONDS - 1
        refreshed = self.store.authorize(session.token)
        self.assertGreater(refreshed.expires_at, self.wall[0])
        self.now[0] += SESSION_IDLE_TTL_SECONDS + 1
        with self.assertRaises(SessionRejected):
            self.store.authorize(session.token)


if __name__ == "__main__":
    unittest.main()
