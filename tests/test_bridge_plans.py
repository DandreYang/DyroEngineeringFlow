from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from dyro.bridge.git_read import (
    GitAncestryObservation,
    GitReadError,
    GitReadFailure,
    build_ancestor_invocation,
    build_helper_invocation,
    build_head_invocation,
    git_read_environment,
    inspect_ancestry_readonly,
    invocation_is_allowlisted,
    is_ancestor_readonly,
    helper_invocation_is_allowlisted,
)
from dyro.bridge.observations import BridgeObservationError, ObservationFailure
from dyro.bridge.plans import (
    PLAN_OPERATIONS,
    BridgePlan,
    build_objective_bridge_plan,
    compute_plan_sha256,
)
from dyro.bridge.schemas import get_operation_schema
from dyro.canonical import canonical_json_bytes
from dyro.config import load
from dyro.continuation.store import create_objective, stop_objective
from dyro.errors import ValidationError
from dyro.tasks import load_task, set_status, task_template
from dyro.read_limits import ObservationLimits
from dyro.workspace import create_line

from .support import WorkspaceCase, shell


def _contract() -> str:
    return """schema_version = 1
id = "release"
title = "Release"
line = "alpha"
targets = ["TASK-A"]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 10
max_attempts_per_task = 2
max_failures = 2
max_no_progress_cycles = 2
max_parallel = 1
"""


