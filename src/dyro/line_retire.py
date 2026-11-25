"""Forward-only, journalled retirement of a delivery line.

Git and journal writes are separate: recovery reconciles each pinned intent
before continuing. Unknown facts keep the operation open, never imply success.
"""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import re
import shlex
import shutil
import uuid

from . import events, line_ops
from .config import Config, validate_id
from .errors import DyroError
from .process import git, git_read, require_ok
from .read_limits import ReadBudget
from .state import exclusive_lock, fsync_directory
from .instructions import INSTRUCTION_NAMES, render_line_persona
from .workspace import (
    Line, MERGE_LOCK_TIMEOUT_SECONDS, _parse_line_content, _state_path,
    _worktree_inventory, get_line, line_repository_path, line_root, list_lines,
    load_line_bounded, merge_lock_path, repository_path,
)


def retired_path(config: Config, line_id: str) -> Path:
    validate_id(line_id, "开发线 ID")
    return config.lines_state_dir / "retired" / f"{line_id}.toml"


def load_retired_line(config: Config, line_id: str, *, read_budget: ReadBudget | None = None) -> Line | None:
    path = retired_path(config, line_id)
    if read_budget is not None:
        try:
            return load_line_bounded(path, read_budget, workspace_root=config.root)
        except FileNotFoundError:
            return None
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise DyroError(f"退役清单不是普通文件：{path}")
    if not path.exists():
        return None
    line = _parse_line_content(path, path.read_bytes())
    if line.id != line_id:
        raise DyroError(f"退役清单 ID 不一致：{path}")
    return line


def archive_worktree_changes(path: Path, destination: Path) -> list[str]:
    """Copy uncommitted work (tracked diff plus untracked files) out of a worktree.

    Ignored files are not copied, matching what `git worktree remove` already drops.
    Returns the saved paths relative to destination; empty when the worktree is clean.
    The copy is built in ``<destination>.partial`` and renamed only once complete
    and verified, so callers may destroy the originals only after this returns.
    """
    dirty = require_ok(git_read(path, "status", "--porcelain=v1", "-uall"), "读取工作区状态").stdout
    if not dirty.strip():
        return []
    if destination.resolve().is_relative_to(path.resolve()):
        raise DyroError(f"存档目录不能位于将被移除的 worktree 内：{destination}")
    if _exists(destination):
        raise DyroError(f"存档目录已存在（可能是上次的存档），拒绝覆盖：{destination}；核对后移走再重试")
    others = [item for item in require_ok(
        git_read(path, "ls-files", "-z", "--others", "--exclude-standard"), "列出未跟踪文件",
    ).stdout.split("\0") if item]
    nested = [item for item in others
              if item.endswith("/") or ((path / item).is_dir() and not (path / item).is_symlink())]
    if nested:
        raise DyroError(f"未跟踪的嵌套仓库无法存档：{', '.join(nested)}（{path}）；请先手工处理")
    partial = destination.with_name(destination.name + ".partial")
    if _exists(partial):
        shutil.rmtree(partial)  # our own copy from a failed attempt; the originals were never touched
    partial.mkdir(parents=True)
    patch = partial / "changes.patch"
    # Plumbing ignores diff.external / textconv / color config; --output keeps raw bytes.
    require_ok(git_read(path, "diff-index", "-p", "--binary", "--no-ext-diff", "--no-textconv",
                        "--no-color", f"--output={patch}", "HEAD"), "导出未提交改动")
    tracked_changed = any(not row.startswith("??") for row in dirty.splitlines() if row)
    if patch.stat().st_size:
        require_ok(git_read(path, "apply", "--check", "-R", str(patch)), "核对未提交改动补丁")
    elif tracked_changed:
        raise DyroError(f"已跟踪文件有改动但补丁为空，拒绝继续：{path}")
    else:
        patch.unlink()
    saved = ["changes.patch"] if patch.exists() else []
    for relative in others:
        source, target = path / relative, partial / "untracked" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, target)
        saved.append(f"untracked/{relative}")
    partial.rename(destination)
    return saved


