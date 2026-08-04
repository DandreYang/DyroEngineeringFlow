"""Verified package resources for the offline local Console shell."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files


@dataclass(frozen=True)
class ConsoleAsset:
    name: str
    content_type: str
    sha256: str
    size: int
    body: bytes


# The values are generated from the checked-in resources.  The server validates
# every byte before listening so it never silently falls back to cwd or source
# tree files when a packaged asset is missing or has drifted.
ASSET_MANIFEST = {
    "index.html": (
        "text/html; charset=utf-8",
        "8980f489da7e7fbd6c6023fc3338c7ff768e14dfade2c13d6b53b69f6304821a",
        1949,
    ),
    "app.js": (
        "text/javascript; charset=utf-8",
        "034405c85e20d7fbcde3d07b0bf0a29abf2c20c9ce5be16b579b48561d69965f",
        10484,
    ),
    "styles.css": (
        "text/css; charset=utf-8",
        "4759e04327b0a29d0bcc039d09d64f633670014ecf7fe111244ec78f60d091af",
        4842,
    ),
}


class ConsoleAssetError(RuntimeError):
    """A packaged static asset is missing, malformed, or has unexpected bytes."""


def load_asset(name: str) -> ConsoleAsset:
    if name not in ASSET_MANIFEST:
        raise ConsoleAssetError("unknown Console asset")
    content_type, expected_sha256, expected_size = ASSET_MANIFEST[name]
    resource = files("dyro.console").joinpath("assets", name)
    if not resource.is_file():
        raise ConsoleAssetError("missing Console asset")
    try:
        body = resource.read_bytes()
    except OSError as exc:
        raise ConsoleAssetError("unreadable Console asset") from exc
    digest = hashlib.sha256(body).hexdigest()
    if (
        not expected_sha256
        or expected_size <= 0
        or len(body) != expected_size
        or digest != expected_sha256
    ):
        raise ConsoleAssetError("invalid Console asset manifest")
    return ConsoleAsset(name, content_type, digest, len(body), body)


def validate_assets() -> None:
    """Fail closed unless every public resource matches the fixed manifest."""
    for name in ASSET_MANIFEST:
        load_asset(name)
