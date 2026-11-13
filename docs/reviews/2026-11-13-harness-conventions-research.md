# 各编码工具的技能目录与无界面运行约定（调研快照）

状态：带日期快照，2026-11-13 由 Grok 联网检索官方文档整理，主会话抽查。易变信息，使用前需重新核对。

## Gemini CLI（Google）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。用户级 `~/.gemini/skills/<name>/SKILL.md`，同级别名 `~/.agents/skills/`（同层时 `.agents/skills/` 优先）。项目级 `.gemini/skills/`、`.agents/skills/` |
| 搬家变量 | `GEMINI_CLI_HOME`：在该路径下创建 `.gemini`，存放 configuration and history。Skills 页写的是 `~/.gemini/skills/`，没有写 skills 是否跟着这个变量走 |
| 单次退出 | `gemini -p` / `--prompt`（非 TTY 也会进 headless） |
| 只读 / plan | `gemini --approval-mode=plan` |
| 结构化输出 | `--output-format`：`text`、`json`、`stream-json`（短选项 `-o`） |
| 登录检查 | 未记载非交互 status 命令。Headless 使用已缓存凭据；没有缓存时必须用环境变量配置认证 |
| 符号链接 | 未记载 |

文档：<https://geminicli.com/docs/cli/skills/>、[headless](https://geminicli.com/docs/cli/headless/)、[plan mode](https://geminicli.com/docs/cli/plan-mode/)、[enterprise](https://geminicli.com/docs/cli/enterprise/)（enterprise 页 Last updated: Jun 16, 2026）。该页写明：2026-06-18 起，非付费与 Google One 用户的 Gemini CLI 已由 Antigravity CLI 接替。

## Antigravity CLI（`agy`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`~/.gemini/config/skills/<name>/SKILL.md`；工作区 `<workspace>/.agents/skills/<name>/SKILL.md`。CLI 额外加载 `~/.gemini/antigravity-cli/skills/` 与 `~/.gemini/skills/`（App 不读这两处） |
| 搬家变量 | 未记载 |
| 单次退出 | `agy -p` / `--print` / `--prompt` |
| 只读 / plan | `agy --mode=plan`（只读检索后出计划） |
| 结构化输出 | `--output-format`：`text`（默认）、`json`、`stream-json` |
| 登录检查 | 未记载 status 命令。Headless 用缓存凭据；未认证且无终端时退出并报 authentication required |
| 符号链接 | 未记载 |

文档：<https://antigravity.google/docs/skills>（页面时间 2026-11-12）、[customization discovery](https://antigravity.google/docs/customization-discovery/)、[headless](https://antigravity.google/docs/cli/headless)、[modes](https://antigravity.google/docs/cli/modes/)。

## Kimi Code CLI（`kimi`，Moonshot）

以较新的 kimi-code 文档为准（页面时间 2026-11-12）。kimi-cli 站（2026-09-22）仍写 `--print` / `--quiet`，两套参考不要混成同一套 flag。

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`$KIMI_CODE_HOME/skills/`（默认 `~/.kimi-code/skills/`）与 `~/.agents/skills/`。项目级 `.kimi-code/skills/`、`.agents/skills/`。可加 `--skills-dir` |
| 搬家变量 | `KIMI_CODE_HOME`。Kimi 专属 skills 跟着走；`~/.agents/skills/` 仍在真实 home |
| 单次退出 | `kimi -p` / `--prompt`。不能与 `--yolo`、`--auto`、`--plan` 同用；非交互固定 auto 权限 |
| 只读 / plan | 有 `--plan`，但不能和 `--prompt` 一起用 |
| 结构化输出 | `--output-format` 只能和 `--prompt` 同用。2026-09-22 的 kimi-cli 参考写取值 `text`、`stream-json`；2026-11-12 的命令页没有在本次摘录里重复取值列表 |
| 登录检查 | 未记载 whoami。`kimi login` 为 device-code，成功退出码 `0`，取消或失败为 `1`。`kimi doctor` 校验配置文件 |
| 符号链接 | 未记载 |