def _generated_guide(path: Path, line: Line) -> bool:
    return (path.name in INSTRUCTION_NAMES and path.is_file() and not path.is_symlink()
            and path.read_text(encoding="utf-8", errors="replace")
            == render_line_persona(line_id=line.id, branch=line.branch))


def _empty_tree(path: Path) -> bool:
    return (path.is_dir() and not path.is_symlink()
            and all(_empty_tree(child) for child in path.iterdir()))


def line_root_extras(config: Config, line: Line) -> list[Path]:
    """Entries under the line root that retire would otherwise leave behind.

    Repository mounts, task worktree roots (if the layout nests them here),
    Dyro-generated AGENTS.md/CLAUDE.md and empty directories are not extras;
    everything else (notes, outputs, caches) is.
    """
    from .tasks import list_tasks, worktree_root

    root = line_root(config, line)
    mounts = {line_repository_path(config, line, repo_id).absolute() for repo_id in line.repositories}
    mounts |= {worktree_root(config, task).absolute() for task in list_tasks(config) if task.line == line.id}
    extras: list[Path] = []

    def walk(directory: Path) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.absolute() in mounts or _empty_tree(entry):
                continue
            if directory == root and _generated_guide(entry, line):
                continue
            if any(mount.is_relative_to(entry.absolute()) for mount in mounts):
                if not entry.is_symlink():  # a symlinked ancestor is left alone, never moved
                    walk(entry)
            else:
                extras.append(entry)

    # A mount of "." makes the line root the checkout itself: nothing is extra.
    if root.is_dir() and not root.is_symlink() and root.absolute() not in mounts:
        walk(root)
    return extras


