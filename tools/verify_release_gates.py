"""Refuse a 1.0.0 release tag when P6-export / P12 / verify-bundle evidence is missing.

A 0.6.x tag of this physics train is also refused: the tree already contains
Proof / Card / Compiler / verify-bundle.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


GATES = (
    ("P6-export", Path("src/dyro/proof/bundle.py"), "def export_bundle"),
    ("P12", Path("src/dyro/host/doctor.py"), "def assert_projections_allow_mutation"),
    ("P13-verify-bundle", Path("src/dyro/proof/bundle.py"), "def verify_bundle"),
    ("P13-identity-en", Path("README.md"), "delivery physics engine"),
    ("P13-identity-zh", Path("README.zh-CN.md"), "交付物理引擎"),
    ("P13-identity-ko", Path("README.ko.md"), "전달 물리 엔진"),
    ("P13-identity-es", Path("README.es.md"), "física de entrega"),
    ("P13-identity-fr", Path("README.fr.md"), "physique de livraison"),
    ("P13-identity-de", Path("README.de.md"), "Delivery-Physik-Engine"),
    ("P13-identity-pt", Path("README.pt-BR.md"), "física de entrega"),
    ("P13-identity-ru", Path("README.ru.md"), "физики поставки"),
    ("P13-bundle-contract-en", Path("README.md"), "verify-bundle"),
    ("P13-a1-lock", Path("tests/test_proof_a1_boundary.py"), "dyro.proof"),
    ("P13-stranger", Path("tools/verify_bundle_stranger.py"), "without --git-dir must not be live"),
    ("P0-missing-git", Path("src/dyro/proof/bundle.py"), "if not git_dirs:"),
)


def _version(root: Path) -> str:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(metadata["project"]["version"])


def _is_physics_train(root: Path) -> bool:
    bundle = root / "src/dyro/proof/bundle.py"
    return bundle.is_file() and "def verify_bundle" in bundle.read_text(encoding="utf-8")


def missing_gates(root: Path) -> list[str]:
    missing: list[str] = []
    for name, path, marker in GATES:
        target = root / path
        if not target.is_file() or marker not in target.read_text(encoding="utf-8"):
            missing.append(name)
    if (root / "src/dyro/proof/bundle.py").read_text(encoding="utf-8").find("def refuse_verify_bundle") >= 0:
        missing.append("P13-refuse-removed")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--release-tag", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    version = _version(root)
    tag = args.release_tag.strip()
    if version.startswith("0.6") and _is_physics_train(root):
        if tag.startswith("v0.6") or tag.startswith("0.6"):
            raise SystemExit("拒绝：本树已含 Proof/Card/Compiler，不得作为 0.6.x 发布")
        print(f"skip 1.0 gates: version={version} tag={tag or '-'}; do not publish this tree as 0.6.x")
        return 0
    if version != "1.0.0" and tag not in {"v1.0.0", "1.0.0"}:
        print(f"skip 1.0 gates: version={version} tag={tag or '-'}")
        return 0
    missing = missing_gates(root)
    if missing:
        raise SystemExit("拒绝 1.0.0：缺少 " + ", ".join(missing))
    print("1.0 gates present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