文档：<https://moonshotai.github.io/kimi-code/en/customization/skills.html>、<https://moonshotai.github.io/kimi-code/en/reference/kimi-command.html>、<https://moonshotai.github.io/kimi-code/zh/configuration/data-locations.html>。

## MiniMax Code（`mcode`，npm `@minimax-ai/code`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。官方仓库 README：项目 `.minimax/skills/`、`.claude/skills/`、`.agents/skills/`；用户 `~/.agents/skills/`，并读取 `~/.claude/skills/`、`~/.codex/skills/`。文档站只写数据目录里有 skills，没有重复这份路径表 |
| 搬家变量 | `MINIMAX_DATA_DIR`（默认 `~/.minimax`）。兼容名 `MAVIS_DATA_DIR`，两者都设时前者优先。文档未写 `~/.agents/skills/` 是否跟着走 |
| 单次退出 | `mcode exec [prompt]` |
| 只读 / plan | exec 选项里没有 plan flag。交互式是 `/plan`。`--permission` 为 `smart`（默认）、`full`、`off` |
| 结构化输出 | `--output-format`：`text`、`json`、`stream-json` |
| 登录检查 | 未记载非交互 status。交互式 `/status`；退出 `mcode logout`。`mcode provider list --json` 是配置检查，不是登录状态 |
| 符号链接 | 未记载 |

文档：<https://agent.minimax.io/docs/cli/automation>（2026-09-17）、[configuration](https://agent.minimax.io/docs/cli/configuration)（2026-11-02）、[features](https://agent.minimax.io/docs/cli/features)。路径表：<https://github.com/MiniMax-AI/mcode-stack>。

## OpenClaw（npm `openclaw`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。优先级：`<workspace>/skills` → `<workspace>/.agents/skills` → `~/.agents/skills` → `~/.openclaw/skills` → bundled → `skills.load.extraDirs`。默认工作区 `~/.openclaw/workspace/skills` |
| 搬家变量 | `OPENCLAW_STATE_DIR`（默认 `~/.openclaw`），含 `~/.openclaw/skills`。`~/.agents/skills` 不在这个根下 |
| 单次退出 | `openclaw agent` 加会话选择器（`--agent` / `--to` / `--session-key` / `--session-id`）和 `--message`。`--local` 为嵌入式一轮。`--json` 把 stdout 留给 JSON |
| 只读 / plan | 未记载 |
| 结构化输出 | `--json` |
| 登录检查 | 未记载单一账号 status。传输失败时文档要求先看 `openclaw gateway status` |
| 符号链接 | 有记载。`~/.openclaw/skills` 与 `~/.agents/skills` 无条件接受技能目录符号链接。工作区、extraDirs、`<workspace>/.agents/skills` 需要 `skills.load.allowSymlinkTargets` |

文档：<https://docs.openclaw.ai/tools/creating-skills>、<https://docs.openclaw.ai/help/faq/skills-and-automation>、<https://docs.openclaw.ai/tools/skills-config>、<https://docs.openclaw.ai/cli/agent>、<https://docs.openclaw.ai/help/faq/where-things-live-on-disk>。

## omp（<https://omp.sh/install>）

官方文档把它定义为终端编码 agent。文档导航指向 GitHub `can1357/oh-my-pi`。npm 包名 `@oh-my-pi/pi-coding-agent` 只出现在非 omp.sh 的安装镜像页，**unverified**。

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。原生用户路径 `~/.omp/agent/skills/<name>/SKILL.md`，项目 `.omp/skills/<name>/SKILL.md`。另外发现 `~/.agent/skills/`、`~/.agents/skills/`、`~/.claude/skills/`、`~/.codex/skills/`、`~/.config/opencode/skills/` 及对应项目目录、`.github/skills/` |
| 搬家变量 | `PI_CODING_AGENT_DIR` 会改 agent 数据位置（文档明确的是 `agent.db`）。没有写 skills 目录是否一起搬 |
| 单次退出 | `omp -p` / `--print` |
| 只读 / plan | 交互式 `/plan`、只读 `/vibe`。一次性运行忽略 `plan.defaultOnStartup`。`--plan <selector>` 是 plan 角色的模型选择，不是 plan mode |
| 结构化输出 | `omp -p --mode json`（NDJSON）。`--mode` 还可为 `text`、`rpc`、`rpc-ui`、`acp` |
| 登录检查 | 使用 auth broker 时：`omp auth-broker status`。普通 provider 登录是交互式 `/setup`，未记载通用 whoami |
| 符号链接 | 未记载 |

