"""Descriptor-bound, optional-lock-disabled Git observations for Agent Bridge."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence

from ..errors import ValidationError


_GIT_HEAD = frozenset("0123456789abcdef")
_MAX_OID_OUTPUT_BYTES = 41
_GIT_PREFIX = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
)
_RESOLVE_ARGUMENTS = (*_GIT_PREFIX, "rev-parse", "--verify", "HEAD^{commit}")
_ANCESTRY_ARGUMENTS = (*_GIT_PREFIX, "merge-base", "--is-ancestor")
_HELPER_SCRIPT = """import ctypes
import errno
import os
import sys

descriptors = tuple(int(value) for value in sys.argv[1:5])
error_fd = int(sys.argv[5])
executable = sys.argv[6]
arguments = tuple(sys.argv[6:])

if error_fd >= 0:
    os.set_inheritable(error_fd, False)

def fail(code):
    if error_fd >= 0:
        try:
            os.write(error_fd, code)
        except OSError:
            pass
    os._exit(125)

def apply_landlock(bound_descriptors):
    create_ruleset = 444
    add_rule = 445
    restrict_self = 446
    version_flag = 1
    path_beneath_rule = 1
    read_execute = (1 << 0) | (1 << 2) | (1 << 3)
    handled = sum(1 << bit for bit in range(13))

    libc = ctypes.CDLL(None, use_errno=True)
    version = libc.syscall(create_ruleset, 0, 0, version_flag)
    if version < 3:
        fail(b"U")
    if version >= 2:
        handled |= 1 << 13
    if version >= 3:
        handled |= 1 << 14
    if version >= 5:
        handled |= 1 << 15

    class RulesetAttr(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class PathBeneathAttr(ctypes.Structure):
        _fields_ = [
            ("allowed_access", ctypes.c_uint64),
            ("parent_fd", ctypes.c_int32),
        ]

    ruleset_attr = RulesetAttr(handled)
    ruleset_fd = libc.syscall(
        create_ruleset,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        fail(b"U")

    opened = []
    try:
        approved = [(descriptor, read_execute) for descriptor in bound_descriptors]
        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc", "/proc/self/fd"):
            try:
                descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            except OSError:
                fail(b"U")
            opened.append(descriptor)
            approved.append((descriptor, read_execute))
        try:
            dev_null = os.open("/dev/null", os.O_PATH | os.O_CLOEXEC)
        except OSError:
            fail(b"U")
        opened.append(dev_null)
        approved.append((dev_null, (1 << 1) | (1 << 2)))
        for descriptor, allowed_access in approved:
            path_attr = PathBeneathAttr(allowed_access, descriptor)
            if libc.syscall(
                add_rule,
                ruleset_fd,
                path_beneath_rule,
                ctypes.byref(path_attr),
                0,
            ) != 0:
                fail(b"U")
        if libc.prctl(38, 1, 0, 0, 0) != 0:
            fail(b"U")
        if libc.syscall(restrict_self, ruleset_fd, 0) != 0:
            fail(b"U")
    finally:
        os.close(ruleset_fd)
        for descriptor in opened:
            os.close(descriptor)

try:
    os.fchdir(descriptors[0])
except PermissionError:
    fail(b"P")
except OSError as exc:
    fail(b"U" if exc.errno in {errno.EBADF, errno.ENOENT} else b"X")

if all(descriptor >= 0 for descriptor in descriptors):
    if not sys.platform.startswith("linux"):
        fail(b"U")
    apply_landlock(descriptors)

try:
    os.execve(executable, arguments, dict(os.environ))
except PermissionError:
    fail(b"P")
except FileNotFoundError:
    fail(b"U")
except OSError:
    fail(b"X")
"""
_HELPER_PREFIX = ("-I", "-S", "-c", _HELPER_SCRIPT)
_SYSTEM_GIT_CANDIDATES = (Path("/usr/bin/git"), Path("/bin/git"))
_SYSTEM_PYTHON_CANDIDATES = (Path("/usr/bin/python3"), Path("/bin/python3"))
_LINUX_FD_ROOT = Path("/proc/self/fd")


class GitReadFailure(str, Enum):
    UNAVAILABLE = "unavailable"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    PARTIAL = "partial"


class GitReadError(ValidationError):
    """Typed operational failure from the only allowed Bridge subprocess."""

    def __init__(self, code: GitReadFailure) -> None:
        if not isinstance(code, GitReadFailure):
            raise TypeError("GitReadError code 必须是 GitReadFailure")
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class GitAncestryObservation:
    task_head_sha256: str
    destination_head_sha256: str
    is_ancestor: bool

    def __post_init__(self) -> None:
        for value in (self.task_head_sha256, self.destination_head_sha256):
            if (
                not isinstance(value, str)
                or len(value) != 71
                or not value.startswith("sha256:")
                or any(character not in _GIT_HEAD for character in value[7:])
            ):
                raise ValidationError("Bridge Git observation digest 无效")
        if type(self.is_ancestor) is not bool:
            raise ValidationError("Bridge Git ancestry result 无效")


@dataclass(frozen=True)
class GitReadInvocation:
    executable: str
    argv: tuple[str, ...]
    cwd: Path
    directory_fd: int | None
    git_dir_fd: int | None
    common_dir_fd: int | None
    object_dir_fd: int | None
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        executable = Path(self.executable)
        if not executable.is_absolute() or executable.name != "git":
            raise ValidationError("Bridge Git executable 必须是绝对 git 路径")
        if not _arguments_are_allowlisted(self.argv):
            raise ValidationError("Bridge Git argv 不在只读 allowlist")
        if not self.cwd.is_absolute():
            raise ValidationError("Bridge Git cwd 必须是绝对路径")
        descriptors = (
            self.directory_fd,
            self.git_dir_fd,
            self.common_dir_fd,
            self.object_dir_fd,
        )
        metadata_present = tuple(
            descriptor is not None for descriptor in descriptors[1:]
        )
        if any(metadata_present) and (
            not all(metadata_present) or self.directory_fd is None
        ):
            raise ValidationError("Bridge Git metadata fd 必须完整绑定")
        for descriptor in descriptors:
            if descriptor is None:
                continue
            if type(descriptor) is not int or descriptor < 0:
                raise ValidationError("Bridge Git directory fd 无效")
            try:
                directory_info = os.fstat(descriptor)
            except OSError as exc:
                raise ValidationError("Bridge Git directory fd 无效") from exc
            if not stat.S_ISDIR(directory_info.st_mode):
                raise ValidationError("Bridge Git directory fd 必须引用目录")
        if not _environment_is_allowlisted(
            dict(self.environment), descriptors=descriptors
        ):
            raise ValidationError("Bridge Git environment 不符合 fail-closed 配置")
        if not 0 < self.timeout_seconds <= 5:
            raise ValidationError("Bridge Git timeout 必须位于 (0, 5] 秒")


def _validate_head(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in _GIT_HEAD for character in value)
    ):
        raise ValidationError("Bridge Git commit ID 必须是 40 位小写十六进制")
    return value


def _arguments_are_allowlisted(argv: Sequence[str]) -> bool:
    arguments = tuple(argv)
    if arguments == _RESOLVE_ARGUMENTS:
        return True
    if len(arguments) != len(_ANCESTRY_ARGUMENTS) + 2:
        return False
    if arguments[: len(_ANCESTRY_ARGUMENTS)] != _ANCESTRY_ARGUMENTS:
        return False
    try:
        _validate_head(arguments[-2])
        _validate_head(arguments[-1])
    except ValidationError:
        return False
    return True


def _oid_digest(value: str) -> str:
    head = _validate_head(value)
    payload = b"dyro.git-oid.v1\0" + head.encode("ascii")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def git_read_environment(
    repository: Path | None = None,
    *,
    directory_fd: int | None = None,
    git_dir_fd: int | None = None,
    common_dir_fd: int | None = None,
    object_dir_fd: int | None = None,
) -> dict[str, str]:
    """Return the complete minimal environment inherited by Bridge Git reads."""
    environment = {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if repository is not None:
        environment["GIT_CEILING_DIRECTORIES"] = str(repository.absolute().parent)
        environment["GIT_DISCOVERY_ACROSS_FILESYSTEM"] = "0"
    descriptors = (directory_fd, git_dir_fd, common_dir_fd, object_dir_fd)
    metadata_descriptors = descriptors[1:]
    if any(descriptor is not None for descriptor in metadata_descriptors):
        if repository is None or not all(
            descriptor is not None for descriptor in descriptors
        ):
            raise ValidationError("Bridge Git metadata binding is incomplete")
        worktree_fd, metadata_fd, common_fd, objects_fd = descriptors
        environment["GIT_DIR"] = str(_LINUX_FD_ROOT / str(metadata_fd))
        environment["GIT_COMMON_DIR"] = str(_LINUX_FD_ROOT / str(common_fd))
        environment["GIT_OBJECT_DIRECTORY"] = str(_LINUX_FD_ROOT / str(objects_fd))
        environment["GIT_WORK_TREE"] = str(_LINUX_FD_ROOT / str(worktree_fd))
    return environment


def _environment_is_allowlisted(
    environment: Mapping[str, str],
    *,
    descriptors: tuple[int | None, int | None, int | None, int | None],
) -> bool:
    fixed = git_read_environment()
    if any(environment.get(key) != value for key, value in fixed.items()):
        return False
    dynamic = set(environment) - set(fixed)
    discovery = {"GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM"}
    metadata = {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    }
    if frozenset(dynamic) not in {
        frozenset(discovery),
        frozenset(discovery | metadata),
    }:
        return False
    if environment.get("GIT_DISCOVERY_ACROSS_FILESYSTEM") != "0":
        return False
    if frozenset(dynamic) == frozenset(discovery):
        return all(descriptor is None for descriptor in descriptors[1:])
    if not all(descriptor is not None for descriptor in descriptors):
        return False
    expected = git_read_environment(
        Path("/"),
        directory_fd=descriptors[0],
        git_dir_fd=descriptors[1],
        common_dir_fd=descriptors[2],
        object_dir_fd=descriptors[3],
    )
    for key in metadata:
        if environment.get(key) != expected[key]:
            return False
    return True


def _resolve_executable(executable: str | None) -> str:
    selected = (
        Path(executable)
        if executable is not None
        else next((path for path in _SYSTEM_GIT_CANDIDATES if path.exists()), None)
    )
    if selected is None:
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    try:
        resolved = selected.resolve(strict=True)
        info = resolved.stat()
    except PermissionError as exc:
        raise GitReadError(GitReadFailure.PERMISSION) from exc
    except OSError as exc:
        raise GitReadError(GitReadFailure.UNAVAILABLE) from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise GitReadError(GitReadFailure.PERMISSION)
    if executable is None and (
        info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    return str(resolved)


def _resolve_system_python() -> str:
    selected = next((path for path in _SYSTEM_PYTHON_CANDIDATES if path.exists()), None)
    if selected is None:
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    try:
        resolved = selected.resolve(strict=True)
        info = resolved.stat()
    except PermissionError as exc:
        raise GitReadError(GitReadFailure.PERMISSION) from exc
    except OSError as exc:
        raise GitReadError(GitReadFailure.UNAVAILABLE) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or not os.access(resolved, os.X_OK)
        or info.st_uid != 0
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    return str(resolved)


def _invocation(
    repository: Path,
    argv: tuple[str, ...],
    *,
    executable: str | None,
    directory_fd: int | None,
    git_dir_fd: int | None,
    common_dir_fd: int | None,
    object_dir_fd: int | None,
    timeout_seconds: float,
) -> GitReadInvocation:
    return GitReadInvocation(
        executable=_resolve_executable(executable),
        argv=argv,
        cwd=repository.absolute(),
        directory_fd=directory_fd,
        git_dir_fd=git_dir_fd,
        common_dir_fd=common_dir_fd,
        object_dir_fd=object_dir_fd,
        environment=tuple(
            sorted(
                git_read_environment(
                    repository,
                    directory_fd=directory_fd,
                    git_dir_fd=git_dir_fd,
                    common_dir_fd=common_dir_fd,
                    object_dir_fd=object_dir_fd,
                ).items()
            )
        ),
        timeout_seconds=timeout_seconds,
    )


def build_head_invocation(
    repository: Path,
    *,
    executable: str | None = None,
    directory_fd: int | None = None,
    git_dir_fd: int | None = None,
    common_dir_fd: int | None = None,
    object_dir_fd: int | None = None,
    timeout_seconds: float = 3.0,
) -> GitReadInvocation:
    return _invocation(
        repository,
        _RESOLVE_ARGUMENTS,
        executable=executable,
        directory_fd=directory_fd,
        git_dir_fd=git_dir_fd,
        common_dir_fd=common_dir_fd,
        object_dir_fd=object_dir_fd,
        timeout_seconds=timeout_seconds,
    )


def build_ancestor_invocation(
    repository: Path,
    ancestor: str,
    destination_head: str,
    *,
    executable: str | None = None,
    directory_fd: int | None = None,
    git_dir_fd: int | None = None,
    common_dir_fd: int | None = None,
    object_dir_fd: int | None = None,
    timeout_seconds: float = 3.0,
) -> GitReadInvocation:
    return _invocation(
        repository,
        (
            *_ANCESTRY_ARGUMENTS,
            _validate_head(ancestor),
            _validate_head(destination_head),
        ),
        executable=executable,
        directory_fd=directory_fd,
        git_dir_fd=git_dir_fd,
        common_dir_fd=common_dir_fd,
        object_dir_fd=object_dir_fd,
        timeout_seconds=timeout_seconds,
    )


def _run_descriptor_bound(
    invocation: GitReadInvocation,
    *,
    capture_stdout: bool,
) -> subprocess.CompletedProcess[bytes]:
    """Use an isolated helper to bind cwd by descriptor before execing Git."""
    directory_fd = invocation.directory_fd
    if directory_fd is None:
        raise ValidationError("Bridge Git execution requires a bound directory fd")
    error_read_fd, error_write_fd = os.pipe()
    os.set_blocking(error_read_fd, False)
    helper_argv = build_helper_invocation(invocation, error_fd=error_write_fd)
    descriptors = tuple(
        descriptor
        for descriptor in (
            invocation.directory_fd,
            invocation.git_dir_fd,
            invocation.common_dir_fd,
            invocation.object_dir_fd,
            error_write_fd,
        )
        if descriptor is not None
    )
    try:
        process = subprocess.Popen(
            helper_argv,
            cwd=None,
            env=dict(invocation.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=descriptors,
        )
    except PermissionError as exc:
        os.close(error_read_fd)
        os.close(error_write_fd)
        raise GitReadError(GitReadFailure.PERMISSION) from exc
    except FileNotFoundError as exc:
        os.close(error_read_fd)
        os.close(error_write_fd)
        raise GitReadError(GitReadFailure.UNAVAILABLE) from exc
    except OSError as exc:
        os.close(error_read_fd)
        os.close(error_write_fd)
        raise GitReadError(GitReadFailure.PARTIAL) from exc
    os.close(error_write_fd)
    stream = process.stdout
    read_fd = None if stream is None else stream.fileno()
    if read_fd is not None:
        os.set_blocking(read_fd, False)
    deadline = time.monotonic() + invocation.timeout_seconds
    output = bytearray()
    try:
        while process.poll() is None:
            if read_fd is not None:
                ready, _, _ = select.select((read_fd,), (), (), 0)
                if ready:
                    chunk = os.read(read_fd, 4096)
                    output.extend(chunk)
                    if len(output) > _MAX_OID_OUTPUT_BYTES:
                        _terminate_and_reap(process)
                        raise GitReadError(GitReadFailure.PARTIAL)
            _raise_binder_error(error_read_fd)
            if time.monotonic() >= deadline:
                _terminate_and_reap(process)
                raise GitReadError(GitReadFailure.TIMEOUT)
            time.sleep(0.005)
        if read_fd is not None:
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > _MAX_OID_OUTPUT_BYTES:
                    raise GitReadError(GitReadFailure.PARTIAL)
        _raise_binder_error(error_read_fd)
    finally:
        if process.poll() is None:
            _terminate_and_reap(process)
        if stream is not None:
            stream.close()
        os.close(error_read_fd)
    return_code = process.returncode
    if return_code is None:
        raise GitReadError(GitReadFailure.PARTIAL)
    return subprocess.CompletedProcess(
        (invocation.executable, *invocation.argv),
        return_code,
        stdout=bytes(output) if capture_stdout else None,
        stderr=None,
    )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    while True:
        try:
            process.wait()
            return
        except InterruptedError:
            continue


def _raise_binder_error(error_fd: int) -> None:
    try:
        code = os.read(error_fd, 1)
    except BlockingIOError:
        return
    if not code:
        return
    failure = {
        b"P": GitReadFailure.PERMISSION,
        b"U": GitReadFailure.UNAVAILABLE,
        b"X": GitReadFailure.PARTIAL,
    }.get(code, GitReadFailure.PARTIAL)
    raise GitReadError(failure)


def build_helper_invocation(
    invocation: GitReadInvocation, *, error_fd: int = -1
) -> tuple[str, ...]:
    directory_fd = invocation.directory_fd
    if directory_fd is None:
        raise ValidationError("Bridge Git helper requires a bound directory fd")
    python = _resolve_system_python()
    return (
        python,
        *_HELPER_PREFIX,
        str(directory_fd),
        str(invocation.git_dir_fd if invocation.git_dir_fd is not None else -1),
        str(invocation.common_dir_fd if invocation.common_dir_fd is not None else -1),
        str(invocation.object_dir_fd if invocation.object_dir_fd is not None else -1),
        str(error_fd),
        invocation.executable,
        *invocation.argv,
    )


def helper_invocation_is_allowlisted(
    argv: Sequence[str], environment: Mapping[str, str]
) -> bool:
    arguments = tuple(argv)
    if len(arguments) <= len(_HELPER_PREFIX) + 7:
        return False
    python, *rest = arguments
    if not Path(python).is_absolute() or tuple(rest[:4]) != _HELPER_PREFIX:
        return False
    try:
        descriptors = tuple(int(value) for value in rest[4:9])
    except (TypeError, ValueError):
        return False
    if descriptors[0] < 0 or any(value < -1 for value in descriptors[1:]):
        return False
    bound = _descriptors_from_environment(environment)
    if bound is not None and tuple(descriptors[:4]) != bound:
        return False
    return invocation_is_allowlisted(rest[9:], environment)


def _run(
    invocation: GitReadInvocation,
    *,
    capture_stdout: bool,
    runner: Callable[[GitReadInvocation, bool], subprocess.CompletedProcess[bytes]]
    | None,
) -> subprocess.CompletedProcess[bytes]:
    if runner is not None:
        return runner(invocation, capture_stdout)
    return _run_descriptor_bound(invocation, capture_stdout=capture_stdout)


def _parse_resolved_head(completed: subprocess.CompletedProcess[bytes]) -> str:
    if completed.returncode != 0:
        raise GitReadError(GitReadFailure.PARTIAL)
    output = completed.stdout
    if not isinstance(output, bytes) or len(output) > _MAX_OID_OUTPUT_BYTES:
        raise GitReadError(GitReadFailure.PARTIAL)
    try:
        value = output.decode("ascii").strip()
        return _validate_head(value)
    except (UnicodeError, ValidationError) as exc:
        raise GitReadError(GitReadFailure.PARTIAL) from exc


def inspect_ancestry_readonly(
    repository: Path,
    ancestor: str,
    *,
    executable: str | None = None,
    directory_fd: int | None = None,
    git_dir_fd: int | None = None,
    common_dir_fd: int | None = None,
    object_dir_fd: int | None = None,
    timeout_seconds: float = 3.0,
    runner: Callable[[GitReadInvocation, bool], subprocess.CompletedProcess[bytes]]
    | None = None,
) -> GitAncestryObservation:
    """Bind HEAD and ancestry to one verified directory object without Git writes."""
    if os.name != "posix":
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    production_read = runner is None and executable is None
    metadata_fds = (directory_fd, git_dir_fd, common_dir_fd, object_dir_fd)
    if production_read and (
        not sys.platform.startswith("linux")
        or not _LINUX_FD_ROOT.is_dir()
        or not all(descriptor is not None for descriptor in metadata_fds)
    ):
        raise GitReadError(GitReadFailure.UNAVAILABLE)
    owned_fd: int | None = None
    try:
        if directory_fd is None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                owned_fd = os.open(repository, flags)
            except PermissionError as exc:
                raise GitReadError(GitReadFailure.PERMISSION) from exc
            except OSError as exc:
                raise GitReadError(GitReadFailure.PARTIAL) from exc
            directory_fd = owned_fd
        started_at = time.monotonic()
        resolved = _parse_resolved_head(
            _run(
                build_head_invocation(
                    repository,
                    executable=executable,
                    directory_fd=directory_fd,
                    git_dir_fd=git_dir_fd,
                    common_dir_fd=common_dir_fd,
                    object_dir_fd=object_dir_fd,
                    timeout_seconds=timeout_seconds,
                ),
                capture_stdout=True,
                runner=runner,
            )
        )
        remaining = timeout_seconds - (time.monotonic() - started_at)
        if remaining <= 0:
            raise GitReadError(GitReadFailure.TIMEOUT)
        ancestry = _run(
            build_ancestor_invocation(
                repository,
                ancestor,
                resolved,
                executable=executable,
                directory_fd=directory_fd,
                git_dir_fd=git_dir_fd,
                common_dir_fd=common_dir_fd,
                object_dir_fd=object_dir_fd,
                timeout_seconds=remaining,
            ),
            capture_stdout=False,
            runner=runner,
        )
        if ancestry.returncode not in {0, 1}:
            raise GitReadError(GitReadFailure.PARTIAL)
        return GitAncestryObservation(
            task_head_sha256=_oid_digest(ancestor),
            destination_head_sha256=_oid_digest(resolved),
            is_ancestor=ancestry.returncode == 0,
        )
    finally:
        if owned_fd is not None:
            os.close(owned_fd)


def is_ancestor_readonly(
    repository: Path,
    ancestor: str,
    **kwargs: object,
) -> bool:
    """Compatibility facade returning only the ancestry decision."""
    return inspect_ancestry_readonly(repository, ancestor, **kwargs).is_ancestor


def invocation_is_allowlisted(
    argv: Sequence[str], environment: Mapping[str, str]
) -> bool:
    """Support black-box process traps without exposing runtime path details."""
    if not argv:
        return False
    executable, *arguments = argv
    descriptors = _descriptors_from_environment(environment)
    if descriptors is None:
        descriptors = (None, None, None, None)
    try:
        GitReadInvocation(
            executable=executable,
            argv=tuple(arguments),
            cwd=Path("/"),
            directory_fd=descriptors[0],
            git_dir_fd=descriptors[1],
            common_dir_fd=descriptors[2],
            object_dir_fd=descriptors[3],
            environment=tuple(sorted(environment.items())),
            timeout_seconds=3.0,
        )
    except (OSError, ValidationError):
        return False
    return True


def _descriptors_from_environment(
    environment: Mapping[str, str],
) -> tuple[int, int, int, int] | None:
    keys = (
        "GIT_WORK_TREE",
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    )
    present = tuple(key in environment for key in keys)
    if not any(present):
        return None
    if not all(present):
        return None
    result: list[int] = []
    for key in keys:
        path = Path(environment[key])
        if path.parent != _LINUX_FD_ROOT:
            return None
        try:
            descriptor = int(path.name)
        except ValueError:
            return None
        if descriptor < 0:
            return None
        result.append(descriptor)
    return tuple(result)  # type: ignore[return-value]
