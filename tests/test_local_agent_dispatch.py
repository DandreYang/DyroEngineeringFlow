from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from experiments.local_agent_dispatch.context_guard import (
    assert_files_allowed,
    check_content,
    check_path,
    guard_file,
    materialize_strict_shadow,
)
from experiments.local_agent_dispatch.errors import DispatchValidationError
from experiments.local_agent_dispatch.locator_verify import (
    verify_evidence,
    verified_ratio,
)
from experiments.local_agent_dispatch.process_identity import (
    current_identity,
    identity_matches,
)
from experiments.local_agent_dispatch.task_contract import parse_task_contract


class TaskContractTests(unittest.TestCase):
    def test_accepts_minimal_valid(self) -> None:
        contract = parse_task_contract(
            {
                "schema_version": 1,
                "backend": "auto",
                "mode": "read-only",
                "strict": True,
                "files": ["src/**/*.py"],
                "task": {
                    "briefing": "CLI tool",
                    "locations": "src/",
                    "objective": "review lock correctness",
                    "constraints": "read-only",
                    "output_contract": "summary + evidence",
                },
            }
        )
        self.assertEqual(contract.mode, "read-only")
        self.assertTrue(contract.strict)

    def test_rejects_star_glob_and_strict_edit(self) -> None:
        with self.assertRaises(DispatchValidationError):
            parse_task_contract(
                {
                    "schema_version": 1,
                    "files": ["**/*"],
                    "task": {
                        "briefing": "a",
                        "locations": "b",
                        "objective": "c",
                        "constraints": "d",
                        "output_contract": "e",
                    },
                }
            )

    def test_rejects_secret_like_text_in_every_task_field(self) -> None:
        token = "sk-proj-" + ("a" * 48)
        for field in ("briefing", "locations", "objective", "constraints", "output_contract"):
            with self.subTest(field=field):
                task = {
                    "briefing": "a",
                    "locations": "b",
                    "objective": "c",
                    "constraints": "d",
                    "output_contract": "e",
                }
                task[field] = f"token={token}"
                with self.assertRaisesRegex(DispatchValidationError, "secret-like"):
                    parse_task_contract(
                        {
                            "schema_version": 1,
                            "files": ["src/a.py"],
                            "task": task,
                        }
                    )
        with self.assertRaises(DispatchValidationError):
            parse_task_contract(
                {
                    "schema_version": 1,
                    "mode": "edit",
                    "strict": True,
                    "files": ["src/a.py"],
                    "task": {
                        "briefing": "a",
                        "locations": "b",
                        "objective": "c",
                        "constraints": "d",
                        "output_contract": "e",
                    },
                }
            )


class ContextGuardTests(unittest.TestCase):
    def test_path_and_content_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "src" / "ok.py"
            good.parent.mkdir(parents=True)
            good.write_text("print('hi')\n", encoding="utf-8")
            env = root / ".env"
            env.write_text("SECRET=1\n", encoding="utf-8")
            leak = root / "src" / "leak.py"
            leak.write_text("token=sk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")

            self.assertTrue(check_path(good, root).allowed)
            self.assertFalse(check_path(env, root).allowed)
            self.assertFalse(check_content(leak.read_text(encoding="utf-8")).allowed)
            self.assertFalse(check_content("token=sk-proj-" + ("a" * 48)).allowed)
            self.assertFalse(check_content("token=github_pat_" + ("a" * 82)).allowed)
            self.assertTrue(guard_file(good, root).allowed)

            assert_files_allowed([good], root)
            with self.assertRaises(DispatchValidationError):
                assert_files_allowed([env], root)
            with self.assertRaises(DispatchValidationError):
                assert_files_allowed([leak], root)

    def test_strict_shadow_materialize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "a" / "b.py"
            src.parent.mkdir(parents=True)
            src.write_text("x = 1\n", encoding="utf-8")
            shadow = root / "shadow"
            materialize_strict_shadow(
                shadow_root=shadow,
                root_dir=root,
                file_paths=[src],
            )
            self.assertEqual(
                (shadow / "a" / "b.py").read_text(encoding="utf-8"),
                "x = 1\n",
            )


class LocatorVerifyTests(unittest.TestCase):
    def test_verify_lines_and_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "mod.py"
            f.write_text("a\nb\nc\n", encoding="utf-8")
            items = verify_evidence(
                [
                    {"file": "mod.py", "lines": "1-2", "claim": "ok"},
                    {"file": "mod.py", "lines": "9-10", "claim": "bad range"},
                    {"file": "../etc/passwd", "lines": "1", "claim": "escape"},
                    {"file": "missing.py", "claim": "missing"},
                ],
                cwd=root,
            )
            self.assertTrue(items[0].verified)
            self.assertFalse(items[1].verified)
            self.assertFalse(items[2].verified)
            self.assertFalse(items[3].verified)
            self.assertEqual(verified_ratio(items), 0.25)


class ProcessIdentityTests(unittest.TestCase):
    def test_current_identity_matches_self(self) -> None:
        identity = current_identity()
        if identity.started_at.startswith("unknown-"):
            self.skipTest("stable process-generation identity is unavailable")
        self.assertTrue(
            identity_matches(
                {"pid": identity.pid, "started_at": identity.started_at}
            )
        )
        self.assertFalse(
            identity_matches({"pid": identity.pid, "started_at": "not-the-start"})
        )


if __name__ == "__main__":
    unittest.main()
