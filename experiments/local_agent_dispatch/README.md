# Local Agent Dispatch (ADR-0002)

First-party **local multi-agent dispatch** (shipped with the `dyro` wheel).
Entry points: `dyro dispatch …` or `python -m experiments.local_agent_dispatch …`.
Never merges, pushes, or signs off — delivery still uses Dyro Core gates/review/merge.

Design: [`docs/designs/optional-local-agent-dispatch.md`](../../docs/designs/optional-local-agent-dispatch.md)  
Discipline: [`docs/agent-orchestration-discipline.md`](../../docs/agent-orchestration-discipline.md)

## Stages

| Level | Capability |
| --- | --- |
| L0 | TaskContract, ContextGuard, LocatorVerify, process identity |
| L1 | RunStore, dual-scope slot leases, strict shadow integration |
| L2 | bounded adapters for Codex, Claude, Cursor, OpenCode, Grok, Hermes, Kimi, DSH, Pi, and AGY (runner only; readiness blocked) + CLI `run` / `result` |
| L3 | `panel`, skill render, routes, `gc` |
| L4 | persistent Batch V1 `plan` / `start` / `status` / `result` / `cancel` |

## CLI

```sh
# After pipx/pip install dyro (or from a repo checkout):
dyro dispatch doctor
dyro dispatch backends

# 默认异步派发并立即返回 run_id；需要同步等待时显式加 --wait。
dyro dispatch run --project . --file task.json --backend codex --allow-unconfined-provider
dyro dispatch run --project . --file task.json --wait --backend claude --allow-unconfined-provider
# Echo is a deliberate test simulation, never an automatic fallback:
dyro dispatch run --project . --file task.json --backend echo --allow-offline-simulation
dyro dispatch result <run_id>
dyro dispatch panel --project . --file task.json --members codex,claude
# Explicit full-harness comparison; all ready Providers, at most four at once:
dyro dispatch panel --project . --file task.json --members all

# Different independent roles: plan without state, review the digest, then start.
dyro dispatch batch-plan --project . --file batch.json
dyro dispatch batch-start --project . --file batch.json --expect-plan-sha256 <sha256>
dyro dispatch batch-status <orchestration_id>
dyro dispatch batch-result <orchestration_id> --wait --timeout 300
dyro dispatch batch-cancel <orchestration_id>
dyro dispatch skill-render --write
dyro dispatch gc --dry-run

# Install the managed host Skill (separate from dyro-control-plane):
dyro integration install dispatch --dry-run
dyro integration install dispatch --yes

# Equivalent module form:
python3 -m experiments.local_agent_dispatch doctor
```

State home: `~/.dyro/local-agent-dispatch/` (override with `--home` or `DYRO_LOCAL_AGENT_DISPATCH_HOME`).

Batch V1 is the advisory plane: two to four independent member contracts,
installed Provider candidates without starting authentication CLIs, and at most
one scratch edit writer. Simultaneous delivery writes use Core peer wave
(`docs/designs/peer-wave-execution.md`), not a second Batch writer. `batch-start` actively authenticates every selected Provider before
creating state. Its reviewed
plan digest binds the canonical project root, normalized contracts, Provider
choices and non-secret execution profiles, guarded context digests, timeouts, and
the Git HEAD for an edit member.
The worker rechecks its planned context digest immediately before Provider use;
edit members require clean tracked context files and create their detached
worktree at the reviewed object ID. Starting the same live request and plan is
idempotent. GC retains a small request tombstone and rejects later reuse of a
garbage-collected `request_id`, preventing an old retry from silently starting
another billed batch. Batch V1 intentionally has no
DAG dependencies, queue beyond four members, automatic retry, or automatic
judge. `panel --members all` remains the explicit synchronous full-ready-Provider
comparison.

Process supervision for `run`, `panel`, and the internal worker is supported on
POSIX hosts (Linux and macOS). This supervision covers the dedicated process
group, not a container; an intentionally daemonized child that escapes the group
is outside the guarantee, which is one reason real Providers remain explicitly
unconfined. Windows can import the shipped package and use
read-only discovery such as `dispatch --dry-run doctor`, but execution fails
closed until a Windows process-tree backend is implemented.

Global `--dry-run` backend and doctor output is passive: it reports command
availability with `authentication_probe=not_run` and never starts a Provider
authentication CLI. Run non-dry `dyro dispatch backends` only at an authorized
execution boundary to verify active login readiness.

