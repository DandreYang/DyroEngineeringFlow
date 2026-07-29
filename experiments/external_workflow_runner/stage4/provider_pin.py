"""Allowlisted provider CLI argv with content-hash integrity pin."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Mapping, Sequence

from ..errors import Stage0ValidationError


DEFAULT_PROVIDER_RELATIVE = "fake_provider_cli.ts"
DEFAULT_ARGV_TEMPLATE = ("bun", "/opt/workflow/fake_provider_cli.ts")


@dataclass(frozen=True)
class ProviderBinaryPin:
    """
    Integrity pin for an optional provider binary (fixture or real CLI).

    The Broker may only spawn this argv after verifying the executable-or-script
    content SHA-256. No shell concatenation; argv is fixed tokens only.
    """

    relative_path: str
    content_sha256: str
    argv: tuple[str, ...]
    allow_real_binary: bool = False

    def __post_init__(self) -> None:
        if not self.relative_path or ".." in Path(self.relative_path).parts:
            raise Stage0ValidationError("provider pin relative_path is invalid")
        if (
            type(self.content_sha256) is not str
            or len(self.content_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.content_sha256)
        ):
            raise Stage0ValidationError("provider pin content_sha256 must be 64 hex")
        if not self.argv or any(
            not isinstance(part, str) or not part or "\n" in part or "\0" in part
            for part in self.argv
        ):
            raise Stage0ValidationError("provider pin argv must be non-empty tokens")
        if any("," in part for part in self.argv):
            raise Stage0ValidationError(
                "provider pin argv tokens must not contain commas (env transport)"
            )

    def resolve(self, bundle_root: Path) -> Path:
        path = (Path(bundle_root) / self.relative_path).resolve()
        root = Path(bundle_root).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise Stage0ValidationError(
                "provider pin path escapes bundle root"
            ) from exc
        if not path.is_file():
            raise Stage0ValidationError(f"provider binary missing: {self.relative_path}")
        return path

    def verify(self, bundle_root: Path) -> Path:
        path = self.resolve(bundle_root)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.content_sha256:
            raise Stage0ValidationError(
                "provider binary content_sha256 mismatch "
                f"(expected {self.content_sha256}, got {digest})"
            )
        return path

    def argv_csv(self) -> str:
        return ",".join(self.argv)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "relative_path": self.relative_path,
            "content_sha256": self.content_sha256,
            "argv": list(self.argv),
            "allow_real_binary": self.allow_real_binary,
            "invocation": "argv-only",
        }


def hash_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pin_from_bundle_fixture(
    bundle_root: Path,
    *,
    relative_path: str = DEFAULT_PROVIDER_RELATIVE,
    argv: Sequence[str] | None = None,
    allow_real_binary: bool = False,
) -> ProviderBinaryPin:
    path = Path(bundle_root) / relative_path
    if not path.is_file():
        raise Stage0ValidationError(f"cannot pin missing provider fixture: {relative_path}")
    tokens = tuple(argv) if argv is not None else DEFAULT_ARGV_TEMPLATE
    return ProviderBinaryPin(
        relative_path=relative_path,
        content_sha256=hash_file(path),
        argv=tokens,
        allow_real_binary=allow_real_binary,
    )


def assert_pin_in_identity(
    identity: Mapping[str, object], pin: ProviderBinaryPin
) -> None:
    section = identity.get("provider_cli")
    if not isinstance(section, Mapping):
        raise Stage0ValidationError("bundle identity missing provider_cli pin")
    if (
        section.get("content_sha256") != pin.content_sha256
        or section.get("relative_path") != pin.relative_path
        or tuple(section.get("argv") or ()) != pin.argv
        or section.get("invocation") != "argv-only"
    ):
        raise Stage0ValidationError("bundle identity provider_cli pin mismatch")