class BridgePlanTests(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = load(self.root)
        create_line(self.config, line_id="alpha", branch="feat/alpha", base="main")
        task_dir = self.config.task_specs_dir / "TASK-A"
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_template(
                "TASK-A",
                "secret-title-must-not-cross-bridge",
                "alpha",
                "api",
                "services/api",
            ).replace('agent = "codex"', 'agent = "secret-agent-name"'),
            encoding="utf-8",
        )
        task_dir.joinpath("handoff.md").write_text(
            "token=secret-handoff-value\n", encoding="utf-8"
        )
        create_objective(self.config, _contract())
        self.now = datetime(2026, 8, 7, 1, 2, 3, tzinfo=timezone.utc)

    def _plan(self, operation: str, *, now: datetime | None = None) -> BridgePlan:
        return build_objective_bridge_plan(
            operation=operation,
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: now or self.now,
            git_reader=lambda *_args, **_kwargs: self.fail(
                "backlog plan must not start Git"
            ),
        )

    def _write_extra_task(self, task_id: str, content: str | None = None) -> Path:
        directory = self.config.task_specs_dir / task_id
        directory.mkdir(parents=True)
        directory.joinpath("task.toml").write_text(
            content or task_template(task_id, task_id, "alpha", "api", "services/api"),
            encoding="utf-8",
        )
        return directory

    def _mark_done_with_head(self, task_id: str, head: str) -> None:
        task = load_task(self.config, task_id)
        task.directory.joinpath("status").write_text("done\n", encoding="utf-8")
        task.directory.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "line": task.line,
                    "branch": f"task/{task_id}",
                    "repositories": {"api": head},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _fixture_git_reader(
        repository: Path, ancestor: str, **_kwargs: object
    ) -> GitAncestryObservation:
        destination_head = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        ancestry = subprocess.run(
            (
                "git",
                "--no-optional-locks",
                "merge-base",
                "--is-ancestor",
                ancestor,
                destination_head,
            ),
            cwd=repository,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ancestry.returncode not in {0, 1}:
            raise GitReadError(GitReadFailure.PARTIAL)

        def digest(value: str) -> str:
            return (
                "sha256:"
                + hashlib.sha256(
                    b"dyro.git-oid.v1\0" + value.encode("ascii")
                ).hexdigest()
            )

        return GitAncestryObservation(
            task_head_sha256=digest(ancestor),
            destination_head_sha256=digest(destination_head),
            is_ancestor=ancestry.returncode == 0,
        )

    def _create_objective(
        self, objective_id: str, targets: tuple[str, ...], *, line: str = "alpha"
    ) -> None:
        rendered_targets = ", ".join(json.dumps(item) for item in targets)
        create_objective(
            self.config,
            f'''schema_version = 1
id = "{objective_id}"
title = "{objective_id}"
line = "{line}"
targets = [{rendered_targets}]

[continuation]
requested_mode = "supervised"
operations = ["execute", "review"]

[budget]
max_actions = 10
max_attempts_per_task = 2
max_failures = 2
max_no_progress_cycles = 2
max_parallel = 2
''',
        )

    def test_every_plan_operation_has_d05_envelope_and_own_schema(self) -> None:
        self.assertEqual(len(PLAN_OPERATIONS), 5)
        read_sets: dict[str, dict[str, object]] = {}
        for operation in PLAN_OPERATIONS:
            plan = self._plan(operation)
            payload = plan.as_dict()
            Draft202012Validator(
                get_operation_schema(operation).output_schema()
            ).validate(payload)
            self.assertIs(payload["executable"], False)
            self.assertEqual(payload["authorization"], "none")
            self.assertEqual(payload["protocol_major"], 1)
            self.assertEqual(payload["operation"], operation)
            self.assertEqual(payload["effects"], [])
            self.assertEqual(payload["maximum_risk"], "PLAN")
            self.assertEqual(payload["effective_risk"], "PLAN")
            self.assertEqual(payload["read_set"]["integration_inspection"], "complete")
            read_sets[operation] = get_operation_schema(operation).output_schema()[
                "properties"
            ]["read_set"]
        self.assertIn("capacity", read_sets["objective.tick"]["required"])
        self.assertIn("next_wake_at", read_sets["objective.attention"]["required"])
        for operation in (
            "objective.plan",
            "objective.explain",
            "objective.graph",
        ):
            self.assertNotIn("capacity", read_sets[operation]["properties"])
            self.assertNotIn("next_wake_at", read_sets[operation]["properties"])

    def test_plan_digest_matches_an_independent_rfc8785_calculation(self) -> None:
        payload = self._plan("objective.plan").as_dict()
        body = {key: value for key, value in payload.items() if key != "plan_sha256"}
        expected = "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        self.assertEqual(payload["plan_sha256"], expected)
        self.assertEqual(compute_plan_sha256(payload), expected)

    def test_identical_facts_input_and_clock_are_deterministic_and_fresh(self) -> None:
        first = self._plan("objective.tick")
        first_payload = first.as_dict()
        first_payload["projection"]["selected_actions"].clear()
        second = self._plan("objective.tick")

        self.assertEqual(first.canonical_json, second.canonical_json)
        self.assertNotEqual(first_payload, second.as_dict())

    def test_visible_clock_and_task_state_changes_break_bridge_digest(self) -> None:
        first = self._plan("objective.plan").as_dict()
        later = self._plan(
            "objective.plan", now=self.now + timedelta(seconds=1)
        ).as_dict()
        self.assertNotEqual(first["plan_sha256"], later["plan_sha256"])

        set_status(self.config, load_task(self.config, "TASK-A"), "assigned")
        changed = self._plan("objective.plan").as_dict()
        self.assertNotEqual(first["plan_sha256"], changed["plan_sha256"])

    def test_hidden_title_agent_path_handoff_and_raw_core_digest_are_excluded(
        self,
    ) -> None:
        payload = self._plan("objective.plan").as_dict()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for secret in (
            "secret-title-must-not-cross-bridge",
            "secret-agent-name",
            "secret-handoff-value",
            str(self.root),
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotEqual(
            payload["plan_sha256"],
            "sha256:" + payload["read_set"]["objective"]["event_sha256"][7:],
        )

    def test_plan_does_not_recover_pending_state_or_call_legacy_snapshot(self) -> None:
        with (
            patch(
                "dyro.continuation.store._recover_pending",
                side_effect=AssertionError("recovery forbidden"),
            ),
            patch(
                "dyro.continuation.snapshot.build_scheduler_snapshot",
                side_effect=AssertionError("legacy snapshot forbidden"),
            ),
        ):
            self._plan("objective.plan")

    def test_authoritative_plan_fails_closed_on_partial_or_invalid_task_sets(
        self,
    ) -> None:
        self._write_extra_task("TASK-B")
        with self.assertRaises(BridgeObservationError) as limited:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                limits=ObservationLimits(task_records=1),
                clock=lambda: self.now,
            )
        self.assertEqual(limited.exception.error.code.value, "RESOURCE_LIMIT_EXCEEDED")

        unexpected = self.config.task_specs_dir / "unexpected.txt"
        unexpected.write_text("invalid record kind\n", encoding="utf-8")
        with self.assertRaises(BridgeObservationError) as invalid_entry:
            self._plan("objective.plan")
        self.assertEqual(invalid_entry.exception.error.code.value, "RECORD_INVALID")
        unexpected.unlink()

        self.config.task_specs_dir.joinpath("TASK-B/task.toml").write_text(
            "not valid = [", encoding="utf-8"
        )
        with self.assertRaises(BridgeObservationError) as invalid:
            self._plan("objective.plan")
        self.assertEqual(invalid.exception.error.code.value, "RECORD_INVALID")

    def test_typed_read_set_limits_are_checked_before_git(self) -> None:
        task = replace(
            load_task(self.config, "TASK-A"),
            repositories=tuple(f"repo-{index}" for index in range(101)),
        )
        with (
            patch(
                "dyro.bridge.plans.load_task_planning_bounded",
                return_value=(task, "done", "0" * 64),
            ),
            self.assertRaises(BridgeObservationError) as raised,
        ):
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "Git must not run before typed limit validation"
                ),
            )
        self.assertEqual(raised.exception.error.code.value, "RESOURCE_LIMIT_EXCEEDED")

        task = replace(
            load_task(self.config, "TASK-A"),
            repositories=tuple(f"repo-{index}" for index in range(51)),
        )
        with (
            patch(
                "dyro.bridge.plans.load_task_planning_bounded",
                return_value=(task, "done", "0" * 64),
            ),
            self.assertRaises(BridgeObservationError) as process_limited,
        ):
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "Git must not run after process budget exhaustion"
                ),
            )
        self.assertEqual(
            process_limited.exception.error.code.value,
            "RESOURCE_LIMIT_EXCEEDED",
        )

    def test_git_metadata_must_remain_inside_workspace_without_alternates(
        self,
    ) -> None:
        repository = self.root / "versions/alpha/services/api"
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._mark_done_with_head("TASK-A", head)
        git_reference = repository / ".git"
        git_reference.write_text("gitdir: /tmp/outside-git-dir\n", encoding="utf-8")
        with self.assertRaises(BridgeObservationError) as external:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "Git must not inspect external metadata"
                ),
            )
        self.assertEqual(external.exception.error.code.value, "RECORD_INVALID")

    def test_git_object_alternates_fail_before_process_start(self) -> None:
        repository = self.root / "versions/alpha/services/api"
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._mark_done_with_head("TASK-A", head)
        alternates = self.anchor / ".git/objects/info/alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text("/tmp/external-objects\n", encoding="utf-8")
        with self.assertRaises(BridgeObservationError) as external:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "Git must not start with object alternates"
                ),
            )
        self.assertEqual(external.exception.error.code.value, "RECORD_INVALID")

    def test_sha256_and_extended_repository_formats_fail_closed_before_git(
        self,
    ) -> None:
        self._mark_done_with_head("TASK-A", "a" * 64)
        with self.assertRaises(BridgeObservationError) as oid_failure:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "unsupported OID must fail before Git"
                ),
            )
        self.assertEqual(oid_failure.exception.error.code.value, "RECORD_INVALID")

        self._mark_done_with_head("TASK-A", "a" * 40)
        config = self.anchor / ".git/config"
        config.write_text(
            config.read_text(encoding="utf-8")
            + "\n[extensions]\n\tobjectFormat = sha256\n",
            encoding="utf-8",
        )
        with self.assertRaises(BridgeObservationError) as format_failure:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: self.fail(
                    "unsupported repository format must fail before Git"
                ),
            )
        self.assertEqual(format_failure.exception.error.code.value, "RECORD_INVALID")

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir(),
        "authoritative descriptor-bound Git requires Linux /proc/self/fd",
    )
    def test_real_git_read_is_allowlisted_and_preserves_repository_metadata(
        self,
    ) -> None:
        line_repository = self.root / "versions/alpha/services/api"
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=line_repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        task = load_task(self.config, "TASK-A")
        task.directory.joinpath("status").write_text("done\n", encoding="utf-8")
        task.directory.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-A",
                    "line": "alpha",
                    "branch": "task/TASK-A",
                    "repositories": {"api": head},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        git_paths = subprocess.run(
            ("git", "rev-parse", "--git-path", "index", "--git-path", "HEAD"),
            cwd=line_repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.splitlines()
        watched = [
            path if Path(path).is_absolute() else str(line_repository / path)
            for path in git_paths
        ]
        before = {
            path: (
                Path(path).stat().st_size,
                Path(path).stat().st_mtime_ns,
                Path(path).stat().st_ctime_ns,
            )
            for path in watched
            if Path(path).exists()
        }
        payload = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
        ).as_dict()

        self.assertEqual(
            payload["read_set"]["tasks"][0]["integration_state"], "integrated"
        )
        checks = payload["read_set"]["tasks"][0]["integration_checks"]
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["is_ancestor"])
        self.assertEqual(checks[0]["repository_id"], "api")
        after = {
            path: (
                Path(path).stat().st_size,
                Path(path).stat().st_mtime_ns,
                Path(path).stat().st_ctime_ns,
            )
            for path in watched
            if Path(path).exists()
        }
        self.assertEqual(before, after)
        self.assertFalse(list(line_repository.rglob("*.lock")))

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir(),
        "metadata ABA proof requires Linux /proc/self/fd",
    )
    def test_public_plan_reads_bound_metadata_when_git_path_is_swapped_and_restored(
        self,
    ) -> None:
        line_repository = self.root / "versions/alpha/services/api"
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=line_repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._mark_done_with_head("TASK-A", head)

        replacement = self.root / "replacement"
        replacement.mkdir()
        shell("git", "init", "-b", "main", cwd=replacement)
        shell("git", "config", "user.name", "Test User", cwd=replacement)
        shell("git", "config", "user.email", "test@example.com", cwd=replacement)
        replacement.joinpath("other.txt").write_text("other\n", encoding="utf-8")
        shell("git", "add", "other.txt", cwd=replacement)
        shell("git", "commit", "-m", "other", cwd=replacement)

        original_metadata = self.anchor / ".git"
        replacement_metadata = replacement / ".git"
        parked_metadata = self.root / "parked-git-metadata"

        def swap_during_read(repository: Path, ancestor: str, **kwargs: object):
            original_metadata.rename(parked_metadata)
            replacement_metadata.rename(original_metadata)
            try:
                return inspect_ancestry_readonly(repository, ancestor, **kwargs)
            finally:
                original_metadata.rename(replacement_metadata)
                parked_metadata.rename(original_metadata)

        payload = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=swap_during_read,
        ).as_dict()
        self.assertEqual(
            payload["read_set"]["tasks"][0]["integration_state"], "integrated"
        )

    def test_git_operational_failure_never_becomes_pending(self) -> None:
        task = load_task(self.config, "TASK-A")
        task.directory.joinpath("status").write_text("done\n", encoding="utf-8")
        task.directory.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-A",
                    "line": "alpha",
                    "branch": "task/TASK-A",
                    "repositories": {"api": "a" * 40},
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(BridgeObservationError) as raised:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                clock=lambda: self.now,
                git_reader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    GitReadError(GitReadFailure.TIMEOUT)
                ),
            )
        self.assertEqual(raised.exception.error.code.value, "OBSERVATION_PARTIAL")
        self.assertNotIn("timeout detail", raised.exception.error.message)

    def test_git_inputs_and_destination_head_are_bound_to_plan_digest(self) -> None:
        repository = self.root / "versions/alpha/services/api"
        initial_head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        repository.joinpath("one.txt").write_text("one\n", encoding="utf-8")
        shell("git", "add", "one.txt", cwd=repository)
        shell("git", "commit", "-m", "test: one", cwd=repository)
        destination_one = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self._mark_done_with_head("TASK-A", initial_head)
        first = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=self._fixture_git_reader,
        ).as_dict()

        self._mark_done_with_head("TASK-A", destination_one)
        second = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=self._fixture_git_reader,
        ).as_dict()
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertNotEqual(
            first["read_set"]["tasks"][0]["integration_checks"][0]["task_head_sha256"],
            second["read_set"]["tasks"][0]["integration_checks"][0]["task_head_sha256"],
        )

        repository.joinpath("two.txt").write_text("two\n", encoding="utf-8")
        shell("git", "add", "two.txt", cwd=repository)
        shell("git", "commit", "-m", "test: two", cwd=repository)
        third = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="release",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=self._fixture_git_reader,
        ).as_dict()
        self.assertNotEqual(second["plan_sha256"], third["plan_sha256"])
        self.assertNotEqual(
            second["read_set"]["tasks"][0]["integration_checks"][0][
                "destination_head_sha256"
            ],
            third["read_set"]["tasks"][0]["integration_checks"][0][
                "destination_head_sha256"
            ],
        )

    def test_anchor_reference_done_task_uses_the_verified_anchor(self) -> None:
        shell("git", "checkout", "-b", "feat/reference", cwd=self.anchor)
        create_line(
            self.config,
            line_id="reference",
            branch="feat/reference",
            base="main",
            storage_modes={"api": "anchor-reference"},
        )
        task_dir = self._write_extra_task(
            "TASK-REF",
            task_template("TASK-REF", "reference", "reference", "api", "services/api"),
        )
        head = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=self.anchor,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        task_dir.joinpath("status").write_text("done\n", encoding="utf-8")
        task_dir.joinpath("task-heads.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "TASK-REF",
                    "line": "reference",
                    "branch": "task/TASK-REF",
                    "repositories": {"api": head},
                }
            ),
            encoding="utf-8",
        )
        self._create_objective("reference", ("TASK-REF",), line="reference")
        payload = build_objective_bridge_plan(
            operation="objective.plan",
            objective_id="reference",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=self._fixture_git_reader,
        ).as_dict()
        facts = next(
            item for item in payload["read_set"]["tasks"] if item["id"] == "TASK-REF"
        )
        self.assertEqual(facts["integration_state"], "integrated")

    def test_attention_accepts_normalized_pending_dependency_fact(self) -> None:
        manifest = self.config.task_specs_dir.joinpath("TASK-A/task.toml")
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "depends_on = []", 'depends_on = ["TASK-B"]'
            ),
            encoding="utf-8",
        )
        self._write_extra_task("TASK-B")
        stop_objective(self.config, "release")
        self._create_objective("dependency", ("TASK-A",))
        payload = build_objective_bridge_plan(
            operation="objective.attention",
            objective_id="dependency",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=lambda *_args, **_kwargs: self.fail("Git is not required"),
        ).as_dict()
        predicates = [item["predicates"] for item in payload["projection"]["attention"]]
        self.assertTrue(
            any(item.get("has_pending_dependency") is True for item in predicates)
        )

    def test_shared_agent_is_one_resource_across_executor_and_reviewer(self) -> None:
        task_a = self.config.task_specs_dir.joinpath("TASK-A/task.toml")
        task_a.write_text(
            task_a.read_text(encoding="utf-8").replace(
                "secret-agent-name", "shared-agent"
            ),
            encoding="utf-8",
        )
        task_b = (
            task_template("TASK-B", "task-b", "alpha", "api", "services/api")
            .replace(
                '[executor]\nagent = "codex"',
                '[executor]\nagent = "other-agent"',
            )
            .replace(
                '[reviewer]\nagent = "codex"',
                '[reviewer]\nagent = "shared-agent"',
            )
        )
        directory = self._write_extra_task("TASK-B", task_b)
        directory.joinpath("status").write_text("review\n", encoding="utf-8")
        stop_objective(self.config, "release")
        self._create_objective("shared-resource", ("TASK-A", "TASK-B"))
        payload = build_objective_bridge_plan(
            operation="objective.tick",
            objective_id="shared-resource",
            workspace=None,
            start=self.root,
            cwd=self.root,
            clock=lambda: self.now,
            git_reader=lambda *_args, **_kwargs: self.fail("Git is not required"),
        ).as_dict()
        task_facts = {item["id"]: item for item in payload["read_set"]["tasks"]}
        self.assertEqual(
            task_facts["TASK-A"]["execution_slot"],
            task_facts["TASK-B"]["review_slot"],
        )
        wave_subjects = {
            item["subject_id"] for item in payload["projection"]["tick_wave"]
        }
        self.assertNotEqual(wave_subjects, {"TASK-A", "TASK-B"})
        self.assertTrue(
            any(
                item["reason"] == "RESOURCE_CONFLICT"
                for item in payload["projection"]["deferred"]
            )
        )

    def test_plan_public_failures_are_typed_and_preserve_permissions(self) -> None:
        with self.assertRaises(BridgeObservationError) as unknown:
            build_objective_bridge_plan(
                operation="objective.unknown",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
            )
        self.assertEqual(unknown.exception.error.code.value, "OPERATION_UNKNOWN")

        with self.assertRaises(BridgeObservationError) as clock_error:
            build_objective_bridge_plan(
                operation="objective.plan",
                objective_id="release",
                workspace=None,
                start=self.root,
                cwd=self.root,
                monotonic=lambda: float("nan"),
            )
        self.assertEqual(clock_error.exception.error.code.value, "INTERNAL_ERROR")

        failure = ObservationFailure("lines", "HOST_READ_PERMISSION_REQUIRED")
        with (
            patch(
                "dyro.bridge.plans.read_bounded_line_facts",
                return_value=((), frozenset(), (failure,), False),
            ),
            self.assertRaises(BridgeObservationError) as permission,
        ):
            self._plan("objective.plan")
        self.assertEqual(
            permission.exception.error.code.value, "HOST_READ_PERMISSION_REQUIRED"
        )

    def test_git_failure_classes_map_to_stable_bridge_errors(self) -> None:
        self._mark_done_with_head("TASK-A", "a" * 40)
        cases = (
            (GitReadFailure.UNAVAILABLE, "OPERATION_UNAVAILABLE"),
            (GitReadFailure.PERMISSION, "HOST_READ_PERMISSION_REQUIRED"),
            (GitReadFailure.TIMEOUT, "OBSERVATION_PARTIAL"),
            (GitReadFailure.PARTIAL, "OBSERVATION_PARTIAL"),
        )
        for failure, expected in cases:
            with (
                self.subTest(failure=failure),
                self.assertRaises(BridgeObservationError) as raised,
            ):
                build_objective_bridge_plan(
                    operation="objective.plan",
                    objective_id="release",
                    workspace=None,
                    start=self.root,
                    cwd=self.root,
                    clock=lambda: self.now,
                    git_reader=lambda *_args, _failure=failure, **_kwargs: (
                        _ for _ in ()
                    ).throw(GitReadError(_failure)),
                )
            self.assertEqual(raised.exception.error.code.value, expected)

    def test_bridge_plan_rejects_digest_tampering_and_extra_fields(self) -> None:
        plan = self._plan("objective.plan")
        payload = plan.as_dict()
        payload["plan_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            BridgePlan(
                plan.operation,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )

        payload = plan.as_dict()
        payload["debug"] = "hidden"
        payload["plan_sha256"] = compute_plan_sha256(payload)
        with self.assertRaisesRegex(ValidationError, "schema"):
            BridgePlan(
                plan.operation,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
            )

    def test_bridge_plan_has_no_mutation_import_or_reverse_consumer(self) -> None:
        plan_tree = ast.parse(
            Path("src/dyro/bridge/plans.py").read_text(encoding="utf-8")
        )
        imports = {
            alias.name
            for node in ast.walk(plan_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(plan_tree)
            if isinstance(node, ast.ImportFrom)
        }
        forbidden = {
            "dyro.cli",
            "dyro.continuation.action_journal",
            "dyro.continuation.actions",
            "dyro.continuation.owner_lease",
            "dyro.continuation.supervision",
        }
        self.assertTrue(imports.isdisjoint(forbidden))
        for path in Path("src/dyro").rglob("*.py"):
            if path.is_relative_to(Path("src/dyro/bridge")):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            package = ".".join(path.with_suffix("").parts[1:-1])
            for node in ast.walk(tree):
                imported: str | None = None
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("dyro.bridge.plans"))
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        prefix = package.split(".")
                        base = prefix[: max(0, len(prefix) - node.level + 1)]
                        imported = ".".join((*base, module))
                    else:
                        imported = module
                    self.assertFalse(imported.startswith("dyro.bridge.plans"))
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    self.assertFalse(node.args[0].value.startswith("dyro.bridge.plans"))