文档：<https://omp.sh/docs/skills>、<https://omp.sh/docs/cli>、<https://omp.sh/docs/modes>、<https://omp.sh/docs/secrets>、<https://omp.sh/docs>。

## Qoder CLI（`qodercli`）

同一套 CLI 文档里命令名同时出现 `qodercli` 和 `qoder`。

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`~/.qoder/skills/<name>/SKILL.md`，项目 `.qoder/skills/<name>/SKILL.md` |
| 搬家变量 | 未记载。认证变量是 `QODER_PERSONAL_ACCESS_TOKEN` |
| 单次退出 | `-p` / `--print` |
| 只读 / plan | `--permission-mode` 含 `plan`（另有 `default`、`auto`、`bypass_permissions`、`accept_edits`、`dont_ask`） |
| 结构化输出 | `-o` / `--output-format`：`text`、`json`、`stream-json` |
| 登录检查 | 未记载非交互 status。交互式 `/login`、`/status`；子命令表有 `login` |
| 符号链接 | 未记载 |

文档：<https://docs.qoder.com/cli/Skills.md>、<https://docs.qoder.com/cli/cli-reference>、<https://docs.qoder.com/cli/run-in-scripts>、<https://docs.qoder.com/en/cli/using-cli>。

## Qwen Code

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`~/.qwen/skills/<name>/SKILL.md` 优先于 `~/.agents/skills/`。项目 `.qwen/skills/`、`.agents/skills/`。只发现一层子目录 |
| 搬家变量 | `QWEN_HOME`（默认 `~/.qwen`）。设置页写明该目录存放 credentials、settings、memory、skills。项目级 `.qwen/` 不受影响。`QWEN_RUNTIME_DIR` 只改运行时输出目录 |
| 单次退出 | `qwen -p` / `--prompt` |
| 只读 / plan | `--approval-mode plan`（另有 `default`、`auto-edit`、`auto`、`yolo`） |
| 结构化输出 | `--output-format` / `-o`：`text`、`json`、`stream-json` |
| 登录检查 | 未记载非交互 status。交互式 `/auth`、`/doctor` |
| 符号链接 | 未记载 |

文档：<https://qwenlm.github.io/qwen-code-docs/en/users/features/skills/>（2026-10-19）、[headless](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)、[approval mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/approval-mode/)（2026-11-13）。`QWEN_HOME` 含 skills 的表述见同站设置页的日文/葡文版（2026-10-19）。

## Factory Droid（`droid`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`~/.factory/skills/<name>/SKILL.md`，项目 `<repo>/.factory/skills/<name>/SKILL.md` |
| 搬家变量 | 未记载。认证变量是 `FACTORY_API_KEY` |
| 单次退出 | `droid exec "query"`（也可用 `-f` 或管道） |
| 只读 / plan | 不传 `--auto` 时默认是 read-only reconnaissance。`--auto` 为 `low`、`medium`、`high` |
| 结构化输出 | `-o` / `--output-format`：`text`、`json`、`stream-json`、`stream-jsonrpc`（exec 概览还列了 `acp`） |
| 登录检查 | 未记载非交互 status。交互式 `/login`、`/status`、`/logout` |
| 符号链接 | 未记载 |

文档：<https://docs.factory.ai/cli/configuration/skills>、<https://docs.factory.ai/cli/configuration/cli-reference>、<https://docs.factory.ai/cli/droid-exec/overview>。

