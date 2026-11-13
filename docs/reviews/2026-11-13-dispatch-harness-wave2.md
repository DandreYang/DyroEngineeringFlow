# dispatch 与多工具协调：第二轮修复清单（草稿）

状态：草稿，未评审，未排期
日期：2026-11-13
基线：main `319519c`（0.7.16）

## 证据来源与分级

- **本次实测**：在 2026-11-13 的 13:56–14:35，通过 Dyro 同时派发 Codex、Grok、Cursor、agy，完成 PR #69 的修复和两份设计草稿。下文的运行 ID 都可以在 `~/.dyro/local-agent-dispatch/` 中查到。
- **同事会话报告**：一个使用方产品工作区的开发线会话在同一时段的反馈。本会话没有重跑，标为"先前说法"。
- **使用方清单 H 类**：使用方汇总的问题清单（在使用方工作区，不在本仓）。
- 每条都标明源码位置（`experiments/local_agent_dispatch/`），这些位置已用当前源码确认。

## 一、失败了却看起来像成功（优先级最高）

这一类的共同点是：调用方拿到退出码 0 或"中等置信度"，却没有可用结果。

1. **结果信封默认 `confidence="medium"`**
   - 位置：`result_envelope.py:20`、`result_envelope.py:67`。
   - 实测：
     - `run-8bdfa41c00b38f91` 超时，交回一个没跑测试的半成品补丁，`confidence=medium`。
     - Grok 成员 `run-a4cc64c134eeac65` 以 `protocol_error` 失败，也是 `confidence=medium`。
   - 建议：`status` 不是 `completed` 时，置信度固定为 `low`；`patch_ref` 要标明是部分结果。

2. **单个任务默认超时 120 秒，合同里没法设**
   - 位置：`cli.py:428/446/513`、`supervisor.py:525/793`、`panel.py:120`。
   - 实测：Codex 修改任务 2 分钟就超时。加 `--timeout 2400` 重跑后，约 6 分钟正常完成。
   - Batch 成员有 `timeout_seconds`，单个 `run` 只能靠命令行参数，Skill 里也没写。
   - 建议：修改任务的默认超时提高到与 batch 同一量级；在 TaskContract 中允许写 `timeout_seconds`；Skill 示例里写明。

3. **输出超限时整个成员作废**（先前说法；位置已确认：`adapters/subprocess_cli.py:1097`）
   - 同事报告：`orch-b6a4cb1b4cf0555d` 的 3 个只读成员都没有结果。codex 和 pi 是 `output_limit`，grok 是 `protocol_error`。用量照付，summary 和 evidence 都是空的。
   - 建议：
     - 只按最终回答计算上限，或者保留截断前的部分 summary。
     - 允许每个合同自己设置输出上限。

3a. **`verified` 只检查定位，不检查论断；多段行号也解析不了**
   - 位置：`locator_verify.py:13` 的 `_LINES_RE` 只接受单个 `N` 或 `N-M`。
   - 实测：Codex 审查 A+B 设计稿（`run-8a58b60da1bdda66`）时，引用写成 `265-266, 636-638` 这种多段行号。结果 7 条证据被标成 `verified=False`、`unparseable lines`，其实内容都正确。
   - 同时，`verified=True` 只代表文件存在、行号在范围内，并不代表论断成立。但它会算进 `verified_ratio`，读起来像"已核实"。
   - 建议：
     - 支持逗号分隔的多段行号，以及长破折号（清单 H 类）。
     - 把字段改名或在文档中写明，它只是"定位有效"。

## 二、工具适配

4. **Grok 结果解析失败**
   - 位置：`adapters/subprocess_cli.py:1068`，报错 "did not contain a valid final assistant result"。
   - 实测：`run-a4cc64c134eeac65` 复现，同事报告里也出现。
   - 对照：直接执行 `grok -p "<prompt>" --output-format plain`，一次就拿到 266 行完整设计稿。
   - 建议：
     - 改用 `--output-format json`，按结构化字段取最终回答。
     - 解析失败时把原始输出保存为证据，不要直接丢弃。

5. **Cursor 只能靠 `CURSOR_API_KEY` 才能走 dispatch**
   - 实测：本机没设这个 key，`dyro dispatch backends` 报 `authenticated=false`。
   - 直接调用时，`cursor-agent -p --mode ask --output-format json` 用它自己的登录就能完成只读任务。我抽查了它引用的 7 处代码行，全部准确。
   - 另一个坑：`--mode plan` 下退出码是 0，但标准输出为空（只有 1 字节），属于静默失败。
   - 建议：
     - 增加"使用 CLI 自带登录、并显式确认不隔离"的只读路径。
     - 固定使用 `ask` 模式加 JSON 输出，结果为空时判为失败。

6. **agy 不在支持列表里**（清单 H 类）
   - 实测：agy 无界面运行时，任何命令都会被自动拒绝，退出码仍是 0，输出为空，原因只写在 stderr。在提示里禁止它执行命令后，它完成了一次有效的代码审查：提出的 3 条问题都属实，已在 `fd3ac6c` 修复。
   - 建议：
     - 新增 agy 适配，固定使用 `--mode plan` 和 `--print-timeout`。
     - 输出为空时把 stderr 作为失败原因返回。

