"""Verify that a release checkout is the commit named by a trusted Git tag."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess


class ReleaseSourceError(RuntimeError):
    """The checked out release source does not have a trusted provenance."""


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReleaseSourceError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def verify_release_source(
    *,
    repository: Path,
    release_tag: str,
    trusted_ref: str,
) -> dict[str, str]:
    if not release_tag.startswith("v") or len(release_tag) == 1:
        raise ReleaseSourceError("release tag must use the vX.Y.Z form")
    tag_ref = f"refs/tags/{release_tag}"
    _git("show-ref", "--verify", "--quiet", tag_ref, cwd=repository)
    tag_commit = _git("rev-parse", f"{tag_ref}^{{commit}}", cwd=repository)
    checkout_commit = _git("rev-parse", "HEAD", cwd=repository)
    if checkout_commit != tag_commit:
        raise ReleaseSourceError(
            "release checkout does not equal the release tag commit: "
            f"checkout={checkout_commit} tag={tag_commit}"
        )
    is_trusted_ancestor = subprocess.run(
        ("git", "merge-base", "--is-ancestor", tag_commit, trusted_ref),
        cwd=repository,
        check=False,
    ).returncode == 0
    if not is_trusted_ancestor:
        raise ReleaseSourceError(
            f"release tag {release_tag} does not point to an ancestor of {trusted_ref}"
        )
    return {
        "release_tag": release_tag,
        "tag_commit": tag_commit,
        "trusted_ref": trusted_ref,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--trusted-ref", default="origin/main")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                verify_release_source(
                    repository=args.repository.resolve(),
                    release_tag=args.release_tag,
                    trusted_ref=args.trusted_ref,
                ),
                sort_keys=True,
            )
        )
    except ReleaseSourceError as exc:
        print(f"release source verification failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