## Task JSON shape

See design §4 (five-part contract). `auto` considers only integrated, authenticated
Providers in a deterministic preference order and fails closed when none are ready.
Integrated Provider IDs are `codex`, `claude`,
`cursor-agent`, `opencode`, `grok`, `hermes`, `kimi`, `dsh`, and `pi`. A Provider
is routable only when its backend-specific authentication probe succeeds. Cursor
supports read-only and edit dispatch via its own CLI login (`cursor-agent status`)
or `CURSOR_API_KEY`. Read-only adds `--mode ask`; edit adds `--force`.
Both use `--print --output-format json --sandbox disabled --workspace <cwd>
--model <model> --trust` and a temporary task-file prompt, with cwd pinned to the
projection or scratch edit worktree. Empty output is a failure.

Credential evidence: installed Cursor 2026.11.11-b912975 `index.js`, modules
`cli-credentials/dist/index.js` and `src/credential-manager.ts`, select keychain
by default on macOS, file storage elsewhere, and accept
`AGENT_CLI_CREDENTIAL_STORE=file`. `getAuthFilePath` uses `~/.cursor/auth.json`
on macOS and `$XDG_CONFIG_HOME/cursor/auth.json` (default `~/.config/cursor`)
on Linux. This machine has `~/.cursor/cli-config.json` but no file auth;
its default login uses the macOS keychain. No keychain secrets are exported.
File-backed login copies only private `auth.json` into a per-run HOME with an
isolated XDG config directory and forces file storage. No CLI config is needed
with the explicit flags; `cli-config.json`, MCP configuration and plugins are
not copied. API-key runs use an empty isolated home. If file credentials cannot
be copied, real HOME is used only with `allow_unconfined_provider=true`, and a
warning names user MCP servers and plugins that may load. Project-local Cursor
configuration can still load; these are unconfined Provider executions.

AGY (`agy`) has a read-only print/plan/JSON runner with a print timeout and a
no-shell prompt, but automatic routing remains disabled: its help exposes no
non-generating authentication-status command. AGY edit mode is unsupported.
MCode remains unsupported: `mcode exec` offers JSON output but no read-only or
plan mode (its `--permission` is smart/full/off). Qoder remains unsupported: the
locally installed `qodercli --permission-mode` accepts only bypass_permissions,
dont_ask and auto, with no plan mode. No flags are inferred for either CLI.
Kimi availability wording still differs between discovery, batch planning and
readiness; unifying it without spawning the CLI during passive discovery is open.
Authentication failures are retried once and backend
rows include failure reasons. Output-limit errors preserve any validated final
answer already received and mark `usage.partial=true`; protocol failures retain a
bounded, secret-filtered stdout tail in warnings.
Kimi binds one selected Provider/model route into the execution-profile digest;
file-backed OAuth copies only that selected token into a per-run home. Keyring-backed
Kimi OAuth is discovered but fails closed because Dyro cannot isolate it. DSH runs
with a reviewed headless patch that pins `deepseek-official/deepseek-v4-flash`.
Backend `echo` is
an explicit offline simulation: task JSON must set `allow_offline_simulation: true`
and callers must not treat its low-confidence result as a Provider conclusion.
Real non-strict Provider calls require `allow_unconfined_provider: true`; read-only
calls receive a guarded context projection, which is not OS-level isolation.
`strict: true` is fail-closed: the selected adapter must declare a verified strict-isolation capability. The shipped external Provider adapters do not; use `echo` only for protocol validation, and reject strict work until an adapter can prove the required isolation.
Edit runs execute in a detached Git worktree and return a hash-bound patch reference; they do not mutate, commit, or push the source worktree.

Single-run contracts may set finite positive `timeout_seconds`; explicit CLI
`--timeout` overrides it. Without either, edit runs use 1800 seconds and read-only
runs use 120 seconds. Batch member timeouts retain their existing behavior.
Unsuccessful results have low confidence and `patch_partial=true` when they
include a patch reference. Envelope success remains `status=ok` (run `completed`).
Evidence `verified` means the file locator is valid, not that its claim is true.
Line locators support comma-separated ranges, `L` prefixes, and hyphen, en dash,
or em dash separators; every range must be within the file.

## Tests

```sh
python3 -m unittest tests.test_local_agent_dispatch tests.test_local_agent_dispatch_l1_l4 -v
```
