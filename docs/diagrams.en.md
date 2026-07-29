# Diagram guide (onboarding)

**Primary surface**: root [README](../README.md) embeds all necessary Mermaid diagrams **inline** (GitHub renders them in the README—not image links).

This page mirrors the same sources for standalone browsing.

- **English labels**: `docs/images/diagrams/src/*.mmd` (this page)
- **Chinese labels**: `docs/images/diagrams/src/zh/*.mmd` → [`diagrams.md`](diagrams.md)

PNG exports are **not** tracked. Optional local render:

```bash
python3 scripts/render_diagrams.py          # English labels
python3 scripts/render_diagrams.py --lang zh  # Chinese labels
```

Related: [`architecture.md`](architecture.md) · [`agent-orchestration-discipline.md`](agent-orchestration-discipline.md)

| # | Diagram |
| --- | --- |
| 01 | 1. Layered architecture |
| 02 | 2. Multi-repo workspace layout |
| 03 | 3. Task state machine |
| 04 | 4. Local delivery sequence |
| 05 | 5. External evidence sequence |
| 06a | 6a. Task graph |
| 06b | 6b. Scheduling wave |
| 07 | 7. Use-case overview |
| 08a | 8a. Multi-agent layers (experiment) |
| 08b | 8b. Multi-agent sequence |
| 09a | 9a. External semantic runtime (experiment) |
| 09b | 9b. Semantic runtime sequence |

---

## 1. Layered architecture

```mermaid
flowchart TB
  subgraph Profile["Project Profile (team-supplied)"]
    P1["repositories / layout / bases"]
    P2["Agent adapter argv"]
    P3["gates / receipt templates / policy"]
  end

  subgraph Core["Dyro Core · dyro CLI (mechanism)"]
    W["workspace<br/>anchors · lines · doctor"]
    L["launch<br/>safe argv templates"]
    D["dispatch<br/>DAG · claim · state machine"]
    V["verify<br/>gates · ledger"]
    M["merge<br/>preflight · recovery · push policy"]
  end

  subgraph Runtime["Workspace runtime .dyro/"]
    R1["tasks / lines / changes"]
    R2["evidence · review · ledger"]
  end

  Profile --> Core
  Core --> Runtime
  Human["Engineer / release owner"] --> Core
  Agent["Local agent CLI"] --> L
  Runner["Isolated runner (optional)"] -.->|"evidence ZIP"| D
```

Core does not embed business rules; those live in the Profile.

---

## 2. Multi-repo workspace layout

```mermaid
flowchart TB
  WS["workspace root<br/>dyro.toml"]
  WS --> REPO["repositories/"]
  WS --> DYRO[".dyro/"]
  WS --> VER["versions/ or layout.lines"]
  WS --> WT["worktrees/ or layout.tasks"]

  REPO --> API["services/api · Git anchor"]
  REPO --> WEB["services/web · Git anchor"]

  DYRO --> LINES["lines/&lt;id&gt;.toml"]
  DYRO --> TASKS["tasks/&lt;id&gt;/"]
  DYRO --> CHG["changes/ · decisions · ledger"]

  TASKS --> TT["task.toml · handoff.md"]
  TASKS --> EV["evidence-imports/ · review.md"]

  WT --> TAPI["task/API-101/services/api"]
  WT --> TWEB["task/API-101/services/web"]

  VER --> LAPI["release-…/services/api worktree"]
  VER --> LWEB["release-…/services/web worktree"]
```

Anchors vs line/task worktrees; control state under `.dyro/`.

---

## 3. Task state machine

```mermaid
stateDiagram-v2
  [*] --> backlog
  backlog --> assigned: claim / next
  assigned --> in_progress: run starts
  in_progress --> waiting_answer: human answer needed
  waiting_answer --> in_progress: task answer
  in_progress --> review: gates pass · enter review
  in_progress --> failed: failure
  failed --> assigned: re-claim
  review --> review_pending_signoff: require_external_signoff
  review --> done: independent review PASS
  review_pending_signoff --> done: task signoff
  done --> [*]: task merge into line
```

Illegal transitions are rejected; forced overrides need explicit `--force` (see architecture).

---

## 4. Local delivery sequence

```mermaid
sequenceDiagram
  actor Eng as Engineer
  participant CLI as dyro CLI
  participant FS as Workspace Git / .dyro
  participant Agent as Agent adapter

  Eng->>CLI: setup / doctor / line create
  CLI->>FS: register line · create line worktrees
  Eng->>CLI: task create · task next
  CLI->>FS: write task.toml · allocate worktree
  Eng->>CLI: task run / open --agent
  CLI->>Agent: argv launch into task worktree
  Agent-->>CLI: work finished (not gate evidence)
  CLI->>CLI: run Profile gates
  CLI->>FS: receipt · heads · attempt
  Eng->>CLI: task review
  CLI->>FS: receipt-bound review
  Eng->>CLI: task merge --yes
  CLI->>FS: merge into line · update ledger
```

Local path: setup → task → run → gates → review → merge.

---

## 5. External evidence sequence

```mermaid
sequenceDiagram
  actor Ctrl as Control-plane operator
  participant CLI as dyro control plane
  participant Run as Isolated runner
  participant Rev as Independent reviewer

  Ctrl->>CLI: task claim --by runner-id
  CLI-->>Run: claim active · conflict group held
  Run->>Run: execute in isolation · declared gates
  Run->>CLI: evidence build → ZIP
  Ctrl->>CLI: evidence execution --bundle
  CLI->>CLI: validate ZIP · heads · gates · signing policy
  Rev->>CLI: evidence review / review-build
  CLI->>CLI: bind receipt + task heads
  opt require_external_signoff
    Ctrl->>CLI: task signoff --by approver
  end
  Ctrl->>CLI: task merge --yes
```

