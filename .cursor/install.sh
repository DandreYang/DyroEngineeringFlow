#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for DyroEngineeringFlow (the `dyro` CLI).
#
# Mirrors the toolchain the CI workflow (.github/workflows/ci.yml) relies on:
# uv-managed Python + a locked `uv sync`, plus the TypeScript interop runner deps.
set -euo pipefail

# uv is the project package/environment manager (see uv.lock + CI). Install once;
# it lands in ~/.local/bin, which is already on the login-shell PATH.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

# Provision a uv-managed CPython so venv creation and `python -m build`
# isolation do not depend on the base image shipping python3.x-venv.
uv python install 3.12

# Fail fast if uv.lock has drifted from pyproject.toml, then install the locked
# project with all extras + dev tools (ruff / build / twine) into .venv.
uv lock --check
uv sync --locked --all-extras --dev --python-preference only-managed

# Optional TypeScript protocol interop reference runner (CI job: typescript-interop).
if command -v npm >/dev/null 2>&1; then
  ( cd examples/typescript-runner && npm ci )
fi

echo "dyro environment ready: $(uv run dyro --version)"
