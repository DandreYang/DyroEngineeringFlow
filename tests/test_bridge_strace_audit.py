from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from dyro.bridge.catalog import MANDATORY_OPERATION_IDS, PHASE0_DECLARED_OPERATION_IDS
from dyro.bridge.git_read import _HELPER_SCRIPT
from dyro.bridge.models import ErrorCode
from dyro.bridge.schemas import get_operation_schema, get_request_envelope_schema
from dyro.bridge.transport import TransportContext, handle_request_bytes
from dyro.config import load
from dyro.workspace import get_line
from tools.audit_bridge_strace import audit_trace_files, main
from tools.verify_bridge_zero_effects import (
    VerificationError,
    load_protocol_corpus,
    prepare_fixture,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "bridge"
GIT_PREFIX = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    "-c",
    "core.commitGraph=false",
)


def _strace_string(value: str) -> str:
    return json.dumps(value)


def _exec_line(executable: str, argv: tuple[str, ...]) -> str:
    rendered = ", ".join(_strace_string(value) for value in argv)
    return f"execve({_strace_string(executable)}, [{rendered}], 0x7ffd) = 0"


class BridgeProtocolCorpusTests(unittest.TestCase):
    def _corpus(self) -> dict[str, object]:
        return json.loads((FIXTURE_ROOT / "protocol-v1.json").read_text())

    def _payload(self, case: dict[str, object]) -> bytes:
        if "request_json" in case:
            return json.dumps(case["request_json"], separators=(",", ":")).encode()
        if "request_text" in case:
            return case["request_text"].encode()
        if "request_hex" in case:
            return bytes.fromhex(case["request_hex"])
        generator = case["request_generator"]
        if generator["kind"] == "nested_array":
            depth = generator["depth"]
            return ("[" * depth + "]" * depth).encode()
        self.assertEqual(generator["kind"], "repeat_byte")
        return bytes.fromhex(generator["byte_hex"]) * generator["count"]

    def test_corpus_is_shared_by_source_wheel_and_sdist(self) -> None:
        corpus = self._corpus()
        self.assertEqual(corpus["version"], 1)
        self.assertEqual(corpus["artifact_matrix"], ["source", "wheel", "sdist"])
        self.assertTrue(corpus["execute_identical_cases_per_artifact"])
        cases = corpus["cases"]
        identifiers = [case["id"] for case in cases]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        valid = [case for case in cases if case["class"] == "valid_mandatory"]
        self.assertEqual(len(cases), 43)
        self.assertEqual(
            tuple(sorted(case["request_json"]["operation"] for case in valid)),
            MANDATORY_OPERATION_IDS,
        )
        for case in cases:
            payload_fields = {
                field
                for field in (
                    "request_json",
                    "request_text",
                    "request_hex",
                    "request_generator",
                )
                if field in case
            }
            self.assertEqual(len(payload_fields), 1, case["id"])
            self.assertIn("expected", case)

        envelope = Draft202012Validator(get_request_envelope_schema())
        for case in valid:
            request = case["request_json"]
            self.assertEqual(list(envelope.iter_errors(request)), [], case["id"])
            validator = Draft202012Validator(
                get_operation_schema(request["operation"]).input_schema()
            )
            self.assertEqual(
                list(validator.iter_errors(request["input"])), [], case["id"]
            )
        schema_targets = {
            case["request_json"]["input"]["operation"]
            for case in cases
            if case["class"] == "valid_schema"
        }
        schema_targets.add("bridge.hello")
        self.assertEqual(tuple(sorted(schema_targets)), MANDATORY_OPERATION_IDS)
        unavailable = {
            case["request_json"]["operation"]
            for case in cases
            if case["class"] == "unavailable_operation"
        }
        self.assertEqual(
            unavailable,
            set(PHASE0_DECLARED_OPERATION_IDS) - set(MANDATORY_OPERATION_IDS),
        )

    def test_objective_plan_fixture_uses_the_existing_anchor_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = prepare_fixture(Path(temporary) / "fixture")
            line = get_line(load(fixture.workspace), "alpha")

        self.assertEqual(line.storage_for("api"), "anchor-reference")

    def test_corpus_covers_required_fail_closed_wire_cases(self) -> None:
        corpus = self._corpus()
        classes = {case["class"] for case in corpus["cases"]}
        self.assertTrue(
            {
                "ambiguous_json",
                "future_protocol",
                "invalid_json",
                "invalid_unicode_scalar",
                "invalid_utf8",
                "resource_bound",
                "schema_error",
                "trailing_bytes",
                "unavailable_operation",
                "unknown_operation",
            }.issubset(classes)
        )
        invalid_utf8 = next(
            case for case in corpus["cases"] if case["id"] == "invalid-utf8"
        )
        self.assertEqual(bytes.fromhex(invalid_utf8["request_hex"]), b"\xff")
        surrogate = next(
            case for case in corpus["cases"] if case["id"] == "surrogate-request-id"
        )
        self.assertIn("\\ud800", surrogate["request_text"])

    def test_corpus_loader_rejects_hex_generator_and_field_drift(self) -> None:
        original = self._corpus()
        mutations = []
        malformed_hex = json.loads(json.dumps(original))
        next(case for case in malformed_hex["cases"] if case["id"] == "invalid-utf8")[
            "request_hex"
        ] = "fg"
        mutations.append(malformed_hex)
        oversized_generator = json.loads(json.dumps(original))
        next(
            case
            for case in oversized_generator["cases"]
            if case["id"] == "oversized-request"
        )["request_generator"]["count"] = 262146
        mutations.append(oversized_generator)
        extra_case_field = json.loads(json.dumps(original))
        extra_case_field["cases"][0]["unreviewed"] = True
        mutations.append(extra_case_field)
        for document in mutations:
            with self.subTest(case=document["cases"][0]["id"]):
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "protocol.json"
                    path.write_text(json.dumps(document))
                    with self.assertRaises(VerificationError):
                        load_protocol_corpus(path)

    def test_fail_closed_corpus_cases_match_transport_codes(self) -> None:
        context = TransportContext(
            platform="macos-15",
            allow_test_services=False,
            event_id_factory=lambda: "evt_corpus",
        )
        for case in self._corpus()["cases"]:
            if case["expected"]["ok"]:
                continue
            with self.subTest(case=case["id"]):
                response, exit_code = handle_request_bytes(self._payload(case), context)
                expected = case["expected"]
                self.assertEqual(exit_code, expected["exit_code"])
                self.assertEqual(response["ok"], expected["ok"])
                self.assertEqual(
                    response["error"]["code"],
                    ErrorCode(expected["error_code"]).value,
                )

    def test_audit_container_has_three_exact_targets_and_non_root_runtime(self) -> None:
        dockerfile = (FIXTURE_ROOT / "Dockerfile.audit").read_text()
        self.assertIn("FROM ubuntu:24.04 AS builder", dockerfile.splitlines()[:5])
        runtime_stage = dockerfile.split("FROM ubuntu:24.04 AS runtime", 1)[1]
        self.assertIn("/usr/sbin/groupadd --gid 10001 audit", runtime_stage)
        self.assertIn("/usr/sbin/useradd --uid 10001 --gid 10001", runtime_stage)
        for tool in (
            "python3.12",
            "python3.12-venv",
            "python3",
            "git",
            "strace",
        ):
            self.assertIn(tool, dockerfile)
        self.assertIn("ARG AUDIT_TARGET", dockerfile)
        self.assertIn("ARG AUDIT_COMMIT", dockerfile)
        for target in ("source)", "wheel)", "sdist)"):
            self.assertIn(target, dockerfile)
        self.assertIn("COPY --from=builder /audit/venv /audit/venv", dockerfile)
        self.assertIn("COPY --from=builder /audit/artifact /audit/artifact", dockerfile)
        self.assertIn("COPY wheelhouse /audit/wheelhouse", dockerfile)
        self.assertIn(
            "pip install --no-index --find-links=/audit/wheelhouse", dockerfile
        )
        self.assertIn("/audit/artifact/metadata.json", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("--mode candidate", dockerfile)
        self.assertIn("--mode public", dockerfile)
        self.assertIn("--network=none --read-only", dockerfile)
        self.assertIn("--cap-drop=ALL --cap-add=SYS_PTRACE", dockerfile)
        self.assertIn("--security-opt no-new-privileges=true", dockerfile)
        self.assertIn("CPU/memory/PID limits", dockerfile)
        self.assertIn("same-UID hostile trace/report", dockerfile)
        self.assertNotIn("--tmpfs /audit/run", dockerfile)
        self.assertIn("/audit/harness/verify_bridge_zero_effects.py", dockerfile)
        self.assertNotIn('CMD ["/audit/venv/bin/dyro-bridge"]', dockerfile)
        self.assertNotIn("COPY .", dockerfile)
        candidate = (FIXTURE_ROOT / "internal_candidate_runner.py").read_text()
        self.assertIn("allow_test_services=False", candidate)
        self.assertNotIn("allow_test_services=True", candidate)


class BridgeStraceAuditTests(unittest.TestCase):
    ENTRY = (
        "/audit/venv/bin/python",
        "-I",
        "-m",
        "dyro.bridge.transport",
    )

    def _helper_argv(
        self,
        *,
        helper_source: str = _HELPER_SCRIPT,
        descriptors: tuple[str, ...] = ("3", "4", "5", "6", "7"),
    ) -> tuple[str, ...]:
        return (
            "/usr/bin/python3.12",
            "-I",
            "-S",
            "-c",
            helper_source,
            *descriptors,
            "/usr/bin/git",
            *GIT_PREFIX,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        )

    def _evidence(
        self,
        *,
        entry_command: tuple[str, ...] | None = None,
        entry_line: str | None = None,
        extra_entry_lines: tuple[str, ...] = (),
        helper_argv: tuple[str, ...] | None = None,
        landlock_lines: tuple[str, ...] | None = None,
        include_entry_terminal: bool = True,
        include_binder_terminal: bool = True,
    ) -> tuple[dict[str, str], dict[str, object]]:
        command = entry_command or self.ENTRY
        entry = entry_line or _exec_line(command[0], command)
        main_lines = [
            entry,
            'openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3',
            *extra_entry_lines,
        ]
        if include_entry_terminal:
            main_lines.append("+++ exited with 0 +++")
        binder = helper_argv or self._helper_argv()
        security = landlock_lines or (
            "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
            "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=3}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=4}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=5}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=6}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=9}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=10}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=11}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=12}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_EXECUTE|LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_READ_DIR, parent_fd=13}, 0) = 0",
            "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, {allowed_access=LANDLOCK_ACCESS_FS_READ_FILE|LANDLOCK_ACCESS_FS_WRITE_FILE, parent_fd=14}, 0) = 0",
            "prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)  = 0",
            "landlock_restrict_self(8, 0)            = 0",
        )
        git_argv = tuple(binder[10:])
        binder_lines = [_exec_line(binder[0], binder), *security]
        if git_argv:
            binder_lines.append(_exec_line(git_argv[0], git_argv))
        if include_binder_terminal:
            binder_lines.append("+++ exited with 0 +++")
        traces = {
            "trace.entry": "\n".join(main_lines) + "\n",
            "trace.binder": "\n".join(binder_lines) + "\n",
        }
        manifest: dict[str, object] = {
            "schema_version": 1,
            "artifact_kind": "source",
            "artifact_sha256": "sha256:" + "a" * 64,
            "artifact_path": "/audit/artifact/input",
            "landlock_evidence_required": True,
            "reviewed_runtime_root": "/audit",
            "entry_executables": {command[0]: "sha256:" + "0" * 64},
            "groups": [
                {
                    "id": "objective.plan",
                    "trace_files": ["trace.entry", "trace.binder"],
                    "entry_trace": "trace.entry",
                    "entry_command": list(command),
                    "entry_role": self._entry_role(command),
                    "expected_exit": 0,
                    "required_binder_count": 1,
                }
            ],
        }
        return traces, manifest

    @staticmethod
    def _entry_role(command: tuple[str, ...]) -> str:
        if len(command) == 1:
            return "bridge"
        if tuple(command[1:]) == ("-I", "-m", "dyro.bridge.transport"):
            return "python_module"
        if Path(command[2]).name == "internal_candidate_runner.py":
            return "internal_candidate"
        return "verifier"

    @staticmethod
    def _sha256(path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _materialize(
        self,
        traces: dict[str, str],
        manifest: dict[str, object],
        root: Path,
    ) -> tuple[dict[str, str], dict[str, object]]:
        runtime = root.resolve() / "reviewed-runtime"
        replacements = {
            "/audit/venv/bin/python": b"venv-python-entry\n",
            "/audit/runner/bin/python": b"runner-python-entry\n",
            "/audit/venv/bin/dyro-bridge": b"bridge-entry\n",
            "/audit/harness/verify_bridge_zero_effects.py": b"verifier-script\n",
            "/audit/harness/internal_candidate_runner.py": b"internal-script\n",
            "/audit/artifact/input": b"reviewed-artifact\n",
        }
        runtime_text = str(runtime)
        materialized_traces = {
            name: content.replace("/audit", runtime_text)
            for name, content in traces.items()
        }
        materialized_manifest = json.loads(
            json.dumps(manifest).replace("/audit", runtime_text)
        )
        for placeholder, content in replacements.items():
            path = Path(placeholder.replace("/audit", runtime_text))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o755)
        materialized_manifest["reviewed_runtime_root"] = runtime_text
        materialized_manifest["artifact_sha256"] = self._sha256(
            Path(materialized_manifest["artifact_path"])
        )
        executable_paths = {
            group["entry_command"][0] for group in materialized_manifest["groups"]
        }
        materialized_manifest["entry_executables"] = {
            path: (
                self._sha256(Path(path))
                if Path(path).is_file()
                else "sha256:" + "0" * 64
            )
            for path in sorted(executable_paths)
        }
        for group in materialized_manifest["groups"]:
            if group["entry_role"] in {"internal_candidate", "verifier"}:
                group["entry_script_sha256"] = self._sha256(
                    Path(group["entry_command"][2])
                )
            else:
                group.pop("entry_script_sha256", None)
            group["trace_sha256"] = {
                name: "sha256:"
                + hashlib.sha256(materialized_traces[name].encode()).hexdigest()
                for name in group["trace_files"]
                if name in materialized_traces
            }
        return materialized_traces, materialized_manifest

    def _audit(
        self, traces: dict[str, str], manifest: dict[str, object]
    ) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            return audit_trace_files(paths, manifest=manifest, manifest_base=root)

    def test_accepts_complete_manifest_exact_entry_and_landlock_sequence(self) -> None:
        traces, manifest = self._evidence(
            extra_entry_lines=(
                'openat(AT_FDCWD, "/dev/null", O_WRONLY|O_CLOEXEC)   = 8',
            )
        )
        report = self._audit(traces, manifest)
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual(report["summary"]["binder"], 1)
        self.assertEqual(report["summary"]["landlock_success"], 1)
        self.assertTrue(report["blind_spots"])

    def test_accepts_non_planning_public_trace_without_binder(self) -> None:
        traces, manifest = self._evidence(
            entry_command=("/audit/venv/bin/dyro-bridge",)
        )
        traces.pop("trace.binder")
        group = manifest["groups"][0]
        group["trace_files"] = ["trace.entry"]
        group["required_binder_count"] = 0
        manifest["landlock_evidence_required"] = False
        report = self._audit(traces, manifest)
        self.assertTrue(report["ok"], report["violations"])
        self.assertEqual(report["summary"]["binder"], 0)
        self.assertEqual(report["summary"]["landlock_success"], 0)

    def test_rejects_trace_digest_drift(self) -> None:
        traces, manifest = self._evidence()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            paths[0].write_text(paths[0].read_text() + "getpid() = 1\n")
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

    def test_exact_console_and_verifier_entry_commands_are_manifest_bound(self) -> None:
        traces, manifest = self._evidence(
            entry_command=("/audit/venv/bin/dyro-bridge",)
        )
        self.assertTrue(self._audit(traces, manifest)["ok"])
        verifier = (
            "/audit/runner/bin/python",
            "-I",
            "/audit/harness/verify_bridge_zero_effects.py",
            "--artifact-kind",
            "wheel",
            "--bridge",
            "/audit/venv/bin/dyro-bridge",
        )
        traces, manifest = self._evidence(entry_command=verifier)
        self.assertTrue(self._audit(traces, manifest)["ok"])
        manifest["groups"][0]["entry_command"].append("--unexpected")
        rules = {item["rule"] for item in self._audit(traces, manifest)["violations"]}
        self.assertIn("ENTRY_COUNT_INVALID", rules)

        runner = (
            "/audit/runner/bin/python",
            "-I",
            "/audit/harness/internal_candidate_runner.py",
        )
        traces, manifest = self._evidence(entry_command=runner)
        self.assertTrue(self._audit(traces, manifest)["ok"])

    def test_rejects_arbitrary_python_and_accepts_exact_execveat_entry(self) -> None:
        traces, manifest = self._evidence()
        manifest["groups"][0]["entry_command"] = [
            "/tmp/python-evil",
            "-I",
            "/tmp/evil.py",
        ]
        report = self._audit(traces, manifest)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

        command = self.ENTRY
        rendered = ", ".join(_strace_string(value) for value in command)
        line = (
            f"execveat(AT_FDCWD, {_strace_string(command[0])}, "
            f"[{rendered}], 0x7ffd, 0) = 0"
        )
        traces, manifest = self._evidence(entry_line=line)
        self.assertTrue(self._audit(traces, manifest)["ok"])
        unbound = line.replace("AT_FDCWD", "9", 1).replace(
            ", 0) = 0", ", AT_EMPTY_PATH) = 0"
        )
        traces, manifest = self._evidence(entry_line=unbound)
        rules = {item["rule"] for item in self._audit(traces, manifest)["violations"]}
        self.assertIn("EXEC_NOT_ALLOWLISTED", rules)
        drift = line.replace("]", ', "--unsafe"]', 1)
        traces, manifest = self._evidence(entry_line=drift)
        rules = {item["rule"] for item in self._audit(traces, manifest)["violations"]}
        self.assertIn("EXEC_NOT_ALLOWLISTED", rules)

    def test_rejects_entry_executable_and_script_digest_drift(self) -> None:
        traces, manifest = self._evidence()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            executable = next(iter(manifest["entry_executables"]))
            manifest["entry_executables"][executable] = "sha256:" + "f" * 64
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

    def test_rejects_intermediate_symlink_escape_but_allows_bound_venv_leaf(
        self,
    ) -> None:
        traces, manifest = self._evidence()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            runtime = Path(manifest["reviewed_runtime_root"])
            outside = root / "outside"
            outside.mkdir()
            outside_python = outside / "python"
            outside_python.write_bytes(b"bound-system-python\n")
            outside_python.chmod(0o755)
            entry = Path(manifest["groups"][0]["entry_command"][0])
            entry.unlink()
            entry.symlink_to(outside_python)
            manifest["entry_executables"][str(entry)] = self._sha256(outside_python)
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
            self.assertTrue(report["ok"], report["violations"])

            outside_artifact = outside / "artifact"
            outside_artifact.write_bytes(b"outside-artifact\n")
            (runtime / "redirect").symlink_to(outside, target_is_directory=True)
            manifest["artifact_path"] = str(runtime / "redirect" / "artifact")
            manifest["artifact_sha256"] = self._sha256(outside_artifact)
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

        traces, manifest = self._evidence()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            manifest["artifact_sha256"] = "sha256:" + "d" * 64
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

        verifier = (
            "/audit/runner/bin/python",
            "-I",
            "/audit/harness/verify_bridge_zero_effects.py",
        )
        traces, manifest = self._evidence(entry_command=verifier)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            manifest["groups"][0]["entry_script_sha256"] = "sha256:" + "e" * 64
            report = audit_trace_files(paths, manifest=manifest, manifest_base=root)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

    def test_rejects_binder_digest_negative_fd_and_git_argv_drift(self) -> None:
        cases = (
            self._helper_argv(helper_source=_HELPER_SCRIPT + "\n# drift"),
            self._helper_argv(descriptors=("3", "4", "5", "6", "-1")),
            self._helper_argv(descriptors=("3", "4", "5", "6", "6")),
            (
                *self._helper_argv()[:10],
                "/usr/bin/git",
                *GIT_PREFIX,
                "status",
                "--short",
            ),
        )
        for helper in cases:
            with self.subTest(helper_tail=helper[-3:]):
                traces, manifest = self._evidence(helper_argv=helper)
                rules = {
                    item["rule"] for item in self._audit(traces, manifest)["violations"]
                }
                self.assertIn("EXEC_NOT_ALLOWLISTED", rules)
                self.assertIn("BINDER_COUNT_MISMATCH", rules)

    def test_rejects_incomplete_failed_or_reordered_landlock_sequence(self) -> None:
        cases = (
            (
                "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
                "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
                "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, 0x7ffd, 0) = 0",
                "landlock_restrict_self(8, 0) = 0",
            ),
            (
                "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
                "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
                "landlock_add_rule(9, LANDLOCK_RULE_PATH_BENEATH, 0x7ffd, 0) = 0",
                "prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) = 0",
                "landlock_restrict_self(8, 0) = 0",
            ),
            (
                "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
                "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
                "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, 0x7ffd, 0) = 0",
                "prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) = 0",
                "landlock_restrict_self(9, 0) = 0",
            ),
            (
                "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
                "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
                "prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) = 0",
                "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, 0x7ffd, 0) = 0",
                "landlock_restrict_self(8, 0) = 0",
            ),
            (
                "landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) = 6",
                "landlock_create_ruleset(0x7ffd, 8, 0) = 8",
                "landlock_add_rule(8, LANDLOCK_RULE_PATH_BENEATH, 0x7ffd, 0) = -1 EPERM",
                "prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) = 0",
                "landlock_restrict_self(8, 0) = 0",
            ),
        )
        for lines in cases:
            with self.subTest(lines=lines):
                traces, manifest = self._evidence(landlock_lines=lines)
                report = self._audit(traces, manifest)
                self.assertFalse(report["ok"])
                self.assertTrue(
                    any(
                        item["rule"].startswith("LANDLOCK_")
                        for item in report["violations"]
                    )
                )

    def test_rejects_landlock_parent_fd_drift_from_binder_argv(self) -> None:
        traces, manifest = self._evidence()
        traces["trace.binder"] = traces["trace.binder"].replace(
            "parent_fd=3}", "parent_fd=30}", 1
        )
        report = self._audit(traces, manifest)
        self.assertFalse(report["ok"])
        self.assertIn(
            "LANDLOCK_RESULT_INVALID",
            {item["rule"] for item in report["violations"]},
        )

    def test_rejects_network_tmpfile_mutations_and_unknown_exec(self) -> None:
        traces, manifest = self._evidence(
            extra_entry_lines=(
                "[pid 42] 12:34:56.123456 socket(AF_INET, SOCK_STREAM, IPPROTO_TCP) = 3",
                'openat(AT_FDCWD, "/tmp", O_TMPFILE|O_RDWR, 0600) = 4',
                'setxattr("/tmp/a", "user.x", "v", 1, 0) = 0',
                "fallocate(4, 0, 0, 4096) = 0",
                _exec_line("/bin/sh", ("/bin/sh", "-c", "true")),
            )
        )
        report = self._audit(traces, manifest)
        rules = {item["rule"] for item in report["violations"]}
        self.assertTrue(
            {
                "EXEC_NOT_ALLOWLISTED",
                "FILESYSTEM_MUTATION",
                "NETWORK_SYSCALL",
                "WRITE_OPEN",
            }.issubset(rules)
        )
        self.assertEqual(report["summary"]["network"], 1)
        self.assertEqual(report["summary"]["write_open"], 1)
        self.assertEqual(report["summary"]["mutation"], 2)

    def test_rejects_unfinished_resumed_and_unparseable_security_lines(self) -> None:
        traces, manifest = self._evidence(
            extra_entry_lines=(
                'openat(AT_FDCWD, "/tmp/a", O_RDONLY <unfinished ...>',
                "<... openat resumed>) = 3",
                'execve("/bin/sh", ["/bin/sh"] = 0',
            )
        )
        rules = {item["rule"] for item in self._audit(traces, manifest)["violations"]}
        self.assertIn("TRACE_INCOMPLETE", rules)
        self.assertIn("SECURITY_LINE_UNPARSEABLE", rules)

    def test_rejects_missing_terminals_single_exec_and_manifest_file_skew(self) -> None:
        traces, manifest = self._evidence(include_entry_terminal=False)
        rules = {item["rule"] for item in self._audit(traces, manifest)["violations"]}
        self.assertIn("TRACE_TERMINAL_INVALID", rules)
        self.assertIn("ENTRY_EXIT_MISMATCH", rules)

        traces, manifest = self._evidence()
        traces.pop("trace.binder")
        manifest["groups"][0]["trace_files"] = ["trace.entry"]
        manifest["groups"][0]["required_binder_count"] = 0
        report = self._audit(traces, manifest)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

        traces, manifest = self._evidence()
        manifest["groups"][0]["trace_files"].append("trace.missing")
        report = self._audit(traces, manifest)
        self.assertEqual(report["violations"][0]["rule"], "MANIFEST_INVALID")

    def test_cli_emits_one_structured_json_report_and_status(self) -> None:
        traces, manifest = self._evidence(
            extra_entry_lines=("socket(AF_INET, SOCK_STREAM, IPPROTO_TCP) = -1 EPERM",)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            traces, manifest = self._materialize(traces, manifest, root)
            paths: list[Path] = []
            for name, content in traces.items():
                path = root / name
                path.write_text(content)
                paths.append(path)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            output = StringIO()
            with redirect_stdout(output):
                status = main(
                    [
                        *(str(path) for path in paths),
                        "--manifest",
                        str(manifest_path),
                    ]
                )
        self.assertEqual(status, 1)
        rendered = output.getvalue()
        self.assertTrue(rendered.endswith("\n"))
        self.assertEqual(rendered.count("\n"), 1)
        report = json.loads(rendered)
        self.assertFalse(report["ok"])
        self.assertEqual(report["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