When `policy.execution_mode = "external"`, the control machine does not run the agent or gates.

---

## 6a. Task graph

```mermaid
flowchart LR
  subgraph Nodes
    T1["Task A"]
    T2["Task B"]
    T3["Task C"]
    D1["Decision<br/>blocked_on"]
  end

  T1 -->|depends_on| T2
  T2 -->|depends_on| T3
  T3 --> D1

  CG["conflict_group: db-migrate<br/>mutex within a wave"]
  T1 -.-> CG
  T2 -.-> CG
```

`depends_on` is hard order; `conflict_group` is resource mutex, not an edge; downstream unlocks only after merge into the line.

---

## 6b. Scheduling wave

```mermaid
flowchart TB
  Snap["Immutable schedule snapshot<br/>graph + state + claims"]
  Ready["ready set<br/>deps integrated · decisions OK"]
  Wave["current wave<br/>--parallel × conflict_group"]
  Snap --> Ready --> Wave
```

Immutable snapshot → ready set → wave with parallelism and conflict groups.

---

## 7. Use-case overview

```mermaid
flowchart LR
  subgraph Actors
    Dev["Developer"]
    Lead["Release owner"]
    Runner["Isolated runner"]
    Reviewer["Independent reviewer"]
  end

  subgraph UC["Primary use cases"]
    U1["Init workspace setup/init"]
    U2["Create line / hotfix"]
    U3["Create and schedule tasks"]
    U4["Local run and gates"]
    U5["Import external evidence"]
    U6["Independent review and signoff"]
    U7["Merge and Change Set verify"]
    U8["Audit sync to Witness"]
  end

  Dev --> U1
  Dev --> U3
  Dev --> U4
  Lead --> U2
  Lead --> U7
  Lead --> U8
  Runner --> U5
  Reviewer --> U6
  Dev --> U6
```

Primary actors and control-plane use cases.

---

## 8a. Multi-agent layers (experiment)

```mermaid
flowchart TB
  Host["Host agent<br/>current conversation"]
  Disp["local_agent_dispatch<br/>contract · guards · leases"]
  B1["Backend CLI A"]
  B2["Backend CLI B"]
  Board["Adversarial review board<br/>signed sections + final call"]
  Dyro["Dyro control plane<br/>claim · gates · merge"]

  Host -->|"TaskContract JSON"| Disp
  Disp --> B1
  Disp --> B2
  B1 -->|"summary + evidence"| Host
  B2 -->|"summary + evidence"| Host
  Host --> Board
  Board -.->|"advisory only"| Host
  Host -->|"explicit dyro commands"| Dyro
```

See `docs/agent-orchestration-discipline.md`. Experiment under `experiments/local_agent_dispatch/` (not in the installed package).

---

## 8b. Multi-agent sequence

```mermaid
sequenceDiagram
  participant H as Host agent
  participant S as DispatchSupervisor
  participant W as Worker / backend
  participant P as Review board file

  H->>S: run --wait TaskContract
  S->>S: validate files · secret guard · take slot
  S->>W: self-contained prompt + allowlisted context
  W-->>S: ResultEnvelope
  S->>S: mark locator verified
  S-->>H: run_id · summary · evidence
  H->>P: write own signed section
  Note over H,P: Never edit others' sections; source is authority
  H->>H: optional code change / PR after final call
  Note over H: Delivery still goes through dyro task/merge
```

Dispatch output is advisory; delivery still uses Dyro gates/merge.

---

## 9a. External semantic runtime (experiment)

```mermaid
flowchart TB
  Sup["Trusted Supervisor"]
  Sand["Workflow Sandbox<br/>pinned TS bundle · no vendor token"]
  Bro["Agent Broker<br/>argv provider · raw only on tmpfs"]
  HostP["Optional host provider<br/>Broker-mounted only"]

  Sup -->|start · verify bundle/claim| Sand
  Sup -->|start · pin| Bro
  Sand -->|loopback IPC| Bro
  HostP -.->|RO bind| Bro
  Sup -->|after dual cleanup| Pack["local evidence pack / dry-run"]
  Pack -.->|forbidden| Merge["merge / push / Core import"]
```

Not Core. Tree: `experiments/external_workflow_runner/`. Production status: Stage5 `NOT_READY`.

---

## 9b. Semantic runtime sequence

```mermaid
sequenceDiagram
  participant S as Supervisor
  participant B as Broker container
  participant W as Sandbox container

  S->>B: start internal net + pin
  S->>W: start shared netns · no token
  W->>B: agent.call JSON-line
  B->>B: spawn provider · destroy raw
  B-->>W: sanitized result
  W-->>S: result-envelope + artifacts
  S->>W: cleanup verify
  S->>B: stop · containers absent
  S->>S: pack only if dual cleanup OK
```

Supervisor dual-cleanup before any local pack.

---

## Newcomer 15-minute path

1. Skim §1–§2 on this page.
2. Follow [README](../README.md) Quick start (`setup` / `doctor`).
3. Walk §4: `task create → run → review → merge`.
4. For external mode, read §5.
5. Multi-agent: read §8 only; never treat dispatch output as a gate.

## Maintenance

- Edit `docs/images/diagrams/src/*.mmd` (English) or `src/zh/*.mmd` (Chinese), then refresh the matching guide / README embeds.
- On conflict with code, code and `architecture.md` win.
