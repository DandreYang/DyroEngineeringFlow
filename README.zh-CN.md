# DyroEngineeringFlow

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [Español](README.es.md)

**DyroEngineeringFlow · `dyro` CLI** 是面向多仓团队的本地优先工程自动化与交付控制平台。它将开发线、Git worktree、Agent 启动、任务门禁、独立复核和合并审计统一到可版本化的工作区配置中。

**让工程从任务到交付自动流转。**

DyroEngineeringFlow 不绑定 Codex、Claude 或任何业务项目。每个团队通过 `dyro.toml` Profile 定义仓库、目录布局、Agent adapter 与交付策略；业务规则、模型成本和发布规范始终留在各自的 Profile 中。

## 核心约束

- 一个任务只能属于一条开发线，不能混用功能版本与 Hotfix 工作区。
- 每个任务在独立 `git worktree` 的 `task/<id>` 分支中执行。
- 门禁由编排器实际执行，不能只采信 Agent 的口头回执。
- 复核同时绑定执行回执和各仓精确任务 HEAD；源码漂移会使复核失效。
- 任务通过独立复核后才能变为 `done`；默认必须显式确认才能合并或推送。
- 可执行配置使用 argv 数组，核心不会执行来自 TOML 的 shell 字符串。

## 快速开始

日常使用 CLI 时，推荐通过隔离的 `pipx` 环境从 PyPI 安装 `dyro`（要求 Python 3.11+）：

```bash
python3 -m pip install --user --upgrade pipx
python3 -m pipx ensurepath
# 执行 ensurepath 后请重新打开终端，再运行：
pipx install dyro
dyro --version
```

升级时运行 `pipx upgrade dyro`。若团队统一使用 `pip` 管理 Python 包，可改用：

```bash
python3 -m pip install --user --upgrade dyro
```

将仓库放入工作区后，使用新人入口一条命令完成仓库发现、状态目录初始化和首条开发线创建：

```bash

mkdir my-workspace && cd my-workspace
# 先将 Git 仓库 clone 或移动到这个目录下。
dyro setup . --name my-workspace --line dev --yes
```

`setup` 会扫描当前目录下的 Git 仓库，自动登记工作区相对路径、推断开发线内挂载位置，并在可用时读取 `origin`，无需手改 TOML。`--yes` 仅用于确认首条开发线会创建 Git worktree；若只想先建立 Profile，可改用 `--no-line`。尚未 clone 仓库时可使用引导式兜底：

```bash
dyro init . --wizard --name my-workspace
```

后续新增仓库也无需打开 `dyro.toml`：

```bash
dyro repo add repositories/services/payments
dyro repo list
```

常用交付策略和 Agent adapter 也无需打开 `dyro.toml`：

```bash
dyro config set policy.execution_mode external
dyro config get policy.execution_mode
dyro agent add ci-runner --preset noop
dyro agent test ci-runner
```

Profile 已配置 remote 时，可以安全补齐缺失仓库 anchor：

```bash
dyro --dry-run bootstrap
dyro bootstrap --yes
dyro doctor
```

新人日常入口只需一条命令：检查工作区后，选择开发线和本机 Agent。

```bash
dyro start
```

## 交付流程

版本负责人或自动化脚本可使用显式命令：

```bash
dyro doctor
dyro line create release-2026-10 --base origin/main --yes
# 仅在某个仓库需要时覆盖它自己的已核实基线。
dyro line create release-2026-10 --base origin/main --repo-base web=v2026.10.0 --yes
dyro open release-2026-10 --agent codex
dyro task create API-101 --title "实现 API 契约" --line release-2026-10 --repository api
dyro task next
dyro task next --run --yes
dyro task review API-101
dyro task merge API-101 --yes
dyro changeset create release-2026-10-ready --line release-2026-10
dyro changeset verify release-2026-10-ready
```

线上 Hotfix 必须明确已核实的生产基线，不能隐式继承默认分支：

```bash
dyro hotfix create incident-123 --base v2026.09.7 --repos api,web --yes
```

如果执行与审批由独立受信任系统承担，可在 Profile 设置 `policy.execution_mode = "external"` 和 `policy.require_external_signoff = true`。此时本机 Dyro 仅允许计划核验；复核结论同时绑定回执和精确任务 HEAD 后，仍必须显式签收才能进入 `done`：

```bash
dyro task claim API-101 --by isolated-runner-1
# 在隔离 runner 中运行声明的门禁，并打包回执、日志和精确 HEAD。
dyro task evidence build API-101 --workspace /runner/workspace --receipt /runner/out/receipt.md --output /runner/out/API-101.zip
# 在控制面导入并校验这一份可移植证据包。
dyro task evidence execution API-101 --bundle /runner/out/API-101.zip
dyro task evidence review API-101 --file /review/out/review.md
dyro task signoff API-101 --by release-manager
```

所有有写入风险的操作都支持先查看计划：

```bash
dyro --dry-run line create release-2026-10 --base origin/main
dyro --dry-run task run API-101
```

## 命令地图

| 命令 | 作用 |
| --- | --- |
| `setup` / `init --discover` / `init --wizard` / `repo add/list` / `bootstrap` / `start` | 无需手改 TOML 地完成新人引导、仓库管理与开发线、Agent 选择。 |
| `doctor` / `status` | 验证和显示控制平面状态。 |
| `line create/list` | 创建、登记和查看功能开发线。 |
| `hotfix create` | 从显式生产基线创建 Hotfix 开发线。 |
| `changeset create/list/verify` | 固化并核验一次多仓交付所包含的干净、精确 Git 提交组合。 |
| `config get/set` / `agent list/add/test` / `open` | 安全管理常用策略与 adapter、校验可执行文件，或在正确开发线启动 Agent。 |
| `task create/list/board/status/next` | 管理任务清单、状态机和下一项可执行任务。 |
| `task run/answer/gates/review/signoff` | 执行任务、回答追问、运行门禁、申请独立复核；需要时记录外部签收。 |
| `task claim` / `task evidence build/execution/review` | 供隔离执行器一次性领取任务、构建/导入可移植执行证据包，并导入与回执绑定的复核证据。 |
| `task merge` | 将已复核的任务分支合入所属开发线。 |
| `task loop/daemon/stats/decisions` | 受控批处理、调度、台账报表和决策门禁。 |

实现细节见[架构与 Profile 契约](docs/architecture.md)与[既有控制面迁移指南](docs/migrating-existing-control-planes.md)。

## 语言与文档

README 提供英语、简体中文、日语、韩语和西语版本。所有译本共享同一组命令、配置键、目录名和安全规则。当前 CLI 提示与扩展技术文档仍主要为中文；README 多语言支持不代表运行时已支持切换语言。

## 当前边界

DyroEngineeringFlow 已具备完整的本地工作流闭环，以及让高保障团队在本机保持“仅计划”模式的策略控制。它不创建远端仓库、不携带 SaaS 凭证，也不负责供给外部 runner；但内置可移植证据包的构建与校验契约。本地多仓 merge 会统一预检并在失败时恢复；不同 Git 远端无法提供原子跨仓 push，因此部分推送失败会写入台账等待恢复。自动 merge 需要任务清单与本地策略双重许可。本项目采用 [MIT License](LICENSE)，并已发布为 [PyPI `dyro`](https://pypi.org/project/dyro/) 包。
