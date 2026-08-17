"""Refuse a physics-train release that is missing Proof / Card / Compiler evidence.

A 0.6.x tag of this train is refused. Remaining delivery-physics features ship
as 0.7.x, not 0.8 / 0.9 / 1.0. A 0.7.x tag must pass 0.7 gates and must not be
narrated as a 1.0 release. 1.0.0 is an identity freeze, not this series' feature
number; if that tag is ever cut, it still keeps the stricter stranger contract.

Markers must appear in real source, not comments.
"""

from __future__ import annotations

import argparse
import io
import re
from pathlib import Path
import tokenize
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
    ("P13-stranger-decayed", Path("tools/verify_bundle_stranger.py"), "must not report decayed"),
    ("P0-missing-git", Path("src/dyro/proof/bundle.py"), "if not git_dirs:"),
)

SEVEN_GATES = GATES + (
    ("P0-F5-helper", Path("src/dyro/capability/cards.py"), "def assert_capability_allows_write"),
    ("P0-F5-run", Path("src/dyro/tasks.py"), "assert_capability_allows_write(config, executor)"),
    ("P0-second-door", Path("src/dyro/capability/__init__.py"), "second write door"),
    ("P0-unconfined", Path("src/dyro/task_dispatch.py"), '"allow_unconfined_provider": False'),
)

SEVEN_X_GATES = (
    ("P7-inspect", Path("src/dyro/observations.py"), "def inspect_workspace_read_snapshot"),
    ("P7-console", Path("src/dyro/console/overview.py"), "def inspect_proofs"),
    ("P7-route", Path("src/dyro/console/server.py"), "/proofs"),
    ("trigger-kind", Path("src/dyro/proof/models.py"), "TRIGGER_OBSERVATION"),
    ("trigger-derive", Path("src/dyro/proof/derive.py"), "def derive_trigger_proofs"),
)

PHYSICS_GATES = SEVEN_GATES + SEVEN_X_GATES


def _version(root: Path) -> str:
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(metadata["project"]["version"])


def _is_physics_train(root: Path) -> bool:
    bundle = root / "src/dyro/proof/bundle.py"
    return bundle.is_file() and _contains_marker(bundle, "def verify_bundle")


def _offset(text: str, start: tuple[int, int]) -> int:
    line, column = start
    if line <= 1:
        return min(column, len(text))
    index = 0
    for _ in range(line - 1):
        newline = text.find("\n", index)
        if newline < 0:
            return len(text)
        index = newline + 1
    return min(index + column, len(text))


def _python_without_comments(text: str) -> str:
    chars = list(text)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            begin = _offset(text, token.start)
            end = _offset(text, token.end)
            chars[begin:end] = [" "] * max(0, end - begin)
    except tokenize.TokenError:
        return ""
    return "".join(chars)


def _markdown_without_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _contains_marker(path: Path, marker: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        text = _python_without_comments(text)
    elif path.suffix == ".md":
        text = _markdown_without_comments(text)
    return marker in text


def missing_gates(root: Path, gates: tuple[tuple[str, Path, str], ...] = GATES) -> list[str]:
    missing: list[str] = []
    for name, path, marker in gates:
        target = root / path
        if not target.is_file() or not _contains_marker(target, marker):
            missing.append(name)
    if _contains_marker(root / "src/dyro/proof/bundle.py", "def refuse_verify_bundle"):
        missing.append("P13-refuse-removed")
    return missing


def _tag_name(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def _refuse_wrong_feature_number(version: str) -> str | None:
    if version.startswith(("0.8.", "0.9.")):
        return f"拒绝 {version}：功能号必须停在 0.7.x"
    if version.startswith("1.") and version != "1.0.0":
        return f"拒绝 {version}：1.x 只允许以后的身份冻结号 1.0.0"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--release-tag", default="")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    version = _version(root)
    tag = args.release_tag.strip()
    if _is_physics_train(root) and (
        tag.startswith("v0.6") or tag.startswith("0.6")
    ):
        raise SystemExit("拒绝：本树已含 Proof/Card/Compiler，不得作为 0.6.x 发布")
    if tag and _tag_name(tag) != version:
        raise SystemExit(f"拒绝：release tag {tag!r} 必须等于 v{version}")
    wrong = _refuse_wrong_feature_number(version)
    if wrong:
        raise SystemExit(wrong)
    if version.startswith("0.7."):
        missing = missing_gates(root, PHYSICS_GATES)
        if missing:
            raise SystemExit(f"拒绝 {version}：缺少 " + ", ".join(missing))
        print("0.7 gates present")
        return 0
    if version == "1.0.0" or tag in {"v1.0.0", "1.0.0"}:
        missing = missing_gates(root, PHYSICS_GATES)
        if missing:
            raise SystemExit("拒绝 1.0.0：缺少 " + ", ".join(missing))
        print("1.0 identity gates present")
        return 0
    if _is_physics_train(root):
        raise SystemExit(f"拒绝 {version}：交付物理列车只发 0.7.x")
    print(f"skip physics gates: version={version} tag={tag or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
