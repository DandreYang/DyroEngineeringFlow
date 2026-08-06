# Dyro Agent Bridge Phase 0 Acceptance Matrix

Status: Proposed release gate

Authority: [ADR 0006](../adr/0006-agent-bridge-phase-0.md)

Protocol: [Agent Bridge Phase 0 Protocol](agent-bridge-protocol.md)

Inventory: [Agent Bridge Operation Inventory](agent-bridge-operation-inventory.md)

## 1. Gate policy

Phase 0 remains No-Go until every required gate below has reproducible evidence
from current source and installed artifacts. A passing unit test with mocked
filesystem, subprocess, network, or HOME is not sufficient for a corresponding
black-box gate.

The gate records:

- commit SHA and dirty-state scope;
- Python and OS version;
- installation source: checkout, wheel, or sdist;
- exact operation and schema versions;
- command or harness invocation;
- expected and actual exit/result;
- captured filesystem/process/network evidence;
- unresolved host behavior marked `须人工核`.

## 2. Required gate matrix

| ID | Area | Required evidence | Stop condition |
| --- | --- | --- | --- |
| A01 | Exposure | Generated catalog contains only approved IDs; every Mandatory Core Surface operation is `public_available` in source, wheel and sdist; mutation words/tools absent | Empty surface, unavailable mandatory operation, or any excluded operation is available |
| A02 | Call graph | Each available operation has a reviewed Core call graph and completed evidence template | Unknown call, recovery, write, network, or process path |
| A03 | Core boundary | Bridge imports no `dyro.cli` handler and parses no human-rendered output | Import/call of `cmd_*` or terminal renderer |
| B01 | Zero write | Read-only HOME, XDG, `DYRO_HOME`, workspace and temp audit show no new path or write-open | Any file/dir/lock/cache/temp/mtime/ledger mutation |
| B02 | No recovery | Injected pending Objective transaction remains byte-identical after every R0/PLAN call | Recovery, repair, lock, or state transition occurs |
| B03 | No network | Socket/DNS/connect traps see zero attempts | Any external or loopback connection attempt |
| B04 | Process boundary | Process trap sees only the documented descriptor-binder argv followed by the per-operation Git read allowlist | Agent, gate, host tool, shell, or unknown process starts |
| B05 | Git read | Git observations disable optional locks; index and repository metadata remain stable on supported OSes | Index refresh, lock creation, fetch, hook, or remote access |
| B06 | Workspace bounds | File/count/aggregate-byte/deadline limits and per-record isolation pass for Profile, tasks and Objectives | Unbounded read, request-wide erasure from one bad record, or missing partial/truncated marker |
| C01 | Resolver | Explicit/local/default/unique precedence passes; malformed local never falls back | Wrong project selected or recent state written |
| C02 | Partial failure | Stale/unreadable workspace and corrupt Objective produce bounded component errors | Healthy components disappear or raw exception leaks |
| C03 | Permission | Registry/workspace combinations inside and outside host sandbox return stable permission codes | Traceback, fallback, hang, or write attempt |
| C04 | Integration semantics | Summary reports `not_inspected` and omits final readiness; authoritative explain/status/plan is unavailable until B05 | Unknown integration rendered as ready, blocked, pending, or dispatchable |
| D01 | JSON stdout | While stdout is writable, success and every error emit exactly one bounded JSON object and one newline; broken pipe exits 5 without retry/traceback | ANSI, progress, help, traceback, second output, or retry after broken pipe |
| D02 | Input bounds | Oversize, duplicate keys, trailing bytes, invalid UTF-8 and deep/numerous structures fail before Core access | Partial parse or unbounded resource use |
| D03 | Schema | Unknown fields/operations/major versions and excluded operations fail closed | Coercion, fallback, or accidental new exposure |
| D04 | Redaction | Secret corpus in input, Profile, argv, remote, exception and stdout/stderr never reaches response | Raw secret/path/argv/log appears |
| D05 | Plan semantics | Every plan is deterministic, `executable=false`, `authorization=none`, with typed read set and planner revision | Apply hint, authorization claim, missing semantic binding |
| E01 | Wheel | Clean venv outside checkout imports Core/Bridge, finds schemas and runs all smoke cases | Checkout-relative import/resource dependency |
| E02 | Sdist | Clean build/install outside checkout behaves identically to wheel | Missing file, schema, entry point, or result drift |
| E03-Core | Core version skew | Protocol major/minor, operation schema and planner revision current/N-1 fixtures plus incompatible future/unknown values are tested | Silent downgrade, digest reuse, or new operation exposure |
| E03-Integration | Integration skew | At S7, Core-newer, integration-newer, N/N-1, missing optional MCP dependency and tool-list pinning are tested | Silent downgrade or widened MCP/Plugin tool exposure |
| F01 | Codex discovery | Fresh Codex session discovers only the intended Skill/tools after previewed install | Missing, duplicate, or stale discovery |
| F02 | Codex sandbox | Real workspace-write sessions pass in-sandbox and out-of-sandbox read scenarios | MCP/Bridge bypasses or misreports host permissions |
| F03 | Trigger precision | Ten fresh-session journeys choose Bridge only for Dyro inspect/plan and choose `dispatch` for advisory panels | False activation or wrong boundary |
| F04 | Context budget | Skill, compact catalog, one fetched schema and typical response stay under recorded byte/token budgets | Full registry injected or unbounded output |

