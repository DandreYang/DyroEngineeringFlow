from __future__ import annotations

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import patch

from dyro.cli import (
    _control_plane_budget,
    _print_control_plane_error,
    cmd_doctor,
    cmd_next,
    cmd_status,
    main,
)
from dyro.config import load
from dyro.continuation.next_step import next_commands
from dyro.errors import ValidationError
from dyro.process import git_read as real_git_read
from dyro.read_limits import (
    CONTROL_PLANE_DEADLINE_CEILING_SECONDS,
    PROTOCOL_DEADLINE_SECONDS,
    _PROTOCOL_DEADLINE_SECONDS,
    ObservationLimits,
    ReadBudget,
    ReadLimitCode,
    ReadLimitError,
    apply_control_plane_fanout,
    control_plane_deadline_seconds,
)
from dyro.workspace import (
    OBSERVATION_DEADLINE_FINDING,
    create_line,
    doctor,
    git_observation_scope_count,
    is_observation_deadline_finding,
    status_rows,
)

from .support import WorkspaceCase


def _raise_deadline_on_worktree(repo, *args, read_budget=None, **kwargs):
    if read_budget is not None and "versions/" in str(repo):
        raise ReadLimitError(
            ReadLimitCode.DEADLINE_EXCEEDED,
            "Core observation deadline exceeded",
        )
    return real_git_read(repo, *args, read_budget=read_budget, **kwargs)


