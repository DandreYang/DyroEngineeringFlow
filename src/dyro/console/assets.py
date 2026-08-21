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
        "ca77441bfa6fce59c5a015c67d5843003017083392d69f035a2e628ca7388158",
        3795,
    ),
    "app.js": (
        "text/javascript; charset=utf-8",
        "80ea029462b4c8231b7d0e1e87b29348aaff6d26e8683d8c6afd13921958ef53",
        91406,
    ),
    "styles.css": (
        "text/css; charset=utf-8",
        "6752c01db706e12fdcd768a4974916f9586299e4312e9e01db0f29e762b40f78",
        24441,
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
