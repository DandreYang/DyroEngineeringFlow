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
        "eb0c18e8cf20df27c21a9ba8f309ffb66ac3f258371c5b450abd48ac5cf7fba5",
        3812,
    ),
    "app.js": (
        "text/javascript; charset=utf-8",
        "194ca77cdfac96539dd391f43d6d14a5e15bf594ec21a280c886a21536124c23",
        60441,
    ),
    "styles.css": (
        "text/css; charset=utf-8",
        "810b196f7e72418d7253c1f2aa490f82fbd51a98a12d2db2d51e8303b4632f07",
        15492,
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
