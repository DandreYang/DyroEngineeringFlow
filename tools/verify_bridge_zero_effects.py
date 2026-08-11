#!/usr/bin/env python3
"""Artifact-aware black-box zero-effect gate for Agent Bridge Phase 0."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Iterable, Mapping, Sequence
import zipfile

try:
    from tools.audit_bridge_strace import audit_trace_files
except ImportError:  # Direct ``python tools/verify_...py`` execution.
    auditor_path = Path(__file__).with_name("audit_bridge_strace.py")
    auditor_spec = importlib.util.spec_from_file_location(
        "dyro_s5_strict_auditor", auditor_path
    )
    if auditor_spec is None or auditor_spec.loader is None:
        raise
    auditor_module = importlib.util.module_from_spec(auditor_spec)
    sys.modules[auditor_spec.name] = auditor_module
    auditor_spec.loader.exec_module(auditor_module)
    audit_trace_files = auditor_module.audit_trace_files


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INTERNAL_CANDIDATE = (
    REPOSITORY_ROOT / "tests/fixtures/bridge/internal_candidate_runner.py"
)
PROTOCOL_CORPUS = REPOSITORY_ROOT / "tests/fixtures/bridge/protocol-v1.json"
MANDATORY_REQUESTS: tuple[tuple[str, dict[str, object]], ...] = (
    ("bridge.capabilities.compact", {}),
    ("bridge.hello", {}),
    ("bridge.operation.schema", {"operation": "bridge.hello"}),
    ("objective.plan", {"objective_id": "OBJ-1"}),
    ("workspace.list", {}),
    ("workspace.observe", {}),
    ("workspace.resolve", {}),
)
MANDATORY_OPERATION_IDS = tuple(
    sorted(operation for operation, _ in MANDATORY_REQUESTS)
)
DYRO_VERSION = "0.6.0"
BRIDGE_VERSION = "1.0"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
CAPABILITIES_DIGEST = (
    "sha256:426aaee45de4da518fcad5c89ab85ce129662e6af2faff37c705b717a4311e8a"
)
SCHEMA_DIGESTS = {
    "bridge.capabilities.compact": "sha256:03d62638856888149eddbfb7ce8d18232680cada55fbc9684ba4c1f8f6422e78",
    "bridge.hello": "sha256:962c2d821892a3feb772ec9cd46a907b5eedaab0fc85bc03fc651f8262341225",
    "bridge.operation.schema": "sha256:203ae1baeaeafddace4f6ab7580a5c90e40dba340ca8ef367c085d020be0560e",
    "objective.plan": "sha256:4e9924616a136dc52af75fd255848d1c2b8a4c3cc2ee29cd4826f4ca100901d9",
    "workspace.list": "sha256:ecb77721e7123ba172025b58cdc3ab154be49737340bbb73eb97562fa5f666c6",
    "workspace.observe": "sha256:2701fcdc0ed948c3567f7b0cf44dea7c65fae695c750e58345cf60be06d3662d",
    "workspace.resolve": "sha256:e86c75ffef9f01b735d5cec36518264f1a2bc124058113b13a262f86c45edeb9",
}
PLANNER_REVISIONS = {"objective.plan": "objective-plan/1"}
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_GENERATED_REQUEST = 262145
_MAX_CAPTURE_BYTES = 1024 * 1024
SNAPSHOT_ROOT_NAMES = ("home", "xdg", "dyro-home", "workspace", "tmp")
_SHA256_HEX = frozenset("0123456789abcdef")
_SNAPSHOT_VALUE_FIELDS = frozenset(
    {
        "mode",
        "uid",
        "gid",
        "inode",
        "nlink",
        "size",
        "mtime_ns",
        "ctime_ns",
        "sha256",
    }
)


class VerificationError(RuntimeError):
    """A stable verification failure safe for the structured report."""


@dataclass(frozen=True)
class Fixture:
    root: Path
    workspace: Path
    environment: dict[str, str]
    pending: Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


def _shell(*arguments: str, cwd: Path) -> bytes:
    return subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def prepare_fixture(root: Path) -> Fixture:
    """Create real Git/workspace/registry/Objective state before auditing."""
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise VerificationError("fixture root must be empty")
    root.mkdir(parents=True, exist_ok=True)
    paths = {name: root / name for name in SNAPSHOT_ROOT_NAMES}
    for path in paths.values():
        path.mkdir()
    workspace = paths["workspace"]
    workspace.joinpath("dyro.toml").write_text(
        """schema_version = 1

[workspace]
name = "bridge-fixture"

[layout]
anchors = "repositories"
lines = "versions"
hotfixes = "hotfixes"
tasks = "worktrees"

[policy]
default_base = "main"
task_branch_prefix = "task/"
allow_push = false
require_clean_merge = true

[adapters.noop]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]

[repositories.api]
path = "repositories/api"
mount = "services/api"
verify = [["git", "diff", "--check"]]
""",
        encoding="utf-8",
    )
    anchor = workspace / "repositories/api"
    anchor.mkdir(parents=True)
    _shell("git", "init", "-b", "main", cwd=anchor)
    _shell("git", "config", "user.name", "Bridge Fixture", cwd=anchor)
    _shell("git", "config", "user.email", "bridge@example.invalid", cwd=anchor)
    anchor.joinpath("README.md").write_text("bridge fixture\n", encoding="utf-8")
    _shell("git", "add", "README.md", cwd=anchor)
    _shell("git", "commit", "-m", "chore: initialize bridge fixture", cwd=anchor)
    anchor_head = _shell("git", "rev-parse", "HEAD", cwd=anchor).decode().strip()

    dyro_root = workspace / ".dyro"
    (dyro_root / "lines").mkdir(parents=True)
    (dyro_root / "lines/alpha.toml").write_text(
        'schema_version = 2\nid = "alpha"\nkind = "line"\nbranch = "feat/alpha"\nbase = "main"\nrepositories = ["api"]\n\n[storage_modes]\napi = "anchor-reference"\n',
        encoding="utf-8",
    )
    task_body = """schema_version = 1
