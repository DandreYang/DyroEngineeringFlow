from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import experiments.local_agent_dispatch.adapters.registry as registry_module
from experiments.local_agent_dispatch.adapters.registry import get_adapter, probe_backends
from experiments.local_agent_dispatch.cli import main as cli_main
from experiments.local_agent_dispatch.context_guard import check_content
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.gc import gc
from experiments.local_agent_dispatch.lease import SlotManager
from experiments.local_agent_dispatch.panel import resolve_panel_members, run_panel
from experiments.local_agent_dispatch.skill_render import render_skill_markdown, save_route, write_skill
from experiments.local_agent_dispatch.supervisor import DispatchSupervisor
from experiments.local_agent_dispatch.task_contract import parse_task_contract


def _home(tmp: str) -> Path:
    return Path(tmp) / "dispatch-home"


def _project(tmp: str) -> Path:
    root = Path(tmp) / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def hello():\n    return 1\n",
        encoding="utf-8",
    )
    return root


def _task(backend: str = "echo", *, strict: bool = False) -> dict:
    return {
        "schema_version": 1,
        "backend": backend,
        "allow_offline_simulation": backend == "echo",
        "mode": "read-only",
        "strict": strict,
        "files": ["src/*.py"],
        "task": {
            "briefing": "tiny project",
            "locations": "src/",
            "objective": "summarize hello()",
            "constraints": "read-only",
            "output_contract": "summary + evidence",
        },
    }


class LeaseTests(unittest.TestCase):
    def test_acquire_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SlotManager(_home(tmp), max_per_backend=1, max_global=1)
            leases = mgr.acquire("echo")
            self.assertEqual(len(leases), 2)
            with self.assertRaises(DispatchValidationError):
                mgr.acquire("echo")
            mgr.release_all(leases)
            leases2 = mgr.acquire("echo")
            mgr.release_all(leases2)


class SecretGuardTests(unittest.TestCase):
    def test_rejects_google_stripe_and_jwt_credentials(self) -> None:
        samples = (
            "AIza" + ("A" * 35),
            "sk_live_" + ("a" * 24),
            "eyJheader123.payload123.signature123",
        )
        for sample in samples:
            with self.subTest(sample=sample[:8]):
                self.assertFalse(check_content(sample).allowed)


