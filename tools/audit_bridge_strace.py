#!/usr/bin/env python3
"""Fail-closed Linux strace auditor for the Dyro Agent Bridge S5 gate.

The evidence manifest binds every trace file to one exact entry command and
expected main-process exit.  The auditor does not import the candidate Dyro
artifact: the reviewed S3 descriptor-binder source digest and Git argv are
frozen independently below, so implementation drift fails the audit.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Iterable, Mapping, Sequence


REPORT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
EXPECTED_BINDER_SOURCE_SHA256 = (
    "22760208d013272c8846163723c14a3cb8caaeec04248cabbbd55582d8e6cb00"
)

_NETWORK_SYSCALLS = frozenset(
    {
        "accept",
        "accept4",
        "bind",
        "connect",
        "getpeername",
        "getsockname",
        "getsockopt",
        "listen",
        "recvfrom",
        "recvmmsg",
        "recvmsg",
        "sendmmsg",
        "sendmsg",
        "sendto",
        "setsockopt",
        "shutdown",
        "socket",
        "socketpair",
    }
)
_MUTATION_SYSCALLS = frozenset(
    {
        "chmod",
        "chown",
        "copy_file_range",
        "creat",
        "fallocate",
        "fchmod",
        "fchmodat",
        "fchmodat2",
        "fchown",
        "fchownat",
        "fremovexattr",
        "fsetxattr",
        "ftruncate",
        "futimesat",
        "lchown",
        "link",
        "linkat",
        "lremovexattr",
        "lsetxattr",
        "mkdir",
        "mkdirat",
        "mknod",
        "mknodat",
        "mount",
        "move_mount",
        "open_tree",
        "pivot_root",
        "removexattr",
        "rename",
        "renameat",
        "renameat2",
        "rmdir",
        "setxattr",
        "symlink",
        "symlinkat",
        "truncate",
        "umount",
        "umount2",
        "unlink",
        "unlinkat",
        "utime",
        "utimensat",
        "utimes",
    }
)
_OPEN_SYSCALLS = frozenset({"open", "openat", "openat2"})
_EXEC_SYSCALLS = frozenset({"execve", "execveat"})
_LANDLOCK_SYSCALLS = frozenset(
    {"landlock_add_rule", "landlock_create_ruleset", "landlock_restrict_self"}
)
_SECURITY_SYSCALLS = frozenset(
    _NETWORK_SYSCALLS
    | _MUTATION_SYSCALLS
    | _OPEN_SYSCALLS
    | _EXEC_SYSCALLS
    | _LANDLOCK_SYSCALLS
    | {"prctl"}
)
_SECURITY_LINE = re.compile(
    r"\b(?:" + "|".join(sorted(_SECURITY_SYSCALLS, key=len, reverse=True)) + r")\("
)
_WRITE_FLAGS = re.compile(r"\b(?:O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_APPEND|O_TMPFILE)\b")
_OPERATION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CALL_START = re.compile(r"^(?P<syscall>[A-Za-z_][A-Za-z0-9_]*)\((?P<body>.*)$")
_PID_PREFIX = re.compile(r"^(?:\[pid\s+(?P<bracket_pid>\d+)\]|(?P<pid>\d+))\s+")
_CLOCK_PREFIX = re.compile(r"^\d\d:\d\d:\d\d(?:\.\d+)?\s+")
_EXITED = re.compile(r"^\+\+\+ exited with (?P<status>\d+) \+\+\+$")
_KILLED = re.compile(r"^\+\+\+ killed by (?P<signal>[A-Z0-9]+).+\+\+\+$")
_HEX_OID = re.compile(r"^[0-9a-f]{40}$")
_SYSTEM_GIT = frozenset({"/bin/git", "/usr/bin/git"})
_SYSTEM_PYTHON = frozenset(
    {"/bin/python3", "/bin/python3.12", "/usr/bin/python3", "/usr/bin/python3.12"}
)
_GIT_PREFIX = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    "-c",
    "core.commitGraph=false",
)


@dataclass(frozen=True)
class TraceCall:
    process: str
    syscall: str
    arguments: tuple[str, ...]
    result: str


@dataclass(frozen=True)
class TraceGroup:
    group_id: str
    trace_files: tuple[Path, ...]
    entry_trace: Path
    entry_command: tuple[str, ...]
    expected_exit: int
    required_binder_count: int
    entry_role: str
    trace_sha256: tuple[tuple[Path, str], ...]


def _decode_quoted(token: str) -> str | None:
    token = token.strip()
    if not token.startswith('"'):
        return None
    escaped = False
    for index in range(1, len(token)):
        character = token[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            try:
                value = ast.literal_eval(token[: index + 1])
            except (SyntaxError, ValueError):
                return None
            return value if isinstance(value, str) else None
    return None


def _split_top_level(arguments: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote = False
    escaped = False
    for index, character in enumerate(arguments):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
            continue
        if character == '"':
            quote = True
        elif character in "[{(":
            depth += 1
        elif character in "]})":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            parts.append(arguments[start:index].strip())
            start = index + 1
    parts.append(arguments[start:].strip())
    return tuple(parts)


def _parse_argv(token: str) -> tuple[str, ...] | None:
    value = token.strip()
    if "..." in value or not value.startswith("[") or not value.endswith("]"):
        return None
    try:
        parsed = ast.literal_eval(value)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        return None
    return tuple(parsed)


def _strip_prefix(line: str, fallback_process: str) -> tuple[str, str]:
    content = line.strip()
    process = fallback_process
    prefix = _PID_PREFIX.match(content)
    if prefix is not None:
        process = prefix.group("bracket_pid") or prefix.group("pid") or process
        content = content[prefix.end() :]
    content = _CLOCK_PREFIX.sub("", content, count=1)
    return process, content


def _parse_call(line: str, fallback_process: str) -> TraceCall | None:
    process, content = _strip_prefix(line, fallback_process)
    match = _CALL_START.match(content)
    if match is None:
        return None
    completed = re.fullmatch(
        r"(?P<arguments>.*)\)\s+=\s+(?P<result>.+)", match.group("body")
    )
    if completed is None:
        return None
    return TraceCall(
        process=process,
        syscall=match.group("syscall"),
        arguments=_split_top_level(completed.group("arguments")),
        result=completed.group("result").strip(),
    )


def _terminal_status(line: str, fallback_process: str) -> tuple[str, int | None] | None:
    process, content = _strip_prefix(line, fallback_process)
    exited = _EXITED.fullmatch(content)
    if exited is not None:
        return process, int(exited.group("status"))
    if _KILLED.fullmatch(content) is not None:
        return process, None
    return None


def _call_succeeded(call: TraceCall) -> bool:
    value = _result_integer(call)
    return value is not None and value >= 0


def _result_integer(call: TraceCall) -> int | None:
    match = re.match(r"^-?\d+", call.result)
    return None if match is None else int(match.group(0))


def _open_path_and_flags(call: TraceCall) -> tuple[str | None, str]:
    if call.syscall == "open":
        path_index, flags_index = 0, 1
    else:
        path_index, flags_index = 1, 2
    if len(call.arguments) <= flags_index:
        return None, ""
    return _decode_quoted(call.arguments[path_index]), call.arguments[flags_index]


def _exec_fields(
    call: TraceCall,
) -> tuple[str | None, tuple[str, ...] | None, bool]:
    executable_index = 1 if call.syscall == "execveat" else 0
    argv_index = executable_index + 1
    if len(call.arguments) <= argv_index:
        return None, None, False
    semantics_are_bound = len(call.arguments) == 3
    if call.syscall == "execveat":
        semantics_are_bound = (
            len(call.arguments) == 5
            and call.arguments[0] == "AT_FDCWD"
            and call.arguments[4] == "0"
        )
    return (
        _decode_quoted(call.arguments[executable_index]),
        _parse_argv(call.arguments[argv_index]),
        semantics_are_bound,
    )


def _git_exec_is_allowed(executable: str, argv: tuple[str, ...]) -> bool:
    if executable not in _SYSTEM_GIT or not argv:
        return False
    if argv[0] != executable or tuple(argv[1 : 1 + len(_GIT_PREFIX)]) != _GIT_PREFIX:
        return False
    command = tuple(argv[1 + len(_GIT_PREFIX) :])
    if command == ("rev-parse", "--verify", "HEAD^{commit}"):
        return True
    return (
        len(command) == 4
        and command[:2] == ("merge-base", "--is-ancestor")
        and _HEX_OID.fullmatch(command[2]) is not None
        and _HEX_OID.fullmatch(command[3]) is not None
    )


def _binder_bound_descriptors(
    executable: str, argv: tuple[str, ...]
) -> tuple[int, ...] | None:
    if executable not in _SYSTEM_PYTHON or len(argv) < 14 or argv[0] != executable:
        return None
    if tuple(argv[1:4]) != ("-I", "-S", "-c"):
        return None
    if (
        hashlib.sha256(argv[4].encode("utf-8")).hexdigest()
        != EXPECTED_BINDER_SOURCE_SHA256
    ):
        return None
    try:
        descriptors = tuple(int(value) for value in argv[5:10])
    except (TypeError, ValueError):
        return None
    if (
        len(descriptors) != 5
        or len(set(descriptors)) != 5
        or any(value < 0 for value in descriptors)
    ):
        return None
    git_argv = tuple(argv[10:])
    if not git_argv or not _git_exec_is_allowed(git_argv[0], git_argv):
        return None
    return descriptors[:4]


def _landlock_parent_fd(argument: str) -> int | None:
    match = re.search(r"(?:^|[{,])\s*parent_fd=(-?\d+)(?:[,}]|$)", argument)
    return None if match is None else int(match.group(1))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _path_is_within(
    path: Path, root: Path, *, allow_leaf_symlink: bool = False
) -> bool:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        return False
    if path != root and root not in path.parents:
        return False
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cursor = root
    for component in relative.parts[:-1]:
        cursor /= component
        if cursor.is_symlink():
            return False
    return allow_leaf_symlink or not path.is_symlink()


def _entry_command_shape_is_allowed(
    command: tuple[str, ...],
    *,
    role: str,
    runtime_root: Path,
    executable_sha256: str,
    script_sha256: str | None,
) -> bool:
    executable = Path(command[0])
    # A venv's leaf `python` is commonly a symlink.  Its path and followed
    # bytes are both manifest-bound; symlinks in every parent component remain
    # forbidden so a directory cannot redirect an asserted runtime subtree.
    if not _path_is_within(executable, runtime_root, allow_leaf_symlink=True):
        return False
    try:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return False
        if _file_sha256(executable) != executable_sha256:
            return False
    except OSError:
        return False
    if role == "bridge":
        return len(command) == 1 and script_sha256 is None
    if role == "python_module":
        return (
            tuple(command[1:]) == ("-I", "-m", "dyro.bridge.transport")
            and script_sha256 is None
        )
    if role not in {"internal_candidate", "verifier"} or len(command) < 3:
        return False
    if command[1] != "-I":
        return False
    script = Path(command[2])
    expected_name = (
        "internal_candidate_runner.py"
        if role == "internal_candidate"
        else "verify_bridge_zero_effects.py"
    )
    if not _path_is_within(script, runtime_root) or script.name != expected_name:
        return False
    if role == "internal_candidate" and len(command) != 3:
        return False
    try:
        return (
            isinstance(script_sha256, str)
            and not script.is_symlink()
            and script.is_file()
            and _file_sha256(script) == script_sha256
        )
    except OSError:
        return False


def _manifest_digest(document: Mapping[str, object]) -> str:
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _parse_manifest(
    document: Mapping[str, object],
    *,
    base: Path,
    supplied_paths: tuple[Path, ...],
) -> tuple[str, tuple[TraceGroup, ...], str, str]:
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest schema version is invalid")
    artifact_kind = document.get("artifact_kind")
    artifact_sha256 = document.get("artifact_sha256")
    if artifact_kind not in {"source", "wheel", "sdist"}:
        raise ValueError("manifest artifact kind is invalid")
    if (
        not isinstance(artifact_sha256, str)
        or _SHA256.fullmatch(artifact_sha256) is None
    ):
        raise ValueError("manifest artifact digest is invalid")
    landlock_required = document.get("landlock_evidence_required")
    if not isinstance(landlock_required, bool):
        raise ValueError("manifest Landlock policy is invalid")
    raw_runtime_root = document.get("reviewed_runtime_root")
    if not isinstance(raw_runtime_root, str) or not raw_runtime_root:
        raise ValueError("manifest reviewed runtime root is invalid")
    runtime_root = Path(raw_runtime_root)
    if (
        not runtime_root.is_absolute()
        or Path(os.path.normpath(str(runtime_root))) != runtime_root
        or runtime_root.is_symlink()
        or not runtime_root.is_dir()
        or runtime_root.resolve(strict=True) != runtime_root
    ):
        raise ValueError("manifest reviewed runtime root is unavailable")
    raw_artifact_path = document.get("artifact_path")
    if not isinstance(raw_artifact_path, str) or not raw_artifact_path:
        raise ValueError("manifest artifact path is invalid")
    artifact_path = Path(raw_artifact_path)
    if not _path_is_within(artifact_path, runtime_root):
        raise ValueError("manifest artifact escapes reviewed runtime root")
    try:
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError("manifest artifact is not a regular owned file")
        if _file_sha256(artifact_path) != artifact_sha256:
            raise ValueError("manifest artifact digest does not match")
    except OSError as exc:
        raise ValueError("manifest artifact is unreadable") from exc
    raw_executables = document.get("entry_executables")
    if not isinstance(raw_executables, dict) or not raw_executables:
        raise ValueError("manifest entry executables are missing")
    entry_executables: dict[str, str] = {}
    for raw_path, raw_digest in raw_executables.items():
        if (
            not isinstance(raw_path, str)
            or not isinstance(raw_digest, str)
            or _SHA256.fullmatch(raw_digest) is None
        ):
            raise ValueError("manifest entry executable identity is invalid")
        executable = Path(raw_path)
        if not _path_is_within(executable, runtime_root, allow_leaf_symlink=True):
            raise ValueError("manifest entry executable escapes reviewed runtime root")
        try:
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise ValueError("manifest entry executable is unavailable")
            if _file_sha256(executable) != raw_digest:
                raise ValueError("manifest entry executable digest does not match")
        except OSError as exc:
            raise ValueError("manifest entry executable is unreadable") from exc
        entry_executables[raw_path] = raw_digest
    raw_groups = document.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("manifest groups are missing")
    groups: list[TraceGroup] = []
    seen_ids: set[str] = set()
    manifest_paths: list[Path] = []
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("manifest group is invalid")
        group_id = raw.get("id")
        if (
            not isinstance(group_id, str)
            or _OPERATION_ID.fullmatch(group_id) is None
            or group_id in seen_ids
        ):
            raise ValueError("manifest group id is invalid")
        seen_ids.add(group_id)
        raw_files = raw.get("trace_files")
        if (
            not isinstance(raw_files, list)
            or not raw_files
            or not all(isinstance(item, str) and item for item in raw_files)
        ):
            raise ValueError("manifest trace files are invalid")
        trace_candidates = tuple(base / item for item in raw_files)
        if any(
            not _path_is_within(path, base) or not path.is_file()
            for path in trace_candidates
        ):
            raise ValueError("manifest trace files must be regular non-symlink files")
        trace_files = tuple(path.resolve() for path in trace_candidates)
        if any(base not in path.parents for path in trace_files):
            raise ValueError("manifest trace file escapes its evidence directory")
        if len(set(trace_files)) != len(trace_files):
            raise ValueError("manifest trace files are duplicated")
        raw_trace_sha256 = raw.get("trace_sha256")
        if (
            not isinstance(raw_trace_sha256, dict)
            or set(raw_trace_sha256) != set(raw_files)
            or any(
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
                for value in raw_trace_sha256.values()
            )
        ):
            raise ValueError("manifest trace digests are invalid")
        trace_sha256 = tuple(
            (path, raw_trace_sha256[name])
            for name, path in zip(raw_files, trace_files, strict=True)
        )
        if any(_file_sha256(path) != digest for path, digest in trace_sha256):
            raise ValueError("manifest trace digest does not match")
        entry_trace_value = raw.get("entry_trace")
        if not isinstance(entry_trace_value, str) or not entry_trace_value:
            raise ValueError("manifest entry trace is invalid")
        entry_trace = (base / entry_trace_value).resolve()
        if entry_trace not in trace_files:
            raise ValueError("manifest entry trace is outside its group")
        raw_command = raw.get("entry_command")
        if (
            not isinstance(raw_command, list)
            or not raw_command
            or not all(isinstance(item, str) and item for item in raw_command)
        ):
            raise ValueError("manifest entry command is invalid")
        entry_command = tuple(raw_command)
        entry_role = raw.get("entry_role")
        script_sha256 = raw.get("entry_script_sha256")
        if not isinstance(entry_role, str) or (
            script_sha256 is not None
            and (
                not isinstance(script_sha256, str)
                or _SHA256.fullmatch(script_sha256) is None
            )
        ):
            raise ValueError("manifest entry identity is invalid")
        executable_sha256 = entry_executables.get(entry_command[0])
        if executable_sha256 is None:
            raise ValueError("manifest entry executable is not reviewed")
        if not _entry_command_shape_is_allowed(
            entry_command,
            role=entry_role,
            runtime_root=runtime_root,
            executable_sha256=executable_sha256,
            script_sha256=script_sha256,
        ):
            raise ValueError("manifest entry command shape is not reviewed")
        expected_exit = raw.get("expected_exit")
        binder_count = raw.get("required_binder_count")
        if (
            not isinstance(expected_exit, int)
            or isinstance(expected_exit, bool)
            or not 0 <= expected_exit <= 255
        ):
            raise ValueError("manifest expected exit is invalid")
        if (
            not isinstance(binder_count, int)
            or isinstance(binder_count, bool)
            or not 0 <= binder_count <= 100
        ):
            raise ValueError("manifest binder count is invalid")
        groups.append(
            TraceGroup(
                group_id,
                trace_files,
                entry_trace,
                entry_command,
                expected_exit,
                binder_count,
                entry_role,
                trace_sha256,
            )
        )
        manifest_paths.extend(trace_files)
    if len(set(manifest_paths)) != len(manifest_paths):
        raise ValueError("a trace file belongs to multiple manifest groups")
    if set(entry_executables) != {group.entry_command[0] for group in groups}:
        raise ValueError("manifest entry executable set differs from its groups")
    if set(manifest_paths) != set(supplied_paths):
        raise ValueError("manifest and supplied trace files differ")
    if landlock_required != (sum(group.required_binder_count for group in groups) > 0):
        raise ValueError("manifest Landlock policy differs from binder requirements")
    return _manifest_digest(document), tuple(groups), artifact_kind, artifact_sha256


def _violation(
    trace: Path,
    line_number: int,
    process: str | None,
    syscall: str,
    rule: str,
    detail: str,
    *,
    group_id: str | None = None,
) -> dict[str, object]:
    return {
        "group": group_id,
        "trace": str(trace),
        "line": line_number,
        "process": process,
        "syscall": syscall,
        "rule": rule,
        "detail": detail,
    }


def _validate_binder_sequence(
    trace: Path,
    process: str,
    events: list[tuple[int, str, bool]],
    group_id: str,
    bound_descriptors: tuple[int, ...],
    add_parent_fds: tuple[int | None, ...],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    labels = [label for _, label, _ in events]
    required = (
        "binder_exec",
        "create_version",
        "create_ruleset",
        "add",
        "prctl",
        "restrict",
        "git_exec",
    )
    positions: list[int] = []
    cursor = -1
    for label in required:
        try:
            cursor = labels.index(label, cursor + 1)
        except ValueError:
            findings.append(
                _violation(
                    trace,
                    0,
                    process,
                    "landlock",
                    "LANDLOCK_SEQUENCE_INCOMPLETE",
                    f"missing ordered {label}",
                    group_id=group_id,
                )
            )
            return findings
        positions.append(cursor)
    label_counts = {label: labels.count(label) for label in set(labels)}
    additions = [success for _, label, success in events if label == "add"]
    required_results = [
        success
        for _, label, success in events
        if label
        in {
            "binder_exec",
            "create_version",
            "create_ruleset",
            "add",
            "prctl",
            "restrict",
            "git_exec",
            "invalid",
        }
    ]
    exactly_once = (
        "binder_exec",
        "create_version",
        "create_ruleset",
        "prctl",
        "restrict",
        "git_exec",
    )
    if (
        any(label_counts.get(label) != 1 for label in exactly_once)
        or len(additions) < 10
        or len(add_parent_fds) != len(additions)
        or tuple(add_parent_fds[:4]) != bound_descriptors
        or label_counts.get("invalid", 0)
        or not all(required_results)
    ):
        findings.append(
            _violation(
                trace,
                0,
                process,
                "landlock",
                "LANDLOCK_RESULT_INVALID",
                "binder Landlock evidence contains a failed call, incomplete rule coverage, descriptor drift, or lacks ABI/ruleset creation",
                group_id=group_id,
            )
        )
    return findings


def audit_trace_files(
    trace_paths: Iterable[Path],
    *,
    manifest: Mapping[str, object],
    manifest_base: Path = Path("."),
) -> dict[str, object]:
    """Return a deterministic report for a complete manifest-bound trace set."""
    paths = tuple(sorted((Path(path).resolve() for path in trace_paths), key=str))
    violations: list[dict[str, object]] = []
    counts = {
        "events": 0,
        "exec": 0,
        "network": 0,
        "mutation": 0,
        "write_open": 0,
        "binder": 0,
        "landlock_success": 0,
    }
    try:
        manifest_sha256, groups, artifact_kind, artifact_sha256 = _parse_manifest(
            manifest,
            base=manifest_base.resolve(),
            supplied_paths=paths,
        )
    except (OSError, ValueError) as exc:
        violations.append(
            _violation(
                Path("<manifest>"), 0, None, "manifest", "MANIFEST_INVALID", str(exc)
            )
        )
        groups = ()
        manifest_sha256 = None
        artifact_kind = None
        artifact_sha256 = None

    for group in groups:
        entry_count = 0
        entry_trace_status: int | None = None
        binder_events: dict[str, list[tuple[int, str, bool]]] = {}
        binder_trace: dict[str, Path] = {}
        binder_bound_fds: dict[str, tuple[int, ...]] = {}
        binder_add_parent_fds: dict[str, list[int | None]] = {}
        binder_ruleset_fds: dict[str, int] = {}
        group_event_count = 0
        terminal_files: set[Path] = set()
        sequence = 0
        for trace in group.trace_files:
            fallback_process = f"file:{trace.name}"
            try:
                lines = trace.read_text(encoding="utf-8", errors="strict").splitlines()
            except (OSError, UnicodeError):
                violations.append(
                    _violation(
                        trace,
                        0,
                        None,
                        "trace",
                        "TRACE_UNREADABLE",
                        "trace is not readable UTF-8",
                        group_id=group.group_id,
                    )
                )
                continue
            terminal_count = 0
            for line_number, line in enumerate(lines, start=1):
                if "<unfinished ...>" in line or re.search(
                    r"<\.\.\.\s+\w+ resumed>", line
                ):
                    violations.append(
                        _violation(
                            trace,
                            line_number,
                            None,
                            "trace",
                            "TRACE_INCOMPLETE",
                            "unfinished or resumed syscall evidence is not accepted",
                            group_id=group.group_id,
                        )
                    )
                    continue
                terminal = _terminal_status(line, fallback_process)
                if terminal is not None:
                    terminal_count += 1
                    terminal_files.add(trace)
                    if trace == group.entry_trace:
                        entry_trace_status = terminal[1]
                    continue
                call = _parse_call(line, fallback_process)
                if call is None:
                    if _SECURITY_LINE.search(line):
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                None,
                                "trace",
                                "SECURITY_LINE_UNPARSEABLE",
                                "security-relevant trace line could not be parsed",
                                group_id=group.group_id,
                            )
                        )
                    continue
                counts["events"] += 1
                group_event_count += 1
                sequence += 1
                if call.syscall in _NETWORK_SYSCALLS:
                    counts["network"] += 1
                    violations.append(
                        _violation(
                            trace,
                            line_number,
                            call.process,
                            call.syscall,
                            "NETWORK_SYSCALL",
                            "network-capable syscall attempted",
                            group_id=group.group_id,
                        )
                    )
                if call.syscall in _MUTATION_SYSCALLS:
                    counts["mutation"] += 1
                    violations.append(
                        _violation(
                            trace,
                            line_number,
                            call.process,
                            call.syscall,
                            "FILESYSTEM_MUTATION",
                            "filesystem mutation syscall attempted",
                            group_id=group.group_id,
                        )
                    )
                if call.syscall in _OPEN_SYSCALLS:
                    path, flags = _open_path_and_flags(call)
                    if not flags or ("O_" not in flags and flags.strip() != "0"):
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                call.process,
                                call.syscall,
                                "OPEN_FLAGS_UNPARSEABLE",
                                "open flags are not symbolic or zero",
                                group_id=group.group_id,
                            )
                        )
                    if _WRITE_FLAGS.search(flags) is not None and path != "/dev/null":
                        counts["write_open"] += 1
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                call.process,
                                call.syscall,
                                "WRITE_OPEN",
                                "write-capable open outside /dev/null",
                                group_id=group.group_id,
                            )
                        )
                if call.syscall in _EXEC_SYSCALLS:
                    counts["exec"] += 1
                    executable, argv, semantics_are_bound = _exec_fields(call)
                    if executable is None or argv is None or not semantics_are_bound:
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                call.process,
                                call.syscall,
                                "EXEC_NOT_ALLOWLISTED",
                                "exec path, argv, dirfd, or flags are not exactly bound",
                                group_id=group.group_id,
                            )
                        )
                    elif (
                        bound_descriptors := _binder_bound_descriptors(executable, argv)
                    ) is not None:
                        counts["binder"] += 1
                        binder_trace[call.process] = trace
                        binder_bound_fds[call.process] = bound_descriptors
                        binder_add_parent_fds[call.process] = []
                        binder_events.setdefault(call.process, []).append(
                            (sequence, "binder_exec", _call_succeeded(call))
                        )
                    elif _git_exec_is_allowed(executable, argv):
                        if call.process not in binder_events:
                            violations.append(
                                _violation(
                                    trace,
                                    line_number,
                                    call.process,
                                    call.syscall,
                                    "GIT_WITHOUT_BINDER",
                                    "Git exec is not owned by a validated binder PID",
                                    group_id=group.group_id,
                                )
                            )
                        else:
                            binder_events[call.process].append(
                                (sequence, "git_exec", _call_succeeded(call))
                            )
                    elif (
                        executable == group.entry_command[0]
                        and argv == group.entry_command
                    ):
                        entry_count += 1
                        if trace != group.entry_trace or not _call_succeeded(call):
                            violations.append(
                                _violation(
                                    trace,
                                    line_number,
                                    call.process,
                                    call.syscall,
                                    "ENTRY_BINDING_INVALID",
                                    "entry exec is in the wrong trace or did not succeed",
                                    group_id=group.group_id,
                                )
                            )
                    else:
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                call.process,
                                call.syscall,
                                "EXEC_NOT_ALLOWLISTED",
                                "exec does not match the exact entry, binder, or Git argv",
                                group_id=group.group_id,
                            )
                        )
                if call.syscall in _LANDLOCK_SYSCALLS or (
                    call.syscall == "prctl"
                    and call.arguments
                    and call.arguments[0] == "PR_SET_NO_NEW_PRIVS"
                ):
                    if call.process not in binder_events:
                        violations.append(
                            _violation(
                                trace,
                                line_number,
                                call.process,
                                call.syscall,
                                "LANDLOCK_WITHOUT_BINDER",
                                "Landlock security call is not owned by a validated binder PID",
                                group_id=group.group_id,
                            )
                        )
                    else:
                        result = _result_integer(call)
                        label = "invalid"
                        success = False
                        if call.syscall == "landlock_create_ruleset":
                            if call.arguments == (
                                "NULL",
                                "0",
                                "LANDLOCK_CREATE_RULESET_VERSION",
                            ):
                                label = "create_version"
                                success = result is not None and result >= 3
                            elif len(call.arguments) == 3 and call.arguments[2] == "0":
                                label = "create_ruleset"
                                success = result is not None and result >= 0
                                if success:
                                    binder_ruleset_fds[call.process] = result
                        elif call.syscall == "landlock_add_rule":
                            ruleset_fd = binder_ruleset_fds.get(call.process)
                            parent_fd = (
                                _landlock_parent_fd(call.arguments[2])
                                if len(call.arguments) >= 3
                                else None
                            )
                            if (
                                len(call.arguments) == 4
                                and ruleset_fd is not None
                                and call.arguments[0] == str(ruleset_fd)
                                and call.arguments[1] == "LANDLOCK_RULE_PATH_BENEATH"
                                and call.arguments[3] == "0"
                            ):
                                label = "add"
                                success = result == 0 and parent_fd is not None
                                binder_add_parent_fds[call.process].append(parent_fd)
                        elif call.syscall == "landlock_restrict_self":
                            ruleset_fd = binder_ruleset_fds.get(call.process)
                            if (
                                len(call.arguments) == 2
                                and ruleset_fd is not None
                                and call.arguments[0] == str(ruleset_fd)
                                and call.arguments[1] == "0"
                            ):
                                label = "restrict"
                                success = result == 0
                        elif call.syscall == "prctl":
                            if call.arguments == (
                                "PR_SET_NO_NEW_PRIVS",
                                "1",
                                "0",
                                "0",
                                "0",
                            ):
                                label = "prctl"
                                success = result == 0
                        binder_events[call.process].append((sequence, label, success))
            if terminal_count != 1:
                violations.append(
                    _violation(
                        trace,
                        0,
                        None,
                        "trace",
                        "TRACE_TERMINAL_INVALID",
                        "each trace file must contain exactly one process terminal record",
                        group_id=group.group_id,
                    )
                )
        if entry_count != 1:
            violations.append(
                _violation(
                    group.entry_trace,
                    0,
                    None,
                    "execve",
                    "ENTRY_COUNT_INVALID",
                    "group must contain exactly one exact entry exec",
                    group_id=group.group_id,
                )
            )
        if entry_trace_status != group.expected_exit:
            violations.append(
                _violation(
                    group.entry_trace,
                    0,
                    None,
                    "exit",
                    "ENTRY_EXIT_MISMATCH",
                    "entry trace exit does not match the manifest",
                    group_id=group.group_id,
                )
            )
        if terminal_files != set(group.trace_files):
            violations.append(
                _violation(
                    group.entry_trace,
                    0,
                    None,
                    "trace",
                    "TRACE_GROUP_INCOMPLETE",
                    "one or more manifest trace files lack terminal evidence",
                    group_id=group.group_id,
                )
            )
        if group_event_count < 2:
            violations.append(
                _violation(
                    group.entry_trace,
                    0,
                    None,
                    "trace",
                    "TRACE_TOO_SHALLOW",
                    "single-exec evidence is insufficient",
                    group_id=group.group_id,
                )
            )
        if len(binder_events) != group.required_binder_count:
            violations.append(
                _violation(
                    group.entry_trace,
                    0,
                    None,
                    "landlock",
                    "BINDER_COUNT_MISMATCH",
                    "validated binder PID count does not match the manifest",
                    group_id=group.group_id,
                )
            )
        for process, events in binder_events.items():
            findings = _validate_binder_sequence(
                binder_trace[process],
                process,
                events,
                group.group_id,
                binder_bound_fds[process],
                tuple(binder_add_parent_fds[process]),
            )
            violations.extend(findings)
            if not findings:
                counts["landlock_success"] += 1

        for trace, expected_digest in group.trace_sha256:
            try:
                actual_digest = _file_sha256(trace)
            except OSError:
                actual_digest = "unreadable"
            if actual_digest != expected_digest:
                violations.append(
                    _violation(
                        trace,
                        0,
                        None,
                        "trace",
                        "TRACE_CHANGED_DURING_AUDIT",
                        "trace bytes changed after manifest validation",
                        group_id=group.group_id,
                    )
                )

    if (
        any(group.required_binder_count for group in groups)
        and counts["landlock_success"] < 1
    ):
        violations.append(
            _violation(
                Path("<manifest>"),
                0,
                None,
                "landlock",
                "LANDLOCK_EVIDENCE_MISSING",
                "the manifest set has no complete successful binder evidence",
            )
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": not violations,
        "manifest_sha256": manifest_sha256,
        "artifact_kind": artifact_kind,
        "artifact_sha256": artifact_sha256,
        "trace_files": [str(path) for path in paths],
        "summary": counts,
        "violations": violations,
        "policy": {
            "network": "deny_all_observed_syscalls",
            "write_open": "deny_except_dev_null",
            "filesystem_mutation": "deny_all_observed_mutations",
            "exec": "exact_manifest_entry_and_frozen_s3_git_read",
            "trace_completeness": "manifest_bound_with_terminal_records",
        },
        "blind_spots": [
            "syscall traces do not prove application-level protocol semantics",
            "the auditor cannot prove container network/read-only/capability launch flags; the outer runner must record them",
            "the auditor covers traced descendants only and requires ptrace/strace setup to be independently trusted",
            "same-UID malicious processes can replace path-based trace evidence after unlinking strace's open inode; this Phase 0 gate detects accidental effects, not hostile evidence forgery",
            "read-only path scope and Git object identity are separate Landlock and snapshot gates",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trace", nargs="+", type=Path, help="all trace files named by the manifest"
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="exact trace-group evidence manifest",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": False,
            "violations": [
                _violation(
                    arguments.manifest,
                    0,
                    None,
                    "manifest",
                    "MANIFEST_INVALID",
                    "manifest is not readable strict JSON",
                )
            ],
            "blind_spots": [],
        }
    else:
        if not isinstance(manifest, dict):
            manifest = {}
        report = audit_trace_files(
            arguments.trace,
            manifest=manifest,
            manifest_base=arguments.manifest.parent,
        )
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
