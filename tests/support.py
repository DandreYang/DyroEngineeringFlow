from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest


CONFIG = '''schema_version = 1

[workspace]
name = "test-workspace"

[layout]
anchors = "repositories"
lines = "versions"
hotfixes = "hotfixes"
tasks = "worktrees"

[policy]
default_base = "main"
task_branch_prefix = "task/"
allow_push = false
require_clean_merge = true

[adapters.noop]
launch = ["/usr/bin/true"]
read = ["/usr/bin/true"]
write = ["/usr/bin/true"]

[repositories.api]
path = "repositories/api"
mount = "services/api"
verify = [["git", "diff", "--check"]]
'''


def shell(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-test-")
        self.root = Path(self.tmp.name)
        (self.root / "dyro.toml").write_text(CONFIG, encoding="utf-8")
        self.anchor = self.root / "repositories/api"
        self.anchor.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=self.anchor)
        shell("git", "config", "user.name", "Test User", cwd=self.anchor)
        shell("git", "config", "user.email", "test@example.com", cwd=self.anchor)
        (self.anchor / "README.md").write_text("anchor\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=self.anchor)
        shell("git", "commit", "-m", "chore: initial", cwd=self.anchor)

    def tearDown(self) -> None:
        self.tmp.cleanup()