class SupervisorEchoTests(unittest.TestCase):
    def test_sync_run_and_strict_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            sup = DispatchSupervisor(home=home, max_per_backend=2, max_global=4)
            record = sup.accept(_task(strict=True), project_root=project)
            self.assertEqual(record.status, "accepted")
            done = sup.execute(record.run_id, timeout_seconds=30)
            self.assertEqual(done.status, "completed")
            self.assertIsNotNone(done.result)
            assert done.result is not None
            self.assertIn("echo-adapter", str(done.result.get("summary")))
            self.assertEqual(done.result.get("execution_kind"), "offline-simulation")
            self.assertEqual(done.result.get("confidence"), "low")
            self.assertTrue(done.shadow_path)
            self.assertTrue(Path(done.shadow_path).is_dir())
            # host .env must not be required; secret file should fail accept
            (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
            bad = _task()
            bad["files"] = [".env", "src/*.py"]
            with self.assertRaises(DispatchValidationError):
                sup.accept(bad, project_root=project)

    def test_timeout_precedence_and_mode_defaults(self) -> None:
        from experiments.local_agent_dispatch.cli import build_parser
        parser = build_parser()
        self.assertIsNone(parser.parse_args(["run", "--project", "."]).timeout)
        self.assertEqual(parser.parse_args(["run", "--project", ".", "--timeout", "9"]).timeout, 9)
        with tempfile.TemporaryDirectory() as tmp:
            sup = DispatchSupervisor(home=_home(tmp))
            record = sup.accept(_task(), project_root=_project(tmp))
            for mode, contract_timeout, explicit, expected in (
                ("read-only", None, None, 120), ("edit", None, None, 1800),
                ("edit", 42, None, 42), ("read-only", 42, None, 42),
                ("edit", 42, 9, 9),
            ):
                with self.subTest(mode=mode, contract_timeout=contract_timeout, explicit=explicit):
                    record.contract["mode"] = mode
                    if contract_timeout is None:
                        record.contract.pop("timeout_seconds", None)
                    else:
                        record.contract["timeout_seconds"] = contract_timeout
                    sup.store.save(record)
                    with patch.object(sup, "spawn_worker", return_value=record) as spawn:
                        sup.execute(record.run_id, sync=False, timeout_seconds=explicit)
                    spawn.assert_called_once_with(record.run_id, timeout_seconds=expected)

    def test_adapter_failures_override_high_confidence(self) -> None:
        from experiments.local_agent_dispatch.adapters.base import AdapterResult
        echo = get_adapter("echo")
        with tempfile.TemporaryDirectory() as tmp:
            sup = DispatchSupervisor(home=_home(tmp))
            project = _project(tmp)
            for status, code in (("timeout", "timeout"), ("error", "protocol_error"),
                                 ("error", "output_limit"), ("cancelled", "cancelled")):
                with self.subTest(status=status, code=code):
                    record = sup.accept(_task(), project_root=project)
                    with patch.object(type(echo), "run", return_value=AdapterResult(
                        status=status, summary="incomplete", confidence="high", error_code=code
                    )):
                        done = sup.execute(record.run_id)
                    self.assertNotEqual(done.status, "completed")
                    self.assertEqual(done.result["confidence"], "low")
                    self.assertEqual(done.result["error_code"], code)

    def test_persisted_failure_envelopes_are_low_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sup = DispatchSupervisor(home=_home(tmp))
            project = _project(tmp)
            for status in ("failed", "timeout", "cancelled"):
                record = sup.accept(_task(), project_root=project)
                done = sup.store.update_status(record.run_id, status)
                self.assertEqual(done.result["confidence"], "low")
                self.assertFalse(done.result["patch_partial"])
            record = sup.accept(_task(), project_root=project)
            done = sup.store.update_status(record.run_id, "failed", result={
                "confidence": "high", "patch_ref": "partial.diff", "status": "ok"})
            self.assertEqual(done.result["confidence"], "low")
            self.assertTrue(done.result["patch_partial"])

    def test_echo_requires_explicit_simulation_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sup = DispatchSupervisor(home=_home(tmp))
            task = _task()
            task["allow_offline_simulation"] = False
            with self.assertRaisesRegex(DispatchValidationError, "allow_offline_simulation"):
                sup.accept(task, project_root=_project(tmp))


class PanelTests(unittest.TestCase):
    def test_panel_echo_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            board = run_panel(
                _task(),
                project_root=project,
                members=["echo"],
                home=home,
                timeout_seconds=30,
            )
            self.assertEqual(board["members"], ["echo"])
            self.assertEqual(len(board["runs"]), 1)
            self.assertTrue(Path(str(board["board_path"])).is_file())

    def test_default_panel_never_falls_back_to_offline_simulation(self) -> None:
        echo = get_adapter("echo")
        with patch.object(registry_module, "_all", return_value={"echo": echo}):
            with self.assertRaisesRegex(DispatchValidationError, "no authenticated"):
                resolve_panel_members(None)

    def test_default_panel_can_use_non_codex_ready_provider(self) -> None:
        with patch(
            "experiments.local_agent_dispatch.panel.probe_backends",
            return_value=[
                {
                    "id": "grok",
                    "available": True,
                    "authenticated": True,
                    "supported": True,
                    "execution_kind": "provider",
                }
            ],
        ):
            self.assertEqual(resolve_panel_members(None), ["grok"])

    def test_all_panel_members_selects_every_ready_provider(self) -> None:
        rows = [
            {
                "id": backend,
                "available": True,
                "authenticated": True,
                "supported": True,
                "execution_kind": "provider",
            }
            for backend in ("pi", "claude", "codex", "grok", "hermes")
        ]
        rows.append(
            {
                "id": "echo",
                "available": True,
                "authenticated": True,
                "supported": False,
                "execution_kind": "offline-simulation",
            }
        )
        with patch(
            "experiments.local_agent_dispatch.panel.probe_backends",
            return_value=rows,
        ):
            self.assertEqual(
                resolve_panel_members(["all"]),
                ["codex", "claude", "grok", "hermes", "pi"],
            )

    def test_all_panel_member_cannot_be_mixed_with_backend_ids(self) -> None:
        with self.assertRaisesRegex(DispatchValidationError, "cannot be combined"):
            resolve_panel_members(["all", "codex"])

    def test_requested_panel_members_are_deduplicated(self) -> None:
        self.assertEqual(resolve_panel_members(["echo", "echo"]), ["echo"])

    def test_panel_member_failure_is_recorded_without_losing_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            with patch.object(
                DispatchSupervisor,
                "execute",
                side_effect=DispatchValidationError("simulated member failure"),
            ):
                board = run_panel(
                    _task(),
                    project_root=_project(tmp),
                    members=["echo"],
                    home=home,
                    timeout_seconds=30,
                )
            self.assertEqual(board["runs"][0]["status"], "failed")
            self.assertEqual(
                board["runs"][0]["error"], "simulated member failure"
            )
            persisted = DispatchSupervisor(home=home).store.load(
                str(board["runs"][0]["run_id"])
            )
            self.assertEqual(persisted.status, "failed")
            self.assertTrue(Path(str(board["board_path"])).is_file())


class SkillAndGcTests(unittest.TestCase):
    def test_skill_render_and_gc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            text = render_skill_markdown(home=home)
            self.assertIn("name: dyro-dispatch", text)
            self.assertIn("Available backends", text)
            self.assertIn("never `**/*`", text)
            path = write_skill(home=home)
            self.assertTrue(path.is_file())

            project = _project(tmp)
            sup = DispatchSupervisor(home=home)
            rec = sup.accept(_task(), project_root=project)
            done = sup.execute(rec.run_id)
            # Force age
            payload_path = home / "runs" / f"{done.run_id}.json"
            data = json.loads(payload_path.read_text(encoding="utf-8"))
            data["updated_at"] = 1.0
            payload_path.write_text(json.dumps(data), encoding="utf-8")
            report = gc(home=home, max_age_seconds=10, dry_run=False)
            self.assertIn(done.run_id, report["removed_runs"])


class CliTests(unittest.TestCase):
    def test_cli_backends_and_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            task_path = Path(tmp) / "task.json"
            task_path.write_text(json.dumps(_task()), encoding="utf-8")
            code = cli_main(
                [
                    "--home",
                    str(home),
                    "--dry-run",
                    "backends",
                ]
            )
            self.assertEqual(code, 0)
            code = cli_main(
                [
                    "--home",
                    str(home),
                    "run",
                    "--project",
                    str(project),
                    "--file",
                    str(task_path),
                    "--wait",
                    "--backend",
                    "echo",
                ]
            )
            self.assertEqual(code, 0)

    def test_dry_run_validates_contract_and_known_backend_without_state_or_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            project = _project(tmp)
            task_path = Path(tmp) / "task.json"
            task_path.write_text(json.dumps(_task(backend="codex")), encoding="utf-8")
            output = io.StringIO()
            errors = io.StringIO()
            with (
                patch(
                    "experiments.local_agent_dispatch.adapters.subprocess_cli.run_bounded"
                ) as bounded,
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                code = cli_main(
                    [
                        "--home",
                        str(home),
                        "--dry-run",
                        "run",
                        "--project",
                        str(project),
                        "--file",
                        str(task_path),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output.getvalue())["valid"])
            self.assertTrue(
                json.loads(output.getvalue())["requires_allow_unconfined_provider"]
            )
            bounded.assert_not_called()
            self.assertFalse(home.exists())

            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli_main(
                    [
                        "--home",
                        str(home),
                        "--dry-run",
                        "run",
                        "--project",
                        str(project),
                        "--file",
                        str(task_path),
                        "--backend",
                        "not-a-provider",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("unknown backend", errors.getvalue())
            self.assertFalse(home.exists())


class RegistryTests(unittest.TestCase):
    def test_auto_and_echo(self) -> None:
        echo = get_adapter("echo")
        self.assertTrue(echo.available())
        with patch.object(registry_module, "_all", return_value={"echo": echo}):
            with self.assertRaisesRegex(DispatchValidationError, "no available authenticated"):
                get_adapter("auto")
        rows = probe_backends(passive=True)
        self.assertTrue(any(r["id"] == "echo" for r in rows))
        for provider_id in (
            "cursor-agent",
            "opencode",
            "grok",
            "hermes",
            "kimi",
            "dsh",
            "pi",
        ):
            row = next(r for r in rows if r["id"] == provider_id)
            self.assertTrue(row["supported"])
            self.assertEqual(row["execution_kind"], "provider")
            self.assertEqual(row["command"], provider_id)

    def test_dry_run_backend_and_doctor_surfaces_never_probe_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            with patch.object(
                registry_module,
                "adapter_is_authenticated",
                side_effect=AssertionError("active auth probe is forbidden"),
            ):
                for command in ("backends", "doctor"):
                    output = io.StringIO()
                    with self.subTest(command=command), redirect_stdout(output):
                        code = cli_main(
                            [
                                "--home",
                                str(home),
                                "--dry-run",
                                command,
                            ]
                        )
                    self.assertEqual(code, 0)
                    rows = json.loads(output.getvalue())["backends"]
                    self.assertTrue(rows)
                    self.assertTrue(
                        all(
                            row["authentication_probe"] == "not_run"
                            for row in rows
                        )
                    )
            self.assertFalse(home.exists())

    def test_routes_reject_simulation_and_unknown_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp)
            for backend in ("echo", "not-a-provider"):
                with self.subTest(backend=backend), self.assertRaises(DispatchValidationError):
                    save_route("default", backend, home=home)


class ContractRejectTests(unittest.TestCase):
    def test_named_provider_credential_assignment_is_rejected(self) -> None:
        for assignment in (
            "KIMI_API_KEY=ordinary-kimi-credential",
            "DEEPSEEK_API_KEY: ordinary-deepseek-credential",
        ):
            with self.subTest(assignment=assignment):
                verdict = check_content(assignment)
                self.assertFalse(verdict.allowed)

    def test_forbidden_star(self) -> None:
        with self.assertRaises(DispatchValidationError):
            parse_task_contract(
                {
                    "schema_version": 1,
                    "files": ["**/*"],
                    "task": {
                        "briefing": "a",
                        "locations": "b",
                        "objective": "c",
                        "constraints": "d",
                        "output_contract": "e",
                    },
                }
            )


if __name__ == "__main__":
    unittest.main()


class AdapterRepairTests(unittest.TestCase):
    def completed(self, stdout='', **kwargs):
        from experiments.local_agent_dispatch.bounded_process import BoundedCompletedProcess
        return BoundedCompletedProcess(args=('fake',), returncode=0, stdout=stdout, stderr=kwargs.pop('stderr', ''), **kwargs)

    def answer(self):
        return json.dumps({'summary': 'bounded answer', 'confidence': 'high', 'evidence': []})

    def test_grok_structured_wrappers_and_raw_failure_evidence(self):
        from experiments.local_agent_dispatch.adapters.subprocess_cli import _parse_wrapped_model_json, _completed_to_result
        for key in ('text', 'result', 'response', 'output'):
            parsed = _parse_wrapped_model_json(json.dumps({key: self.answer()}), backend='grok')
            self.assertEqual(parsed['summary'], 'bounded answer')
        result = _completed_to_result(self.completed('unrecognized provider response'), backend='grok', parser=lambda text: _parse_wrapped_model_json(text, backend='grok'))
        self.assertEqual(result.error_code, 'protocol_error')
        self.assertIn('unrecognized provider response', result.warnings)

    def test_output_limit_preserves_received_final(self):
        from experiments.local_agent_dispatch.adapters.subprocess_cli import _completed_to_result
        result = _completed_to_result(self.completed(self.answer() + '\ntruncated event', output_limited=True), backend='fake')
        self.assertEqual(result.summary, 'bounded answer')
        self.assertTrue(result.usage['partial'])
        self.assertEqual(result.error_code, 'output_limit')

    def test_empty_stdout_surfaces_stderr(self):
        from experiments.local_agent_dispatch.adapters.subprocess_cli import _completed_to_result
        for backend in ('agy', 'cursor-agent'):
            result = _completed_to_result(self.completed(stderr='Shell command auto-denied'), backend=backend)
            self.assertIn('Shell command auto-denied', result.warnings)
            self.assertEqual(result.error_code, 'protocol_error')

    def test_auth_retry_once_and_failed_row_reason(self):
        from unittest.mock import Mock
        adapter = Mock(id='grok', command='grok', strict_isolation=False)
        adapter.available.return_value = True
        adapter.authenticated.side_effect = [False, True]
        self.assertTrue(registry_module.adapter_is_authenticated(adapter))
        self.assertEqual(adapter.authenticated.call_count, 2)
        adapter.authenticated.side_effect = [False, False]
        adapter.readiness_reason.return_value = 'local probe failed'
        with patch.object(registry_module, '_all', return_value={'grok': adapter}):
            self.assertEqual(probe_backends()[0]['reason'], 'local probe failed')

    def test_agy_argv_no_shell_and_fail_closed_discovery(self):
        import experiments.local_agent_dispatch.adapters.subprocess_cli as module
        from types import SimpleNamespace
        adapter = module.agy_adapter()
        with patch.object(adapter, 'available', return_value=True), patch.object(module, '_build_prompt', return_value='task'), patch.object(adapter, '_run', return_value='fake') as run:
            contract = SimpleNamespace(mode='read-only', allow_unconfined_provider=True)
            adapter.run(contract=contract, cwd=Path('.'), context_files={}, timeout_seconds=12)
            self.assertEqual(run.call_args.args[0], ['agy', '--print', '--mode', 'plan', '--output-format', 'json', '--print-timeout', '12s', '--disable-slash-commands'])
            self.assertIn('Do not run shell commands', run.call_args.kwargs['prompt'])
            self.assertFalse(adapter.authenticated())
            self.assertNotIn('edit', adapter.supported_modes)

    def test_fake_cli_limit_keeps_answer_before_noisy_suffix(self):
        import sys
        from experiments.local_agent_dispatch.bounded_process import run_bounded
        from experiments.local_agent_dispatch.adapters.subprocess_cli import _completed_to_result
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fake_cli.py'
            script.write_text('import sys\nsys.stdout.write(' + repr(self.answer()) + ')\nsys.stdout.flush()\nsys.stdout.write("x" * 100000)\nsys.stdout.flush()\n')
            completed = run_bounded([sys.executable, str(script)], cwd=Path(tmp), timeout_seconds=5, max_output_bytes=4096)
        self.assertTrue(completed.output_limited)
        result = _completed_to_result(completed, backend='fake')
        self.assertEqual(result.summary, 'bounded answer')
        self.assertTrue(result.usage['partial'])

    def test_grok_argv_from_help(self):
        import experiments.local_agent_dispatch.adapters.subprocess_cli as module
        from types import SimpleNamespace
        adapter = module.grok_adapter()
        with tempfile.TemporaryDirectory() as tmp, patch.object(adapter, 'available', return_value=True), patch.object(adapter, '_execution_value', return_value='fake-model'), patch.object(module, '_build_prompt', return_value='task'), patch.object(adapter, '_run', return_value='fake') as run:
            adapter.run(contract=SimpleNamespace(mode='read-only'), cwd=Path(tmp), context_files={}, timeout_seconds=10)
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index('--output-format') + 1], 'json')
            self.assertNotIn('--no-memory', argv)
            self.assertEqual(argv[argv.index('--permission-mode') + 1], 'plan')
            self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')

    def test_probe_failure_records_safe_reason(self):
        import experiments.local_agent_dispatch.adapters.subprocess_cli as module
        from experiments.local_agent_dispatch.bounded_process import BoundedCompletedProcess
        adapter = module.codex_adapter()
        failed = BoundedCompletedProcess(args=('fake',), returncode=1, stdout='', stderr='login expired')
        with patch.object(adapter, 'available', return_value=True), patch.object(module, 'run_bounded', return_value=failed) as bounded:
            self.assertFalse(registry_module.adapter_is_authenticated(adapter))
            self.assertEqual(bounded.call_count, 2)
            self.assertEqual(adapter.readiness_reason(), 'login expired')
