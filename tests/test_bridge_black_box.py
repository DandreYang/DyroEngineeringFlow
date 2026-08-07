from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from tools.verify_bridge_zero_effects import (
    INTERNAL_CANDIDATE,
    MANDATORY_REQUESTS,
    PROTOCOL_CORPUS,
    load_protocol_corpus,
    prepare_fixture,
    verify_fixture,
)
from dyro.bridge.transport import _platform_name


class BridgeBlackBoxTests(unittest.TestCase):
    def test_internal_candidate_runs_every_mandatory_request_without_effects(
        self,
    ) -> None:
        if _platform_name() != "linux-ubuntu-24.04":
            self.skipTest("authoritative candidate success requires Ubuntu 24.04")
        with tempfile.TemporaryDirectory(prefix="bridge-black-box-") as temporary:
            temporary_root = Path(temporary)
            artifact = Path(__file__).resolve().parents[1] / "pyproject.toml"
            fixture = prepare_fixture(temporary_root / "fixture")
            pending_before = fixture.pending.read_bytes()
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, "-I", str(INTERNAL_CANDIDATE)),
                cases=load_protocol_corpus(PROTOCOL_CORPUS),
                strace_dir=temporary_root / "traces",
                reviewed_runtime_root=Path(__file__).resolve().parents[1],
                evidence={
                    "artifact": {
                        "kind": "source",
                        "path": str(artifact),
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    }
                },
            )
            self.assertTrue(result["passed"], result["errors"])
            self.assertTrue(result["trace"]["ok"], result["trace"]["violations"])
            self.assertEqual(result["trace"]["summary"]["binder"], 2)
            self.assertEqual(result["trace"]["summary"]["landlock_success"], 2)
            self.assertEqual(
                sorted(
                    item["operation"]
                    for item in result["operations"]
                    if item["operation"] in {op for op, _ in MANDATORY_REQUESTS}
                    and item["ok"] is True
                ),
                sorted(operation for operation, _ in MANDATORY_REQUESTS),
            )
            self.assertTrue(fixture.pending.exists())
            self.assertEqual(fixture.pending.read_bytes(), pending_before)

    def test_public_process_exposes_mandatory_surface_on_linux(self) -> None:
        if _platform_name() != "linux-ubuntu-24.04":
            self.skipTest("authoritative public success requires Ubuntu 24.04")
        with tempfile.TemporaryDirectory(prefix="bridge-public-") as temporary:
            fixture = prepare_fixture(Path(temporary) / "fixture")
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, "-m", "dyro.bridge.transport"),
                mode="public",
                cases=load_protocol_corpus(PROTOCOL_CORPUS),
            )
            self.assertTrue(result["passed"], result["errors"])
            self.assertEqual(
                {
                    item["operation"]
                    for item in result["operations"]
                    if str(item["case"]).startswith("valid.mandatory.")
                    and item["ok"] is True
                },
                {operation for operation, _ in MANDATORY_REQUESTS},
            )

    def test_snapshot_detects_mutation_even_when_protocol_response_is_valid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-mutation-") as temporary:
            root = Path(temporary)
            fixture = prepare_fixture(root / "fixture")
            fake = root / "mutating_bridge.py"
            fake.write_text(
                """from __future__ import annotations
import json
import os
from pathlib import Path
import sys

json.load(sys.stdin)
Path(os.environ["TMPDIR"]).joinpath("unexpected").write_text("mutation")
sys.stdout.write(json.dumps({
    "ok": False,
    "meta": {},
    "error": {"code": "OPERATION_UNAVAILABLE", "message": "unavailable"},
}, separators=(",", ":")) + "\\n")
raise SystemExit(4)
""",
                encoding="utf-8",
            )
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, str(fake)),
                mode="public",
                cases=(load_protocol_corpus(PROTOCOL_CORPUS)[0],),
            )
            self.assertFalse(result["passed"])
            unexpected = next(
                difference
                for difference in result["snapshot"]["differences"]
                if difference["root"] == "tmp" and difference["path"] == "unexpected"
            )
            self.assertEqual(unexpected["change"], "added")
            self.assertIn("sha256", unexpected["changed_fields"])
            self.assertNotIn("kind", unexpected["values"])
            self.assertNotIn("mutation", json.dumps(unexpected, sort_keys=True))
            self.assertNotIn(str(fixture.root), json.dumps(unexpected, sort_keys=True))
            self.assertTrue(
                set(unexpected["values"]).issubset(
                    {
                        "mode",
                        "uid",
                        "gid",
                        "inode",
                        "nlink",
                        "size",
                        "mtime_ns",
                        "ctime_ns",
                        "sha256",
                    }
                )
            )
            self.assertTrue(
                any(
                    error.startswith("snapshot changed: tmp/unexpected ")
                    for error in result["errors"]
                )
            )

    def test_fixed_ok_fake_bridge_cannot_satisfy_success_gate(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-fixed-ok-") as temporary:
            root = Path(temporary)
            fixture = prepare_fixture(root / "fixture")
            fake = root / "fixed_ok_bridge.py"
            fake.write_text(
                """from __future__ import annotations
import json
import sys

json.load(sys.stdin)
sys.stdout.write(json.dumps({
    "ok": True,
    "meta": {},
    "data": {},
    "warnings": [],
}, separators=(",", ":")) + "\\n")
""",
                encoding="utf-8",
            )
            hello = next(
                case
                for case in load_protocol_corpus(PROTOCOL_CORPUS)
                if case["id"] == "valid.mandatory.bridge-hello"
            )
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, str(fake)),
                mode="candidate",
                cases=(hello,),
            )
            self.assertFalse(result["passed"])
            self.assertTrue(
                any(
                    "metadata" in error or "invariant" in error
                    for error in result["errors"]
                )
            )

    def test_removed_snapshot_root_is_a_structured_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-root-removed-") as temporary:
            root = Path(temporary)
            fixture = prepare_fixture(root / "fixture")
            fake = root / "moving_bridge.py"
            fake.write_text(
                """from __future__ import annotations
import json
import os
from pathlib import Path
import sys

json.load(sys.stdin)
workspace = Path(os.getcwd())
workspace.rename(workspace.with_name("workspace-moved"))
sys.stdout.write(json.dumps({
    "ok": False,
    "meta": {},
    "error": {"code": "OPERATION_UNAVAILABLE", "message": "unavailable"},
}, separators=(",", ":")) + "\\n")
raise SystemExit(4)
""",
                encoding="utf-8",
            )
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, str(fake)),
                mode="public",
                cases=(load_protocol_corpus(PROTOCOL_CORPUS)[0],),
            )
            self.assertFalse(result["passed"])
            workspace_root = next(
                difference
                for difference in result["snapshot"]["differences"]
                if difference["root"] == "workspace" and difference["path"] == "."
            )
            self.assertEqual(workspace_root["change"], "modified")
            self.assertIn("kind", workspace_root["changed_fields"])
            self.assertTrue(
                any("pending sibling changed" in error for error in result["errors"])
            )

    def test_timeout_kills_the_complete_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-timeout-") as temporary:
            root = Path(temporary)
            fixture = prepare_fixture(root / "fixture")
            pid_file = root / "child.pid"
            fake = root / "hanging_bridge.py"
            fake.write_text(
                """from __future__ import annotations
import os
from pathlib import Path
import time

child_pid = os.fork()
if child_pid == 0:
    time.sleep(60)
    raise SystemExit(0)
Path(os.environ["PID_FILE"]).write_text(str(child_pid))
time.sleep(60)
""",
                encoding="utf-8",
            )
            fixture.environment["PID_FILE"] = str(pid_file)
            result = verify_fixture(
                fixture,
                bridge_command=(sys.executable, str(fake)),
                mode="public",
                cases=(load_protocol_corpus(PROTOCOL_CORPUS)[0],),
                timeout_seconds=0.25,
            )
            self.assertFalse(result["passed"])
            self.assertTrue(
                any("timed out" in error for error in result["errors"]),
                result["errors"],
            )
            child_pid = int(pid_file.read_text())
            for _ in range(40):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(
                    f"timed-out descendant {child_pid} survived process-group cleanup"
                )


if __name__ == "__main__":
    unittest.main()
