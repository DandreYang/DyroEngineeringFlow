from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping

from .errors import ValidationError


CURRENT_EVIDENCE_FILE = "current-evidence.json"
EVIDENCE_GENERATIONS_DIR = "evidence-imports"
MANIFEST_FILE = "manifest.json"
GENERATION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_generation_id(generation_id: str) -> str:
    if not GENERATION_PATTERN.fullmatch(generation_id):
        raise ValidationError(f"证据 generation ID 无效：{generation_id!r}")
    return generation_id


def _validate_relative_path(relative: str | Path) -> Path:
    raw = relative.as_posix() if isinstance(relative, Path) else relative
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise ValidationError(f"证据相对路径无效：{raw!r}")
    return Path(*pure.parts)


def _regular_file_bytes(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"证据文件不存在：{path}") from None
    if not stat.S_ISREG(mode):
        raise ValidationError(f"证据文件必须是普通文件且不能是符号链接：{path}")
    return path.read_bytes()


def _load_manifest(directory: Path, generation_id: str) -> tuple[bytes, dict[str, object]]:
    manifest_path = directory / MANIFEST_FILE
    content = _regular_file_bytes(manifest_path)
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"证据世代 manifest 损坏：{manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("generation") != generation_id
        or not isinstance(manifest.get("files"), dict)
    ):
        raise ValidationError(f"证据世代 manifest 格式无效：{manifest_path}")
    return content, manifest


def current_evidence_directory(task_directory: Path) -> Path | None:
    pointer_path = task_directory / CURRENT_EVIDENCE_FILE
    if not pointer_path.exists() and not pointer_path.is_symlink():
        return None
    pointer_bytes = _regular_file_bytes(pointer_path)
    try:
        pointer = json.loads(pointer_bytes)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"当前证据指针损坏：{pointer_path}") from exc
    if (
        not isinstance(pointer, dict)
        or pointer.get("schema_version") != 1
        or not isinstance(pointer.get("generation"), str)
        or not isinstance(pointer.get("manifest_sha256"), str)
    ):
        raise ValidationError(f"当前证据指针格式无效：{pointer_path}")
    generation_id = _validate_generation_id(str(pointer["generation"]))
    directory = task_directory / EVIDENCE_GENERATIONS_DIR / generation_id
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        raise ValidationError(f"当前证据世代不存在：{directory}") from None
    if not stat.S_ISDIR(mode):
        raise ValidationError(f"当前证据世代必须是普通目录且不能是符号链接：{directory}")
    manifest_bytes, _ = _load_manifest(directory, generation_id)
    if hashlib.sha256(manifest_bytes).hexdigest() != pointer["manifest_sha256"]:
        raise ValidationError(f"当前证据世代 manifest 哈希不匹配：{directory}")
    return directory


def _verified_generation_path(directory: Path, relative: Path) -> Path:
    generation_id = directory.name
    _, manifest = _load_manifest(directory, generation_id)
    files = manifest["files"]
    assert isinstance(files, dict)
    key = relative.as_posix()
    entry = files.get(key)
    path = directory / relative
    if entry is None:
        return path
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("sha256"), str)
        or isinstance(entry.get("size"), bool)
        or not isinstance(entry.get("size"), int)
    ):
        raise ValidationError(f"证据世代 manifest 文件条目无效：{key}")
    content = _regular_file_bytes(path)
    if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
        raise ValidationError(f"不可变证据文件哈希不匹配：{path}")
    return path


def resolve_evidence_path(task_directory: Path, relative: str | Path) -> Path:
    relative_path = _validate_relative_path(relative)
    directory = current_evidence_directory(task_directory)
    if directory is None:
        return task_directory / relative_path
    return _verified_generation_path(directory, relative_path)


def iter_generation_artifacts(
    task_directory: Path,
    relative_directory: str | Path,
    *,
    suffix: str = "",
) -> Iterator[Path]:
    prefix = _validate_relative_path(relative_directory)
    generations = task_directory / EVIDENCE_GENERATIONS_DIR
    if not generations.is_dir():
        return
    for directory in sorted(generations.iterdir(), key=lambda item: item.name):
        if directory.name.startswith(".") or not GENERATION_PATTERN.fullmatch(directory.name):
            continue
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ValidationError(f"证据世代必须是普通目录且不能是符号链接：{directory}")
        _, manifest = _load_manifest(directory, directory.name)
        files = manifest["files"]
        assert isinstance(files, dict)
        for raw in sorted(files):
            relative = _validate_relative_path(raw)
            if relative.parent != prefix or (suffix and relative.suffix != suffix):
                continue
            yield _verified_generation_path(directory, relative)


def _write_file(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, mode)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_real_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            raise ValidationError(f"证据目录状态发生并发变化：{path}") from None
        if not stat.S_ISDIR(mode):
            raise ValidationError(f"证据目录必须是普通目录且不能是符号链接：{path}")
        return
    path.mkdir(parents=True, mode=0o700)


def _publish_pointer(task_directory: Path, generation_id: str, manifest_bytes: bytes) -> None:
    pointer = {
        "schema_version": 1,
        "generation": generation_id,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    content = (json.dumps(pointer, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".current-evidence.", dir=task_directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, task_directory / CURRENT_EVIDENCE_FILE)
        _fsync_directory(task_directory)
    finally:
        temporary.unlink(missing_ok=True)


def publish_evidence_generation(
    task_directory: Path,
    generation_id: str,
    files: Mapping[str | Path, bytes],
) -> Path:
    generation_id = _validate_generation_id(generation_id)
    normalized: dict[Path, bytes] = {}
    for relative, content in files.items():
        path = _validate_relative_path(relative)
        if path == Path(MANIFEST_FILE):
            raise ValidationError(f"{MANIFEST_FILE} 由证据存储层管理")
        if path in normalized:
            raise ValidationError(f"证据世代包含重复路径：{path.as_posix()}")
        normalized[path] = bytes(content)
    if not normalized:
        raise ValidationError("证据世代不能为空")

    generations = task_directory / EVIDENCE_GENERATIONS_DIR
    _ensure_real_directory(generations)
    final = generations / generation_id
    manifest = {
        "schema_version": 1,
        "generation": generation_id,
        "files": {
            path.as_posix(): {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
            for path, content in sorted(normalized.items(), key=lambda item: item[0].as_posix())
        },
    }
    manifest_bytes = (_canonical_json(manifest) + b"\n")

    if final.exists() or final.is_symlink():
        try:
            final_mode = final.lstat().st_mode
        except FileNotFoundError:
            raise ValidationError(f"证据 generation 状态发生并发变化：{generation_id}") from None
        if not stat.S_ISDIR(final_mode):
            raise ValidationError(f"证据 generation 必须是普通目录且不能是符号链接：{generation_id}")
        existing_manifest, existing = _load_manifest(final, generation_id)
        if _canonical_json(existing) != _canonical_json(manifest):
            raise ValidationError(f"证据 generation 已存在但内容不同：{generation_id}")
        for relative in normalized:
            _verified_generation_path(final, relative)
        _publish_pointer(task_directory, generation_id, existing_manifest)
        return final

    temporary = generations / f".{generation_id}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        for relative, content in normalized.items():
            _write_file(temporary / relative, content)
        _write_file(temporary / MANIFEST_FILE, manifest_bytes)
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(temporary)
        os.rename(temporary, final)
        _fsync_directory(generations)
        for path in final.rglob("*"):
            os.chmod(path, 0o500 if path.is_dir() else 0o400)
        os.chmod(final, 0o500)
        _publish_pointer(task_directory, generation_id, manifest_bytes)
        return final
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise
