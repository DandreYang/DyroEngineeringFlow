# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**DyroEngineeringFlow · `dyro` CLI** 는 여러 저장소를 사용하는 팀을 위한 로컬 우선 엔지니어링 자동화 및 배포 제어 플랫폼입니다. 개발 라인, Git worktree, Agent 실행, 작업 게이트, 독립 검토, 병합 감사를 버전 관리 가능한 워크스페이스 설정으로 통합합니다.

**작업에서 배포까지 엔지니어링을 자동으로 흐르게 합니다.**

Codex, Claude 또는 특정 비즈니스 도메인에 종속되지 않습니다. 각 팀은 `dyro.toml` Profile에서 저장소, 레이아웃, Agent adapter, 배포 정책을 정의하며, 비즈니스 규칙, 모델 비용, 릴리스 규약은 Profile에 둡니다.

## 핵심 보장

- 하나의 작업은 정확히 하나의 개발 라인에만 속하며 기능 버전과 Hotfix 워크스페이스를 혼합하지 않습니다.
- 모든 작업은 `task/<id>` 브랜치의 독립된 `git worktree`에서 실행됩니다.
- 게이트는 오케스트레이터가 실행하며 Agent의 자체 보고만으로 성공을 판단하지 않습니다.
- 검토는 실행 receipt와 저장소별 정확한 작업 HEAD에 함께 연결되며 소스 변경 시 무효가 됩니다.
- 독립 검토를 통과한 작업만 `done`이 되며, 병합과 push는 기본적으로 명시적 확인이 필요합니다.
- 실행 가능한 설정은 argv 배열입니다. 코어는 TOML에서 제공한 shell 문자열을 실행하지 않습니다.

## 빠른 시작

일상적인 CLI 사용에는 격리된 `pipx` 환경에서 PyPI의 `dyro`를 설치하세요. Python 3.11 이상이 필요합니다.

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# ensurepath 실행 후 새 터미널을 연 다음 실행하세요.
pipx install dyro
dyro --version
```

업데이트할 때는 `pipx upgrade dyro`를 실행합니다. 팀에서 `pip`로 Python 패키지를 관리한다면 다음을 사용할 수 있습니다.

```bash
python3 -m pip install --user --upgrade dyro
```

저장소를 워크스페이스에 넣은 뒤, 새 구성원용 한 명령으로 검색, 상태 디렉터리 생성, 첫 개발 라인 생성을 수행합니다.

```bash

mkdir my-workspace && cd my-workspace
# 먼저 Git 저장소를 이 디렉터리 아래에 clone하거나 옮깁니다.
dyro setup . --name my-workspace --line dev --yes
```

`setup`은 로컬 Git 저장소를 검색해 워크스페이스 상대 경로와 개발 라인 내 위치를 자동 등록하고, 가능하면 `origin`도 읽습니다. TOML을 직접 편집할 필요가 없습니다. `--yes`는 Git worktree를 만드는 첫 개발 라인에만 필요합니다. Profile만 먼저 만들려면 `--no-line`을 사용하세요. 아직 저장소가 없다면 대화형 대안을 사용하세요.

```bash
dyro init . --wizard --name my-workspace
```

나중에 저장소를 추가할 때도 `dyro.toml`을 열 필요가 없습니다.

```bash
dyro repo add repositories/services/payments
dyro repo list
```

자주 쓰는 배포 정책과 Agent adapter도 `dyro.toml`을 열지 않고 관리할 수 있습니다.

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Profile에 remote가 있으면 누락된 repository anchor를 안전하게 보완할 수 있습니다.

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

새 구성원의 일반적인 진입점은 하나의 명령입니다. 워크스페이스를 검사한 뒤 개발 라인과 로컬 Agent를 선택합니다.

```bash
dyro start
```

## 배포 흐름

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# 필요한 저장소에만 검증된 base를 개별로 덮어쓸 수 있습니다.
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

프로덕션 Hotfix에는 검증된 프로덕션 기준을 반드시 명시합니다.

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

실행과 승인을 별도의 신뢰된 시스템에서 처리하는 Profile은 `policy.execution_mode = "external"` 및 `policy.require_external_signoff = true`를 설정합니다. 이때 로컬 Dyro는 계획만 허용하며, receipt와 정확한 작업 HEAD에 연결된 검토 뒤에도 명시적 signoff가 필요합니다.

```bash
dyro task claim API-101 --by isolated-runner-1
# 격리 runner에서 선언된 gate와 receipt, 로그, 정확한 HEAD를 하나의 패키지로 만듭니다.
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

모든 쓰기 가능 작업에는 계획 모드가 있습니다.

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## 명령 지도

| 명령 | 용도 |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | TOML 편집 없는 온보딩, anchor 관리, 개발 라인과 Agent 선택. |
| `doctor` / `status` | 제어 평면 상태 검증 및 표시. |
| `line create/list` / `hotfix create` | 기능 개발 라인 또는 명시적 프로덕션 기준의 Hotfix 생성. |
| `changeset create/list/verify` | 다중 저장소 배포를 구성하는 깨끗하고 정확한 Git HEAD 고정 및 검증. |
| `config get/set` / `agent list/add/test` / `open` | 자주 쓰는 정책과 adapter의 안전한 관리, 실행 파일 검사 또는 올바른 개발 라인에서 Agent 실행. |
| `task create/list/board/status/next` | 작업 정의, 상태, 다음 실행 가능 작업 관리. |
| `task run/answer/gates/review/signoff/merge` | 실행, 질문 해결, 게이트, 독립 검토, 서명, 병합. |
| `task claim` / `task evidence build/execution/review` | 격리 runner의 일회성 작업 수령, 이식 가능한 실행 증거 패키지 생성/가져오기, 검토 증거 가져오기. |
| `task loop/daemon/stats/decisions` | 제어된 일괄 처리, 스케줄링, 원장, 의사결정 게이트. |

## 언어 및 현재 범위

README는 영어, 간체 중국어, 일본어, 한국어, 스페인어를 제공합니다. 명령, 구성 키, 디렉터리 이름, 안전 규칙은 모든 번역에서 동일합니다. 현재 CLI 메시지와 상세 기술 문서는 주로 중국어입니다. README의 다국어 지원은 런타임 언어 전환을 의미하지 않습니다.

DyroEngineeringFlow는 로컬 워크플로를 완결하고 엄격한 팀을 로컬 계획 전용 모드로 유지하는 정책 제어를 제공합니다. remote 저장소 생성, SaaS 자격 증명, 외부 runner 공급은 포함하지 않지만 이식 가능한 증거 패키지 생성/검증 계약을 제공합니다. [MIT License](LICENSE)로 제공되며 [PyPI의 `dyro`](https://pypi.org/project/dyro/)로 배포되었습니다.
