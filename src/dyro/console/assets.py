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
        "4018afa6a9bfa3b694fa7c1c8cbc2fec0b31a9594691c5fe9ca123ad519ea389",
        3040,
    ),
    "app.js": (
        "text/javascript; charset=utf-8",
        "3ce02999da246edc48caceca5a23bc258478e53c333c4bfb151fb06a6bcba7d2",
        15196,
    ),
    "styles.css": (
        "text/css; charset=utf-8",
        "c622a5eda03264a2495f15830a8a061308a3bed6235c6425155b8995eb5069b9",
        10178,
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
