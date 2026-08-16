from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import hashlib
import json
import zipfile

from dyro.cli import main
from dyro.config import load
from dyro.proof.project import (
    VERIFY_EXIT_DECAYED,
    VERIFY_EXIT_ERROR,
    VERIFY_EXIT_INCONCLUSIVE,
    VERIFY_EXIT_OK,
)
from dyro.provenance import review_binding
from dyro.tasks import load_task, review_task, run_task, task_template
from dyro.workspace import create_line

from .support import WorkspaceCase


def _write_bound_review(task_path: Path) -> None:
    receipt_hash = hashlib.sha256(task_path.joinpath("receipt.md").read_bytes()).hexdigest()
    heads_hash = hashlib.sha256(task_path.joinpath("task-heads.json").read_bytes()).hexdigest()
    binding = review_binding(task_path)
    provenance = (
        f"attempt_id: {binding[0]}\nplan_sha256: {binding[1]}\n" if binding is not None else ""
    )
    task_path.joinpath("review.md").write_text(
        f"verdict: PASS\nreceipt_sha256: {receipt_hash}\ntask_heads_sha256: {heads_hash}\n{provenance}",
        encoding="utf-8",
    )


class ProofCliTests(WorkspaceCase):
    def _reviewed_task(self, task_id: str = "TASK-CLI"):
        config = load(self.root)
        create_line(config, line_id="alpha", branch="feat/alpha", base="main")
        task_path = config.task_specs_dir / task_id
        task_path.mkdir(parents=True)
        task_path.joinpath("task.toml").write_text(
            task_template(task_id, "cli", "alpha", "api", "services/api").replace(
                'agent = "codex"', 'agent = "noop"'
            ),
            encoding="utf-8",
        )
        task_path.joinpath("handoff.md").write_text("# handoff\n", encoding="utf-8")
        task_path.joinpath("receipt.md").write_text("result: DONE\n", encoding="utf-8")
        task = load_task(config, task_id)
        self.assertEqual(run_task(config, task), "review")
        _write_bound_review(task_path)
        self.assertEqual(review_task(config, task), "done")
        return task_id

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                main(["--root", str(self.root), *argv])
                code = 0
            except SystemExit as exc:
                code = 0 if exc.code is None else int(exc.code)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_list_and_verify_rebind_without_gate_side_effects(self) -> None:
        task_id = self._reviewed_task()
        logs_before = list((self.root / ".dyro").rglob("ledger.jsonl"))
        ledger = self.root / ".dyro/ledger.jsonl"
        before = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        code, out, _err = self._run(["proof", "list", "--task", task_id, "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["schema_version"], 1)
        kinds = {item["kind"] for item in payload["proofs"]}
        self.assertIn("review_verdict", kinds)
        self.assertNotIn("action_receipt", kinds)
        self.assertTrue(all(item["procedure_reproduced"] is False for item in payload["proofs"]))
        review = next(item for item in payload["proofs"] if item["kind"] == "review_verdict")
        self.assertEqual(review["status"], "live")

        code, verify_out, _ = self._run(
            ["proof", "verify", review["id"], "--format", "json"]
        )
        self.assertEqual(code, VERIFY_EXIT_OK)
        verified = json.loads(verify_out)
        self.assertEqual(verified["mode"], "rebind")
        self.assertFalse(verified["proofs"][0]["procedure_reproduced"])
        after = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        self.assertEqual(before, after)
        self.assertEqual(logs_before, list((self.root / ".dyro").rglob("ledger.jsonl")))

    def test_verify_decayed_exit_code_and_rerun_refused(self) -> None:
        task_id = self._reviewed_task("TASK-DECAY-CLI")
        task_dir = load(self.root).task_specs_dir / task_id
        task_dir.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")
        code, out, _ = self._run(["proof", "verify", "--task", task_id, "--format", "json"])
        self.assertEqual(code, VERIFY_EXIT_DECAYED)
        payload = json.loads(out)
        review = next(item for item in payload["proofs"] if item["kind"] == "review_verdict")
        self.assertEqual(review["status"], "decayed")

        code, _out, err = self._run(["proof", "verify", "--rerun-procedure", "--task", task_id])
        self.assertEqual(code, VERIFY_EXIT_ERROR)
        self.assertIn("未提供隔离重跑", err)

    def test_export_schema_is_frozen_and_contains_no_git_objects(self) -> None:
        task_id = self._reviewed_task("TASK-EXPORT")
        bundle = self.root / "out" / "proofs.zip"
        code, out, _ = self._run(["proof", "export", "--task", task_id, "--bundle", str(bundle)])
        self.assertEqual(code, 0)
        self.assertIn("已导出", out)
        self.assertNotIn("experimental", out)
        self.assertTrue(bundle.is_file())
        with zipfile.ZipFile(bundle) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["kind"], "dyro.proof.bundle")
            self.assertIn("proof_sha256", manifest)
            self.assertNotIn("objects", names)
            self.assertFalse(any(part in {"objects", ".git"} for name in names for part in name.split("/")))
            blob = b"".join(archive.read(name) for name in names).decode("utf-8", errors="ignore")
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn("/usr/bin", blob)

        code, verify_out, _ = self._run(
            ["proof", "verify-bundle", str(bundle), "--git-dir", str(self.anchor), "--format", "json"]
        )
        self.assertEqual(code, VERIFY_EXIT_OK)
        payload = json.loads(verify_out)
        self.assertEqual(payload["mode"], "integrity")
        self.assertEqual(payload["conclusion"], "integrity")
        self.assertFalse(payload["merge_equivalent"])
        self.assertTrue(payload["proofs"])
        self.assertTrue(all(item["status"] == "live" for item in payload["proofs"]))
        self.assertTrue(all(item["procedure_reproduced"] is False for item in payload["proofs"]))
        with zipfile.ZipFile(bundle) as archive:
            exported = json.loads(archive.read(next(name for name in archive.namelist() if name.startswith("proofs/"))))
        self.assertEqual(exported["status"], "inconclusive")

        code, missing_out, _ = self._run(["proof", "verify-bundle", str(bundle), "--format", "json"])
        self.assertEqual(code, VERIFY_EXIT_INCONCLUSIVE)
        missing = json.loads(missing_out)
        self.assertEqual(missing["mode"], "integrity")
        self.assertEqual(missing["conclusion"], "integrity")
        self.assertFalse(missing["merge_equivalent"])
        self.assertTrue(any(item["status"] == "inconclusive" for item in missing["proofs"]))
        self.assertFalse(any(item["status"] == "decayed" for item in missing["proofs"]))

    def test_objective_attention_json_includes_proof_decayed(self) -> None:
        task_id = self._reviewed_task("TASK-ATTN")
        load(self.root).task_specs_dir.joinpath(task_id, "review.md").write_text(
            "verdict: PASS\n", encoding="utf-8"
        )
        main(
            [
                "--root",
                str(self.root),
                "objective",
                "start",
                "--id",
                "release",
                "--title",
                "Release",
                "--line",
                "alpha",
                "--targets",
                task_id,
                "--yes",
            ]
        )
        code, out, _err = self._run(["objective", "attention", "release", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        reasons = [item["reason"] for item in payload["items"]]
        self.assertIn("PROOF_DECAYED", reasons)
        self.assertNotIn("argv", json.dumps(payload))

    def test_export_proof_id_and_task_are_mutex(self) -> None:
        task_id = self._reviewed_task("TASK-MUTEX")
        code, out, _ = self._run(["proof", "list", "--task", task_id, "--format", "json"])
        proof_id = json.loads(out)["proofs"][0]["id"]
        code, _out, err = self._run(
            [
                "proof",
                "export",
                proof_id,
                "--task",
                task_id,
                "--bundle",
                str(self.root / "x.zip"),
            ]
        )
        self.assertEqual(code, VERIFY_EXIT_ERROR)
        self.assertIn("互斥", err)

    def test_verify_bundle_rejects_evidence_zip_as_inconclusive(self) -> None:
        evidence = self.root / "evidence.zip"
        with zipfile.ZipFile(evidence, "w") as archive:
            archive.writestr("receipt.md", "result: DONE\n")
            archive.writestr("provenance.json", "{}\n")
            archive.writestr("gates.json", '{"schema_version":1,"gates":[]}\n')
        code, out, _ = self._run(
            ["proof", "verify-bundle", str(evidence), "--git-dir", str(self.anchor), "--format", "json"]
        )
        self.assertEqual(code, VERIFY_EXIT_INCONCLUSIVE)
        payload = json.loads(out)
        self.assertTrue(all(item["status"] == "inconclusive" for item in payload["proofs"]))
        self.assertFalse(any(item["status"] == "live" for item in payload["proofs"]))

    def test_missing_review_file_stays_inconclusive_after_list(self) -> None:
        task_id = self._reviewed_task("TASK-MISSING-REVIEW")
        (load(self.root).task_specs_dir / task_id / "review.md").unlink()
        code, out, _ = self._run(["proof", "verify", "--task", task_id, "--format", "json"])
        self.assertEqual(code, VERIFY_EXIT_INCONCLUSIVE)
        payload = json.loads(out)
        review = next(item for item in payload["proofs"] if item["kind"] == "review_verdict")
        self.assertEqual(review["status"], "inconclusive")
        self.assertNotEqual(review["status"], "decayed")

    def test_workspace_verify_and_verify_bundle_are_separate_conclusions(self) -> None:
        from .support import shell

        task_id = self._reviewed_task("TASK-TWO-CONCLUSIONS")
        bundle = self.root / "two.zip"
        self.assertEqual(self._run(["proof", "export", "--task", task_id, "--bundle", str(bundle)])[0], 0)
        task_dir = load(self.root).task_specs_dir / task_id
        task_dir.joinpath("review.md").write_text("verdict: PASS\n", encoding="utf-8")
        code, verify_out, _ = self._run(["proof", "verify", "--task", task_id, "--format", "json"])
        self.assertEqual(code, VERIFY_EXIT_DECAYED)
        workspace = json.loads(verify_out)
        review = next(item for item in workspace["proofs"] if item["kind"] == "review_verdict")
        self.assertEqual(review["status"], "decayed")

        code, bundle_out, _ = self._run(
            ["proof", "verify-bundle", str(bundle), "--git-dir", str(self.anchor), "--format", "json"]
        )
        self.assertEqual(code, VERIFY_EXIT_OK)
        integrity = json.loads(bundle_out)
        self.assertEqual(integrity["mode"], "integrity")
        self.assertEqual(integrity["conclusion"], "integrity")
        self.assertFalse(integrity["merge_equivalent"])
        self.assertFalse(any(item["status"] == "decayed" for item in integrity["proofs"]))
        self.assertTrue(all(item["status"] == "live" for item in integrity["proofs"]))

        shell("git", "commit", "--allow-empty", "-m", "move head", cwd=self.anchor)
        moved = shell_head(self.anchor)
        heads = self.root / "current-heads.json"
        heads.write_text(json.dumps({"api": moved}), encoding="utf-8")
        code, moved_out, _ = self._run(
            [
                "proof",
                "verify-bundle",
                str(bundle),
                "--git-dir",
                str(self.anchor),
                "--current-heads",
                str(heads),
                "--format",
                "json",
            ]
        )
        self.assertEqual(code, VERIFY_EXIT_DECAYED)
        with_heads = json.loads(moved_out)
        review = next(item for item in with_heads["proofs"] if item["kind"] == "review_verdict")
        self.assertEqual(review["status"], "decayed")


def shell_head(repo: Path) -> str:
    import subprocess

    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()
