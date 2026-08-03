# Workspace blueprints

A workspace blueprint is a small, versioned TOML contract that lets a new
teammate create the same isolated multi-repository workspace with one command:

```bash
dyro join git@github.com:acme/platform-blueprints.git --ref main
```

Dyro Core does not contain company-specific repository names, URLs, branch
rules, or credentials. Teams keep those values in their own blueprint file or
private blueprint repository. The public example uses fictional Acme projects:
[`examples/blueprints/acme-platform.toml`](../examples/blueprints/acme-platform.toml).

## New teammate flow

Validate and preview before creating anything:

```bash
dyro blueprint validate ./dyro-blueprint.toml
dyro join ./dyro-blueprint.toml --dry-run
dyro join ./dyro-blueprint.toml
```

In an interactive terminal, `join` shows the available development lines and
one complete plan, then asks for confirmation. In scripts and CI, pass the line
and confirmation explicitly:

```bash
dyro join ./dyro-blueprint.toml \
  --line feature-a \
  --path /workspaces/acme-platform \
  --yes
```

Without `--path`, Dyro uses
`~/DyroProjects/<workspace.suggested_directory>`. A successful join registers
the workspace in the reversible global home, so the teammate can subsequently
run `dyro` from any directory. Use `--no-register` for ephemeral automation.

## Supported sources

`SOURCE` can be:

- a local TOML file;
- a local directory containing `dyro-blueprint.toml`;
- an HTTPS URL pointing directly to a TOML file; or
- a Git repository over HTTPS, SSH, or `file://`.

For Git sources, the default file is `dyro-blueprint.toml`. Override it with
`--file path/in/repository.toml` and select a Git branch or tag with `--ref`.
Prefix an otherwise ambiguous Git URL with `git+`, for example
`git+https://example.com/acme/blueprints`.

HTTP credentials, passwords, query parameters, and fragments are rejected.
Use SSH or a Git credential helper instead of putting secrets in a blueprint or
command line.

## Contract

The v1 contract contains:

- `[workspace]`: a safe workspace ID, suggested directory, default line,
  fallback Profile base, and optional non-binding `recommended_tool`;
- `[repositories.<id>]`: remote, anchor path, development-line mount, and
  argv-array verification gates; and
- `[lines.<id>]`: one branch name plus a complete `bases` table that maps every
  repository to an immutable full commit SHA.

Moving branches and tags are not accepted as bases. During join, each
repository is cloned into a temporary sibling, verified at its pinned commit,
checked out as a detached anchor, and only then renamed into the workspace.
The selected development line receives isolated linked worktrees; it never
shares an anchor checkout.

`workspace.recommended_tool` is copied to the generated Profile and affects
the interactive home badge and order only. It cannot install a tool, inject an
install command, create an adapter, or grant delivery authority.

## Failure and retry behavior

Dyro never overwrites an unrelated non-empty target. It records the blueprint
SHA-256 and selected line in `.dyro/join.json`. A failed clone leaves completed
anchors intact and can be retried with the same command. Reusing the target with
a different blueprint digest or line fails closed.

An existing anchor is reused only when all of these remain true:

- it is a real Git directory, not a symbolic link;
- its `origin` exactly matches the blueprint;
- it is clean and detached; and
- `HEAD` exactly matches the pinned commit.

`join` never pushes, merges, copies uncommitted work, or writes into an existing
project checkout.
