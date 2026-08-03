# Coding-tool catalog and guided installation

Dyro keeps local coding tools separate from Profile adapters. A detected tool
can open a selected workspace, but it receives no Dyro execution, gate,
review, signoff, merge, or push authority. Only an explicitly configured
`[adapters.<id>]` contract can participate in controlled execution.

## Home ordering

The interactive home uses a stable, availability-first order:

1. the tool last used in the current workspace;
2. the project's recommended tool, when ready;
3. the user's global default tool;
4. other ready tools, with configured adapters before launch-only tools;
5. installed tools that still need setup;
6. tools with a guided installation path;
7. unavailable tools without an audited recipe; and
8. Shell as the final fallback.

Availability always wins over preference. A missing default or recommended
tool never blocks Enter from opening a ready tool. Ties use the user's pinned
order and then a stable tool ID, so the menu does not jump randomly.

Personal preferences live in the user's Dyro state directory, not in a
project checkout:

```bash
dyro tool list
dyro tool default cursor-desktop
dyro tool pin cursor-desktop codex openclaw
dyro tool default --clear
dyro tool pin --clear
```

A team may add a non-binding recommendation to its Profile or workspace
blueprint:

```toml
[workspace]
recommended_tool = "cursor-desktop"
```

For an existing Profile, the same value can be managed without hand-editing
TOML:

```bash
dyro config set workspace.recommended_tool cursor-desktop
```

The recommendation changes a badge and ordering only. It does not install,
configure, authorize, or launch the tool without a user choice.

## Guided installation

Missing supported tools remain visible as `installable`. Selecting one shows
the official source, exact command, install scope, post-install step, and
permission warning before asking for confirmation. The same flow is available
explicitly:

```bash
dyro tool install openclaw
dyro --dry-run tool install openclaw
dyro tool install openclaw --yes
```

Built-in npm recipes are argv arrays executed without a shell. Dyro never
takes an install command from `dyro.toml` or a workspace blueprint. It does not
use `sudo`, and it verifies a completed command with `<tool> --version` before
returning to discovery.

When an official installation path is a downloaded script, Dyro does not run
the script. It opens the official installation page after confirmation so the
user can inspect and choose the platform-specific installer. This applies to
Cursor CLI and Hermes; Cursor Desktop likewise opens the official download
page.

Current catalog entries include Codex, Claude Code, Cursor Desktop, Cursor CLI,
Grok, OpenCode, OpenClaw, Hermes, Kimi Code, and Shell. An entry can be known
without having an audited installation recipe; such entries fail closed with
an actionable message.

## Cursor Desktop and OpenClaw

Cursor Desktop is distinct from Cursor CLI. Dyro detects the `cursor` command,
the standard macOS application bundle, or the standard per-user Windows
application path, then opens the selected workspace as a launch-only desktop
tool.

OpenClaw is treated as an external runtime, not a Dyro adapter. A configured
installation receives the selected path through `OPENCLAW_WORKSPACE_DIR`. A
fresh installation starts official onboarding with that workspace and
`--skip-bootstrap`, avoiding automatic bootstrap-file generation in the
project. The user must confirm onboarding separately. The selected workspace
is a default working directory, not an operating-system sandbox; OpenClaw can
still reach other paths allowed to the current user unless its own sandboxing
is configured.
