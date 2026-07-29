from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from experiments.external_workflow_runner.artifacts import (
    ArtifactPolicy,
    validate_artifacts,
)
from experiments.external_workflow_runner.broker import BrokerLimiter
from experiments.external_workflow_runner.errors import Stage0ValidationError
from experiments.external_workflow_runner.manifest import (
    build_bundle_manifest,
    verify_bundle_manifest,
)
from experiments.external_workflow_runner.process import (
    ProcessLimits,
    run_bounded_process,
)
from experiments.external_workflow_runner.result import validate_result_envelope
from experiments.external_workflow_runner.sandbox import (
    BUN_IMAGE,
    BUN_USER,
    BUN_VERSION,
    DockerSandboxConfig,
    DockerSandboxResult,
    DockerSandboxRunner,
)
from experiments.external_workflow_runner.supervisor import (
    Stage0Supervisor,
    SupervisorConfig,
    _read_result,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BundleManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ewr-stage0-manifest-")
        self.root = Path(self.temporary.name)
        (self.root / "workflow").mkdir()
        (self.root / "workflow/main.ts").write_text(
            "export default 1;\n", encoding="utf-8"
        )
        (self.root / "wrapper.py").write_text("print('wrapper')\n", encoding="utf-8")
        self.identity = {
            "workflow_runtime": {
                "implementation": "evaluated-typescript-runtime",
                "version": "0.2.0",
                "integrity": "sha512-example",
            },
            "runtime": {
                "bun": "1.3.11",
                "container": BUN_IMAGE,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_manifest_binds_exact_files_identity_and_root_hash(self) -> None:
        manifest = build_bundle_manifest(self.root, identity=self.identity)

        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            ["workflow/main.ts", "wrapper.py"],
        )
        self.assertRegex(manifest["bundle_manifest_sha256"], r"^[0-9a-f]{64}$")
        verify_bundle_manifest(self.root, manifest, expected_identity=self.identity)

    def test_modified_or_extra_file_is_rejected(self) -> None:
        manifest = build_bundle_manifest(self.root, identity=self.identity)
        (self.root / "workflow/main.ts").write_text(
            "export default 2;\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(Stage0ValidationError, "bundle"):
            verify_bundle_manifest(self.root, manifest, expected_identity=self.identity)

        (self.root / "workflow/main.ts").write_text(
            "export default 1;\n", encoding="utf-8"
        )
        (self.root / "undeclared.ts").write_text("export {};\n", encoding="utf-8")
        with self.assertRaisesRegex(Stage0ValidationError, "bundle"):
            verify_bundle_manifest(self.root, manifest, expected_identity=self.identity)

    def test_symlink_is_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.write_text("secret\n", encoding="utf-8")
        self.addCleanup(outside.unlink)
        (self.root / "linked.ts").symlink_to(outside)

        with self.assertRaisesRegex(Stage0ValidationError, "symbolic link"):
            build_bundle_manifest(self.root, identity=self.identity)

    def test_reserved_manifest_filename_is_rejected(self) -> None:
        (self.root / "bundle-manifest.json").write_text(
            '{"untrusted":true}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(Stage0ValidationError, "reserved"):
            build_bundle_manifest(self.root, identity=self.identity)

    def test_bundle_entry_enumeration_is_bounded(self) -> None:
        with mock.patch(
            "experiments.external_workflow_runner.manifest.MAX_BUNDLE_ENTRIES",
            2,
            create=True,
        ):
            with self.assertRaisesRegex(Stage0ValidationError, "entry count"):
                build_bundle_manifest(self.root, identity=self.identity)


class ResultEnvelopeTests(unittest.TestCase):
    def _done(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "DONE",
            "workflow_run_id": "run-123",
            "branches": [
                {
                    "id": "analysis-a",
                    "critical": True,
                    "status": "success",
                    "error_code": "",
                },
                {
                    "id": "analysis-b",
                    "critical": True,
                    "status": "success",
                    "error_code": "",
                },
            ],
            "artifacts": [
                {
                    "repository": "docs",
                    "path": "report.md",
                    "sha256": "a" * 64,
                }
            ],
            "question": "",
        }

    def test_done_requires_exact_run_and_critical_branch_set(self) -> None:
        envelope = self._done()
        validated = validate_result_envelope(
            envelope,
            workflow_run_id="run-123",
            expected_branches={"analysis-a": True, "analysis-b": True},
        )
        self.assertEqual(validated["status"], "DONE")

        envelope["branches"] = [envelope["branches"][0]]
        with self.assertRaisesRegex(Stage0ValidationError, "branch"):
            validate_result_envelope(
                envelope,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )

    def test_null_failed_or_duplicate_critical_branch_cannot_be_done(self) -> None:
        for invalid in (None, "failed", "question"):
            with self.subTest(invalid=invalid):
                envelope = self._done()
                envelope["branches"][0]["status"] = invalid
                with self.assertRaises(Stage0ValidationError):
                    validate_result_envelope(
                        envelope,
                        workflow_run_id="run-123",
                        expected_branches={"analysis-a": True, "analysis-b": True},
                    )

        envelope = self._done()
        envelope["branches"].append(dict(envelope["branches"][0]))
        with self.assertRaisesRegex(Stage0ValidationError, "duplicate"):
            validate_result_envelope(
                envelope,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )

    def test_blocked_and_question_need_explicit_reason(self) -> None:
        blocked = self._done()
        blocked["status"] = "BLOCKED"
        blocked["branches"][0]["status"] = "failed"
        blocked["branches"][0]["error_code"] = ""
        with self.assertRaisesRegex(Stage0ValidationError, "error_code"):
            validate_result_envelope(
                blocked,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )

        question = self._done()
        question["status"] = "QUESTION"
        question["branches"][0]["status"] = "question"
        with self.assertRaisesRegex(Stage0ValidationError, "question"):
            validate_result_envelope(
                question,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )

    def test_wrong_json_types_fail_closed_with_validation_errors(self) -> None:
        wrong_schema = self._done()
        wrong_schema["schema_version"] = True
        with self.assertRaises(Stage0ValidationError):
            validate_result_envelope(
                wrong_schema,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )

        wrong_status = self._done()
        wrong_status["status"] = []
        with self.assertRaises(Stage0ValidationError):
            validate_result_envelope(
                wrong_status,
                workflow_run_id="run-123",
                expected_branches={"analysis-a": True, "analysis-b": True},
            )


class ArtifactValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="ewr-stage0-artifact-")
        self.root = Path(self.temporary.name)
        (self.root / "nested").mkdir()
        self.report = self.root / "nested/report.md"
        self.report.write_text("# Report\n", encoding="utf-8")
        self.policy = ArtifactPolicy(
            repository_roots={"docs": self.root},
            allowed_paths={("docs", "nested/report.md")},
            max_artifacts=2,
            max_artifact_bytes=1024,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_allowed_regular_file_is_reopened_without_following_links(self) -> None:
        artifacts = [
            {
                "repository": "docs",
                "path": "nested/report.md",
                "sha256": _sha256(self.report),
            }
        ]
        validated = validate_artifacts(artifacts, self.policy)
        self.assertEqual(validated[0].size, len("# Report\n".encode()))

    def test_traversal_and_final_symlink_are_rejected(self) -> None:
        traversal = [
            {
                "repository": "docs",
                "path": "../outside.md",
                "sha256": "a" * 64,
            }
        ]
        with self.assertRaises(Stage0ValidationError):
            validate_artifacts(traversal, self.policy)

        link = self.root / "nested/link.md"
        link.symlink_to(self.report)
        link_policy = ArtifactPolicy(
            repository_roots={"docs": self.root},
            allowed_paths={("docs", "nested/link.md")},
            max_artifacts=2,
            max_artifact_bytes=1024,
        )
        with self.assertRaisesRegex(Stage0ValidationError, "symbolic link"):
            validate_artifacts(
                [
                    {
                        "repository": "docs",
                        "path": "nested/link.md",
                        "sha256": _sha256(self.report),
                    }
                ],
                link_policy,
            )

    def test_parent_symlink_and_hash_mismatch_are_rejected(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside"
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside)
        (outside / "report.md").write_text("# Outside\n", encoding="utf-8")
        (self.root / "linked").symlink_to(outside, target_is_directory=True)
        linked_policy = ArtifactPolicy(
            repository_roots={"docs": self.root},
            allowed_paths={("docs", "linked/report.md")},
            max_artifacts=2,
            max_artifact_bytes=1024,
        )
        with self.assertRaisesRegex(Stage0ValidationError, "symbolic link"):
            validate_artifacts(
                [
                    {
                        "repository": "docs",
                        "path": "linked/report.md",
                        "sha256": _sha256(outside / "report.md"),
                    }
                ],
                linked_policy,
            )

        with self.assertRaisesRegex(Stage0ValidationError, "SHA-256"):
            validate_artifacts(
                [
                    {
                        "repository": "docs",
                        "path": "nested/report.md",
                        "sha256": "b" * 64,
                    }
                ],
                self.policy,
            )

    def test_non_object_artifact_fails_closed(self) -> None:
        with self.assertRaises(Stage0ValidationError):
            validate_artifacts([None], self.policy)

    def test_policy_copies_allowlist_instead_of_retaining_mutable_input(self) -> None:
        allowed_paths = {("docs", "nested/report.md")}
        policy = ArtifactPolicy(
            repository_roots={"docs": self.root},
            allowed_paths=allowed_paths,
            max_artifacts=2,
            max_artifact_bytes=1024,
        )
        rogue = self.root / "rogue.md"
        rogue.write_text("rogue\n", encoding="utf-8")
        allowed_paths.add(("docs", "rogue.md"))

        with self.assertRaisesRegex(Stage0ValidationError, "allowlisted"):
            validate_artifacts(
                [
                    {
                        "repository": "docs",
                        "path": "rogue.md",
                        "sha256": _sha256(rogue),
                    }
                ],
                policy,
            )

    def test_policy_rejects_untrusted_roots_and_noncanonical_allowlist(self) -> None:
        with self.assertRaisesRegex(Stage0ValidationError, "absolute"):
            ArtifactPolicy(
                repository_roots={"docs": Path("relative-root")},
                allowed_paths={("docs", "report.md")},
                max_artifacts=1,
                max_artifact_bytes=1024,
            )
        with self.assertRaisesRegex(Stage0ValidationError, "traversal"):
            ArtifactPolicy(
                repository_roots={"docs": self.root},
                allowed_paths={("docs", "../outside.md")},
                max_artifacts=1,
                max_artifact_bytes=1024,
            )


class BrokerLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fractional_concurrency_is_rejected(self) -> None:
        with self.assertRaises(Stage0ValidationError):
            BrokerLimiter(max_concurrency=1.5, default_timeout_seconds=1)

    async def test_actual_concurrency_never_exceeds_semaphore(self) -> None:
        limiter = BrokerLimiter(max_concurrency=2, default_timeout_seconds=1)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(call_id: str) -> str:
            if limiter.active_calls == 2:
                entered.set()
            await release.wait()
            return call_id

        tasks = [
            asyncio.create_task(limiter.call(f"call-{index}", handler))
            for index in range(6)
        ]
        await asyncio.wait_for(entered.wait(), timeout=1)
        self.assertEqual(limiter.active_calls, 2)
        self.assertEqual(limiter.max_observed_concurrency, 2)
        release.set()
        self.assertEqual(
            await asyncio.gather(*tasks), [f"call-{index}" for index in range(6)]
        )
        self.assertEqual(limiter.max_observed_concurrency, 2)

    async def test_per_call_timeout_is_fail_closed(self) -> None:
        limiter = BrokerLimiter(max_concurrency=1, default_timeout_seconds=0.02)

        async def handler(_: str) -> str:
            await asyncio.sleep(10)
            return "late"

        with self.assertRaisesRegex(TimeoutError, "call-timeout"):
            await limiter.call("call-timeout", handler)
        self.assertEqual(limiter.active_calls, 0)

    async def test_zero_and_non_finite_deadlines_are_rejected(self) -> None:
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(default_timeout_seconds=invalid):
                with self.assertRaises(Stage0ValidationError):
                    BrokerLimiter(
                        max_concurrency=1,
                        default_timeout_seconds=invalid,
                    )

        limiter = BrokerLimiter(max_concurrency=1, default_timeout_seconds=1)

        async def handler(call_id: str) -> str:
            return call_id

        for index, invalid in enumerate((0, float("nan"), float("inf"), float("-inf"))):
            with self.subTest(timeout_seconds=invalid):
                with self.assertRaises(Stage0ValidationError):
                    await limiter.call(
                        f"invalid-deadline-{index}",
                        handler,
                        timeout_seconds=invalid,
                    )


class BoundedProcessTests(unittest.TestCase):
    def test_boolean_limits_are_rejected(self) -> None:
        with self.assertRaises(Stage0ValidationError):
            ProcessLimits(
                timeout_seconds=True,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            )

    def test_non_finite_process_deadlines_are_rejected(self) -> None:
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout_seconds=invalid):
                with self.assertRaises(Stage0ValidationError):
                    ProcessLimits(
                        timeout_seconds=invalid,
                        max_stdout_bytes=1024,
                        max_stderr_bytes=1024,
                    )
            with self.subTest(terminate_grace_seconds=invalid):
                with self.assertRaises(Stage0ValidationError):
                    ProcessLimits(
                        timeout_seconds=1,
                        max_stdout_bytes=1024,
                        max_stderr_bytes=1024,
                        terminate_grace_seconds=invalid,
                    )

    def test_timeout_terminates_the_complete_process_group(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "experiments/external_workflow_runner/fixtures/spawn_descendant.py"
        )
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-process-") as temporary:
            pid_file = Path(temporary) / "child.pid"
            result = run_bounded_process(
                [sys.executable, os.fspath(fixture), os.fspath(pid_file)],
                cwd=Path(temporary),
                environment={},
                limits=ProcessLimits(
                    timeout_seconds=0.25,
                    max_stdout_bytes=1024,
                    max_stderr_bytes=1024,
                    terminate_grace_seconds=0.1,
                ),
            )
            self.assertTrue(result.timed_out)
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertTrue(self._eventually_process_is_gone(child_pid))

    def test_output_limit_is_fail_closed_and_bounded(self) -> None:
        result = run_bounded_process(
            [sys.executable, "-c", "print('x' * 100000, flush=True)"],
            cwd=Path.cwd(),
            environment={},
            limits=ProcessLimits(
                timeout_seconds=2,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
                terminate_grace_seconds=0.1,
            ),
        )
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout), 1024)
        self.assertFalse(result.succeeded)

    def test_child_receives_only_the_explicit_environment(self) -> None:
        sentinel = "stage0-host-secret"
        previous = os.environ.get("DYRO_EXECUTION_KEY")
        os.environ["DYRO_EXECUTION_KEY"] = sentinel
        self.addCleanup(
            DockerSandboxIntegrationTests._restore_environment,
            "DYRO_EXECUTION_KEY",
            previous,
        )
        result = run_bounded_process(
            [
                sys.executable,
                "-c",
                (
                    "import os; "
                    "print(os.environ.get('SAFE_NAME', 'missing')); "
                    "print(os.environ.get('DYRO_EXECUTION_KEY', 'absent'))"
                ),
            ],
            cwd=Path.cwd(),
            environment={"SAFE_NAME": "allowed"},
            limits=ProcessLimits(
                timeout_seconds=2,
                max_stdout_bytes=1024,
                max_stderr_bytes=1024,
            ),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["allowed", "absent"])
        self.assertNotIn(sentinel, result.stdout)

    @staticmethod
    def _eventually_process_is_gone(pid: int) -> bool:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.02)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        return False


