from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dyro.provenance import (
    begin_execution_attempt,
    build_external_attempt_record,
    finish_execution_attempt,
    import_external_execution_attempt,
    render_review_binding,
    review_binding,
    validate_review_binding,
)
from dyro.tasks import run_task


class ProvenanceTest(unittest.TestCase):
    def test_attempts_share_run_and_increment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            first = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(first, result="failed")
            second = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(second, result="review")

            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual((first.attempt_number, second.attempt_number), (1, 2))
            self.assertEqual(review_binding(task_directory), (second.attempt_id, second.plan_sha256))

    def test_review_binding_requires_latest_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            attempt = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(attempt, result="review")

            valid, expected, reviewed = validate_review_binding(
                task_directory,
                f"verdict: PASS\nattempt_id: {attempt.attempt_id}\nplan_sha256: {attempt.plan_sha256}\n",
            )
            self.assertTrue(valid)
            self.assertEqual(expected, reviewed)
            invalid, _, _ = validate_review_binding(
                task_directory,
                "verdict: PASS\nattempt_id: wrong\nplan_sha256: " + ("0" * 64) + "\n",
            )
            self.assertFalse(invalid)
            self.assertEqual(
                render_review_binding("A", expected),
                f"attempt_id: {attempt.attempt_id}\nplan_sha256: {attempt.plan_sha256}\n",
            )

    def test_failed_retry_does_not_replace_review_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            reviewed = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(reviewed, result="review")
            failed = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(failed, result="failed")

            self.assertEqual(review_binding(task_directory), (reviewed.attempt_id, reviewed.plan_sha256))

    def test_continuation_attempt_records_parent_and_answer_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            parent = begin_execution_attempt(task_directory, "A", {"ready_set": ["A"]})
            finish_execution_attempt(parent, result="waiting_answer")
            continuation = {
                "ready_set": ["A"],
                "continuation": {
                    "parent_attempt_id": parent.attempt_id,
                    "answer_sha256": "b" * 64,
                },
            }
            child = begin_execution_attempt(
                task_directory,
                "A",
                continuation,
                parent_attempt_id=parent.attempt_id,
            )
            finish_execution_attempt(child, result="review")

            record = json.loads(
                (task_directory / "attempts" / f"{child.attempt_id}.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["parent_attempt_id"], parent.attempt_id)
            self.assertEqual(record["plan"]["continuation"]["answer_sha256"], "b" * 64)
            self.assertEqual(review_binding(task_directory), (child.attempt_id, child.plan_sha256))

    def test_run_task_dry_run_does_not_create_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            task = SimpleNamespace(id="A", directory=task_directory)
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))
            with patch("dyro.tasks._run_task", return_value="dry-run"):
                self.assertEqual(run_task(config, task, dry_run=True), "dry-run")
            self.assertFalse((task_directory / "attempts").exists())

    def test_run_task_persists_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            task = SimpleNamespace(id="A", directory=task_directory)
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))
            with (
                patch("dyro.tasks._execution_plan_snapshot", return_value={"ready_set": ["A"]}),
                patch("dyro.tasks._reserve_local_execution"),
                patch("dyro.tasks._run_task", return_value="failed"),
                patch("dyro.tasks.ledger"),
            ):
                self.assertEqual(run_task(config, task), "failed")
                self.assertEqual(run_task(config, task), "failed")
            attempts = sorted((task_directory / "attempts").glob("*.json"))
            self.assertEqual(len(attempts), 2)

    def test_reservation_failure_does_not_create_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            task = SimpleNamespace(id="A", directory=task_directory)
            config = SimpleNamespace(policy=SimpleNamespace(execution_mode="local"))
            with patch(
                "dyro.tasks._reserve_local_execution",
                side_effect=RuntimeError("reservation failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "reservation failed"):
                    run_task(config, task)
            self.assertFalse((task_directory / "attempts").exists())

    def test_external_provenance_is_verified_and_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task_directory = Path(temporary)
            (task_directory / "task.toml").write_text('schema_version = 1\nid = "A"\n', encoding="utf-8")
            receipt_sha256 = "a" * 64
            plan = {"schema_version": 1, "source": "external_evidence_bundle", "task_id": "A"}
            record = build_external_attempt_record(
                task_directory,
                "A",
                plan,
                result="DONE",
                receipt_sha256=receipt_sha256,
            )
            provenance = task_directory / "provenance.json"
            provenance.write_text(json.dumps(record), encoding="utf-8")
            imported = import_external_execution_attempt(
                task_directory,
                "A",
                provenance=provenance,
                receipt_sha256=receipt_sha256,
                result="DONE",
                expected_plan=plan,
            )
            self.assertEqual(imported["attempt_id"], record["attempt_id"])
            self.assertEqual(review_binding(task_directory), (record["attempt_id"], record["plan_sha256"]))
            self.assertEqual(
                import_external_execution_attempt(
                    task_directory,
                    "A",
                    provenance=provenance,
                    receipt_sha256=receipt_sha256,
                    result="DONE",
                    expected_plan=plan,
                )["attempt_id"],
                record["attempt_id"],
            )

            tampered = dict(record)
            tampered["plan_sha256"] = "0" * 64
            provenance.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "plan_sha256"):
                import_external_execution_attempt(
                    task_directory,
                    "A",
                    provenance=provenance,
                    receipt_sha256=receipt_sha256,
                    result="DONE",
                    expected_plan=plan,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