id = "{task_id}"
title = "{task_id}"
line = "alpha"
risk = "write"
timeout_minutes = 60
review_timeout_minutes = 45
depends_on = []
blocked_on = []
conflict_group = ""

[executor]
agent = "codex"

[reviewer]
agent = "codex"

[[repositories]]
id = "api"

[[gates]]
name = "diff-check"
argv = ["git", "diff", "--check"]
cwd = "services/api"
timeout_seconds = 120

[merge]
auto = false
push = false
"""
    for task_id in ("TASK-A", "TASK-B"):
        task_dir = dyro_root / "tasks" / task_id
        task_dir.mkdir(parents=True)
        task_dir.joinpath("task.toml").write_text(
            task_body.format(task_id=task_id), encoding="utf-8"
        )
        task_dir.joinpath("status").write_text(
            "done\n" if task_id == "TASK-A" else "backlog\n", encoding="utf-8"
        )
        if task_id == "TASK-A":
            task_dir.joinpath("task-heads.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "task_id": task_id,
                        "line": "alpha",
                        "branch": f"task/{task_id}",
                        "repositories": {"api": anchor_head},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

    # These records are an independently frozen wire fixture.  Their hashes are
    # not calculated by, or imported from, the candidate package.
    objective_records = {
        "OBJ-1": {
            "task": "TASK-A",
            "contract": "9bbdf9fc918d1d55a5ba96768cb4e1e07b3910befd160b5430fa36625f0cec39",
            "scope": "f4ea2db36b208aa22043717e6b6c5aee1c8578c97c58cc148c913762aeed711d",
            "task_contract": "c8c15533cadbf8f05369e62bf80de24879bb4998129e57c8da069731ff1c9bbd",
            "event": "58adde720a93392f56f18f47049d6a79432e9610c045b35f2957ee7dfd6ec063",
            "state": "43c06e9d225d85462c47f59787f97b0a2a362b1f810aac1aa3569bd84a21c0d8",
        },
        "pending-sibling": {
            "task": "TASK-B",
            "contract": "3815754e018723a281f26e24472a11e9fca5f4315b7743c01bfbada1d71000d8",
            "scope": "25bd0e1956b3edad4c0162584e911a922b66bda96efa228d1eb46e2deba3992e",
            "task_contract": "7fc8d82278a2463cf7ee667181580bc0cfb8ec0ba032323e040a184b727f07b2",
            "event": "67f576db2bd1794ac9d284627bf42e60180c4c2b46c1879d09c2945aea38b5b8",
            "state": "11f19cfa2bbf31b19b4f8fc6b535f52872bfdd4a5de69c631c942233bca2db74",
        },
    }
    (dyro_root / "objectives.lock").touch()
    (dyro_root / "dispatch.lock").touch()
    for objective_id, record in objective_records.items():
        directory = dyro_root / "objectives" / objective_id
        directory.mkdir(parents=True)
        state = {
            "contract_sha256": record["contract"],
            "id": objective_id,
            "operator_state": "active",
            "revision": 1,
            "schema_version": 1,
            "scope": [record["task"]],
            "scope_sha256": record["scope"],
            "task_contract_sha256": [[record["task"], record["task_contract"]]],
        }
        event = {
            "event": "created",
            "previous_sha256": "",
            "record": state,
            "schema_version": 1,
            "seq": 1,
            "sha256": record["event"],
        }
        directory.joinpath("state.json").write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        directory.joinpath("checkpoint.json").write_text(
            json.dumps(
                {
                    "event_seq": 1,
                    "event_sha256": record["event"],
                    "schema_version": 1,
                    "state_sha256": record["state"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        directory.joinpath("events.jsonl").write_text(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        directory.joinpath("contract-1.toml").write_text(
            f'''schema_version = 1
id = "{objective_id}"
title = "{objective_id}"
line = "alpha"
targets = ["{record["task"]}"]
completion = "all_targets_integrated"

[continuation]
requested_mode = "observe"
operations = ["execute"]

[budget]
max_actions = 20
max_attempts_per_task = 2
max_failures = 3
max_no_progress_cycles = 2
max_parallel = 1
''',
            encoding="utf-8",
        )
    pending = dyro_root / "objectives/pending-sibling/pending.json"
    pending.write_text(
        '{"action_cancellation":null,"contract_revision":1,"contract_sha256":"","event":{"event":"objective_updated","previous_sha256":"67f576db2bd1794ac9d284627bf42e60180c4c2b46c1879d09c2945aea38b5b8","record":{"contract_sha256":"3815754e018723a281f26e24472a11e9fca5f4315b7743c01bfbada1d71000d8","id":"pending-sibling","operator_state":"active","revision":1,"schema_version":1,"scope":["TASK-B"],"scope_sha256":"25bd0e1956b3edad4c0162584e911a922b66bda96efa228d1eb46e2deba3992e","task_contract_sha256":[["TASK-B","7fc8d82278a2463cf7ee667181580bc0cfb8ec0ba032323e040a184b727f07b2"]]},"schema_version":1,"seq":2,"sha256":"a765c4a580ca7ab74e6b1eb8d4ec7ad27ffd0dac43c37881b000fa7b746b1f86"},"schema_version":1}\n',
        encoding="utf-8",
    )
    registry = {
        "schema_version": 1,
        "default": "sample",
        "workspaces": [
            {
                "name": "sample",
                "root": str(workspace),
                "last_kind": "",
                "last_target": "",
                "last_agent": "",
            }
        ],
    }
    paths["dyro-home"].joinpath("workspaces.json").write_text(
        json.dumps(registry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    environment = {
        "HOME": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["xdg"]),
        "DYRO_HOME": str(paths["dyro-home"]),
        "TMPDIR": str(paths["tmp"]),
        "DYRO_BRIDGE_FIXTURE_ROOT": str(workspace),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    return Fixture(root, workspace, environment, pending)


def _entry(path: Path, root: Path) -> dict[str, object]:
    metadata = path.lstat()
    relative = "." if path == root else path.relative_to(root).as_posix()
    result: dict[str, object] = {
        "path": relative,
        "mode": stat.S_IMODE(metadata.st_mode),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "inode": metadata.st_ino,
        "nlink": metadata.st_nlink,
        "mtime_ns": metadata.st_mtime_ns,
        "ctime_ns": metadata.st_ctime_ns,
    }
    if stat.S_ISREG(metadata.st_mode):
        result.update(
            kind="file",
            size=metadata.st_size,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    elif stat.S_ISDIR(metadata.st_mode):
        result["kind"] = "directory"
    elif stat.S_ISLNK(metadata.st_mode):
        result.update(kind="symlink", target=os.readlink(path))
    else:
        result.update(kind="other", size=metadata.st_size)
    return result


def snapshot_roots(
    fixture: Fixture, *, tolerate_errors: bool = False
) -> dict[str, tuple[dict[str, object], ...]]:
    snapshots: dict[str, tuple[dict[str, object], ...]] = {}
    for name in SNAPSHOT_ROOT_NAMES:
        root = fixture.root / name
        try:
            paths = [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]
            snapshots[name] = tuple(_entry(path, root) for path in paths)
        except OSError as exc:
            if not tolerate_errors:
                raise VerificationError(
                    f"snapshot root is unavailable before execution: {name}"
                ) from exc
            snapshots[name] = (
                {
                    "path": ".",
                    "kind": "unavailable",
                    "error_code": type(exc).__name__,
                },
            )
    return snapshots


def compare_snapshots(
    before: Mapping[str, tuple[dict[str, object], ...]],
    after: Mapping[str, tuple[dict[str, object], ...]],
) -> tuple[dict[str, object], ...]:
    """Return bounded, content-free differences keyed by root and relative path."""
    differences: list[dict[str, object]] = []
    for root_name in SNAPSHOT_ROOT_NAMES:
        before_entries = {
            str(entry["path"]): entry for entry in before.get(root_name, ())
        }
        after_entries = {
            str(entry["path"]): entry for entry in after.get(root_name, ())
        }
        for relative_path in sorted(before_entries.keys() | after_entries.keys()):
            old = before_entries.get(relative_path)
            new = after_entries.get(relative_path)
            if old == new:
                continue
            if old is None:
                change = "added"
            elif new is None:
                change = "removed"
            else:
                change = "modified"
            changed_fields = sorted(
                field
                for field in set(old or {}) | set(new or {})
                if field != "path" and (old or {}).get(field) != (new or {}).get(field)
            )
            values = {
                field: {
                    "before": (old or {}).get(field),
                    "after": (new or {}).get(field),
                }
                for field in changed_fields
                if field in _SNAPSHOT_VALUE_FIELDS
            }
            differences.append(
                {
                    "root": root_name,
                    "path": relative_path,
                    "change": change,
                    "changed_fields": changed_fields,
                    "values": values,
                }
            )
    return tuple(differences)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in _SHA256_HEX for character in value)


def load_protocol_corpus(path: Path) -> tuple[dict[str, object], ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise VerificationError("protocol corpus is unreadable") from exc
    if (
        not isinstance(document, dict)
        or set(document)
        != {
            "version",
            "artifact_matrix",
            "execute_identical_cases_per_artifact",
            "wire_encoding",
            "case_payload",
            "cases",
        }
        or document.get("version") != 1
        or document.get("artifact_matrix") != ["source", "wheel", "sdist"]
        or document.get("execute_identical_cases_per_artifact") is not True
        or document.get("wire_encoding") != "utf-8"
    ):
        raise VerificationError("protocol corpus version is invalid")
    cases = document.get("cases")
    if not isinstance(cases, list) or not cases:
        raise VerificationError("protocol corpus is empty")
    identifiers: set[str] = set()
    valid_operations: list[str] = []
    result: list[dict[str, object]] = []
    for item in cases:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"].isascii()
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", item["id"]) is None
            or not isinstance(item.get("class"), str)
        ):
            raise VerificationError("protocol corpus case is invalid")
        identifier = item["id"]
        if identifier in identifiers or not isinstance(item.get("expected"), dict):
            raise VerificationError("protocol corpus case identity is invalid")
        identifiers.add(identifier)
        if item.get("class") == "valid_mandatory":
            request = item.get("request_json")
            if not isinstance(request, dict) or not isinstance(
                request.get("operation"), str
            ):
                raise VerificationError("mandatory corpus request is invalid")
            valid_operations.append(request["operation"])
        payload_fields = {
            field
            for field in (
                "request_json",
                "request_text",
                "request_hex",
                "request_generator",
            )
            if field in item
        }
        if len(payload_fields) != 1 or set(item) - {
            "id",
            "class",
            "fixture",
            "expected",
            *payload_fields,
        }:
            raise VerificationError("protocol corpus case fields are invalid")
        expected = item["expected"]
        if (
            set(expected)
            not in ({"exit_code", "ok"}, {"exit_code", "ok", "error_code"})
            or type(expected.get("exit_code")) is not int
            or not 0 <= expected["exit_code"] <= 255
            or not isinstance(expected.get("ok"), bool)
            or (
                expected["ok"] is True
                and (expected["exit_code"] != 0 or "error_code" in expected)
            )
            or (
                expected["ok"] is False
                and (
                    expected["exit_code"] == 0
                    or re.fullmatch(
                        r"[A-Z][A-Z0-9_]{0,63}", str(expected.get("error_code"))
                    )
                    is None
                )
            )
        ):
            raise VerificationError("protocol corpus expectation is invalid")
        # Materialize now so malformed hex, Unicode, and oversized generators
        # fail before any candidate process starts.
        _case_payload(item)
        result.append(dict(item))
    if tuple(sorted(valid_operations)) != MANDATORY_OPERATION_IDS:
        raise VerificationError("mandatory protocol corpus is incomplete")
    return tuple(result)


def _case_payload(case: Mapping[str, object]) -> bytes:
    if "request_json" in case:
        try:
            payload = json.dumps(
                case["request_json"], separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        except (TypeError, ValueError, RecursionError) as exc:
            raise VerificationError("protocol corpus JSON payload is invalid") from exc
        if len(payload) > _MAX_GENERATED_REQUEST:
            raise VerificationError("protocol corpus JSON payload is too large")
        return payload
    if "request_text" in case:
        value = case["request_text"]
        if not isinstance(value, str):
            raise VerificationError("protocol corpus text payload is invalid")
        try:
            payload = value.encode("utf-8")
        except UnicodeError as exc:
            raise VerificationError("protocol corpus text payload is invalid") from exc
        if len(payload) > _MAX_GENERATED_REQUEST:
            raise VerificationError("protocol corpus text payload is too large")
        return payload
    if "request_hex" in case:
        value = case["request_hex"]
        if (
            not isinstance(value, str)
            or len(value) % 2
            or len(value) > _MAX_GENERATED_REQUEST * 2
            or re.fullmatch(r"[0-9a-f]*", value) is None
        ):
            raise VerificationError("protocol corpus hex payload is invalid")
        return bytes.fromhex(value)
    generator = case.get("request_generator")
    if not isinstance(generator, dict):
        raise VerificationError("protocol corpus payload is invalid")
    if generator.get("kind") == "nested_array":
        if set(generator) != {"kind", "depth"}:
            raise VerificationError("protocol corpus generator fields are invalid")
        depth = generator.get("depth")
        if type(depth) is not int or not 1 <= depth <= 1024:
            raise VerificationError("protocol corpus generator is invalid")
        return ("[" * depth + "]" * depth).encode()
    if generator.get("kind") == "repeat_byte":
        if set(generator) != {"kind", "byte_hex", "count"}:
            raise VerificationError("protocol corpus generator fields are invalid")
        count = generator.get("count")
        byte_hex = generator.get("byte_hex")
        if (
            type(count) is not int
            or not 1 <= count <= _MAX_GENERATED_REQUEST
            or not isinstance(byte_hex, str)
            or re.fullmatch(r"[0-9a-f]{2}", byte_hex) is None
        ):
            raise VerificationError("protocol corpus generator is invalid")
        return bytes.fromhex(byte_hex) * count
    raise VerificationError("protocol corpus generator is unknown")


def _single_json(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8", errors="strict")
        value, offset = json.JSONDecoder().raw_decode(text)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise VerificationError("stdout is not one JSON document") from exc
    if text[offset:].strip(" \t\r\n") or not isinstance(value, dict):
        raise VerificationError("stdout is not one JSON object")
    return value


def _trace_prefix(trace_path: Path | None) -> list[str]:
    if trace_path is None:
        return []
    executable = shutil.which("strace", path="/usr/bin:/bin")
    if executable is None:
        raise VerificationError("strace is unavailable")
    return [
        executable,
        "-ff",
        "--kill-on-exit",
        "-s",
        "65535",
        "-o",
        str(trace_path),
        "-e",
        (
            "trace=%file,%network,%process,prctl,landlock_create_ruleset,"
            "landlock_add_rule,landlock_restrict_self"
        ),
        "--",
    ]


def _trace_group(
    trace_prefix: Path,
    *,
    base: Path,
    identifier: str,
    command: Sequence[str],
    expected_exit: int,
    required_binder_count: int,
    entry_role: str,
) -> dict[str, object]:
    files = tuple(sorted(trace_prefix.parent.glob(trace_prefix.name + "*")))
    if not files:
        raise VerificationError("strace produced no trace files")
    needle = f'execve("{command[0]}"'
    entry_files = tuple(
        path
        for path in files
        if needle in path.read_text(encoding="utf-8", errors="strict")
    )
    if len(entry_files) != 1:
        raise VerificationError("strace entry trace is ambiguous")
    group: dict[str, object] = {
        "id": identifier,
        "trace_files": [str(path.relative_to(base)) for path in files],
        "entry_trace": str(entry_files[0].relative_to(base)),
        "entry_command": list(command),
        "expected_exit": expected_exit,
        "required_binder_count": required_binder_count,
        "entry_role": entry_role,
        "trace_sha256": {
            str(path.relative_to(base)): "sha256:" + _sha256(path) for path in files
        },
    }
    if entry_role in {"internal_candidate", "verifier"}:
        group["entry_script_sha256"] = "sha256:" + _sha256(Path(command[2]))
    else:
        group["entry_script_sha256"] = None
    return group


def _run_case(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    payload: bytes,
    timeout_seconds: float,
    capture_dir: Path | None = None,
) -> ProcessResult:
    with (
        tempfile.TemporaryFile(dir=capture_dir) as stdin,
        tempfile.TemporaryFile(dir=capture_dir) as stdout,
        tempfile.TemporaryFile(dir=capture_dir) as stderr,
    ):
        stdin.write(payload)
        stdin.seek(0)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(environment),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if (
                    time.monotonic() >= deadline
                    or os.fstat(stdout.fileno()).st_size > _MAX_CAPTURE_BYTES
                    or os.fstat(stderr.fileno()).st_size > _MAX_CAPTURE_BYTES
                ):
                    timed_out = True
                    break
                time.sleep(0.01)
        except OSError:
            # Monitoring failures are fail-closed and receive the same complete
            # process-group cleanup as a timeout.
            timed_out = True
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
        stdout.seek(0)
        stderr.seek(0)
        stdout_bytes = stdout.read(_MAX_CAPTURE_BYTES + 1)
        stderr_bytes = stderr.read(_MAX_CAPTURE_BYTES + 1)
    if len(stdout_bytes) > _MAX_CAPTURE_BYTES or len(stderr_bytes) > _MAX_CAPTURE_BYTES:
        timed_out = True
    return ProcessResult(
        process.returncode,
        stdout_bytes[:_MAX_CAPTURE_BYTES],
        stderr_bytes[:_MAX_CAPTURE_BYTES],
        timed_out,
    )


def _meta_errors(
    response: Mapping[str, object], request: Mapping[str, object]
) -> list[str]:
    operation = request["operation"]
    meta = response.get("meta")
    if not isinstance(meta, dict):
        return ["response metadata is missing"]
    expected = {
        "server_protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        "requested_protocol": request["protocol"],
        "dyro_version": DYRO_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "operation": operation,
        "operation_schema_version": 1,
        "planner_revision": PLANNER_REVISIONS.get(str(operation)),
        "request_id": request.get("request_id"),
        "capabilities_digest": CAPABILITIES_DIGEST,
        "truncated": False,
    }
    errors = [
        f"response metadata mismatch: {field}"
        for field, value in expected.items()
        if meta.get(field) != value
    ]
    event_id = meta.get("event_id")
    if not isinstance(event_id, str) or not event_id.startswith("evt_"):
        errors.append("response metadata event ID is invalid")
    if not isinstance(meta.get("partial"), bool):
        errors.append("response metadata partial marker is invalid")
    return errors


def _success_errors(
    response: Mapping[str, object], request: Mapping[str, object]
) -> tuple[list[str], dict[str, str]]:
    operation_id = str(request["operation"])
    errors = _meta_errors(response, request)
    if response.get("ok") is not True or "error" in response:
        errors.append("success envelope is invalid")
    if not isinstance(response.get("warnings"), list):
        errors.append("success warnings are invalid")
    data = response.get("data")
    if not isinstance(data, dict):
        return [*errors, "success data is not an object"], {}
    meta = response.get("meta")
    expected_partial = (
        operation_id == "workspace.observe" and data.get("completeness") == "partial"
    )
    if not isinstance(meta, dict) or meta.get("partial") is not expected_partial:
        errors.append("response metadata partial marker mismatches data")
    observed: dict[str, str] = {f"schema:{operation_id}": SCHEMA_DIGESTS[operation_id]}
    if operation_id == "bridge.hello":
        if data != {
            "dyro_version": DYRO_VERSION,
            "bridge_version": BRIDGE_VERSION,
            "server_protocol": {"major": PROTOCOL_MAJOR, "minor": PROTOCOL_MINOR},
        }:
            errors.append("hello invariants failed")
    elif operation_id == "bridge.capabilities.compact":
        operations = data.get("operations")
        records = (
            [item for item in operations if isinstance(item, dict)]
            if isinstance(operations, list)
            else []
        )
        operation_ids = [item.get("operation") for item in records]
        if (
            data.get("capabilities_digest") != CAPABILITIES_DIGEST
            or operation_ids != sorted(operation_ids)
            or set(MANDATORY_OPERATION_IDS) - set(operation_ids)
            or len(operation_ids) != 18
            or len(records) != len(operation_ids)
            or any(
                set(item)
                != {
                    "available",
                    "kind",
                    "maximum_risk",
                    "operation",
                    "operation_schema_version",
                    "planner_revision",
                }
                or item.get("available")
                is not (item.get("operation") in MANDATORY_OPERATION_IDS)
                or item.get("operation_schema_version") != 1
                for item in records
            )
        ):
            errors.append("capabilities digest or catalog invariant failed")
        observed["capabilities"] = str(data.get("capabilities_digest"))
    elif operation_id == "bridge.operation.schema":
        target = str(request["input"]["operation"])
        input_schema = data.get("input_schema")
        output_schema = data.get("output_schema")
        if (
            data.get("operation") != target
            or data.get("operation_schema_version") != 1
            or data.get("schema_digest") != SCHEMA_DIGESTS[target]
            or not isinstance(input_schema, dict)
            or not isinstance(output_schema, dict)
            or input_schema.get("$schema")
            != "https://json-schema.org/draft/2020-12/schema"
            or output_schema.get("$schema")
            != "https://json-schema.org/draft/2020-12/schema"
        ):
            errors.append("operation schema bundle or digest invariant failed")
        observed[f"discovered-schema:{target}"] = str(data.get("schema_digest"))
    elif operation_id == "workspace.resolve":
        if (
            data.get("health") != "available"
            or data.get("resolution_source") != "local"
            or not _workspace_identity_is_valid(data.get("workspace"))
        ):
            errors.append("workspace resolve fixture semantics failed")
    elif operation_id == "workspace.list":
        workspaces = data.get("workspaces")
        item = (
            workspaces[0]
            if isinstance(workspaces, list)
            and len(workspaces) == 1
            and isinstance(workspaces[0], dict)
            else {}
        )
        if (
            item.get("default") is not True
            or item.get("registry_alias") != "sample"
            or item.get("health") != "available"
            or item.get("failure_code") is not None
            or not _workspace_identity_is_valid(item.get("workspace"))
        ):
            errors.append("workspace list fixture semantics failed")
    elif operation_id == "workspace.observe":
        if (
            data.get("completeness") != "partial"
            or data.get("integration_inspection") != "not_inspected"
            or data.get("lines")
            != [
                {
                    "id": "alpha",
                    "integration_inspection": "not_inspected",
                    "status": None,
                }
            ]
            or data.get("objectives")
            != [
                {
                    "id": "OBJ-1",
                    "integration_inspection": "not_inspected",
                    "status": "active",
                }
            ]
            or data.get("tasks")
            != [
                {
                    "id": "TASK-A",
                    "integration_inspection": "not_inspected",
                    "status": "done",
                },
                {
                    "id": "TASK-B",
                    "integration_inspection": "not_inspected",
                    "status": "backlog",
                },
            ]
            or data.get("failures")
            != [{"code": "RECORD_INVALID", "component": "objective:pending-sibling"}]
            or not _workspace_identity_is_valid(data.get("workspace"))
            or re.fullmatch(r"capture-[0-9a-f]{24}", str(data.get("capture_id")))
            is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(data.get("workspace_revision")))
            is None
            or re.fullmatch(r"\d{4}-\d\d-\d\dT.*Z", str(data.get("observed_at")))
            is None
        ):
            errors.append("workspace observe fixture semantics failed")
    elif operation_id == "objective.plan":
        if data.get("operation") != operation_id:
            errors.append("plan operation invariant failed")
        if data.get("planner_revision") != PLANNER_REVISIONS[operation_id]:
            errors.append("plan revision invariant failed")
        if data.get("plan_sha256") != _plan_digest(data):
            errors.append("plan digest invariant failed")
        if (
            data.get("effects") != []
            or data.get("maximum_risk") != "PLAN"
            or data.get("effective_risk") != "PLAN"
            or data.get("executable") is not False
            or data.get("authorization") != "none"
            or data.get("protocol_major") != 1
            or data.get("normalized_input") != {"objective_id": "OBJ-1"}
            or set(data.get("projection", {}))
            != {"completion", "selected_actions", "blocked", "attention"}
            or not _workspace_plan_identity_is_valid(data.get("workspace"))
        ):
            errors.append("plan zero-effect invariant failed")
        observed[f"planner:{operation_id}"] = PLANNER_REVISIONS[operation_id]
        observed[f"plan-contract:{operation_id}"] = _canonical_digest(
            {
                "operation": operation_id,
                "planner_revision": PLANNER_REVISIONS[operation_id],
                "normalized_input": {"objective_id": "OBJ-1"},
                "effects": [],
                "maximum_risk": "PLAN",
            }
        )
    return errors, observed


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _plan_digest(data: Mapping[str, object]) -> str:
    return _canonical_digest(
        {key: value for key, value in data.items() if key != "plan_sha256"}
    )


def _workspace_identity_is_valid(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "name", "profile_schema_version"}
        and value.get("name") == "bridge-fixture"
        and value.get("profile_schema_version") == 1
        and re.fullmatch(r"workspace:[0-9a-f]{64}", str(value.get("id"))) is not None
    )


def _workspace_plan_identity_is_valid(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "config_sha256"}
        and re.fullmatch(r"workspace:[0-9a-f]{64}", str(value.get("id"))) is not None
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(value.get("config_sha256")))
        is not None
    )


def _expected_result(
    case: Mapping[str, object], *, mode: str
) -> tuple[int, bool, str | None]:
    expected = case["expected"]
    if not isinstance(expected, dict):
        raise VerificationError("protocol corpus expected result is invalid")
    if mode not in {"candidate", "public"}:
        raise VerificationError("unknown bridge verification mode")
    exit_code = expected.get("exit_code")
    ok = expected.get("ok")
    error_code = expected.get("error_code")
    if type(exit_code) is not int or not isinstance(ok, bool):
        raise VerificationError("protocol corpus expectation is invalid")
    return exit_code, ok, str(error_code) if error_code is not None else None


def verify_fixture(
    fixture: Fixture,
    *,
    bridge_command: Sequence[str],
    mode: str = "candidate",
    cases: Sequence[Mapping[str, object]] | None = None,
    strace_dir: Path | None = None,
    reviewed_runtime_root: Path | None = None,
    evidence: Mapping[str, object] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    if mode not in {"candidate", "public"} or not bridge_command:
        raise VerificationError("explicit bridge mode and command are required")
    corpus = (
        tuple(cases) if cases is not None else load_protocol_corpus(PROTOCOL_CORPUS)
    )
    if strace_dir is not None:
        strace_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot_roots(fixture)
    pending_before = fixture.pending.read_bytes()
    operation_results: list[dict[str, object]] = []
    errors: list[str] = []
    observed_digests: dict[str, str] = {}
    trace_groups: list[dict[str, object]] = []
    for index, case in enumerate(corpus):
        identifier = str(case["id"])
        trace_path = strace_dir / f"trace-{index:03d}" if strace_dir else None
        try:
            completed = _run_case(
                [*_trace_prefix(trace_path), *bridge_command],
                cwd=fixture.workspace,
                environment=fixture.environment,
                payload=_case_payload(case),
                timeout_seconds=timeout_seconds,
                capture_dir=strace_dir,
            )
        except OSError as exc:
            completed = ProcessResult(255, b"", b"", False)
            errors.append(f"process launch failed: {identifier}: {type(exc).__name__}")
        if completed.timed_out:
            errors.append(f"process timed out or exceeded output limit: {identifier}")
        if completed.stderr:
            errors.append(f"stderr was not empty: {identifier}")
        try:
            response = _single_json(completed.stdout)
        except VerificationError as exc:
            errors.append(f"{exc}: {identifier}")
            response = {}
        expected_exit, expected_ok, expected_error = _expected_result(case, mode=mode)
        if trace_path is not None:
            request_for_trace = case.get("request_json")
            operation_for_trace = (
                request_for_trace.get("operation")
                if isinstance(request_for_trace, dict)
                else None
            )
            try:
                trace_groups.append(
                    _trace_group(
                        trace_path,
                        base=strace_dir,
                        identifier=identifier,
                        command=bridge_command,
                        expected_exit=expected_exit,
                        required_binder_count=(
                            2
                            if mode in {"candidate", "public"}
                            and operation_for_trace == "objective.plan"
                            and case.get("class") == "valid_mandatory"
                            else 0
                        ),
                        entry_role=(
                            "internal_candidate" if mode == "candidate" else "bridge"
                        ),
                    )
                )
            except (OSError, UnicodeError, VerificationError) as exc:
                errors.append(f"trace group is incomplete: {identifier}: {exc}")
        if completed.returncode != expected_exit:
            errors.append(f"unexpected exit code: {identifier}")
        if response.get("ok") is not expected_ok:
            errors.append(f"unexpected response status: {identifier}")
        request = case.get("request_json")
        original_expected = case.get("expected")
        originally_successful = (
            isinstance(original_expected, dict) and original_expected.get("ok") is True
        )
        if originally_successful and isinstance(request, dict):
            case_errors, case_digests = _success_errors(response, request)
            errors.extend(f"{error}: {identifier}" for error in case_errors)
            observed_digests.update(case_digests)
        if expected_error is not None:
            error = response.get("error")
            if not isinstance(error, dict) or error.get("code") != expected_error:
                errors.append(f"error code mismatch: {identifier}")
        operation_results.append(
            {
                "case": identifier,
                "operation": request.get("operation")
                if isinstance(request, dict)
                else None,
                "exit_code": completed.returncode,
                "ok": response.get("ok"),
            }
        )
    after = snapshot_roots(fixture, tolerate_errors=True)
    snapshot_differences = compare_snapshots(before, after)
    errors.extend(
        "snapshot changed: "
        f"{difference['root']}/{difference['path']} "
        f"({','.join(difference['changed_fields'])})"
        for difference in snapshot_differences
    )
    if not fixture.pending.exists() or fixture.pending.read_bytes() != pending_before:
        errors.append("pending sibling changed or recovered")
    trace_report: dict[str, object]
    if strace_dir is None:
        trace_report = {
            "enabled": False,
            "summary": {},
            "violations": [],
            "blind_spots": ["host syscall tracing was not requested"],
        }
    else:
        if reviewed_runtime_root is None:
            raise VerificationError("strict strace audit requires a runtime root")
        trace_paths = tuple(
            sorted(path for path in strace_dir.glob("trace-*") if path.is_file())
        )
        artifact_kind = str(
            (evidence or {}).get("artifact", {}).get("kind", "source")
            if isinstance((evidence or {}).get("artifact", {}), dict)
            else "source"
        )
        artifact_sha256 = str(
            (evidence or {}).get("artifact", {}).get("sha256", "0" * 64)
            if isinstance((evidence or {}).get("artifact", {}), dict)
            else "0" * 64
        )
        artifact_path = str(
            (evidence or {}).get("artifact", {}).get("path", "")
            if isinstance((evidence or {}).get("artifact", {}), dict)
            else ""
        )
        manifest = {
            "schema_version": 1,
            "artifact_kind": artifact_kind,
            "artifact_sha256": "sha256:" + artifact_sha256,
            "artifact_path": artifact_path,
            "landlock_evidence_required": mode in {"candidate", "public"},
            "reviewed_runtime_root": str(reviewed_runtime_root.resolve()),
            "entry_executables": {
                bridge_command[0]: "sha256:" + _sha256(Path(bridge_command[0]))
            },
            "groups": trace_groups,
        }
        _write_json_exclusive(manifest, strace_dir / "manifest.json")
        trace_report = audit_trace_files(
            trace_paths, manifest=manifest, manifest_base=strace_dir
        )
        if not trace_report.get("ok"):
            errors.append("strict strace audit failed")
    return {
        "schema_version": 2,
        "passed": not errors,
        "mode": mode,
        "evidence": dict(evidence or {}),
        "operations": operation_results,
        "errors": sorted(set(errors)),
        "snapshot": {
            "roots": list(SNAPSHOT_ROOT_NAMES),
            "differences": list(snapshot_differences),
            "fields": [
                "kind",
                "size",
                "sha256",
                "mode",
                "uid",
                "gid",
                "inode",
                "nlink",
                "mtime_ns",
                "ctime_ns",
                "target",
            ],
        },
        "digests": dict(sorted(observed_digests.items())),
        "contract_digest": _canonical_digest(dict(sorted(observed_digests.items()))),
        "trace": trace_report,
    }


def _safe_archive_name(name: str) -> tuple[str, ...]:
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        raise VerificationError("artifact member path is unsafe")
    parts = tuple(part for part in name.split("/") if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise VerificationError("artifact member path is unsafe")
    return parts


def _package_relative(parts: tuple[str, ...]) -> str | None:
    for index in range(len(parts) - 1):
        if parts[index] == "dyro" and (index == 0 or parts[index - 1] == "src"):
            return "/".join(parts[index + 1 :])
    return None


def _artifact_package_manifest(kind: str, artifact: Path) -> dict[str, str]:
    package: dict[str, str] = {}
    seen: set[tuple[str, ...]] = set()

    def add(name: str, payload: bytes, *, is_regular: bool) -> None:
        parts = _safe_archive_name(name)
        if parts in seen:
            raise VerificationError("artifact contains duplicate member paths")
        seen.add(parts)
        relative = _package_relative(parts)
        if relative is None or not is_regular:
            return
        if relative in package:
            raise VerificationError("artifact contains duplicate Dyro package paths")
        package[relative] = hashlib.sha256(payload).hexdigest()

    if kind == "wheel":
        with zipfile.ZipFile(artifact) as archive:
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise VerificationError("artifact contains a symlink member")
                add(
                    member.filename,
                    archive.read(member) if not member.is_dir() else b"",
                    is_regular=not member.is_dir(),
                )
    else:
        with tarfile.open(artifact, "r:gz") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk() or member.isdev():
                    raise VerificationError("artifact contains a link or device member")
                stream = archive.extractfile(member) if member.isfile() else None
                add(
                    member.name,
                    stream.read() if stream is not None else b"",
                    is_regular=member.isfile(),
                )
    if "__init__.py" not in package or len(package) < 10:
        raise VerificationError("artifact does not contain a complete Dyro package")
    return dict(sorted(package.items()))


def _installed_package_manifest(runtime_root: Path) -> tuple[Path, dict[str, str]]:
    candidates = [
        path
        for path in runtime_root.rglob("dyro")
        if path.is_dir() and "site-packages" in path.parts
    ]
    if len(candidates) != 1:
        raise VerificationError(
            "reviewed runtime must contain exactly one Dyro package"
        )
    package_root = candidates[0]
    package: dict[str, str] = {}
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            raise VerificationError("installed Dyro package contains a symlink")
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(package_root).as_posix()
        package[relative] = _sha256(path)
    return package_root, package


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("candidate", "public"), required=True)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--protocol-corpus", type=Path, required=True)
    parser.add_argument(
        "--artifact-kind", choices=("source", "wheel", "sdist"), required=True
    )
    parser.add_argument("--artifact-file", type=Path, required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--artifact-metadata", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--dirty", choices=("clean", "dirty"), required=True)
    parser.add_argument("--source-checkout", type=Path, required=True)
    parser.add_argument("--reviewed-runtime-root", type=Path)
    parser.add_argument("--candidate-python", type=Path)
    parser.add_argument("--internal-candidate", type=Path)
    parser.add_argument("--bridge", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--strace-dir", type=Path)
    return parser


def _write_report(report: Mapping[str, object], path: Path) -> None:
    _write_json_exclusive(report, path)


def _write_json_exclusive(document: Mapping[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    if parent != path.parent.absolute() or path.name in {"", ".", ".."}:
        raise VerificationError("evidence path is not canonical")
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        file_fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(file_fd, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(directory_fd)


def _validate_cli(options: argparse.Namespace) -> tuple[str, ...]:
    artifact = options.artifact_file.resolve(strict=True)
    options.protocol_corpus.resolve(strict=True)
    source_checkout = options.source_checkout.resolve(strict=True)
    if not _valid_sha256(options.artifact_sha256):
        raise VerificationError("artifact SHA-256 format is invalid")
    if _sha256(artifact) != options.artifact_sha256:
        raise VerificationError("artifact SHA-256 mismatch")
    if _COMMIT.fullmatch(options.commit) is None:
        raise VerificationError("commit must be an exact 40-character lowercase Git ID")
    if options.commit == "0" * 40:
        raise VerificationError("zero Git IDs are not accepted as provenance")
    try:
        metadata = json.loads(
            options.artifact_metadata.resolve(strict=True).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError("artifact metadata is unreadable") from exc
    if metadata != {
        "artifact_kind": options.artifact_kind,
        "artifact_sha256": options.artifact_sha256,
        "commit": options.commit,
    }:
        raise VerificationError("artifact metadata does not bind exact provenance")
    if options.reviewed_runtime_root is None or options.strace_dir is None:
        raise VerificationError(
            "every gate mode requires runtime-bound strace evidence"
        )
    runtime_root = options.reviewed_runtime_root.resolve(strict=True)
    if (
        source_checkout == runtime_root
        or source_checkout in runtime_root.parents
        or runtime_root in source_checkout.parents
    ):
        raise VerificationError("source checkout and reviewed runtime are not isolated")
    if options.mode == "candidate":
        if (
            options.bridge is not None
            or options.candidate_python is None
            or options.internal_candidate is None
        ):
            raise VerificationError("candidate mode arguments are incomplete")
        python = Path(os.path.abspath(options.candidate_python))
        runner = Path(os.path.abspath(options.internal_candidate))
        if not python.is_file() or not runner.is_file():
            raise VerificationError("candidate runtime is unavailable")
        command = (str(python), "-I", str(runner))
    else:
        if (
            options.bridge is None
            or options.candidate_python is not None
            or options.internal_candidate is not None
        ):
            raise VerificationError("public mode arguments are incomplete")
        bridge = Path(os.path.abspath(options.bridge))
        if not bridge.is_file():
            raise VerificationError("public Bridge executable is unavailable")
        command = (str(bridge),)
    return command


def main(arguments: Iterable[str] | None = None) -> int:
    options = _parser().parse_args(arguments)
    temporary: tempfile.TemporaryDirectory[str] | None = None
    report: dict[str, object]
    try:
        bridge_command = _validate_cli(options)
        cases = load_protocol_corpus(options.protocol_corpus)
        if options.fixture_root is None:
            temporary = tempfile.TemporaryDirectory(prefix="dyro-bridge-s5-")
            fixture_root = Path(temporary.name)
        else:
            fixture_root = options.fixture_root
        fixture = prepare_fixture(fixture_root)
        runtime_executable = Path(sys.executable).resolve()
        artifact = options.artifact_file.resolve()
        corpus = options.protocol_corpus.resolve()
        candidate = (
            options.internal_candidate.resolve()
            if options.mode == "candidate"
            else options.bridge.resolve()
        )
        artifact_package = _artifact_package_manifest(options.artifact_kind, artifact)
        package_root, installed_package = _installed_package_manifest(
            options.reviewed_runtime_root.resolve(strict=True)
        )
        if artifact_package != installed_package:
            missing = sorted(set(artifact_package) - set(installed_package))[:20]
            unexpected = sorted(set(installed_package) - set(artifact_package))[:20]
            changed = sorted(
                path
                for path in set(artifact_package) & set(installed_package)
                if artifact_package[path] != installed_package[path]
            )[:20]
            raise VerificationError(
                "installed Dyro package differs from artifact: "
                f"missing={missing!r}, unexpected={unexpected!r}, changed={changed!r}"
            )
        package_manifest_digest = _canonical_digest(artifact_package)
        evidence = {
            "commit": options.commit,
            "dirty": options.dirty,
            "artifact": {
                "kind": options.artifact_kind,
                "path": str(artifact),
                "sha256": options.artifact_sha256,
                "bytes": artifact.stat().st_size,
                "package_file_count": len(artifact_package),
                "package_manifest_sha256": package_manifest_digest,
                "metadata_sha256": _sha256(options.artifact_metadata.resolve()),
            },
            "runtime": {
                "platform": "linux-ubuntu-24.04",
                "python": sys.version.split()[0],
                "python_executable_sha256": _sha256(runtime_executable),
                "dyro_version": DYRO_VERSION,
                "dyro_package_root": str(package_root),
                "dyro_package_file_count": len(installed_package),
                "dyro_package_manifest_sha256": _canonical_digest(installed_package),
                "candidate_sha256": _sha256(candidate),
            },
            "harness": {
                "verifier_sha256": _sha256(Path(__file__).resolve()),
                "auditor_sha256": _sha256(
                    Path(__file__).with_name("audit_bridge_strace.py").resolve()
                ),
            },
            "protocol_corpus_sha256": _sha256(corpus),
        }
        report = verify_fixture(
            fixture,
            bridge_command=bridge_command,
            mode=options.mode,
            cases=cases,
            strace_dir=options.strace_dir,
            reviewed_runtime_root=options.reviewed_runtime_root,
            evidence=evidence,
        )
    except (
        OSError,
        subprocess.SubprocessError,
        VerificationError,
        zipfile.BadZipFile,
        tarfile.TarError,
        UnicodeError,
        ValueError,
    ) as exc:
        report = {
            "schema_version": 2,
            "passed": False,
            "mode": options.mode,
            "errors": [str(exc)],
        }
    finally:
        if temporary is not None:
            temporary.cleanup()
    _write_report(report, options.report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
