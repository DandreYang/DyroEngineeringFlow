from pathlib import Path
import tempfile
import unittest

from dyro.errors import DyroError
from dyro.state import atomic_write_text, ensure_safe_child_directory, exclusive_directory_lock, exclusive_lock

_MACOS_TMP_ALIAS = (
    Path("/tmp").is_symlink() and Path("/tmp").resolve() == Path("/private/tmp")
)


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

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlink support is required")
    def test_descriptor_scoped_directory_lock_and_create_reject_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-state-") as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            link = root / "state-link"
            link.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(DyroError):
                ensure_safe_child_directory(link, "objectives")
            with self.assertRaises(DyroError):
                with exclusive_directory_lock(link, "objectives.lock"):
                    pass

            self.assertFalse((outside / "objectives").exists())
            self.assertFalse((outside / "objectives.lock").exists())

    @unittest.skipUnless(_MACOS_TMP_ALIAS, "requires the macOS /tmp system alias")
    def test_exclusive_lock_reentry_across_tmp_alias_spellings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dyro-lock-alias-", dir="/tmp") as temporary:
            canonical = Path(temporary).resolve()
            try:
                alias = Path("/tmp") / canonical.relative_to("/private/tmp")
            except ValueError:
                self.skipTest("temporary directory is not below /private/tmp")
            lock_canonical = canonical / "state.lock"
            lock_alias = alias / "state.lock"
            with exclusive_lock(lock_canonical, timeout_seconds=0.2):
                with exclusive_lock(lock_alias, timeout_seconds=0.2):
                    atomic_write_text(canonical / "value", "ok\n")
            self.assertEqual((canonical / "value").read_text(encoding="utf-8"), "ok\n")
