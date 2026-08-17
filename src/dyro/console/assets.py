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
        "76a17597706397bad43a0ba84a121e625abb70e30f2f2cf42f61521308cb44f2",
        3138,
    ),
    "app.js": (
        "text/javascript; charset=utf-8",
        "7e546606308a9ea85169c2b938b54d81b20018614ca96980615f123fc6675167",
        22848,
    ),
    "styles.css": (
        "text/css; charset=utf-8",
        "1b295330e8a23907a2b48b00778a9ab5a59e4b6af10e691cf6d3113c8d1d2366",
        11333,
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
