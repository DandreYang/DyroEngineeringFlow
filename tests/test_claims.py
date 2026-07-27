from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyro.tasks import Task, claim_task, release_task_claim, renew_task_claim, status


class ClaimLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = Task(
            id="A",
            title="Claim lease",
            line="alpha",
            risk="write",
            executor="noop",
            reviewer="noop",
            repositories=("api",),
            directory=self.root / "A",
        )
        self.task.directory.mkdir()
        self.config = SimpleNamespace(
            root=self.root,
            policy=SimpleNamespace(
                execution_mode="external",
                require_external_signoff=False,
            )
        )
        self.patches = (
            patch("dyro.tasks.exclusive_lock", return_value=nullcontext()),
            patch("dyro.tasks.check_dispatchable"),
            patch("dyro.tasks.ledger"),
        )
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temporary.cleanup()

    def test_expired_claim_can_be_taken_over(self) -> None:
        self.assertEqual(claim_task(self.config, self.task, runner="runner-1", lease_seconds=60), "assigned")
        claim_path = self.task.directory / "claim.json"
        payload = json.loads(claim_path.read_text(encoding="utf-8"))
        payload["lease_expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        claim_path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(claim_task(self.config, self.task, runner="runner-2", lease_seconds=120), "assigned")
        replaced = json.loads(claim_path.read_text(encoding="utf-8"))
        self.assertEqual(replaced["runner"], "runner-2")
        self.assertEqual(replaced["generation"], 2)

    def test_claim_can_be_renewed_and_released(self) -> None:
        claim_task(self.config, self.task, runner="runner-1", lease_seconds=60)
        before = json.loads((self.task.directory / "claim.json").read_text(encoding="utf-8"))

        self.assertEqual(
            renew_task_claim(self.config, self.task, runner="runner-1", lease_seconds=120),
            "assigned",
        )
        after = json.loads((self.task.directory / "claim.json").read_text(encoding="utf-8"))
        self.assertGreater(after["lease_expires_at"], before["lease_expires_at"])

        self.assertEqual(release_task_claim(self.config, self.task, runner="runner-1"), "backlog")
        self.assertFalse((self.task.directory / "claim.json").exists())
        self.assertEqual(status(self.config, self.task), "backlog")


if __name__ == "__main__":
    unittest.main()
