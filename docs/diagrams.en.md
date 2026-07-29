# Diagram guide (onboarding)

Mermaid diagrams for DyroEngineeringFlow. Rendered natively on GitHub.

Chinese edition: [`diagrams.md`](diagrams.md)

| Diagram | Purpose |
| --- | --- |
| [1. Layered architecture](#1-layered-architecture) | Core vs Profile |
| [2. Multi-repo workspace layout](#2-multi-repo-workspace-layout) | On-disk structure |
| [3. Task state machine](#3-task-state-machine) | Legal transitions |
| [4. Local delivery sequence](#4-local-delivery-sequence) | Line → merge |
| [5. External evidence sequence](#5-external-evidence-sequence) | Claim → import → signoff |
| [6. Task graph and scheduling](#6-task-graph-and-scheduling) | depends_on / conflict_group |
| [7. Use-case overview](#7-use-case-overview) | Actors and main uses |
| [8. Multi-agent collaboration](#8-multi-agent-collaboration-dev-side) | Dispatch / review board / control plane |
| [9. Optional external semantic runtime](#9-optional-external-semantic-runtime-experiment) | Sandbox / Broker / Supervisor |

See also: [`architecture.md`](architecture.md) (Chinese technical detail).

---

## 1. Layered architecture

```mermaid
flowchart TB
  subgraph Profile["Project Profile (team-owned)"]
    P1["repositories / layout / baselines"]
    P2["Agent adapters (argv)"]
    P3["gates / receipts / policy"]
  end

  subgraph Core["Dyro Core · dyro CLI"]
    W["workspace"]
    L["launch"]
    D["dispatch"]
    V["verify"]
    M["merge"]
  end

  subgraph Runtime["Workspace runtime · .dyro/"]
    R1["tasks / lines / changes"]
    R2["evidence · review · ledger"]
  end

  Profile --> Core
  Core --> Runtime
  Human["Engineer / release owner"] --> Core
  Agent["Local Agent CLI"] --> L
  Runner["Isolated runner (optional)"] -.->|"evidence ZIP"| D
```

---

## 2. Multi-repo workspace layout

```mermaid
flowchart TB
  WS["workspace root · dyro.toml"]
  WS --> REPO["repositories/ anchors"]
  WS --> DYRO[".dyro/ control state"]
  WS --> VER["versions/ line worktrees"]
  WS --> WT["worktrees/ task worktrees"]

  REPO --> API["services/api"]
  REPO --> WEB["services/web"]
  DYRO --> TASKS["tasks/<id>/"]
  WT --> T1["task/<id>/… mounts"]
```

```text
workspace/
  dyro.toml
  repositories/…
  versions/<line>/…
  worktrees/task-<id>/…
  .dyro/lines|tasks|changes|ledger.jsonl
```

---

## 3. Task state machine

```mermaid
stateDiagram-v2
  [*] --> backlog
  backlog --> assigned
  assigned --> in_progress
  in_progress --> waiting_answer
  waiting_answer --> in_progress
  in_progress --> review
  in_progress --> failed
  failed --> assigned
  review --> review_pending_signoff
  review --> done
  review_pending_signoff --> done
  done --> [*]
```

---

## 4. Local delivery sequence

```mermaid
sequenceDiagram
  actor Eng as Engineer
  participant CLI as dyro CLI
  participant FS as Git / .dyro
  participant Agent as Agent adapter

  Eng->>CLI: setup / line create / task create
  CLI->>FS: line + task worktrees
  Eng->>CLI: task run / open --agent
  CLI->>Agent: argv into task worktree
  Agent-->>CLI: work finished
  CLI->>CLI: run Profile gates
  Eng->>CLI: task review
  Eng->>CLI: task merge --yes
  CLI->>FS: integrate into line + ledger
```

---

## 5. External evidence sequence

```mermaid
sequenceDiagram
  participant CLI as Control plane
  participant Run as Isolated runner
  participant Rev as Independent reviewer

  CLI->>CLI: task claim
  Run->>Run: execute + declared gates
  Run->>CLI: evidence build ZIP
  CLI->>CLI: evidence execution import
  Rev->>CLI: evidence review
  opt require_external_signoff
    CLI->>CLI: task signoff
  end
  CLI->>CLI: task merge
```

---

## 6. Task graph and scheduling

```mermaid
flowchart LR
  A["Task A"] --> B["Task B"]
  B --> C["Task C"]
  C --> D["Decision"]
  CG["conflict_group"]
  A -.-> CG
  B -.-> CG
```

Hard edges: `depends_on`. Mutex resource: `conflict_group` (not an edge). Downstream unlocks only after dependency is `done` **and** merged into the line.

---

## 7. Use-case overview

```mermaid
flowchart LR
  Dev["Developer"] --> Setup["setup / init"]
  Dev --> Tasks["create / run tasks"]
  Lead["Release owner"] --> Lines["lines / hotfixes"]
  Lead --> Merge["merge / changeset"]
  Runner["Isolated runner"] --> Evidence["evidence packages"]
  Reviewer["Independent reviewer"] --> Review["review / signoff"]
```

---

## 8. Multi-agent collaboration (dev side)

Optional experiment: `experiments/local_agent_dispatch/` (not installed with `dyro`).

```mermaid
flowchart TB
  Host["Host agent"] --> Disp["local_agent_dispatch"]
  Disp --> B1["Backend A"]
  Disp --> B2["Backend B"]
  B1 --> Host
  B2 --> Host
  Host --> Board["Adversarial review board"]
  Host -->|"explicit dyro commands"| Dyro["Dyro control plane"]
```

Dispatch results are **advisory**. Delivery still goes through Dyro gates and merge.

---

## 9. Optional external semantic runtime (experiment)

```mermaid
flowchart TB
  Sup["Supervisor"] --> Sand["Sandbox · fixed bundle"]
  Sup --> Bro["Broker · provider"]
  Sand -->|IPC| Bro
  Sup --> Pack["Local pack after dual cleanup"]
  Pack -.->|forbidden| CoreImport["Core import / merge / push"]
```

Not production-ready; see Stage5 `NOT_READY` docs under the experiment tree.

---

## 15-minute path

1. Skim §1–§2 here.  
2. Run README Quick start (`setup`, `doctor`).  
3. Walk §4 with a noop adapter if available.  
4. Read `architecture.md` for policy details.  
5. Treat §8–§9 as optional experiments only.