## 3. Test layers

### Layer 1: unit and contract

Planned test modules:

- `tests/test_bridge_models.py`
- `tests/test_bridge_catalog.py`
- `tests/test_bridge_resolution.py`
- `tests/test_bridge_observations.py`
- `tests/test_bridge_plans.py`
- `tests/test_bridge_transport.py`
- `tests/test_bridge_redaction.py`

These tests inject forbidden writers, network functions, subprocess launchers,
recovery paths, and renderers that raise immediately if called. They also hold
golden JSON-schema, workspace/config identity, canonical-digest, and Core
version-skew vectors. In-process traps are one evidence layer and cannot replace
process-level or host evidence.

### Layer 2: local black box

Planned harness: `tools/verify_bridge_zero_effects.py`.

It creates an isolated fixture, snapshots every permitted root and relevant
metadata, runs the installed `dyro-bridge`, and compares the result. It denies
socket creation, records process starts, makes state roots read-only, and
injects pending recovery state. A test that merely points `DYRO_HOME` to a
writable temporary directory does not satisfy B01.

Linux Ubuntu 24.04 is the reference process-level audit: pinned CI tooling uses
`strace -ff` to capture file mutation syscalls, socket/connect/DNS paths, and
`execve`, plus before/after Git metadata snapshots. The report includes the
exact trace filter and known blind spots. The authoritative Git adapter must
also prove that worktree, Git directory, common directory, and object store are
opened before launch and passed only as `/proc/self/fd` references, config
includes/extensions are rejected, hooks/credentials/commit-graph use are
overridden, and the binder's Landlock policy denies writes and reads outside
the approved directory objects before Git executes. Linux `strace` evidence
must confirm that remaining validated repository config cannot widen process,
network, or filesystem effects.

The Linux gate requires Landlock ABI 3 or newer and a real test whose Git
executable reaches the denied write syscall. SHA-1 repositories are the Phase 0
surface; SHA-256 object format and other repository extensions must fail closed
before Git starts. Consistent with ADR 0006, these gates do not claim an
immutable snapshot against an actively malicious same-identity process.

macOS 15 combines in-process traps, read-only roots, before/after filesystem and
Git metadata snapshots, fake-PATH process recording, and a real managed Codex
sandbox run. A platform-level network/process observer not available to the
test account is recorded as `须人工核`; mocks or fake PATH alone cannot close that
gate. Authoritative Git-dependent plans return `OPERATION_UNAVAILABLE` on macOS
until an equivalent descriptor or OS-snapshot proof exists. Windows is
import/fail-closed only in Phase 0 and does not count as a supported Objective
operation platform.

### Layer 3: artifact

CI builds wheel and sdist, installs each into a new environment outside the
checkout, and runs the same protocol corpus. Artifact tests verify console
entry points, packaged schemas, `-I` imports, every Mandatory Core Surface
operation, and Core protocol/schema/planner compatibility. Missing MCP optional
dependencies and integration version skew belong to E03-Integration at S7, not
the Phase 0 Core gate.

### Layer 4: real host

Codex acceptance is manual-plus-scripted because the actual sandbox and tool
discovery boundary belongs to the host. The evidence records the host version,
permission profile, installed integration version, tool list, request, response,
and filesystem/process/network audit.

