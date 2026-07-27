from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest

from dyro.evidence_store import (
    cleanup_evidence_generations,
    list_evidence_generations,
    plan_evidence_generation_cleanup,
    publish_evidence_generation,
    resolve_evidence_path,
)


class EvidenceGenerationMaintenanceTests(unittest.TestCase):
    def test_cleanup_preserves_current_and_retained_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            first = publish_evidence_generation(task, "attempt-1", {"receipt.md": b"first"})
            second = publish_evidence_generation(task, "attempt-2", {"receipt.md": b"second"})
            current = publish_evidence_generation(task, "attempt-3", {"receipt.md": b"current"})
            old = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
            os.utime(first, (old, old))
            os.utime(second, (old + 1, old + 1))

            plan = plan_evidence_generation_cleanup(
                task,
                older_than_days=30,
                keep=1,
            )

            self.assertEqual([record.generation_id for record in plan], ["attempt-1"])
            removed = cleanup_evidence_generations(task, older_than_days=30, keep=1)
            self.assertEqual([record.generation_id for record in removed], ["attempt-1"])
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(current.exists())
            self.assertEqual(resolve_evidence_path(task, "receipt.md").read_bytes(), b"current")

    def test_dry_run_reports_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary)
            old_generation = publish_evidence_generation(task, "attempt-1", {"receipt.md": b"old"})
            publish_evidence_generation(task, "attempt-2", {"receipt.md": b"current"})
            old = (datetime.now(timezone.utc) - timedelta(days=90)).timestamp()
            os.utime(old_generation, (old, old))

            targets = cleanup_evidence_generations(
                task,
                older_than_days=30,
                keep=0,
                dry_run=True,
            )

            self.assertEqual([record.generation_id for record in targets], ["attempt-1"])
            self.assertTrue(old_generation.exists())
            self.assertEqual(len(list_evidence_generations(task)), 2)


if __name__ == "__main__":
    unittest.main()