6a. **agy 超时后退出码仍是 0**
   - 实测：用 agy 审第二轮补丁时，`--print-timeout 1800s` 到时后，stderr 输出 `AGY_ERROR {"status":"DEADLINE_EXCEEDED","reason":"print_timeout"}`，stdout 为空，退出码仍是 0。
   - 同一天 agy 已经第三次出现"退出码 0、没有产出"。前两次分别是命令被自动拒绝、`--mode plan` 不写正文。
   - 建议：agy 适配不能只看退出码。stdout 为空、或 stderr 含 `AGY_ERROR` 时，一律判为失败，并把错误中的 `reason` 字段带回。

7. **kimi 在三处的说法不一致**（先前说法；位置已确认：`orchestration.py:57`、`adapters/subprocess_cli.py:2146`）
   - `--dry-run backends` 显示 available=true；batch-plan 报"未安装"；真正探测时报"没有可用 provider"。
   - 建议：三处使用同一个判定，并给出同一个原因。

8. **登录探测时好时坏**（先前说法）
   - grok 两次探测相隔约 1 分钟，一次 false，一次 true。
   - 建议：失败时重试一次，并返回失败原因。

## 三、使用门槛

9. **TaskContract 的格式只能靠撞报错摸索**（先前说法，与本次实测一致）
   - 本次也需要读 `task_contract.py` 和 `batch_contract.py`，才知道这些规则：
     - 五个任务字段要放在 `task` 对象里；
     - `allow_unconfined_provider` 的写法；
     - `strict=true` 不能用于 edit 模式；
     - `files` 最多匹配 200 个路径，单个文件不超过 512 KiB，总量不超过 2 MiB。
   - 建议：在 dyro-dispatch Skill 中附一份最小的合法示例，覆盖单个 `run`、batch 中的修改成员、batch 中的只读成员，并写明以上上限。

9a. **上下文文件的两条隐性限制**
    - 密钥扫描会拦下 dispatch 自己的测试文件 `tests/test_local_agent_dispatch.py`。这个文件里有用来测试扫描本身的假密钥，结果整个任务被拒，只能把它移出注入的上下文文件列表。
    - 修改任务的上下文只能包含已提交的文件（报错 "edit context must contain only files tracked at the reviewed HEAD"）。未提交的计划或设计文档无法传给执行者，只能把要点抄进任务描述。
    - 建议：
      - 密钥扫描允许按文件声明"测试夹具"，或者对测试目录只做告警。
      - 修改任务允许附带只读的未提交文档。

9b. **单个任务不能取消，合同里不认识的字段会被悄悄忽略**
    - 单个 `run` 没有取消命令，只有 `batch-cancel`。我发现一个任务的超时设错了，但取消不了，只能等它在 120 秒时超时，白白耗掉一次额度。
    - 本机安装的 0.7.16 不认识合同里的 `timeout_seconds`（第二轮才新增这个字段），但没有报错，直接用了默认的 120 秒。写合同的人会以为超时已经设好了。
    - 建议：
      - 新增 `dispatch cancel <run-id>`。
      - 合同中出现未知字段时直接拒绝，或者至少给出警告。

10. **每个 batch 最多一个修改成员**
    - 实测：两路 Codex 修改只能拆成一个 batch 加一个单独的 `run`。
    - Skill 推荐的"多路写入走 Core 任务"要求每个执行者配置不同的 adapter，而本工作区只配置了 codex。
    - 建议：
      - 说明多路写入的最小可行路径；
      - 或者允许不同仓、不同文件范围的多个修改成员共存。

## 四、清单 H 类中尚未覆盖的条目

以下来自使用方清单，本次没有复测：
- 并发满时直接失败，不排队（`no free slot ... max=2`）。
- 行号写成长破折号会报 unparseable（`locator_verify.py:90`）。
- 运行目录涨到 5.1 GB，因为每次都整份复制 `.codex.home`。
- 派发的运行无法追溯来源。
- scratch 模式需要手动 `git apply`。

## 4a. 工具覆盖面

要求：cc-switch 支持的工具全部要覆盖，常见工具也要覆盖。

Dyro 在两个层面支持一个工具：

- **技能分发**：把 dyro-* 技能装到该工具的技能目录里（`integration` 宿主表）。
- **任务派发**：通过 dispatch 无人值守地调用它（dispatch Provider）。

cc-switch 4.0.21 支持的工具清单，是从本机 `/Applications/CC Switch.app` 的程序和 `~/.cc-switch/cc-switch.db` 中只读提取的。本机安装情况按 2026-11-13 的 `command -v` 结果和技能目录是否存在来判断。

