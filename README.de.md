# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [한국어](README.ko.md) | [Español](README.es.md) | [Français](README.fr.md) | [Deutsch](README.de.md) | [Português (Brasil)](README.pt-BR.md) | [Русский](README.ru.md)

**DyroEngineeringFlow · `dyro` CLI** ist eine local-first Plattform für Engineering-Automatisierung und Liefersteuerung in Multi-Repository-Teams. Sie bündelt Entwicklungslinien, Git-Worktrees, Agent-Start, Task-Gates, unabhängige Reviews und Merge-Audit in einer versionierbaren Workspace-Konfiguration.

**Engineering von der Aufgabe bis zur Auslieferung vorantreiben.**

DyroEngineeringFlow ist nicht an Codex, Claude oder eine Fachdomäne gebunden. Jedes Team liefert ein `dyro.toml`-Profile für Repositories, Layout, Agent-Adapter und Lieferpolitik; Fachregeln, Modellkosten und Release-Praktiken bleiben im Profile.

## Was erzwungen wird

- Eine Aufgabe gehört genau einer Entwicklungslinie — niemals ein gemischter Feature-/Hotfix-Workspace.
- Jede Aufgabe läuft im eigenen `git worktree` auf einem Branch `task/<id>`.
- Gates führt der Orchestrator aus; die Selbstauskunft eines Agents ist kein Erfolgsnachweis.
- Reviews sind an Execution-Receipt und exakte pro-Repository Task-HEADs gebunden; Quelldrift invalidiert sie.
- Eine Aufgabe braucht unabhängiges Review vor `done`; Merge und Push erfordern standardmäßig explizite Bestätigung.
- Eine fertige Abhängigkeit gibt Downstream erst frei, wenn ihre exakten HEADs in der besitzenden Linie integriert sind.
- Ausführbare Konfiguration ist als argv-Arrays dargestellt. Der Core führt keine TOML-Shell-Strings aus.

## Schnellstart

Für den täglichen CLI-Einsatz `dyro` aus PyPI in einer isolierten `pipx`-Umgebung installieren (Python 3.11+):

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# Nach ensurepath neues Terminal öffnen, dann:
pipx install dyro
dyro --version
```

Später aktualisieren mit `pipx upgrade dyro`. Mit `pip`:

```bash
python3 -m pip install --user --upgrade dyro
```

Repositories in einen Workspace legen, dann den Einstiegspfad nutzen:

```bash

mkdir my-workspace && cd my-workspace
# Zuerst Git-Repositories unter dieses Verzeichnis klonen oder verschieben.
dyro setup . --name my-workspace --line dev --yes
```

`setup` scannt lokale Git-Repos, speichert workspace-relative Pfade, leitet Line-Mounts ab und liest `origin` wenn vorhanden — ohne TOML-Edit. `--yes` nur, weil die erste Line Worktrees anlegt. Mit `--no-line` zuerst nur das Profile. Ohne Repos:

```bash
dyro init . --wizard --name my-workspace
```

Repository später ohne `dyro.toml`-Editor hinzufügen:

```bash
dyro repo add repositories/services/payments
dyro repo list
```

Häufige Policies und Adapter:

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Fehlende Anchors bei vorhandenen Remotes sicher ergänzen:

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

Normaler Einstieg für neue Teammitglieder:

```bash
dyro start
```

## Lieferfluss

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "Implement API contract" --line release-2026-10 --repository api
dyro task next
dyro task next --run --yes
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

Produktions-Hotfix mit verifizierter Produktionsbasis:

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

Bei Ausführung/Freigabe außerhalb der lokalen Vertrauensgrenze: `policy.execution_mode = "external"` und `policy.require_external_signoff = true`. Lokales Dyro bleibt planungsorientiert; nach receipt- und HEAD-gebundenem Review ist eine explizite externe Signatur nötig.

```bash
dyro task claim API-101 --by isolated-runner-1
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

Alle schreibenden Operationen haben einen Planungsmodus:

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## Befehlskarte

| Befehl | Zweck |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | Onboarding ohne TOML, Anchors, Line- und Agent-Wahl. |
| `doctor` / `status` | Control-Plane prüfen und anzeigen. |
| `line create/list` / `hotfix create` | Feature- oder Hotfix-Linien von expliziter Produktionsbasis. |
| `changeset create/list/verify` | Exakte saubere Git-HEADs einer Multi-Repo-Lieferung pinnen und prüfen. |
| `config get/set` / `agent list/add/test` / `open` | Policies/Adapter, Executable testen, Agent auf richtiger Line öffnen. |
| `task create/list/board/status/next` | Manifeste, Status, nächste ausführbare Aufgabe. |
| `task run/answer/gates/review/signoff/merge` | Ausführen, klären, Gates, Review, Sign-off, Merge. |
| `task claim` / `task evidence build/execution/review` | Claim, portables Evidence-Paket für isolierten Runner. |
| `task loop/daemon/stats/decisions` | Kontrollierte Batches, Planung, Ledger, Entscheidungsgates. |

## Sprachen und aktueller Umfang

Dieses README wird auf Englisch, Vereinfachtem Chinesisch, Koreanisch, Spanisch, Französisch, Deutsch, brasilianischem Portugiesisch und Russisch gepflegt. Befehle, Konfigurationsschlüssel, Verzeichnisnamen und Sicherheitsregeln sind in allen Übersetzungen identisch. CLI-Meldungen und ausführliche Technikleitfäden sind derzeit vor allem chinesisch. Mehrsprachige READMEs bedeuten noch keinen Laufzeit-Sprachwechsel.

DyroEngineeringFlow bietet eine vollständige lokale Schleife und Policy-Kontrollen für strengere Teams im planungs-only lokalen Modus. Es legt keine Remote-Repos an, liefert keine SaaS-Credentials und provisioniert keinen externen Runner; es liefert den Vertrag zum Erzeugen und Validieren portabler Evidence-Pakete. [MIT-Lizenz](LICENSE) und [`dyro` auf PyPI](https://pypi.org/project/dyro/).