## Trae / Trae CN（ByteDance）

CLI 与 IDE 是两套目录。

| 项 | 官方记载 |
| --- | --- |
| Skills | CLI 支持。用户 `~/.traecli/skills`，项目 `.traecli/skills/<name>/SKILL.md`。IDE 另为 `~/.trae/skills`（Windows `%userprofile%/.trae/skills`）与项目 `.trae/skills/` |
| 搬家变量 | `TRAE_HOME` 覆盖 CLI 配置和运行时目录。Skills 页没有写 `~/.traecli/skills` 是否跟着走 |
| 单次退出 | `traecli exec [PROMPT]`（PROMPT 省略或为 `-` 时读 stdin） |
| 只读 / plan | 权限模式取值 `plan`（先出计划再执行），用 `traecli config edit` 写入。YAML 键名在检索摘要里被截断，不猜。未记载 exec 的 plan flag |
| 结构化输出 | `traecli exec --json`：JSONL 事件流到 stdout |
| 登录检查 | `traecli login status`。文档没有给出期望输出样例。另有 `traecli doctor` |
| 符号链接 | 未记载 |

文档：<https://docs.trae.cn/cli_skills>、<https://docs.trae.cn/cli_command-line-parameters>（2026-09-20）、<https://docs.trae.cn/cli_environment-variables>（2026-08-05）、<https://docs.trae.cn/cli_permission-mode>、<https://docs.trae.cn/cli_status-indication-and-troubleshooting>。IDE：<https://docs.trae.ai/ide/skills>。

## Kiro（AWS，命令 `kiro-cli`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持（文档矩阵里 CLI 列为 ✓）。用户 `~/.kiro/skills/`，工作区 `.kiro/skills/`。同名时工作区优先 |
| 搬家变量 | 未记载。Headless 认证变量是 `KIRO_API_KEY` |
| 单次退出 | `kiro-cli chat --no-interactive "prompt"`（也可只从 stdin 读） |
| 只读 / plan | 交互式 Shift+Tab 进入 Plan mode。未记载对应的启动 flag |
| 结构化输出 | `--output-format stream-json`（JSON Lines） |
| 登录检查 | `kiro-cli whoami`：报告当前认证方式。文档没有固定输出样例 |
| 符号链接 | 未记载 |

文档：<https://kiro.dev/docs/skills/>（2026-10-21）、<https://kiro.dev/docs/cli/headless/>（2026-11-12）、<https://kiro.dev/docs/cli/authentication/>。Kiro Crew 的 `~/.kiro/crew/skills/` 是另一套产品，不是这条 CLI 路径。

## iFlow CLI（心流）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。`~/.iflow/skills/<name>/SKILL.md`，项目 `.iflow/skills/`。其 frontmatter 把 `license` 也标为必填 |
| 搬家变量 | 未记载 |
| 单次退出 | `iflow -p` / `--prompt`。位置参数等价于 `iflow -p`。`-c` 可与 `-p` 组合以续接无头会话（changelog 0.5.15，2026-03-02） |
| 只读 / plan | 交互式 Shift+Tab。未记载启动 flag |
| 结构化输出 | 已读的命令行参数表没有 `--output-format` |
| 登录检查 | 未记载非交互 status。交互式 `/auth`。版本：`iflow --version` |
| 符号链接 | changelog 0.5.15 写明「Agent/Skill 符号链接扫描支持」，没有展开规则 |

文档：<https://platform.iflow.cn/cli/examples/skill>、<https://platform.iflow.cn/cli/configuration/settings>、<https://platform.iflow.cn/cli/changelog>、<https://platform.iflow.cn/cli/examples/plan-mode>。

## Goose（Block）

