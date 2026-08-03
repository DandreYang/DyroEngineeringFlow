# ADR 0005: Local read-only web console

## Status

Proposed

## Context

Dyro can already register multiple workspaces globally, inspect delivery lines
and task state, explain a TaskGraph, verify evidence, and expose a single safe
next action through Home. The native continuation design adds durable
Objectives, attention, budgets, triggers, action receipts, and recovery state.

Those capabilities remain difficult to understand as a whole when an operator
is responsible for several projects or when a newcomer does not yet know the
CLI vocabulary. Requiring a sequence of list, status, graph, attempt, evidence,
and objective commands makes routine observation slower than it needs to be.

A browser view can improve discoverability and situational awareness, but it
also creates two architectural risks:

1. a dashboard may accidentally become a second state store or a second
   scheduler; and
2. a local HTTP listener is reachable by arbitrary browser pages and must not
   be treated as safe merely because it binds to loopback.

## Decision

Dyro will add **Dyro Console**, a local, read-only web surface over the same
authoritative state used by the CLI.

1. `dyro console` starts a foreground HTTP server on `127.0.0.1` with an
   operating-system-assigned port, opens the user's browser after the listener
   is ready, and stops on Ctrl-C. `--no-open` supports headless local use.
2. The Console shows all globally registered workspaces and lets the user drill
   into lines, Objectives, tasks, attempts, gates, review, sign-off, integration,
   attention, activity, and local health.
3. A Core-owned, presentation-neutral `WorkspaceReadSnapshot` composes the
   shared scheduler snapshot, TaskGraph, AttentionItems, evidence metadata,
   workspace registry, and cached tool/update state in one capture. Console
   DTOs only slice and further redact that snapshot. The browser never parses
   CLI text and the Console never serializes internal objects directly.
4. The Console has no delivery mutation API in its first release. It cannot
   execute, review, sign off, merge, push, import evidence, answer a task,
   change an Objective, install a tool, or update Dyro. Recommended actions are
   displayed as copyable CLI commands that still pass through existing policy,
   confirmation, and audit paths.
5. No Console database or durable cache is introduced. In-memory caches are
   bounded and disposable. Refreshing or opening the Console does not update
   recent-workspace state, task state, Objective state, or ledgers.
6. Global aggregation is best-effort and explicitly partial: every workspace
   has its own capture time, digest, health, and error. One stale or invalid
   workspace cannot hide healthy workspaces or crash the whole overview.
7. Potentially blocking registry, Profile, task, evidence, Objective, and Git
   inspection runs in bounded child processes. The HTTP process can terminate
   a worker and its process group at a hard deadline; a stuck filesystem call
   therefore cannot consume a request thread forever or prevent shutdown.
8. Static assets ship inside the wheel and sdist. The browser loads no CDN,
   remote font, remote script, analytics, telemetry, or service worker. Console
   requests do not perform external network access; update information comes
   only from the CLI's existing cached update state.
9. The first release is local-only. It exposes no public bind option, remote
   access mode, account system, team sharing, or background daemon. A shared
   service would require a separate decision covering authentication, TLS,
   authorization, synchronization, and deployment operations.
10. The Console ships as part of Dyro `v0.6.0`, on the same release train as
    the native continuation engine. That does not grant it delivery authority:
    the Console remains read-only, while automatic continuation remains
    disabled by default and requires an explicit local lease.

## Security decision

Loopback is a network boundary, not an authentication boundary. The first
release therefore uses all of the following controls:

- bind only to the numeric IPv4 loopback address and use an ephemeral port by
  default;
- accept only the exact `Host` value for that listener and never honor proxy
  headers;
- serve an unauthenticated static shell containing no workspace data;
- place a high-entropy, one-time bootstrap secret in the URL fragment, which
  is not sent in the initial HTTP request;
- exchange that secret once for a separate, short-lived bearer session; keep
  the bearer only in the current tab's `sessionStorage`, clear the fragment
  immediately, and send it in the `Authorization` header;