| 工具 | 来源 | 本机命令 | 本机技能目录 | 技能分发 | 任务派发 |
|---|---|---|---|---|---|
| Claude Code | cc-switch | `claude` | 有 | 已支持 | 已支持 |
| Codex | cc-switch | `codex` | 有 | 已支持 | 已支持 |
| Gemini CLI | cc-switch | 未安装 | `~/.gemini/skills` | **缺** | **缺** |
| Grok Build | cc-switch（grokbuild） | `grok` | 有 | 已支持 | 已支持，但结果解析失败（第 4 条） |
| OpenCode | cc-switch | `opencode` | 有 | 已支持 | 已支持 |
| Hermes | cc-switch | `hermes` | 有 | 已支持 | 已支持 |
| Pi | cc-switch | `pi` | 有 | 已支持 | 已支持 |
| Kimi Code | cc-switch | `kimi` | 未确认 | **缺** | 已支持，但状态不一致（第 7 条） |
| Antigravity CLI | cc-switch（agy） | `agy` | 未确认 | **缺** | **缺**（第 6 条） |
| MiniMax Code | cc-switch（mcode） | `mcode` | 未确认 | **缺** | **缺** |
| OpenClaw | cc-switch | 不在 PATH | `~/.openclaw/skills` | **缺** | **缺** |
| omp | cc-switch | 未安装 | 未确认 | **缺** | **缺**，全称和命令行用法待现查 |
| Cursor CLI | 常见 | `cursor-agent` | 有 | 已支持 | 只能在有 `CURSOR_API_KEY` 时只读运行（第 5 条） |
| DeepSeek（dsh） | 常见 | `dsh` | 有 | 已支持 | 已支持 |
| Qoder CLI | 常见 | `qodercli` | `~/.qoder/skills` | **缺** | **缺** |
| Qwen Code | 常见 | 未安装 | `~/.qwen/skills` | **缺** | **缺** |
| Factory Droid | 常见 | 未安装 | `~/.factory/skills` | **缺** | **缺** |
| Trae / Trae CN | 常见 | 未确认 | `~/.trae/skills`、`~/.trae-cn/skills` | **缺** | 待确认是否有无界面模式 |
| Kiro | 常见 | 未安装 | `~/.kiro/skills` | **缺** | **缺** |
| iFlow CLI | 常见 | 未安装 | `~/.iflow/skills` | **缺** | **缺** |
| Goose | 常见 | 未安装 | `~/.config/goose/skills` | **缺** | **缺** |
| Crush | 常见 | 未安装 | `~/.config/crush/skills` | **缺** | **缺** |
| GitHub Copilot CLI | 常见 | 未安装 | 无 | **缺** | **缺** |

接入前提与做法：

- 每个新工具接入派发前，都要现查它的无界面参数、只读或计划模式、结构化输出格式、登录探测方式和技能目录约定。这些都属于易变信息，不能凭记忆写进适配代码。
- 本机没装的工具没法实测。只能先在技能分发层接入，派发层标为"未验证"。
- 优先接入有本机命令的工具（agy、mcode、qodercli、kimi），它们可以直接实测。

**调研结论（2026-11-13）**

依据官方文档核对，详见 `2026-11-13-harness-conventions-research.md`：

- 以下工具都会读取 `~/.agents/skills/`，已经被 Dyro 现有的 `agents` 分身间接覆盖，技能分发层不需要新增宿主：
  - Gemini CLI、Kimi Code、Qwen Code、OpenClaw、omp、GitHub Copilot CLI、Crush、Goose；
  - MiniMax Code 还会读 `~/.claude/skills`、`~/.codex/skills`。
- 以下工具只读自己的目录，已新增为宿主（提交 `eb8be5f`）：
  - `gemini`（`~/.gemini/skills`，Antigravity CLI 也会读这里）；
  - `qoder`、`factory`、`trae`（IDE）、`traecli`（CLI）、`kiro`。
- iFlow 要求 SKILL.md 带 `license` 字段，Dyro 的技能目前没有这个字段，暂不接入。
- 无界面运行方面：
  - Gemini、agy、Qoder、Qwen、Droid、MiniMax 都有 `-p` 或 `exec` 形式的单次运行方式和 JSON 输出；
  - Crush 没有正式的无界面运行方式；
  - Copilot 和 iFlow 没有结构化输出。
  - 派发层先接本机已安装的 agy、mcode、qodercli，其余标为"未验证"。

## 五、本次协调的整体结论

- 4 个工具中，只有 Codex 能走完 dispatch 的完整流程。Grok、Cursor、agy 都要绕开 dispatch 直接调用，才能拿到结果。
- 绕开 dispatch 后结果质量不差：
  - Cursor 和 Grok 的设计稿中对现有代码的引用全部准确；
  - agy 的审查发现了 3 个真实问题。
- 结论：问题集中在适配层和失败报告，不在工具能力本身。
- 人工复核仍然必要。Codex 的两个补丁各有一处问题需要主会话修正：
  - 未提交的改动被当成"无改动"跳过；
  - 汇总状态的改动会挡住 setup/update 的自动修复。
  
  两个补丁都自报高置信度，测试也全部通过，但都没有发现这些问题。

## 建议的修复顺序

1. 第一节第 1、2 条：置信度和超时。改动小，直接消除"假成功"。
2. 第二节第 4、5、6 条：Grok、Cursor、agy 适配，让 4 个工具都能走 dispatch。
3. 第三节第 9 条：Skill 中的合同示例。
4. 其余条目。