class SandboxConfigurationTests(unittest.TestCase):
    def test_runtime_lock_matches_enforced_sandbox_identity(self) -> None:
        runtime_lock = json.loads(
            (
                Path(__file__).parents[1]
                / "experiments/external_workflow_runner/runtime-lock.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(runtime_lock["schema_version"], 1)
        self.assertEqual(runtime_lock["runtime"]["bun_version"], BUN_VERSION)
        self.assertEqual(runtime_lock["runtime"]["container_image"], BUN_IMAGE)
        self.assertEqual(runtime_lock["runtime"]["container_user"], BUN_USER)

    def test_unapproved_container_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-config-") as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(Stage0ValidationError, "approved"):
                DockerSandboxConfig(
                    name="dyro-stage0-config",
                    image=f"unapproved.invalid/runtime@sha256:{'0' * 64}",
                    bundle_root=root,
                    run_root=root,
                    worktrees={"docs": root},
                    environment={},
                )

    def test_environment_is_snapshotted_and_exposed_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-config-") as temporary:
            root = Path(temporary)
            environment = {
                "DYRO_WORKFLOW_RUN_ID": "stage0-config",
                "DYRO_RESULT_PATH": "/run/dyro/result.json",
            }
            config = DockerSandboxConfig(
                name="dyro-stage0-config",
                image=BUN_IMAGE,
                bundle_root=root,
                run_root=root,
                worktrees={"docs": root},
                environment=environment,
            )
            environment["DYRO_EXECUTION_KEY"] = "must-not-appear"

            self.assertNotIn(
                "DYRO_EXECUTION_KEY",
                "\n".join(config.argv(["bun", "--version"])),
            )
            with self.assertRaises(TypeError):
                config.environment["DYRO_EXECUTION_KEY"] = "must-fail"
            with self.assertRaises(TypeError):
                config.worktrees["rogue"] = root

    def test_non_finite_cpu_limits_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-config-") as temporary:
            root = Path(temporary)
            for invalid in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(cpus=invalid):
                    with self.assertRaises(Stage0ValidationError):
                        DockerSandboxConfig(
                            name="dyro-stage0-config",
                            image=BUN_IMAGE,
                            bundle_root=root,
                            run_root=root,
                            worktrees={"docs": root},
                            environment={},
                            cpus=invalid,
                        )

    def test_cleanup_waits_for_a_late_owned_container(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-config-") as temporary:
            root = Path(temporary)
            config = DockerSandboxConfig(
                name="dyro-stage0-late-cleanup",
                image=BUN_IMAGE,
                bundle_root=root,
                run_root=root,
                worktrees={"docs": root},
                environment={},
            )
            runner = DockerSandboxRunner(config)
            removal = subprocess.CompletedProcess(
                args=["docker", "rm"],
                returncode=0,
                stdout="",
                stderr="",
            )

            with (
                mock.patch.object(
                    runner,
                    "_container_owner",
                    side_effect=[None, config.cleanup_token, None],
                ),
                mock.patch(
                    "experiments.external_workflow_runner.sandbox.subprocess.run",
                    return_value=removal,
                ) as run,
            ):
                runner._force_remove(settle_seconds=0.2)

            run.assert_called_once()
            self.assertEqual(
                run.call_args.args[0],
                ["docker", "rm", "--force", config.name],
            )


def _docker_image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "image", "inspect", BUN_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


@unittest.skipUnless(_docker_image_available(), f"requires local image {BUN_IMAGE}")
class DockerSandboxIntegrationTests(unittest.TestCase):
    def test_result_reader_converts_parser_limits_to_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-result-") as temporary:
            root = Path(temporary)
            invalid_payloads = {
                "huge-integer.json": (
                    '{"schema_version":' + ("1" * 5000) + ',"status":"DONE"}'
                ),
                "deeply-nested.json": ("[" * 1100) + "0" + ("]" * 1100),
            }
            for filename, payload in invalid_payloads.items():
                with self.subTest(filename=filename):
                    (root / filename).write_text(payload, encoding="utf-8")
                    with self.assertRaises(Stage0ValidationError):
                        _read_result(root, filename, 16 * 1024)

    def test_supervisor_rejects_runtime_identity_drift(self) -> None:
        fixture_root = (
            Path(__file__).parents[1] / "experiments/external_workflow_runner/fixtures"
        )
        identity = json.loads(
            (
                Path(__file__).parents[1]
                / "experiments/external_workflow_runner/runtime-lock.json"
            ).read_text(encoding="utf-8")
        )
        manifest = build_bundle_manifest(fixture_root, identity=identity)
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-identity-") as temporary:
            root = Path(temporary)
            sandbox = DockerSandboxConfig(
                name="dyro-stage0-identity",
                image=BUN_IMAGE,
                bundle_root=fixture_root,
                run_root=root,
                worktrees={"docs": root},
                environment={
                    "DYRO_WORKFLOW_RUN_ID": "stage0-identity",
                    "DYRO_RESULT_PATH": "/run/dyro/result.json",
                },
            )
            identity["runtime"]["container_user"] = "0:0"

            with self.assertRaisesRegex(Stage0ValidationError, "runtime identity"):
                SupervisorConfig(
                    sandbox=sandbox,
                    bundle_manifest=manifest,
                    bundle_identity=identity,
                    workflow_run_id="stage0-identity",
                    expected_branches={"analysis": True},
                    artifact_policy=ArtifactPolicy(
                        repository_roots={"docs": root},
                        allowed_paths=set(),
                        max_artifacts=1,
                        max_artifact_bytes=1024,
                    ),
                    result_filename="result.json",
                    max_result_bytes=1024,
                )

    def test_supervisor_reverifies_bundle_after_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ewr-stage0-reverify-") as temporary:
            root = Path(temporary)
            bundle_root = root / "bundle"
            run_root = root / "run"
            worktree = root / "worktree"
            bundle_root.mkdir()
            run_root.mkdir()
            worktree.mkdir()
            payload = bundle_root / "workflow.ts"
            payload.write_text("export {};\n", encoding="utf-8")
            identity = {
                "schema_version": 1,
                "runtime": {
                    "bun_version": BUN_VERSION,
                    "container_image": BUN_IMAGE,
                    "container_user": BUN_USER,
                },
            }
            manifest = build_bundle_manifest(bundle_root, identity=identity)
            supervisor = Stage0Supervisor(
                SupervisorConfig(
                    sandbox=DockerSandboxConfig(
                        name="dyro-stage0-reverify",
                        image=BUN_IMAGE,
                        bundle_root=bundle_root,
                        run_root=run_root,
                        worktrees={"docs": worktree},
                        environment={
                            "DYRO_WORKFLOW_RUN_ID": "stage0-reverify",
                            "DYRO_RESULT_PATH": "/run/dyro/result.json",
                        },
                    ),
                    bundle_manifest=manifest,
                    bundle_identity=identity,
                    workflow_run_id="stage0-reverify",
                    expected_branches={"analysis": True},
                    artifact_policy=ArtifactPolicy(
                        repository_roots={"docs": worktree},
                        allowed_paths=set(),
                        max_artifacts=1,
                        max_artifact_bytes=1024,
                    ),
                    result_filename="result.json",
                    max_result_bytes=1024,
                )
            )

            def mutate_bundle(*_args, **_kwargs) -> DockerSandboxResult:
                payload.write_text("export const changed = true;\n", encoding="utf-8")
                return DockerSandboxResult(
                    returncode=2,
                    stdout="",
                    stderr="failed",
                    cleanup_verified=True,
                )

            with mock.patch(
                "experiments.external_workflow_runner.supervisor."
                "DockerSandboxRunner.run",
                side_effect=mutate_bundle,
            ):
                with self.assertRaisesRegex(Stage0ValidationError, "bundle"):
                    supervisor.execute(
                        ["bun", "/opt/workflow/workflow.ts"], timeout_seconds=1
                    )

    def test_existing_container_with_same_name_is_never_removed(self) -> None:
        fixture_root = (
            Path(__file__).parents[1] / "experiments/external_workflow_runner/fixtures"
        )
        shared_root = Path.cwd()
        with tempfile.TemporaryDirectory(
            prefix=".ewr-stage0-owner-worktree-",
            dir=shared_root,
        ) as worktree_raw:
            with tempfile.TemporaryDirectory(
                prefix=".ewr-stage0-owner-run-",
                dir=shared_root,
            ) as run_raw:
                worktree = Path(worktree_raw)
                run_root = Path(run_raw)
                worktree.chmod(0o777)
                run_root.chmod(0o777)
                name = f"dyro-stage0-owner-{os.getpid()}"
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "--detach",
                        "--name",
                        name,
                        BUN_IMAGE,
                        "sh",
                        "-c",
                        "sleep 60",
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.addCleanup(
                    subprocess.run,
                    ["docker", "rm", "--force", name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                config = DockerSandboxConfig(
                    name=name,
                    image=BUN_IMAGE,
                    bundle_root=fixture_root,
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    environment={
                        "DYRO_WORKFLOW_RUN_ID": "stage0-owner",
                        "DYRO_RESULT_PATH": "/run/dyro/probe.json",
                    },
                    memory="128m",
                    pids_limit=32,
                )

                with self.assertRaisesRegex(Stage0ValidationError, "already exists"):
                    DockerSandboxRunner(config).run(
                        ["bun", "--version"],
                        timeout_seconds=5,
                    )

                inspection = subprocess.run(
                    ["docker", "container", "inspect", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                self.assertEqual(inspection.returncode, 0)

    def test_supervisor_validates_bundle_result_and_artifact_end_to_end(self) -> None:
        fixture_root = (
            Path(__file__).parents[1] / "experiments/external_workflow_runner/fixtures"
        )
        identity = json.loads(
            (
                Path(__file__).parents[1]
                / "experiments/external_workflow_runner/runtime-lock.json"
            ).read_text(encoding="utf-8")
        )
        manifest = build_bundle_manifest(fixture_root, identity=identity)
        shared_root = Path.cwd()
        with tempfile.TemporaryDirectory(
            prefix=".ewr-stage0-supervisor-worktree-",
            dir=shared_root,
        ) as worktree_raw:
            with tempfile.TemporaryDirectory(
                prefix=".ewr-stage0-supervisor-run-",
                dir=shared_root,
            ) as run_raw:
                worktree = Path(worktree_raw)
                run_root = Path(run_raw)
                worktree.chmod(0o777)
                run_root.chmod(0o777)
                workflow_run_id = "stage0-supervised-run"
                sandbox = DockerSandboxConfig(
                    name=f"dyro-stage0-supervisor-{os.getpid()}",
                    image=BUN_IMAGE,
                    bundle_root=fixture_root,
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    environment={
                        "DYRO_WORKFLOW_RUN_ID": workflow_run_id,
                        "DYRO_RESULT_PATH": "/run/dyro/result-envelope.json",
                    },
                    memory="128m",
                    pids_limit=32,
                )
                policy = ArtifactPolicy(
                    repository_roots={"docs": worktree},
                    allowed_paths={("docs", "report.md")},
                    max_artifacts=1,
                    max_artifact_bytes=1024,
                    max_total_bytes=1024,
                )
                supervisor = Stage0Supervisor(
                    SupervisorConfig(
                        sandbox=sandbox,
                        bundle_manifest=manifest,
                        bundle_identity=identity,
                        workflow_run_id=workflow_run_id,
                        expected_branches={
                            "analysis-a": True,
                            "analysis-b": True,
                        },
                        artifact_policy=policy,
                        result_filename="result-envelope.json",
                        max_result_bytes=16 * 1024,
                    )
                )
                manifest["bundle_manifest_sha256"] = "0" * 64

                result = supervisor.execute(
                    ["bun", "/opt/workflow/success_workflow.ts"],
                    timeout_seconds=10,
                )

                self.assertEqual(result.envelope["status"], "DONE")
                self.assertEqual(result.process.returncode, 0)
                self.assertTrue(result.process.cleanup_verified)
                self.assertEqual(result.artifacts[0].path, "report.md")
                self.assertEqual(
                    (worktree / "report.md").read_text(encoding="utf-8"),
                    "# Stage 0 report\n",
                )

    def test_malicious_top_level_typescript_is_confined(self) -> None:
        fixture_root = (
            Path(__file__).parents[1] / "experiments/external_workflow_runner/fixtures"
        )
        shared_root = Path.cwd()
        with tempfile.TemporaryDirectory(
            prefix=".ewr-stage0-worktree-",
            dir=shared_root,
        ) as worktree_raw:
            with tempfile.TemporaryDirectory(
                prefix=".ewr-stage0-run-",
                dir=shared_root,
            ) as run_raw:
                worktree = Path(worktree_raw)
                run_root = Path(run_raw)
                worktree.chmod(0o777)
                run_root.chmod(0o777)
                sentinel = "must-not-enter-container"
                previous = os.environ.get("DYRO_EXECUTION_KEY")
                os.environ["DYRO_EXECUTION_KEY"] = sentinel
                self.addCleanup(
                    self._restore_environment, "DYRO_EXECUTION_KEY", previous
                )
                config = DockerSandboxConfig(
                    name=f"dyro-stage0-{os.getpid()}",
                    image=BUN_IMAGE,
                    bundle_root=fixture_root,
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    environment={
                        "DYRO_WORKFLOW_RUN_ID": "stage0-run",
                        "DYRO_RESULT_PATH": "/run/dyro/probe.json",
                    },
                    memory="128m",
                    pids_limit=32,
                )

                result = DockerSandboxRunner(config).run(
                    ["bun", "/opt/workflow/malicious_workflow.ts"],
                    timeout_seconds=15,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                probe = json.loads(
                    (run_root / "probe.json").read_text(encoding="utf-8")
                )
                self.assertTrue(probe["allowed_write"])
                self.assertTrue(probe["bundle_write_denied"])
                self.assertTrue(probe["root_write_denied"])
                self.assertTrue(probe["network_denied"])
                self.assertTrue(probe["secret_absent"])
                self.assertNotIn(sentinel, json.dumps(probe))
                self.assertEqual(
                    (worktree / "allowed.md").read_text(encoding="utf-8"), "allowed\n"
                )

    def test_workflow_deadline_removes_the_container(self) -> None:
        fixture_root = (
            Path(__file__).parents[1] / "experiments/external_workflow_runner/fixtures"
        )
        shared_root = Path.cwd()
        with tempfile.TemporaryDirectory(
            prefix=".ewr-stage0-timeout-worktree-",
            dir=shared_root,
        ) as worktree_raw:
            with tempfile.TemporaryDirectory(
                prefix=".ewr-stage0-timeout-run-",
                dir=shared_root,
            ) as run_raw:
                worktree = Path(worktree_raw)
                run_root = Path(run_raw)
                worktree.chmod(0o777)
                run_root.chmod(0o777)
                name = f"dyro-stage0-timeout-{os.getpid()}"
                config = DockerSandboxConfig(
                    name=name,
                    image=BUN_IMAGE,
                    bundle_root=fixture_root,
                    run_root=run_root,
                    worktrees={"docs": worktree},
                    environment={
                        "DYRO_WORKFLOW_RUN_ID": "stage0-timeout",
                        "DYRO_RESULT_PATH": "/run/dyro/probe.json",
                    },
                    memory="128m",
                    pids_limit=32,
                )

                with self.assertRaisesRegex(TimeoutError, "deadline"):
                    DockerSandboxRunner(config).run(
                        ["bun", "/opt/workflow/hang_workflow.ts"],
                        timeout_seconds=0.25,
                    )

                inspection = subprocess.run(
                    ["docker", "container", "inspect", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                self.assertNotEqual(inspection.returncode, 0)

    @staticmethod
    def _restore_environment(name: str, previous: str | None) -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