def _archive_before_retire(config: Config, line: Line, snapshots: dict, dirty: list[str],
                           archive_to: Path) -> list[str]:
    """Move uncommitted work and line-root extras out, then clean the worktrees."""
    root = line_root(config, line)
    if archive_to.resolve().is_relative_to(root.resolve()):
        raise DyroError(f"存档目录不能位于线根目录内：{archive_to}")
    destination, archived = archive_to / line.id, []
    for repo_id in dirty:
        path = Path(snapshots[repo_id]["path"])
        saved = archive_worktree_changes(path, destination / "uncommitted" / repo_id)
        # Submodule changes are not in the patch, so never reset into them.
        require_ok(git(path, "-c", "submodule.recurse=false", "reset", "--hard", "HEAD"),
                   "清理已存档的未提交改动")
        require_ok(git(path, "clean", "-fd"), "清理已存档的未跟踪文件")
        archived.append(f"{repo_id}: {len(saved)} 项未提交改动 -> {destination / 'uncommitted' / repo_id}")
    for entry in line_root_extras(config, line):
        target = destination / entry.relative_to(root)
        if _exists(target):
            raise DyroError(f"存档目标已存在，拒绝覆盖：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(target))
        archived.append(f"{entry.relative_to(root)} -> {target}")
    return archived


def _cleanup_line_root(config: Config, line: Line) -> list[str]:
    """Remove the retired line's empty shell; returns entries that keep it alive."""
    root = line_root(config, line)
    extras = line_root_extras(config, line)
    if extras or not root.is_dir() or root.is_symlink():
        return [str(entry.relative_to(root)) for entry in extras]
    for name in INSTRUCTION_NAMES:
        if _generated_guide(root / name, line):
            (root / name).unlink()
    for directory, _, _ in os.walk(root, topdown=False):
        try:
            os.rmdir(directory)
        except OSError:
            pass  # not empty (e.g. a mount that is still present): leave it
    return []


def _identity_preflight(config: Config, line_id: str) -> None:
    from .tasks import FINISHED_STATUSES, list_tasks, status, worktree_root

    children = sorted(line.id for line in list_lines(config) if line.parent == line_id)
    if children:
        raise DyroError(
            f"仍有未退役直接子线：{', '.join(children)}；请先退役子线，"
            f"或用 dyro line reparent <子线> --parent <其他线>|--detach 改挂"
        )
    unfinished = [task.id for task in list_tasks(config)
                  if task.line == line_id and status(config, task) not in FINISHED_STATUSES]
    if unfinished:
        raise DyroError(
            f"仍有未完成任务引用 {line_id}：{', '.join(unfinished)}；"
            f"不再继续的 failed/backlog 任务可用 dyro task abandon <id> --reason <原因> 结束"
        )
    # `task close` needs the live line manifest, so it must run before retirement.
    open_worktrees = [task.id for task in list_tasks(config) if task.line == line_id and any(
        (worktree_root(config, task) / config.repositories[repo_id].mount).is_dir()
        for repo_id in task.repositories)]
    if open_worktrees:
        raise DyroError(
            f"任务 worktree 仍在：{', '.join(open_worktrees)}；"
            f"请先 dyro task close <id> --yes [--archive-to <目录>]"
        )


def _resolve(path: Path, ref: str) -> str:
    result = git_read(path, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.code == 0 else ""


def _ref_tip(anchor: Path, ref: str) -> str:
    result = git_read(anchor, "show-ref", "--verify", "--hash", ref)
    if result.code not in (0, 1, 128):
        require_ok(result, f"读取 ref {ref}")
    return result.stdout.strip() if result.code == 0 else ""


def _branch(path: Path) -> str:
    return require_ok(git_read(path, "branch", "--show-current"), "读取分支").stdout.strip()


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _require_idle_clean(path: Path, *, allow_dirty: bool = False) -> bool:
    """Refuse in-progress Git operations; returns whether the worktree is dirty."""
    dirty = bool(require_ok(git_read(path, "status", "--porcelain=v1", "-uall"), "读取工作区状态").stdout.strip())
    if dirty and not allow_dirty:
        raise DyroError(f"工作区不干净：{path}；新发起的 retire 可用 --archive-to <目录> 先存档未提交改动")
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        result = require_ok(git_read(path, "rev-parse", "--git-path", name), "读取 Git 操作状态")
        location = Path(result.stdout.strip())
        if not location.is_absolute():
            location = path / location
        if location.exists():
            raise DyroError(f"存在进行中的 Git 操作 {name}：{path}")
    return dirty


def _check_worktree(snapshot: dict, saved_sha: str, *, allow_dirty: bool = False) -> bool:
    path, anchor = Path(snapshot["path"]), Path(snapshot["anchor"])
    expected = snapshot["branch"]
    if snapshot["storage_mode"] == "anchor-reference":
        matches = path.is_symlink() and path.resolve() == anchor.resolve()
        head, branch = _resolve(anchor, "HEAD"), _branch(anchor)
    else:
        matches = not path.is_symlink() and path.resolve() in _worktree_inventory(anchor)
        head, branch = _resolve(path, "HEAD"), _branch(path)
    if not matches or head != saved_sha or branch != expected:
        raise DyroError(
            f"R2 漂移：期望 HEAD={saved_sha}，实际 HEAD={head or 'missing'}；"
            f"路径={path}（匹配={matches}），分支={branch or 'DETACHED'}，期望={expected}"
        )
    if snapshot["storage_mode"] != "anchor-reference":
        return _require_idle_clean(path, allow_dirty=allow_dirty)
    return False


# The usual integration branches (home.py offers the same set as base overrides).
MAINLINE_REFS = ("origin/main", "origin/master", "origin/release")
_SHA = re.compile(r"[0-9a-f]{7,64}")


def _integration_candidates(config: Config, line: Line, parent: Line | None, repo_id: str) -> list[str]:
    """Refs that, if they contain HEAD, prove the line was integrated.

    Any ref containing HEAD is a sufficient proof, so extra candidates only
    remove false refusals; a pinned base SHA is skipped because HEAD is never
    contained in its own starting point.
    """
    base = line.base_for(repo_id)
    named_base = [] if _SHA.fullmatch(base) else [base]
    return list(dict.fromkeys([*([parent.branch] if parent else []), *named_base,
                               config.policy.default_base, *MAINLINE_REFS]))


def _not_integrated(anchor: Path, repo_id: str, line: Line, tip: str, checked: list[str]) -> DyroError:
    remote = _resolve(anchor, f"refs/remotes/origin/{line.branch}")
    pushed = ("与 HEAD 一致（已推送但未合入）" if remote == tip
              else f"指向 {remote[:12]}，与 HEAD 不同" if remote else "不存在")
    return DyroError(
        f"本线 HEAD 尚未被集成目标包含：{repo_id} HEAD={tip[:12]}；已检查：{', '.join(checked)}；"
        f"本地跟踪 ref origin/{line.branch} {pushed}。"
        f"已合入其他分支时用 --integration-ref <ref> 指明；只需保全不合入时用 --keep-branch --force"
    )


def _preflight(config: Config, line_id: str, options: dict, *,
               allow_dirty: bool = False) -> tuple[Line, dict, list[str]]:
    """Validate a new retire; returns the line, per-repo snapshots and dirty repo IDs."""
    line_ops.require_no_unfinished_op(config, line_id)
    line = get_line(config, line_id)
    _identity_preflight(config, line_id)
    if _exists(retired_path(config, line_id)):
        raise DyroError(f"现役与退役清单并存：{line_id}；请使用 --recover")
    parent = get_line(config, line.parent) if line.parent and not options["integration_ref"] else None
    snapshots, dirty = {}, []
    for repo_id in sorted(line.repositories):
        anchor = repository_path(config, repo_id).resolve()
        path = line_repository_path(config, line, repo_id).absolute()
        tip = _resolve(anchor, f"refs/heads/{line.branch}")
        if options["integration_ref"]:
            if not _resolve(anchor, options["integration_ref"]):
                raise DyroError(f"无法解析集成目标：{repo_id}/{options['integration_ref']}")
            candidates = [options["integration_ref"]]
        else:
            candidates = [ref for ref in _integration_candidates(config, line, parent, repo_id)
                          if _resolve(anchor, ref)] or [
                ref for ref in (line.base_for(repo_id),) if _resolve(anchor, ref)]
            if not candidates:
                raise DyroError(f"无法解析集成目标：{repo_id}；请用 --integration-ref 指定")
        # The first candidate that contains HEAD is journalled and rechecked at R3.
        target = next((ref for ref in candidates if tip and git_read(
            anchor, "merge-base", "--is-ancestor", tip, _resolve(anchor, ref)).code == 0), "")
        if not target and tip and not options["force"]:
            raise _not_integrated(anchor, repo_id, line, tip, candidates)
        target = target or candidates[0]
        snapshot = dict(path=str(path), anchor=str(anchor), branch=line.branch,
                        storage_mode=line.storage_for(repo_id), integration_ref=target,
                        integration_sha=_resolve(anchor, target))
        if _exists(path):
            if not tip:
                raise DyroError(f"本线分支不存在：{repo_id}/{line.branch}")
            if _check_worktree(snapshot, tip, allow_dirty=allow_dirty):
                dirty.append(repo_id)
        elif not options["missing_ok"]:
            raise DyroError(f"worktree 缺失：{path}；可使用 --missing-ok")
        snapshots[repo_id] = snapshot
    return line, snapshots, dirty


def _step(config: Config, op: dict, repo_id: str, action: str) -> dict:
    return line_ops.load_op(config, op["op_id"])["repositories"][repo_id]["steps"].get(action, {})


def _intent(config: Config, op: dict, repo_id: str, action: str, sha: str, **details: object) -> None:
    line_ops.record_repo_intent(config, op["op_id"], repo_id, action,
                               original_head=sha, target_sha=sha, **details)


def _result(config: Config, op: dict, repo_id: str, action: str, outcome: str, sha: str = "") -> None:
    line_ops.record_repo_result(config, op["op_id"], repo_id, action, outcome=outcome, new_tip=sha)
    line_ops.update_repo_phase(config, op["op_id"], repo_id, phase=action, outcome=outcome)


def _save(config: Config, op: dict, repo_id: str, snapshot: dict) -> str:
    anchor, path = Path(snapshot["anchor"]), Path(snapshot["path"])
    ref = f"refs/dyro/retired/{op['subject_line_ids'][0]}/{repo_id}"
    step = _step(config, op, repo_id, "r1")
    current = _ref_tip(anchor, ref)
    if step.get("result", {}).get("outcome") == "skipped_no_head":
        return ""
    if not step:
        if current:
            raise DyroError(f"R1 unknown：保全 ref 已存在但没有 intent：{ref}")
        sha = _resolve(path, "HEAD") if _exists(path) else _resolve(anchor, f"refs/heads/{snapshot['branch']}")
        if not sha and not op["effective_options"]["missing_ok"]:
            raise DyroError(f"R1 无法解析 HEAD：{repo_id}")
        _intent(config, op, repo_id, "r1", sha, ref=ref)
        if not sha:
            _result(config, op, repo_id, "r1", "skipped_no_head")
            return ""
        step = _step(config, op, repo_id, "r1")
    sha = step["intent"]["target_sha"]
    if step["intent"].get("ref") != ref:
        raise DyroError(f"R1 保全 ref 与 intent 不一致：{ref}")
    if not sha:
        if not op["effective_options"]["missing_ok"]:
            raise DyroError(f"R1 未保全 HEAD：{repo_id}")
        _result(config, op, repo_id, "r1", "skipped_no_head")
        return ""
    if current and current != sha:
        raise DyroError(f"R1 保全 ref 冲突：{ref}，期望 {sha}，实际 {current}；永不覆盖")
    if step.get("result", {}).get("outcome") == "ok":
        if not current:
            raise DyroError(f"R1 保全 ref 丢失：{ref}，期望 {sha}")
        return sha
    if not current:
        # Compare-and-create also protects against writers outside dyro's locks.
        require_ok(git(anchor, "update-ref", ref, sha, "0" * len(sha)), "保全退役 HEAD")
    _result(config, op, repo_id, "r1", "ok", sha)
    return sha


def _remove(config: Config, op: dict, repo_id: str, snapshot: dict, sha: str) -> None:
    path, anchor = Path(snapshot["path"]), Path(snapshot["anchor"])
    step = _step(config, op, repo_id, "r2")
    outcome = step.get("result", {}).get("outcome")
    if outcome in {"ok", "skipped_missing"}:
        if _exists(path):
            raise DyroError(f"R2 unknown：声称已移除的路径仍在：{path}")
        return
    if step and (step["intent"]["target_sha"] != sha or any(
        step["intent"].get(key) != snapshot[key] for key in ("path", "anchor", "branch", "storage_mode")
    )):
        raise DyroError(f"R2 路径／分支 intent 与快照不一致：{path}")
    if not sha and _exists(path):
        raise DyroError(f"R2 未保全 HEAD，拒绝移除现有路径：{path}")
    if not _exists(path):
        if not step and not op["effective_options"]["missing_ok"]:
            raise DyroError(f"R2 未获准的 worktree 缺失：{path}")
        if not step:
            _intent(config, op, repo_id, "r2", sha, **snapshot)
        _result(config, op, repo_id, "r2", "ok" if step and sha else "skipped_missing", sha)
        return
    _check_worktree(snapshot, sha)
    if not step:
        _intent(config, op, repo_id, "r2", sha, **snapshot)
    if snapshot["storage_mode"] == "anchor-reference":
        path.unlink()
    else:
        require_ok(git(anchor, "worktree", "remove", "--", str(path)), "移除退役 worktree")
    _result(config, op, repo_id, "r2", "ok", sha)


def _unowned(config: Config, line: Line, snapshot: dict) -> bool:
    return (
        snapshot["storage_mode"] == "anchor-reference"
        or _branch(Path(snapshot["anchor"])) == line.branch
        or any(other.id != line.id and other.branch == line.branch for other in list_lines(config))
    )


def _delete_branch(config: Config, op: dict, line: Line, repo_id: str, snapshot: dict, sha: str) -> None:
    anchor = Path(snapshot["anchor"])
    ref = f"refs/heads/{line.branch}"
    step = _step(config, op, repo_id, "r3")
    outcome = step.get("result", {}).get("outcome", "")
    tip = _ref_tip(anchor, ref)
    if step and (step["intent"]["target_sha"] != sha or step["intent"].get("ref") != ref):
        raise DyroError(f"R3 分支／SHA 与 intent 不一致：{ref}")
    if outcome in {"ok", "skipped_absent"}:
        if tip:
            raise DyroError(f"R3 unknown：已删除分支重新出现：{ref}")
        return
    if outcome.startswith("skipped_"):
        return
    if not step:
        _intent(config, op, repo_id, "r3", sha, ref=ref)
    if op["effective_options"]["keep_branch"]:
        outcome = "skipped_keep"
    elif not tip:
        outcome = "skipped_absent"
    elif not sha or tip != sha:
        outcome = "skipped_moved"
    elif _unowned(config, line, snapshot):
        outcome = "skipped_unowned"
    else:
        if not op["effective_options"]["force"]:
            target = snapshot["integration_ref"]
            result = git_read(anchor, "merge-base", "--is-ancestor", sha, target)
            if result.code == 1:
                raise DyroError(
                    f"R3 本线尚未被集成目标包含：{repo_id}/{target}；"
                    f"请先将 {line.branch} 集成到 {target}，再运行 --recover"
                )
            require_ok(result, "R3 检查集成包含")
        require_ok(git(anchor, "update-ref", "-d", ref, sha), "删除本线独占分支")
        outcome = "ok"
    _result(config, op, repo_id, "r3", outcome, sha)


def _check_snapshot(config: Config, line: Line, repo_id: str, snapshot: dict) -> None:
    if (snapshot["path"] != str(line_repository_path(config, line, repo_id).absolute())
            or snapshot["anchor"] != str(repository_path(config, repo_id).resolve())
            or snapshot["branch"] != line.branch
            or snapshot["storage_mode"] != line.storage_for(repo_id)):
        raise DyroError(f"仓库路径／身份与 journal 快照不一致：{repo_id}")


def _archive(config: Config, op: dict, line: Line) -> None:
    repo_id = op["repos_ordered"][0]
    active, retired = _state_path(config, line.kind, line.id), retired_path(config, line.id)
    step = _step(config, op, repo_id, "r4")
    content = op["manifest_snapshot"]["content"]
    if _exists(active) and _exists(retired):
        raise DyroError("R4 unknown：现役与墓碑并存")
    if _exists(retired):
        if not step or retired.is_symlink() or retired.read_text(encoding="utf-8") != content:
            raise DyroError("R4 unknown：墓碑与快照不一致或没有归档 intent")
    else:
        if (not _exists(active) or active.is_symlink()
                or active.read_text(encoding="utf-8") != content or step.get("result", {}).get("outcome") == "ok"):
            raise DyroError("R4 unknown：现役清单与快照不一致或墓碑丢失")
        if not step:
            _intent(config, op, repo_id, "r4", "", source=str(active), destination=str(retired))
        retired.parent.mkdir(parents=True, exist_ok=True)
        active.rename(retired)
        fsync_directory(active.parent)
        fsync_directory(retired.parent)
    if step.get("result", {}).get("outcome") != "ok":
        _result(config, op, repo_id, "r4", "ok")


def _event_payload(line: Line, facts: dict) -> dict:
    return dict(kind="retire", actor=line.id, subject=line.id, family=line.parent or line.id, facts=facts)


def _publish(config: Config, op: dict, line: Line) -> None:
    repo_id = op["repos_ordered"][0]
    step = _step(config, op, repo_id, "r5")
    if step.get("result", {}).get("outcome") != "ok":
        if not step:
            _intent(config, op, repo_id, "r5", "", event_op_id=op["op_id"])
        try:
            with events.overlay_lock(config):
                records = events.read_event_records_locked(config)
                if not any(row["kind"] == "retire" and row["facts"].get("op_id") == op["op_id"]
                           for row in records):
                    events.append_event_locked(config, **_event_payload(line, op["event_facts"]))
        except (DyroError, OSError) as exc:
            events.record_event_gap(config, code=getattr(exc, "code", "EVENT_WRITE_FAILED"))
            raise
        _result(config, op, repo_id, "r5", "ok")
    for repo in op["repos_ordered"]:
        line_ops.update_repo_phase(config, op["op_id"], repo, phase="completed", outcome="done")
    line_ops.finish_op(config, op["op_id"], phase="completed")


def _report(config: Config, op: dict, error: Exception) -> DyroError:
    current = line_ops.load_op(config, op["op_id"])
    rows = [f"retire {op['op_id']} 未完成：{error}"]
    for repo_id, repo in current["repositories"].items():
        state = "unknown" if repo["phase"] == "unknown" else ("done" if repo["phase"] == "completed" else "pending")
        rows.append(f"{repo_id}: {state} ({repo['phase']})")
        if state == "unknown":
            snapshot = repo["snapshot"]
            path, anchor = shlex.quote(snapshot["path"]), shlex.quote(snapshot["anchor"])
            saved = repo["steps"].get("r1", {}).get("intent", {}).get("target_sha", "")
            rows.extend((f"  git -C {anchor} show-ref", f"  git -C {anchor} worktree list --porcelain",
                         f"  git -C {path} status", f"  git -C {path} merge --abort  # 仅在存在 MERGE_HEAD 时"))
            if saved:
                rows.extend((f"  git -C {path} branch <keep-name> HEAD  # 先保存要保留的提交",
                             f"  git -C {path} switch {shlex.quote(snapshot['branch'])}",
                             f"  git -C {path} reset --hard {saved}  # 破坏性：丢弃未提交改动"))
    rows.append(f"核对路径／保全 ref 后再运行 dyro line retire {op['subject_line_ids'][0]} --recover {op['op_id']}")
    return DyroError("\n".join(rows))


def _advance(config: Config, op: dict, line: Line) -> None:
    for repo_id in op["repos_ordered"]:
        try:
            snapshot = op["repositories"][repo_id]["snapshot"]
            _check_snapshot(config, line, repo_id, snapshot)
            sha = _save(config, op, repo_id, snapshot)
            _remove(config, op, repo_id, snapshot, sha)
            _delete_branch(config, op, line, repo_id, snapshot, sha)
            # A previously unknown repo may now have all three facts confirmed.
            line_ops.update_repo_phase(config, op["op_id"], repo_id, phase="r3", outcome="done")
        except (DyroError, OSError) as exc:
            line_ops.update_repo_phase(config, op["op_id"], repo_id, phase="unknown", outcome="unknown")
            raise _report(config, op, exc) from exc
    try:
        _archive(config, op, line)
    except (DyroError, OSError) as exc:
        line_ops.update_repo_phase(config, op["op_id"], op["repos_ordered"][0], phase="unknown", outcome="unknown")
        raise _report(config, op, exc) from exc


def retire_line(
    config: Config, line_id: str, *, dry_run: bool = False,
    keep_branch: bool | None = None, force: bool | None = None,
    integration_ref: str | None = None, missing_ok: bool | None = None,
    recover: str | None = None, archive_to: Path | None = None,
) -> dict:
    """Retire in repo ID order, or reconcile an existing op forward only.

    ``recover=None`` starts a new op; ``recover=''`` selects the newest open
    retire op. None option values mean absent, so recovery can detect conflicts.
    ``archive_to`` is not journalled: it runs before a new op starts, and the
    originals are cleaned only after their archive is complete, so recovery never
    depends on it. A retry after a crash may stop on the earlier archive and ask
    the user to move it aside.
    """
    validate_id(line_id, "开发线 ID")
    if recover is not None and archive_to is not None:
        raise DyroError("--archive-to 只用于新发起的 retire，不能与 --recover 同用")
    supplied = {key: value for key, value in dict(keep_branch=keep_branch, force=force,
                integration_ref=integration_ref, missing_ok=missing_ok).items() if value is not None}
    options = dict(keep_branch=False, force=False, integration_ref="", missing_ok=False)
    options.update(supplied)
    if recover is None:
        line, _, _ = _preflight(config, line_id, options, allow_dirty=archive_to is not None)
    with line_ops.line_identity_lock(config):
        if recover is not None:
            op = (line_ops.load_op(config, recover) if recover else
                  line_ops.find_unfinished_op(config, line_id, kinds=["retire"]))
            if op is None or op["kind"] != "retire" or op["subject_line_ids"] != [line_id]:
                raise DyroError(f"未找到该线的 retire 操作：{line_id}")
            if "integration_ref" in supplied:
                for repo in op["repositories"].values():
                    line_ops.require_compatible_options(
                        {"effective_options": repo["snapshot"]},
                        {"integration_ref": supplied["integration_ref"]},
                    )
            options = line_ops.require_compatible_options(
                op, {key: value for key, value in supplied.items() if key != "integration_ref"},
            )
            line = _parse_line_content(retired_path(config, line_id), op["manifest_snapshot"]["content"].encode("utf-8"))
            if line.id != line_id or sorted(line.repositories) != op["repos_ordered"]:
                raise DyroError("journal 线身份／仓序不一致")
            _identity_preflight(config, line_id)
        else:
            line_ops.require_no_unfinished_op(config, line_id)
            line = get_line(config, line_id)
        with exclusive_lock(merge_lock_path(config, line_id), timeout_seconds=MERGE_LOCK_TIMEOUT_SECONDS):
            with line_ops.anchor_locks(config, line.repositories):
                if recover is None:
                    checked_line, snapshots, dirty = _preflight(
                        config, line_id, options, allow_dirty=archive_to is not None)
                    if checked_line != line:
                        raise DyroError("开发线清单在加锁期间变化；请重新执行 retire")
                    targets = {item["integration_ref"] for item in snapshots.values()}
                    if len(targets) == 1:
                        options["integration_ref"] = next(iter(targets))
                    else:
                        options.pop("integration_ref")
                    op_id = uuid.uuid4().hex
                    facts = dict(repo_count=len(snapshots), op_id=op_id)
                    events.validate_event_write(**_event_payload(line, facts))
                    if dry_run:
                        return dict(dry_run=True, line=line_id, repositories=snapshots, effective_options=options,
                                    dirty=dirty, extras=[str(path) for path in line_root_extras(config, line)])
                    archived = (_archive_before_retire(config, line, snapshots, dirty, archive_to)
                                if archive_to is not None else [])
                    content = _state_path(config, line.kind, line_id).read_text(encoding="utf-8")
                    op = line_ops.create_op(config, kind="retire", subject_line_ids=[line_id],
                                            manifest_snapshot=dict(asdict(line), content=content),
                                            repos_ordered=line.repositories, effective_options=options,
                                            repository_snapshots=snapshots, event_facts=facts, op_id=op_id)
                else:
                    events.validate_event_write(**_event_payload(line, op["event_facts"]))
                    if dry_run:
                        raise DyroError("--recover 不能与 --dry-run 同用")
                    archived = []
                    if op["phase"] in line_ops.TERMINAL_PHASES:
                        return dict(op, archived=archived, line_root_remaining=_cleanup_line_root(config, line))
                    active = _state_path(config, line.kind, line.id)
                    retired = retired_path(config, line.id)
                    present = [path for path in (active, retired) if _exists(path)]
                    if (len(present) != 1 or present[0].is_symlink()
                            or present[0].read_text(encoding="utf-8") != op["manifest_snapshot"]["content"]):
                        line_ops.update_repo_phase(config, op["op_id"], op["repos_ordered"][0],
                                                   phase="unknown", outcome="unknown")
                        raise _report(config, op, DyroError("R4 unknown：线清单与 journal 快照不一致；停止恢复"))
                _advance(config, op, line)
        # L3 only after releasing L2/L1; L0 still protects line identity.
        try:
            _publish(config, op, line)
        except (DyroError, OSError) as exc:
            raise _report(config, op, exc) from exc
        return dict(line_ops.load_op(config, op["op_id"]), archived=archived,
                    line_root_remaining=_cleanup_line_root(config, line))
