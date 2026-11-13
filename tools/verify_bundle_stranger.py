"""Stranger-style integrity check: sdist-installed dyro + caller git objects."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile


def _run(argv: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _write_fixture(root: Path) -> tuple[Path, Path]:
    work = root / "work"
    work.mkdir()
    _run(["git", "init", "-b", "main"], cwd=work)
    _run(["git", "config", "user.name", "Stranger"], cwd=work)
    _run(["git", "config", "user.email", "stranger@example.com"], cwd=work)
    (work / "README").write_text("fixture\n", encoding="utf-8")
    _run(["git", "add", "README"], cwd=work)
    _run(["git", "commit", "-m", "fixture"], cwd=work)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
    repo = root / "objects.git"
    _run(["git", "clone", "--bare", str(work), str(repo)])
    proof_id = hashlib.sha256(b"stranger-fixture").hexdigest()
    payload = {
        "bytes_sha256": hashlib.sha256(b"fixture").hexdigest(),
        "decay_reason": "",
        "declared_key_ids": [],
        "generation": "1",
        "id": proof_id,
        "kind": "review_verdict",
        "observed_at": "",
        "policy_require_signed": {"require_signed_review": "false"},
        "procedure": "stranger fixture",
        "procedure_reproduced": False,
        "produced_at": "",
        "status": "inconclusive",
        "subject": "TASK-STRANGER",
        "substrate": {
            "attempt_id": "",
            "contract_hash": "",
            "extra": {},
            "plan_sha256": "",
            "repo_heads": {"api": sha},
        },
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    bundle = root / "fixture.proof.zip"
    manifest = {
        "kind": "dyro.proof.bundle",
        "proof_ids": [proof_id],
        "proof_sha256": {proof_id: hashlib.sha256(body.encode("utf-8")).hexdigest()},
        "schema_version": 1,
    }
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        archive.writestr(f"proofs/{proof_id}.json", body)
    return bundle, repo


def main(argv: list[str] | None = None) -> int:
    dyro = list(argv) if argv else ["dyro"]
    with tempfile.TemporaryDirectory(prefix="dyro-stranger-") as tmp:
        root = Path(tmp)
        bundle, git_dir = _write_fixture(root)
        live = _run(
            [*dyro, "proof", "verify-bundle", str(bundle), "--git-dir", str(git_dir), "--format", "json"]
        )
        payload = json.loads(live.stdout)
        if payload.get("mode") != "integrity":
            raise SystemExit("stranger verify-bundle must report mode=integrity")
        if any(item.get("status") != "live" for item in payload.get("proofs", [])):
            raise SystemExit(f"expected integrity live, got {payload}")
        if any(item.get("status") == "decayed" for item in payload.get("proofs", [])):
            raise SystemExit("bare verify-bundle must not report decayed")
        missing = subprocess.run(
            [*dyro, "proof", "verify-bundle", str(bundle), "--format", "json"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if missing.returncode == 0:
            raise SystemExit("verify-bundle without --git-dir must not be live")
        absent = json.loads(missing.stdout or "{}")
        if any(item.get("status") == "live" for item in absent.get("proofs", [])):
            raise SystemExit("missing git objects must be inconclusive, not live")
        if any(item.get("status") == "decayed" for item in absent.get("proofs", [])):
            raise SystemExit("missing git objects must not report decayed")
        print(json.dumps({"ok": True, "mode": "integrity", "live": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
