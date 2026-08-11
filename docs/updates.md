# Safe Dyro updates

Dyro keeps update checks outside every project. Preferences and the last daily
check live in `updates.json` under the user-level Dyro state directory
(`$DYRO_HOME`, the platform configuration directory, or `~/.config/dyro`). No
workspace path, project data, machine identifier, or usage event is sent.

## Daily check

An interactive `dyro`, `dyro home`, or `dyro start` launch checks
`https://pypi.org/pypi/dyro/json` at most once per local calendar day. The
request uses a 1.5-second timeout and a bounded response. Network, parsing,
lock, or state-write failures are ignored by the launcher so entering a
workspace keeps working. The state lock is released before network access and
uses a short acquisition timeout, so concurrent terminal launches do not queue
behind a slow request. Script-oriented commands, non-interactive terminals,
dry runs, help, version output, and dispatch commands do not perform an
automatic check.

Use an explicit command to retry or to control the saved preference:

```bash
dyro update          # same as: dyro update check
dyro update check
dyro update enable
dyro update disable
```

## First-run choice

Interactive `dyro setup` presents these controls as a small, recommendation-led
choice before its final confirmation. Daily detection is recommended on;
patch-only automatic updates are recommended off. The preview states the exact
choice and the final confirmation writes it to the user-level state directory.
Cancelling setup leaves both project files and update preferences untouched.

`dyro setup --non-interactive` leaves existing update preferences unchanged,
which keeps scripts and CI free of user-level side effects.

Set `DYRO_NO_UPDATE_CHECK=1` for a process-level opt-out without changing the
saved preference. The same gate currently also skips the interactive startup
repair of an outdated managed Skill (they share the daily-update launch
filter).

## Installing an update

`dyro update now` fetches the latest stable `X.Y.Z` version, shows the exact
plan, and asks before writing. For automation, review the plan first and then
use `dyro update now --yes`. Dyro recognizes its active `uv tool` or `pipx`
environment and otherwise uses the active Python interpreter's `pip`. A normal
virtual environment without pip falls back to `uv pip --python` when uv is
available. Commands are fixed argument arrays, never shell strings or
instructions returned by the network. The requirement is pinned to the version
that was checked and Dyro verifies the installed distribution version after the
command succeeds.

## Control-plane Skill

Interactive `dyro setup` can install the Skill during personal preferences
(preview in the plan, applied only after confirmation). After a successful
`dyro update now` (or patch auto-update), Dyro best-effort syncs an **already
managed** Skill via the fresh `dyro` entry point. On interactive
`dyro` / `dyro home` / `dyro start` launches, an **outdated** managed Skill is
repaired automatically. First-time install is never forced on upgrade or
startup; use setup or:

```bash
dyro integration install skill --dry-run
dyro integration install skill --yes
dyro integration sync skill --yes   # upgrade-only; skips absent installs
```

(`codex` is an alias for `skill`.)

Editable source installations are deliberately rejected. Update those through
their Git checkout so a convenience command cannot replace a development
environment with a published wheel.

## Optional patch automation

```bash
dyro update auto on
dyro update auto status
dyro update auto off
```

This preference is off by default. When enabled, only a higher patch within the
same major and minor line can install automatically, such as `0.5.5` to
`0.5.6`. A minor or major change still produces a notice and requires
`dyro update now`. An installation failure is reported but does not prevent the
current Dyro launch from continuing.