class BridgeGitReadTests(unittest.TestCase):
    def test_default_git_ignores_caller_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.joinpath("git").write_text("untrusted\n", encoding="utf-8")
            with patch.dict(os.environ, {"PATH": str(root)}):
                invocation = build_head_invocation(root)
        self.assertNotEqual(Path(invocation.executable).parent, root)
        self.assertEqual(Path(invocation.executable), Path("/usr/bin/git").resolve())

    def test_git_invocation_is_exact_and_disables_optional_locks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bin/git"
            executable.parent.mkdir()
            executable.write_bytes(b"")
            executable.chmod(0o755)
            descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                head_invocation = build_head_invocation(
                    root,
                    executable=str(executable),
                    directory_fd=descriptor,
                )
                invocation = build_ancestor_invocation(
                    root,
                    "a" * 40,
                    "b" * 40,
                    executable=str(executable),
                    directory_fd=descriptor,
                )
                helper = build_helper_invocation(invocation)
            finally:
                os.close(descriptor)

        self.assertEqual(invocation.argv[-3:], ("--is-ancestor", "a" * 40, "b" * 40))
        self.assertEqual(dict(invocation.environment), git_read_environment(root))
        self.assertEqual(dict(invocation.environment)["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(dict(invocation.environment)["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(dict(invocation.environment)["GIT_CONFIG"], os.devnull)
        self.assertNotIn("PATH", dict(invocation.environment))
        for candidate in (head_invocation, invocation):
            self.assertTrue(
                invocation_is_allowlisted(
                    (candidate.executable, *candidate.argv),
                    dict(candidate.environment),
                )
            )
        self.assertTrue(
            helper_invocation_is_allowlisted(helper, dict(invocation.environment))
        )

    def test_git_metadata_environment_is_bound_to_four_descriptors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "bin/git"
            executable.parent.mkdir()
            executable.write_bytes(b"")
            executable.chmod(0o755)
            directories = [root / name for name in ("work", "git", "common", "objects")]
            for directory in directories:
                directory.mkdir()
            descriptors = tuple(
                os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                for directory in directories
            )
            try:
                invocation = build_head_invocation(
                    directories[0],
                    executable=str(executable),
                    directory_fd=descriptors[0],
                    git_dir_fd=descriptors[1],
                    common_dir_fd=descriptors[2],
                    object_dir_fd=descriptors[3],
                )
                helper = build_helper_invocation(invocation)
                environment = dict(invocation.environment)
                self.assertEqual(
                    environment["GIT_WORK_TREE"], f"/proc/self/fd/{descriptors[0]}"
                )
                self.assertEqual(
                    environment["GIT_DIR"], f"/proc/self/fd/{descriptors[1]}"
                )
                self.assertEqual(
                    environment["GIT_COMMON_DIR"], f"/proc/self/fd/{descriptors[2]}"
                )
                self.assertEqual(
                    environment["GIT_OBJECT_DIRECTORY"],
                    f"/proc/self/fd/{descriptors[3]}",
                )
                self.assertTrue(
                    invocation_is_allowlisted(
                        (invocation.executable, *invocation.argv), environment
                    )
                )
                self.assertTrue(helper_invocation_is_allowlisted(helper, environment))
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)

    @unittest.skipIf(
        sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir(),
        "this host supports the authoritative descriptor namespace",
    )
    def test_authoritative_git_fails_closed_without_linux_fd_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(GitReadError) as raised:
                inspect_ancestry_readonly(Path(temporary), "a" * 40)
        self.assertEqual(raised.exception.code, GitReadFailure.UNAVAILABLE)

    @unittest.skipUnless(
        sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir(),
        "Landlock proof requires Linux",
    )
    def test_linux_binder_denies_writes_inside_bound_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "worktree"
            git_dir = worktree / ".git"
            objects = git_dir / "objects"
            executable = worktree / "bin/git"
            objects.mkdir(parents=True)
            executable.parent.mkdir()
            executable.write_text(
                "#!/bin/sh\n"
                "if printf changed > forbidden-write; then exit 70; else exit 73; fi\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            directories = (worktree, git_dir, git_dir, objects)
            descriptors = tuple(
                os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                for directory in directories
            )
            try:
                invocation = build_head_invocation(
                    worktree,
                    executable=str(executable),
                    directory_fd=descriptors[0],
                    git_dir_fd=descriptors[1],
                    common_dir_fd=descriptors[2],
                    object_dir_fd=descriptors[3],
                )
                completed = subprocess.run(
                    build_helper_invocation(invocation),
                    env=dict(invocation.environment),
                    pass_fds=descriptors,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)
            self.assertEqual(completed.returncode, 73)
            self.assertFalse(worktree.joinpath("forbidden-write").exists())

    def test_git_reader_never_exposes_output_and_types_return_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "git"
            executable.write_bytes(b"")
            executable.chmod(0o755)
            captured = []

            def runner(invocation, capture_stdout):
                captured.append((invocation, capture_stdout))
                return subprocess.CompletedProcess(
                    invocation.argv,
                    0,
                    stdout=(b"c" * 40 + b"\n") if capture_stdout else None,
                )

            observation = inspect_ancestry_readonly(
                root,
                "b" * 40,
                executable=str(executable),
                runner=runner,
            )
            self.assertTrue(observation.is_ancestor)
            self.assertEqual([item[1] for item in captured], [True, False])
            for invocation, _capture in captured:
                self.assertTrue(
                    invocation_is_allowlisted(
                        (invocation.executable, *invocation.argv),
                        dict(invocation.environment),
                    )
                )

            self.assertFalse(
                is_ancestor_readonly(
                    root,
                    "b" * 40,
                    executable=str(executable),
                    runner=lambda invocation, capture: subprocess.CompletedProcess(
                        invocation.argv,
                        0 if capture else 1,
                        stdout=b"c" * 40 + b"\n" if capture else None,
                    ),
                )
            )
            with self.assertRaises(GitReadError):
                is_ancestor_readonly(
                    root,
                    "b" * 40,
                    executable=str(executable),
                    runner=lambda invocation, capture: subprocess.CompletedProcess(
                        invocation.argv,
                        0 if capture else 2,
                        stdout=b"c" * 40 + b"\n" if capture else None,
                    ),
                )

    @unittest.skipUnless(os.name == "posix", "descriptor-bound Git is POSIX-only")
    def test_descriptor_bound_git_enforces_deadline_from_a_worker_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "git"
            executable.write_text("#!/bin/sh\nexec /bin/sleep 2\n", encoding="utf-8")
            executable.chmod(0o755)
            failures: list[GitReadFailure] = []

            def inspect() -> None:
                try:
                    inspect_ancestry_readonly(
                        root,
                        "a" * 40,
                        executable=str(executable),
                        timeout_seconds=0.05,
                    )
                except GitReadError as exc:
                    failures.append(exc.code)

            worker = threading.Thread(target=inspect)
            worker.start()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [GitReadFailure.TIMEOUT])

    @unittest.skipUnless(os.name == "posix", "descriptor-bound Git is POSIX-only")
    def test_descriptor_bound_git_enforces_its_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "git"
            executable.write_text("#!/bin/sh\nexec /bin/sleep 2\n", encoding="utf-8")
            executable.chmod(0o755)
            started = time.monotonic()
            with self.assertRaises(GitReadError) as raised:
                inspect_ancestry_readonly(
                    root,
                    "a" * 40,
                    executable=str(executable),
                    timeout_seconds=0.05,
                )
            self.assertEqual(raised.exception.code, GitReadFailure.TIMEOUT)
            self.assertLess(time.monotonic() - started, 1)

    @unittest.skipUnless(os.name == "posix", "descriptor-bound Git is POSIX-only")
    def test_descriptor_bound_git_reaps_child_when_parent_is_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "git"
            executable.write_text("#!/bin/sh\nexec /bin/sleep 2\n", encoding="utf-8")
            executable.chmod(0o755)
            child_pids: list[int] = []
            real_popen = subprocess.Popen

            def record_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                child_pids.append(process.pid)
                return process

            with (
                patch(
                    "dyro.bridge.git_read.subprocess.Popen",
                    side_effect=record_popen,
                ),
                patch(
                    "dyro.bridge.git_read.select.select",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                inspect_ancestry_readonly(
                    root,
                    "a" * 40,
                    executable=str(executable),
                    timeout_seconds=1,
                )
            self.assertEqual(len(child_pids), 1)
            with self.assertRaises(ChildProcessError):
                os.waitpid(child_pids[0], os.WNOHANG)

    @unittest.skipUnless(os.name == "posix", "descriptor-bound Git is POSIX-only")
    def test_git_reader_rejects_path_replacement_even_when_path_is_restored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            replacement = root / "replacement"
            parked = root / "parked"
            for repository, marker in ((original, "original"), (replacement, "evil")):
                repository.mkdir()
                shell("git", "init", "-b", "main", cwd=repository)
                shell("git", "config", "user.name", "Test User", cwd=repository)
                shell(
                    "git",
                    "config",
                    "user.email",
                    "test@example.com",
                    cwd=repository,
                )
                repository.joinpath("marker.txt").write_text(
                    marker + "\n", encoding="utf-8"
                )
                shell("git", "add", "marker.txt", cwd=repository)
                shell("git", "commit", "-m", marker, cwd=repository)
            replacement_head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=replacement,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            descriptor = os.open(
                original,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                original.rename(parked)
                original.symlink_to(replacement, target_is_directory=True)
                with self.assertRaises(GitReadError):
                    inspect_ancestry_readonly(
                        original,
                        replacement_head,
                        directory_fd=descriptor,
                    )
                original.unlink()
                parked.rename(original)
            finally:
                if original.is_symlink():
                    original.unlink()
                if parked.exists() and not original.exists():
                    parked.rename(original)
                os.close(descriptor)


class BridgePlanVectorTests(unittest.TestCase):
    def test_frozen_plan_digest_vectors(self) -> None:
        vectors = json.loads(
            Path("tests/fixtures/bridge/contracts-v1.json").read_text(encoding="utf-8")
        )["plan_digest_vectors"]
        for vector in vectors:
            self.assertEqual(compute_plan_sha256(vector["payload"]), vector["value"])


if __name__ == "__main__":
    unittest.main()