Claude, Cursor, and OpenCode remain `须人工核` and unsupported until equivalent
evidence exists for each host.

## 4. Resolver journeys

| Journey | Expected result |
| --- | --- |
| Explicit valid alias from unrelated directory | `resolution_source=explicit` |
| Valid local Profile plus different registry default | local Profile wins |
| Malformed local Profile plus valid default | `LOCAL_PROFILE_INVALID`; no fallback |
| No local Profile and valid default | `resolution_source=default` |
| No default and one usable registration | `resolution_source=unique` |
| No default and multiple usable registrations | `AMBIGUOUS_WORKSPACE` |
| Explicit stale alias | `REGISTERED_ROOT_STALE` |
| Corrupt registry | `REGISTRY_INVALID`; file remains unchanged |
| Registry readable, workspace denied by host | `HOST_READ_PERMISSION_REQUIRED` |
| Nothing discoverable | `WORKSPACE_NOT_FOUND` |

Every journey asserts that registry recent/default fields and file metadata are
unchanged.

## 5. Protocol corpus

The shared corpus covers:

- valid public request for every Mandatory Core Surface operation and every
  other public-available operation;
- `declared` and `implemented_testable` operations remain unavailable through
  the public process;
- unknown protocol major and future-minor behavior, including explicit
  `PROTOCOL_MINOR_UNSUPPORTED` without silent downgrade;
- unknown, unavailable, and excluded operation IDs;
- missing, extra, wrong-type, oversized, duplicate, and deeply nested fields;
- invalid UTF-8, two concatenated JSON values, and trailing bytes;
- partial component errors;
- non-ASCII identifiers and messages;
- deterministic canonical plan digest;
- output truncation at collection and byte limits;
- secret patterns in every request and source surface;
- broken pipe exit 5 with no response retry or traceback, and interrupted input
  with a structured response while stdout remains writable;
- oversized `task.toml`, oversized Objective journal, excessive record counts,
  aggregate-byte/deadline exhaustion, and one malformed record beside healthy
  siblings;
- `integration_inspection=not_inspected` never rendered as ready, pending,
  blocked, integrated, or dispatchable;
- plan `projection` preserves selected/blocked/attention/wave facts, and digest
  vectors prove allowlist/redaction occurs before canonical hashing.

The same corpus runs against in-process Core, source-tree `dyro-bridge`, wheel,
sdist, and later MCP mapping.

## 6. Fresh-session product journeys

1. “列出 Dyro 中登记的工程。”
2. “我现在在哪个 Dyro 工程？”
3. “这个工程为什么不可用？”
4. “列出当前开发线和任务。”
5. “TASK-42 为什么被阻塞？”
6. “展示任务依赖图。”
7. “当前 Objective 下一步可能做什么？”
8. “解释 Objective 为什么在等待。”
9. “帮我找三个 Agent 对方案进行评审。”——必须选择 outbound `dispatch`
10. “执行 gates/合并/推送。”——Bridge 必须拒绝并说明 Phase 0 无执行能力

The test starts without prior conversation context and records tool selection,
catalog/schema bytes, response bytes, false activations, retries, and whether
the model fabricates an apply path.

## 7. Context budgets

Initial budgets to validate and revise with measured evidence:

| Artifact | Initial maximum |
| --- | ---: |
| Host-neutral `SKILL.md` | 8 KiB |
| Compact capabilities response | 12 KiB and 64 operations |
| One operation request+response schema | 32 KiB |
| Typical R0 response | 64 KiB |
| Error response | 8 KiB |
| Warning count | 64 |

Budgets are byte gates. Token measurements are recorded for supported hosts but
do not replace byte limits because tokenizers vary.

## 8. Go/No-Go checklist

Phase 0 Core and JSON transport become Go only when A01–E02 and E03-Core pass.
Skill beta requires the same Core gates plus F01, F03, and F04. Codex read-only
MCP/Plugin additionally requires E03-Integration and F02. E03-Integration does
not block Core JSON or Skill work before an integration artifact exists.

Any discovery of mutation, recovery, network, unexpected process execution,
secret leakage, silent workspace fallback, CLI handler reuse, or Agent apply
surface returns the affected module to No-Go and reopens ADR review.

Passing Phase 0 does not authorize R1/R2/R3 design, implementation, release, or
publication.
