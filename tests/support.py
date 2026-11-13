from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


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


@contextmanager
def executor_writes_receipt(task_directory: Path, content: str = "result: DONE\n"):
    """Write the task receipt during executor run, not before the attempt starts."""
    from dyro import tasks as tasks_mod

    original = tasks_mod._execute_task_agent

    def wrapped(*args, **kwargs):
        result = original(*args, **kwargs)
        task_directory.joinpath("receipt.md").write_text(content, encoding="utf-8")
        return result

    with patch.object(tasks_mod, "_execute_task_agent", side_effect=wrapped):
        yield


def publish_origin_branch(repo: Path, branch: str) -> Path:
    """Publish HEAD to origin/<branch> without minting a local branch."""
    remote = repo.parent / f"{repo.name}.origin.git"
    if not remote.exists():
        remote.mkdir(parents=True)
        shell("git", "init", "--bare", cwd=remote)
    remotes = subprocess.run(
        ["git", "-C", str(repo), "remote"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.split()
    if "origin" not in remotes:
        shell("git", "remote", "add", "origin", str(remote), cwd=repo)
    shell("git", "push", "origin", f"HEAD:refs/heads/{branch}", cwd=repo)
    shell("git", "fetch", "origin", cwd=repo)
    return remote


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="dyro-test-")
        self.root = Path(self.tmp.name)
        self.registry_tmp = tempfile.TemporaryDirectory(prefix="dyro-registry-")
        self.registry_environment = patch.dict(
            os.environ, {"DYRO_HOME": self.registry_tmp.name}, clear=False
        )
        self.registry_environment.start()
        (self.root / "dyro.toml").write_text(CONFIG, encoding="utf-8")
        self.anchor = self.root / "repositories/api"
        self.anchor.mkdir(parents=True)
        shell("git", "init", "-b", "main", cwd=self.anchor)
        shell("git", "config", "user.name", "Test User", cwd=self.anchor)
        shell("git", "config", "user.email", "test@example.com", cwd=self.anchor)
        shell("git", "config", "commit.gpgsign", "false", cwd=self.anchor)
        (self.anchor / "README.md").write_text("anchor\n", encoding="utf-8")
        shell("git", "add", "README.md", cwd=self.anchor)
        shell("git", "commit", "-m", "chore: initial", cwd=self.anchor)

    def tearDown(self) -> None:
        self.registry_environment.stop()
        self.registry_tmp.cleanup()
        self.tmp.cleanup()
