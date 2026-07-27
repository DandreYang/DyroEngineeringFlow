from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dyro.transactions as transactions
from dyro.transactions import FileTransaction


class FileTransactionTests(unittest.TestCase):
    def test_commit_replaces_all_staged_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "nested" / "second.txt"
            first.write_text("old", encoding="utf-8")
            transaction = FileTransaction(root)
            transaction.stage_path(first, b"new-first")
            transaction.stage_path(second, b"new-second")

            transaction.commit()

            self.assertEqual(first.read_text(encoding="utf-8"), "new-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "new-second")

    def test_commit_rolls_back_all_files_when_a_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("old-first", encoding="utf-8")
            second.write_text("old-second", encoding="utf-8")
            transaction = FileTransaction(root)
            transaction.stage_path(first, b"new-first")
            transaction.stage_path(second, b"new-second")
            real_replace = transactions.os.replace
            calls = 0

            def fail_fourth_replace(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("simulated replace failure")
                real_replace(source, target)

            with patch("dyro.transactions.os.replace", side_effect=fail_fourth_replace):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    transaction.commit()

            self.assertEqual(first.read_text(encoding="utf-8"), "old-first")
            self.assertEqual(second.read_text(encoding="utf-8"), "old-second")


if __name__ == "__main__":
    unittest.main()
