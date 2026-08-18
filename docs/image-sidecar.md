# Optional image sidecar (`local-image-gen`)

`local-image-gen` is a sibling first-party CLI. It is **not** a Dyro coding tool,
**not** a managed Skill seat, and **not** part of Objective / Change Set / gates
/ merge / push. Dyro only helps a person discover it and read a normalized
health report. Image generation stays on the upstream command.

Official source: <https://github.com/DandreYang/local-image-gen>

## What Dyro does

| Command | What it is allowed to do |
| --- | --- |
| `dyro doctor` | Cheap `PATH` lookup for the `local-image-gen` wrapper. JSON adds `sidecars.local_image_gen.state` = `absent` or `present`. Missing sidecar never fails the workspace. |
| `dyro image doctor` | The only command that may spawn `local-image-gen --doctor`. Reports `absent`, `needs_setup`, `ready`, or `unavailable`. |
| `dyro image install` | Prints the official repository and `install.sh` URL. `--yes` opens the GitHub page. Dyro never runs a remote install script. |
| `local-image-gen …` | Actual generation. Dyro does not wrap billed generate. |

```bash
dyro doctor --format json
dyro image doctor --format json
dyro --dry-run image doctor
dyro --dry-run image install
dyro image install --yes
```

`dyro --dry-run doctor` and `dyro --dry-run image doctor` must not spawn the
sidecar. `dyro --dry-run image install` must not open a browser.

Do **not** install this through `dyro tool install`. Home / `dyro open` stay
coding-tool launchers.

## Workspace output

When a Dyro workspace (`dyro.toml`) is an ancestor and the user omits
`-o` / `--out-dir`, `local-image-gen` writes to `<workspace>/outputs/images/`.
Those files are generated artifacts: they are not Proof, they do not belong in
`repositories/`, and they are not a task worktree. Workspace `doctor()` does
not treat `outputs/images/` as structural damage.

The Codex image path is experimental on the upstream side. Confirm current
status in that repository before relying on it.

## What the navigator seat must not do

`dyro-control-plane` does not run `dyro image`. Isolated Console does not
allowlist `image doctor` or `image install` (`install --yes` opens a browser).
Personal skill directories are never scanned to discover this sidecar. Only the
PATH wrapper named `local-image-gen` counts as installed.

## Normalized `dyro image doctor` JSON

Dyro does not pass through the upstream `--doctor` document. Default output
omits local paths, login files, `api_base`, and secrets. `--include-paths`
may add `output_dir` and `workspace` only.

```json
{
  "id": "local-image-gen",
  "optional": true,
  "state": "absent",
  "version": "0.1.0",
  "usable_providers": ["grok", "codex"]
}
```

`ready` means the upstream report succeeded and at least one provider has a
subscription or API key. A wrapper on PATH with no backend is `needs_setup`,
not a workspace error.
