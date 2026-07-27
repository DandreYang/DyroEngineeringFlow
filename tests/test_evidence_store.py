from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dyro.evidence_store import (
    current_evidence_directory,
    publish_evidence_generation,
    resolve_evidence_path,
)
from dyro.errors import ValidationError


class ImmutableEvidenceStoreTests(unittest.TestCase):
    def test_pointer_switch_exposes_one_complete_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            first = publish_evidence_generation(
                task,
                "attempt-1",
                {
                    "receipt.md": b"result: DONE\n",
                    "task-heads.json": b'{"generation": 1}\n',
                },
            )

            self.assertEqual(current_evidence_directory(task), first)
            self.assertEqual(resolve_evidence_path(task, "task-heads.json").read_bytes(), b'{"generation": 1}\n')

            second = publish_evidence_generation(
                task,
                "attempt-2",
                {
                    "receipt.md": b"result: DONE\n",
                    "task-heads.json": b'{"generation": 2}\n',
                },
            )

            self.assertEqual(current_evidence_directory(task), second)
            self.assertEqual(resolve_evidence_path(task, "task-heads.json").read_bytes(), b'{"generation": 2}\n')

    def test_failed_pointer_replace_keeps_previous_generation_current(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            first = publish_evidence_generation(task, "attempt-1", {"receipt.md": b"first"})
            real_replace = os.replace

            def fail_pointer_replace(source: Path, target: Path) -> None:
                if Path(target).name == "current-evidence.json":
                    raise OSError("simulated pointer failure")
                real_replace(source, target)

            with patch("dyro.evidence_store.os.replace", side_effect=fail_pointer_replace):
                with self.assertRaisesRegex(OSError, "simulated pointer failure"):
                    publish_evidence_generation(task, "attempt-2", {"receipt.md": b"second"})

            self.assertEqual(current_evidence_directory(task), first)
            self.assertEqual(resolve_evidence_path(task, "receipt.md").read_bytes(), b"first")
            self.assertTrue((task / "evidence-imports" / "attempt-2").is_dir())

    def test_current_generation_detects_artifact_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            generation = publish_evidence_generation(task, "attempt-1", {"receipt.md": b"trusted"})
            receipt = generation / "receipt.md"
            receipt.chmod(0o600)
            receipt.write_bytes(b"tampered")

            with self.assertRaisesRegex(ValidationError, "哈希不匹配"):
                resolve_evidence_path(task, "receipt.md")

    def test_generation_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            generations = task / "evidence-imports"
            generations.mkdir()
            outside = task / "outside"
            outside.mkdir()
            (generations / "attempt-1").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValidationError, "不能是符号链接"):
                publish_evidence_generation(task, "attempt-1", {"receipt.md": b"trusted"})


if __name__ == "__main__":
    unittest.main()
