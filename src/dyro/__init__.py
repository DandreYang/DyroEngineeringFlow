"""DyroEngineeringFlow: the dyro CLI for multi-repository delivery automation."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _package_version() -> str:
    try:
        return version("dyro")
    except PackageNotFoundError:
        # Source tree without an installed distribution still needs a marker.
        return "0.0.0+dev"


__version__ = _package_version()