旧站 `block.github.io/goose` 已指向 <https://goose-docs.ai>。

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。推荐 `~/.agents/skills/<name>/SKILL.md`，项目 `.agents/skills/`，插件 `~/.agents/plugins/<plugin-name>/`。兼容 `.goose/skills/`、`.claude/skills/`、`~/.claude/skills/` 以及 “platform-specific config directories”（页内没有把最后一个展开成具体路径） |
| 搬家变量 | `GOOSE_PATH_ROOT` 覆盖 goose 自己的 data/config/state。推荐 skills 在 `~/.agents/skills/`，文档没说它跟着搬 |
| 单次退出 | `goose run -t "instructions"` |
| 只读 / plan | 已读页面未记载 plan/read-only flag |
| 结构化输出 | `goose run --output-format json` 或 `stream-json` |
| 登录检查 | 未记载统一 whoami。Provider 在 `~/.config/goose/config.yaml`（Windows `%APPDATA%\Block\goose\config\config.yaml`）；`GOOSE_PROVIDER` / `GOOSE_MODEL` 可覆盖当次进程 |
| 符号链接 | 未记载 |

文档：<https://goose-docs.ai/docs/guides/context-engineering/using-skills/>（2026-10-13）、<https://goose-docs.ai/docs/guides/running-tasks>（2026-11-12）、<https://goose-docs.ai/docs/guides/environment-variables>、<https://goose-docs.ai/docs/tutorials/headless-goose>（2026-11-11）。

## Crush（Charm）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。用户级：`$CRUSH_SKILLS_DIR`；`$XDG_CONFIG_HOME/agents/skills` 或 `~/.config/agents/skills/`；`$XDG_CONFIG_HOME/crush/skills` 或 `~/.config/crush/skills/`；`~/.agents/skills/`；`~/.claude/skills/`。Windows 另有 `%LOCALAPPDATA%\agents\skills\`、`%LOCALAPPDATA%\crush\skills\`。项目：`.agents/skills`、`.crush/skills`、`.claude/skills`、`.cursor/skills`。还可 `option skill-path` |
| 搬家变量 | 配置默认 `~/.config/crush/crushrc`。没有单独的 “Crush home” 变量；skills 用 `CRUSH_SKILLS_DIR` 与 `XDG_CONFIG_HOME` |
| 单次退出 | 正式 README 未写 headless。维护者在官方 issue #1030 写：非交互用 `crush run`，且始终 yolo。issue #2705（2026-04-24，crush 0.62.0）报告 `crush run` 不把回复打到 stdout，检索时该 issue 仍为 Open。因此“打印答案并退出”没有正式文档保证 |
| 只读 / plan | 未记载 |
| 结构化输出 | 未记载 |
| 登录检查 | 未记载 |
| 符号链接 | 未记载 |

来源：<https://github.com/charmbracelet/crush> README 的 Agent Skills 段；配置说明同仓库 `docs/config`。`crush run` 仅来自 <https://github.com/charmbracelet/crush/issues/1030> 与 [#2705](https://github.com/charmbracelet/crush/issues/2705)。

## GitHub Copilot CLI（`copilot`）

| 项 | 官方记载 |
| --- | --- |
| Skills | 支持。用户 `~/.copilot/skills/<name>/SKILL.md` 或 `~/.agents/skills/`。项目 `.github/skills`、`.claude/skills`、`.agents/skills` |
| 搬家变量 | 未记载 |
| 单次退出 | `copilot -p` / `--prompt`。管道也可；若同时给了 `-p`，管道被忽略。`-s` 去掉会话元数据。`--no-ask-user` 关掉澄清提问 |
| 只读 / plan | 交互式 Shift+Tab 与 `/plan`：阻止改项目文件。未记载 `--plan` 启动 flag |
| 结构化输出 | 已读的 programmatic 文档没有 `--output-format` |
| 登录检查 | 用 GitHub CLI token 回退时，文档要求先跑 `gh auth status`（数据驻留实例加 `--hostname HOSTNAME`）。Copilot 自己的检查是交互式 `/user`。登录命令是 `copilot login`。没有引用固定 stdout 样例 |
| 符号链接 | 未记载 |

文档：<https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-skills>、<https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically>、<https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference>（Plan mode）、<https://docs.github.com/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli>。