class ControlPlaneDeadlineScaleTests(unittest.TestCase):
    def test_default_observation_deadline_stays_five_seconds(self) -> None:
        limits = ObservationLimits()
        self.assertEqual(limits.deadline_seconds, PROTOCOL_DEADLINE_SECONDS)
        self.assertEqual(PROTOCOL_DEADLINE_SECONDS, 5.0)
        self.assertEqual(_PROTOCOL_DEADLINE_SECONDS, PROTOCOL_DEADLINE_SECONDS)
        self.assertLess(
            _PROTOCOL_DEADLINE_SECONDS, CONTROL_PLANE_DEADLINE_CEILING_SECONDS
        )

    def test_observation_limits_allow_documented_control_plane_ceiling(self) -> None:
        limits = ObservationLimits(
            deadline_seconds=CONTROL_PLANE_DEADLINE_CEILING_SECONDS
        )
        self.assertEqual(
            limits.deadline_seconds, CONTROL_PLANE_DEADLINE_CEILING_SECONDS
        )
        with self.assertRaises(ValidationError):
            ObservationLimits(
                deadline_seconds=CONTROL_PLANE_DEADLINE_CEILING_SECONDS + 0.01
            )

    def test_deadline_scales_with_git_scope_count_and_caps(self) -> None:
        self.assertEqual(control_plane_deadline_seconds(1), 5.0)
        large = control_plane_deadline_seconds(58)
        self.assertGreaterEqual(large, 20.0)
        self.assertLessEqual(large, CONTROL_PLANE_DEADLINE_CEILING_SECONDS)
        self.assertEqual(
            control_plane_deadline_seconds(10_000),
            CONTROL_PLANE_DEADLINE_CEILING_SECONDS,
        )
        self.assertGreater(control_plane_deadline_seconds(58), 5.0)

    def test_default_budget_widens_for_fanout_but_explicit_deadline_does_not(
        self,
    ) -> None:
        budget = ReadBudget(ObservationLimits())
        apply_control_plane_fanout(budget, 58)
        self.assertGreaterEqual(budget.limits.deadline_seconds, 20.0)
        tight = ReadBudget(ObservationLimits(deadline_seconds=0.05))
        apply_control_plane_fanout(tight, 58)
        self.assertEqual(tight.limits.deadline_seconds, 0.05)

    def test_seven_second_fanout_fits_scaled_budget_not_flat_five(self) -> None:
        """Text path ~7s must not be a JSON DEADLINE on a ~58-scope workspace."""

        class Clock:
            def __init__(self) -> None:
                self.t = 1000.0

            def __call__(self) -> float:
                return self.t

        scaled_clock = Clock()
        scaled = ReadBudget(ObservationLimits(), monotonic=scaled_clock)
        apply_control_plane_fanout(scaled, 58)
        scaled_clock.t += 7.0
        scaled.check_deadline()
        self.assertGreater(scaled.remaining_seconds(), 10.0)

        flat_clock = Clock()
        flat = ReadBudget(ObservationLimits(), monotonic=flat_clock)
        flat_clock.t += 5.34
        with self.assertRaises(ReadLimitError) as raised:
            flat.check_deadline()
        self.assertIs(raised.exception.code, ReadLimitCode.DEADLINE_EXCEEDED)

    def test_json_fanout_commands_do_not_start_on_the_five_second_cliff(self) -> None:
        for command in ("doctor", "status", "next"):
            budget = _control_plane_budget(Namespace(command=command))
            self.assertEqual(
                budget.limits.deadline_seconds,
                CONTROL_PLANE_DEADLINE_CEILING_SECONDS,
                command,
            )
            self.assertGreater(budget.remaining_seconds(), 5.35)

        other = _control_plane_budget(Namespace(command="line"))
        self.assertEqual(other.limits.deadline_seconds, PROTOCOL_DEADLINE_SECONDS)
        via_func = _control_plane_budget(Namespace(command=None, func=cmd_status))
        self.assertEqual(
            via_func.limits.deadline_seconds,
            CONTROL_PLANE_DEADLINE_CEILING_SECONDS,
        )

    def test_json_fanout_budget_survives_stable_mac_5_35s_wall(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.t = 1000.0

            def __call__(self) -> float:
                return self.t

        clock = Clock()
        budget = ReadBudget(
            ObservationLimits(
                deadline_seconds=CONTROL_PLANE_DEADLINE_CEILING_SECONDS
            ),
            monotonic=clock,
        )
        clock.t += 5.35
        budget.check_deadline()
        self.assertGreater(budget.remaining_seconds(), 20.0)


class ControlPlaneTimeoutFindingTests(WorkspaceCase):
    def _workspace_with_completed_fail_and_worktree(self):
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        config_path = self.root / "dyro.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + "\n[repositories.web]\n"
            + 'path = "repositories/web"\n'
            + 'mount = "clients/web"\n',
            encoding="utf-8",
        )
        return load(self.root)

    def test_scope_count_is_anchors_plus_line_worktrees(self) -> None:
        config = self._workspace_with_completed_fail_and_worktree()
        self.assertEqual(git_observation_scope_count(config), 3)

    def test_doctor_keeps_completed_fails_and_adds_timeout_finding(self) -> None:
        config = self._workspace_with_completed_fail_and_worktree()
        with patch("dyro.workspace.git_read", side_effect=_raise_deadline_on_worktree):
            findings = doctor(config, read_budget=ReadBudget(ObservationLimits()))

        self.assertTrue(
            any(
                item.startswith("FAIL repository web:")
                and "missing or not Git" in item
                for item in findings
            ),
            findings,
        )
        self.assertTrue(
            any(is_observation_deadline_finding(item) for item in findings),
            findings,
        )
        self.assertIn(OBSERVATION_DEADLINE_FINDING, findings)
        self.assertFalse(
            any(item.startswith("PASS line:alpha/web") for item in findings),
            findings,
        )

    def test_status_rows_keep_completed_rows_and_mark_timeout(self) -> None:
        config = self._workspace_with_completed_fail_and_worktree()
        with patch("dyro.workspace.git_read", side_effect=_raise_deadline_on_worktree):
            rows = status_rows(config, read_budget=ReadBudget(ObservationLimits()))

        self.assertTrue(
            any(scope == "anchor" and repository == "api" for scope, repository, *_ in rows),
            rows,
        )
        self.assertTrue(
            any(
                scope == "observation" and branch == "TIMEOUT"
                for scope, _repository, branch, *_ in rows
            ),
            rows,
        )

    def test_next_commands_repair_on_timeout_instead_of_empty_ready(self) -> None:
        config = self._workspace_with_completed_fail_and_worktree()
        with patch("dyro.workspace.git_read", side_effect=_raise_deadline_on_worktree):
            commands = next_commands(
                config,
                "selected",
                read_budget=ReadBudget(ObservationLimits()),
            )
        self.assertEqual(commands, ["dyro --workspace selected doctor"])

    def test_json_doctor_and_next_are_not_bare_deadline_errors(self) -> None:
        self._workspace_with_completed_fail_and_worktree()
        with patch("dyro.workspace.git_read", side_effect=_raise_deadline_on_worktree):
            doctor_out = StringIO()
            doctor_err = StringIO()
            with (
                redirect_stdout(doctor_out),
                redirect_stderr(doctor_err),
                self.assertRaises(SystemExit) as raised,
            ):
                main(
                    [
                        "--root",
                        str(self.root),
                        "doctor",
                        "--format",
                        "json",
                    ]
                )
            next_out = StringIO()
            next_err = StringIO()
            with redirect_stdout(next_out), redirect_stderr(next_err):
                main(
                    [
                        "--root",
                        str(self.root),
                        "next",
                        "--format",
                        "json",
                    ]
                )
            status_out = StringIO()
            status_err = StringIO()
            with redirect_stdout(status_out), redirect_stderr(status_err):
                main(
                    [
                        "--root",
                        str(self.root),
                        "status",
                        "--format",
                        "json",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(doctor_err.getvalue(), "")
        doctor_payload = json.loads(doctor_out.getvalue())
        self.assertEqual(doctor_payload["kind"], "doctor")
        self.assertNotEqual(doctor_payload.get("kind"), "error")
        self.assertEqual(doctor_payload["code"], "DEADLINE_EXCEEDED")
        self.assertFalse(doctor_payload["passed"])
        self.assertTrue(doctor_payload["partial"])
        self.assertTrue(
            any(
                item["status"] == "FAIL" and "missing or not Git" in item["message"]
                for item in doctor_payload["findings"]
            ),
            doctor_payload,
        )
        self.assertTrue(
            any(
                item["status"] == "FAIL" and "deadline" in item["message"]
                for item in doctor_payload["findings"]
            ),
            doctor_payload,
        )

        self.assertEqual(next_err.getvalue(), "")
        next_payload = json.loads(next_out.getvalue())
        self.assertEqual(next_payload["kind"], "next_step")
        self.assertEqual(next_payload["state"], "needs_repair")
        self.assertNotEqual(next_payload["state"], "ready")
        self.assertEqual(next_payload["code"], "DEADLINE_EXCEEDED")
        self.assertTrue(next_payload["partial"])
        self.assertFalse(next_payload["mutation_available"])

        self.assertEqual(status_err.getvalue(), "")
        status_payload = json.loads(status_out.getvalue())
        self.assertEqual(status_payload["kind"], "workspace_status")
        self.assertEqual(status_payload["code"], "DEADLINE_EXCEEDED")
        self.assertTrue(status_payload["partial"])
        self.assertTrue(
            any(row["branch"] == "TIMEOUT" for row in status_payload["rows"]),
            status_payload,
        )

    def test_json_commands_do_not_bare_deadline_when_observation_raises(self) -> None:
        self._workspace_with_completed_fail_and_worktree()
        deadline = ReadLimitError(
            ReadLimitCode.DEADLINE_EXCEEDED,
            "Core observation deadline exceeded",
        )
        cases = (
            (["doctor"], "doctor", "dyro.cli.doctor"),
            (["status"], "workspace_status", "dyro.cli.status_rows"),
            (["next"], "next_step", "dyro.cli.doctor"),
        )
        for argv, kind, target in cases:
            stdout = StringIO()
            stderr = StringIO()
            with (
                patch(target, side_effect=deadline),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                main(["--root", str(self.root), *argv, "--format", "json"])
            self.assertEqual(raised.exception.code, 2, argv)
            self.assertEqual(stderr.getvalue(), "", argv)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["kind"], kind, payload)
            self.assertEqual(payload["code"], "DEADLINE_EXCEEDED", payload)
            self.assertTrue(payload["partial"], payload)
            if kind == "next_step":
                self.assertEqual(payload["state"], "needs_repair")
                self.assertNotEqual(payload["state"], "ready")

    def test_print_error_does_not_emit_mac_bare_status_envelope(self) -> None:
        """Verifier payload {code, command:status} is a total-failure agents abandon."""

        args = Namespace(
            command="status",
            format="json",
            workspace_alias="selected",
            func=cmd_status,
        )
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _print_control_plane_error(
                args,
                ReadLimitError(
                    ReadLimitCode.DEADLINE_EXCEEDED,
                    "Core observation deadline exceeded",
                ),
            )
        self.assertEqual(stderr.getvalue(), "")
        payload = json.loads(stdout.getvalue())
        self.assertNotEqual(payload.get("kind"), "error")
        self.assertNotEqual(payload.get("command"), "status")
        self.assertEqual(payload["kind"], "workspace_status")
        self.assertEqual(payload["code"], "DEADLINE_EXCEEDED")
        self.assertTrue(payload["partial"])

        via_func = Namespace(command=None, format="json", func=cmd_status)
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _print_control_plane_error(
                via_func,
                ReadLimitError(
                    ReadLimitCode.DEADLINE_EXCEEDED,
                    "Core observation deadline exceeded",
                ),
            )
        self.assertEqual(stderr.getvalue(), "")
        via_payload = json.loads(stdout.getvalue())
        self.assertEqual(via_payload["kind"], "workspace_status")
        self.assertNotEqual(via_payload.get("kind"), "error")
        self.assertNotIn("command", via_payload)

        for func, kind in (
            (cmd_doctor, "doctor"),
            (cmd_next, "next_step"),
        ):
            stdout = StringIO()
            with redirect_stdout(stdout), redirect_stderr(StringIO()):
                _print_control_plane_error(
                    Namespace(command=None, format="json", func=func),
                    ReadLimitError(
                        ReadLimitCode.DEADLINE_EXCEEDED,
                        "Core observation deadline exceeded",
                    ),
                )
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["kind"], kind)
            self.assertTrue(payload["partial"])
            if kind == "next_step":
                self.assertEqual(payload["state"], "needs_repair")


if __name__ == "__main__":
    unittest.main()
