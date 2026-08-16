"""A1 lock: merge / dispatch predicates must not import or name dyro.proof."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "src" / "dyro" / "tasks.py"
BANNED_MODULES = {"dyro.proof"}
BANNED_NAMES = {"list_proofs", "evaluate_proofs", "evaluate_proof", "verify_bundle"}
PROTECTED = {
    "merge_task",
    "check_dispatchable",
    "_prepare_merge",
    "_merge_task_repositories",
    "_merge_task_repositories_locked",
}


def _function_nodes(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROTECTED:
            found[node.name] = node
    return found


class ProofA1BoundaryTests(unittest.TestCase):
    def test_tasks_module_does_not_import_proof(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(TASKS))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "dyro.proof" or alias.name.startswith("dyro.proof."):
                        offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "dyro.proof" or mod.startswith("dyro.proof."):
                    offenders.append(mod)
                if mod in {".proof", "proof"}:
                    offenders.append(mod)
        self.assertEqual(offenders, [])
        self.assertNotIn("dyro.proof", source)

    def test_merge_and_dispatch_do_not_name_proof_apis(self) -> None:
        tree = ast.parse(TASKS.read_text(encoding="utf-8"), filename=str(TASKS))
        functions = _function_nodes(tree)
        self.assertEqual(set(functions), PROTECTED)
        offenders: list[str] = []
        for name, fn in functions.items():
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
                    offenders.append(f"{name}:{node.id}")
                if isinstance(node, ast.Attribute) and node.attr in BANNED_NAMES:
                    offenders.append(f"{name}:{node.attr}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
