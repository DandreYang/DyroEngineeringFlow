"""Optional host-mounted provider binary with path allowlist + content pin.

The binary is visible only to the Agent Broker container (read-only bind).
The Workflow Sandbox never receives the host path, provider token, or argv.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Mapping, Sequence

from ..errors import Stage0ValidationError


CONTAINER_PROVIDER_DIR = "/opt/host-provider"
CONTAINER_PROVIDER_NAME = "provider_cli"


@dataclass(frozen=True)
class HostProviderPin:
    """
    Integrity pin for a host-side provider CLI (fixture stand-in or real binary).

    Real Codex/Claude binaries can be pointed here in operator environments; the
    experiment suite uses a host fixture script so CI remains deterministic.
    """

    host_path: Path
    content_sha256: str
    argv: tuple[str, ...]
    allowed_roots: tuple[Path, ...]
    container_path: str = f"{CONTAINER_PROVIDER_DIR}/{CONTAINER_PROVIDER_NAME}"

    def __post_init__(self) -> None:
        path = Path(self.host_path)
        if not path.is_absolute():
            raise Stage0ValidationError("host provider path must be absolute")
        if path.is_symlink():
            raise Stage0ValidationError("host provider path must not be a symlink")
        if not path.is_file():
            raise Stage0ValidationError(f"host provider missing: {path}")
        if (
            type(self.content_sha256) is not str
            or len(self.content_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.content_sha256)
        ):
            raise Stage0ValidationError("host provider content_sha256 must be 64 hex")
        if not self.argv or any(
            not isinstance(part, str) or not part or "\n" in part or "\0" in part
            for part in self.argv
        ):
            raise Stage0ValidationError("host provider argv must be non-empty tokens")
        if any("," in part for part in self.argv):
            raise Stage0ValidationError(
                "host provider argv tokens must not contain commas"
            )
        if not self.container_path.startswith(CONTAINER_PROVIDER_DIR + "/"):
            raise Stage0ValidationError(
                "host provider container_path must stay under /opt/host-provider"
            )
        if ".." in self.container_path:
            raise Stage0ValidationError("host provider container_path is invalid")
        if not self.allowed_roots:
            raise Stage0ValidationError("host provider requires allowed_roots")

    def assert_under_allowlist(self) -> Path:
        resolved = Path(self.host_path).resolve()
        for root in self.allowed_roots:
            root_resolved = Path(root).resolve()
            try:
                resolved.relative_to(root_resolved)
                return resolved
            except ValueError:
                continue
        raise Stage0ValidationError(
            "host provider path is outside allowed_roots: "
            f"{resolved} not under {tuple(str(r) for r in self.allowed_roots)}"
        )

    def verify(self) -> Path:
        path = self.assert_under_allowlist()
        # Refuse world-writable host binaries.
        mode = path.stat().st_mode
        if mode & 0o002:
            raise Stage0ValidationError("host provider must not be world-writable")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != self.content_sha256:
            raise Stage0ValidationError(
                "host provider content_sha256 mismatch "
                f"(expected {self.content_sha256}, got {digest})"
            )
        return path

    def argv_csv(self) -> str:
        return ",".join(self.argv)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source": "host",
            "host_path_basename": Path(self.host_path).name,
            "content_sha256": self.content_sha256,
            "argv": list(self.argv),
            "container_path": self.container_path,
            "invocation": "argv-only",
            "sandbox_visibility": False,
        }


def hash_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pin_host_provider(
    host_path: Path,
    *,
    allowed_roots: Sequence[Path],
    argv: Sequence[str] | None = None,
    container_path: str = f"{CONTAINER_PROVIDER_DIR}/{CONTAINER_PROVIDER_NAME}",
) -> HostProviderPin:
    host_path = Path(host_path)
    tokens = (
        tuple(argv)
        if argv is not None
        else ("bun", container_path)
    )
    return HostProviderPin(
        host_path=host_path,
        content_sha256=hash_file(host_path),
        argv=tokens,
        allowed_roots=tuple(Path(r) for r in allowed_roots),
        container_path=container_path,
    )


def write_host_fixture_cli(destination: Path) -> Path:
    """Deterministic host-side fixture used when no real provider binary exists."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            [
                "/**",
                " * Stage 5 host-mounted provider fixture.",
                " * Broker-only; never mounted into the Workflow Sandbox.",
                " */",
                'const prompt = process.argv.slice(2).join(" ") || "empty-prompt";',
                'const token = process.env.DYRO_PROVIDER_FAKE_TOKEN ?? "missing-token";',
                "process.stdout.write(",
                "  [",
                '    "BEGIN PRIVATE KEY",',
                '    "RAW_VENDOR_SECRET_MUST_NOT_ESCAPE",',
                "    `token=${token}`,",
                "    `prompt=${prompt}`,",
                '    "sk-stage5-host-cli-token",',
                '    "final:stage5-host-cli-summary",',
                '    "",',
                '  ].join("\\n"),',
                ");",
                "process.stderr.write(",
                '  ["RAW_VENDOR_STDERR_MARKER", "host-cli diagnostics", ""].join("\\n"),',
                ");",
                "process.exit(0);",
                "",
            ]
        ),
        encoding="utf-8",
    )
    os.chmod(destination, 0o644)
    return destination


def assert_host_pin_in_identity(
    identity: Mapping[str, object], pin: HostProviderPin
) -> None:
    section = identity.get("host_provider")
    if not isinstance(section, Mapping):
        raise Stage0ValidationError("bundle identity missing host_provider pin")
    if (
        section.get("content_sha256") != pin.content_sha256
        or section.get("container_path") != pin.container_path
        or tuple(section.get("argv") or ()) != pin.argv
        or section.get("source") != "host"
        or section.get("sandbox_visibility") is not False
    ):
        raise Stage0ValidationError("bundle identity host_provider pin mismatch")
