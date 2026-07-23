from pathlib import Path
import tempfile
import unittest

from dyro.state import atomic_write_text, exclusive_lock


class StateTests(unittest.TestCase):
    def test_atomic_write_and_reentrant_lock_leave_complete_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-state-") as tmp:
            root = Path(tmp)
            target = root / "state" / "status"
            lock = root / "state" / "status.lock"
            with exclusive_lock(lock):
                atomic_write_text(target, "assigned\n")
                with exclusive_lock(lock):
                    atomic_write_text(target, "in_progress\n")

            self.assertEqual(target.read_text(encoding="utf-8"), "in_progress\n")
            self.assertEqual(list(target.parent.glob(".status.*")), [])