- never use a localhost cookie, because cookies are shared across ports for the
  same host; require the bearer on every data request and reject unexpected
  `Origin`, cross-origin requests, CORS, and unsupported methods;
- emit a strict Content Security Policy, `frame-ancestors 'none'`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, and
  `Cache-Control: no-store` for authenticated data;
- use text-only DOM rendering for state-derived strings and reject unsafe asset
  paths, encoded traversal, symlinks, oversized requests, slow requests, and
  excess concurrency.

The authoritative session record and bootstrap secret exist only in server
process memory. The browser never places either secret in a query, cookie,
`localStorage`, or IndexedDB. Server request logging is disabled. Closing the
tab discards its `sessionStorage`; closing the process invalidates every bearer.

## User experience contract

The default overview answers three questions without requiring Dyro-specific
terminology:

1. Which projects are healthy, active, waiting, or in need of repair?
2. What is running or blocked, and why?
3. What is the single safest next action for each affected project?

The Console uses progressive disclosure. The overview emphasizes attention and
active work; task hashes, attempt bindings, graph constraints, and evidence
metadata are available in detail views. Every degraded state includes a stable
reason code, a short explanation, and one copyable recovery command. Status is
never communicated by color alone, and the task graph has a keyboard-readable
list or table equivalent.

Running `dyro console` from any directory requires no Profile editing. The
current workspace is focused when it can be resolved; otherwise the registered
default is focused. `--workspace <alias>` selects the initial workspace, while
`--root <path>` can inspect one explicit Profile without registering it.

## Consequences

The Console gives newcomers and multi-project operators a coherent view while
preserving Dyro's control-plane boundaries. The package gains a small HTTP
server, bundled static assets, a versioned read API, browser security tests,
and accessibility requirements. `v0.5.7` remains maintenance-only; `v0.6.0`
is released only after both the Console and continuation-engine gates pass.
Because the first Console release is read-only and foreground-only, it
deliberately postpones convenient but higher-risk browser actions and remote
team access.

## Non-goals

- A second TaskGraph, scheduler, Objective store, evidence store, or completion
  state machine.
- Executing delivery actions from the browser in the first release.
- Displaying raw prompts, raw command lines, environment variables, credentials,
  full gate logs, provider output, or absolute filesystem paths by default.
- Replacing the CLI, desktop coding tools, or the bare `dyro` Home.
- Exposing a listener on a LAN, public interface, SSH-forwarded deployment, or
  hosted control plane.
- Triggering update checks, package installation, or tool installation from a
  page refresh.

## Acceptance criteria

- A new user can run `dyro console` from an unrelated directory and understand
  the state and recommended next action of every valid registered workspace.
- A malformed Profile, unreadable repository, timed-out Git probe, corrupt
  Objective, or stale registry entry appears as a bounded partial failure and
  does not suppress other workspaces.
- A worker blocked indefinitely in a filesystem read is terminated with its
  process group; the overview and Ctrl-C shutdown still complete within their
  documented deadlines.
- Every task status, graph edge, Objective result, attention item, and evidence
  marker is derived from the same Core APIs used by the CLI; no CLI output is
  scraped and no workspace truth is copied into Console storage.
- Opening, navigating, polling, losing the browser connection, and stopping the
  server produce no workspace, registry, ledger, or Objective writes.
- Requests without a valid session, with an invalid Host or Origin, using an
  unsupported method, or attempting traversal are rejected without disclosing
  workspace data.
- Source-tree and clean wheel/sdist installations serve identical local assets
  and API schema without downloading frontend resources.
- The overview remains responsive with 50 registered workspaces and a 1,000
  task synthetic workspace; slow details degrade independently and never make
  the page wait indefinitely.
- Keyboard-only navigation, visible focus, reduced motion, semantic status
  labels, responsive layouts, and the graph's non-visual equivalent pass the
  documented accessibility gate.
