# Dyro 全项目对抗式复核审查委员会

日期：2026-07-31

范围：

- 基线：`origin/main@80819b5e73e185fcd9dea0752feece66b07fb229`（`v0.5.1`）
- Core：`src/dyro/`
- 本地 Agent dispatch：`experiments/local_agent_dispatch/`
- 供应链、发布与制品：`pyproject.toml`、`uv.lock`、`.github/workflows/`、GitHub Release / PyPI 工作流
- 用户 CLI 路径、状态/证据/签名/并发持久化及文档契约

审查材料：

- 当前隔离工作区：`/private/tmp/dyro-adversarial-release-review-20260731`
- 发布标签：`v0.5.1`
- 发布工作流：`https://github.com/DandreYang/DyroEngineeringFlow/actions/runs/30624818485`

固定决策：

- Docker-backed 外部 TypeScript semantic runtime 已从 Dyro 主项目移出；不得重新引入。
- Dyro 保持控制平面；本地 dispatch 不能取得 review、sign-off、merge、push 或发布权限。
- `v0.5.1` 已创建 Release，PyPI 上传处于人工环境审批边界；本次审查不会批准或上传。

## 规则

1. 审查员只编辑自己的签名章节。
2. 源码、可执行制品和真实 GitHub 状态优先于文档或历史结论。
3. 不可由本地或可读远端证实的项目标为“须人工核”。
4. 发现按 P0/P1/P2 分类，并给出可定位证据和复现/验证方法。
5. 必须尝试推翻自己的假设；禁止只用同一种文本搜索作为验证。
6. 不报告纯风格偏好；优先安全、授权、完整性、发布、数据一致性和用户可理解性。

---

# Atlas：安全、供应链与发布审查章节

审查员：Atlas
时间：2026-07-31（Asia/Taipei）
结论：**当前已发布制品的源码完整性可复证，未发现 P0；但下一次生产发布为 No-Go，直至关闭 P1-A1、P1-A2 和 P1-A4。** `v0.5.1` 已实际上传 PyPI，不能再把它描述为“等待人工审批”。

## 发现

### 已复证的正向事实（用于反驳误报）

- `v0.5.1` 的 GitHub Release 指向 `80819b5e73e185fcd9dea0752feece66b07fb229`；`git ls-remote --tags origin v0.5.1` 与 `git merge-base --is-ancestor <tag-sha> origin/main` 均成立。
- 远端工作流 `30624818485` 的 **Build and validate distributions** 与 **Publish to PyPI** 两个 job 都是 `success`；PyPI `dyro/0.5.1` 已在 `2026-07-31T10:50:36Z`（wheel）和 `10:50:37Z`（sdist）上传，不再是等待状态。
- 直接下载 PyPI 两个文件并按 JSON 索引 SHA-256 复算均匹配；wheel 的 48 个 `.py` 与 tag `80819b5` 逐字节一致，sdist 的 75 个可追踪源文件也逐字节一致；两者均不含 `external_workflow_runner`。wheel `RECORD` 的 53 个带哈希条目也全部通过。因此“已移出的 Docker/TS runtime 被重新发布”这一假设被推翻。
- `.github/workflows/` 的 `actions/checkout`、`setup-python`、`setup-uv`、`setup-node`、`upload-artifact`、`download-artifact` 与 `pypa/gh-action-pypi-publish` 都是完整 SHA pin；逐一用其远端 tag 的 peeled commit 比对，均与注释版本匹配。前次对 `pypa` 的误差来自没有解引用 annotated tag；`v1.14.1^{}` 正是配置的 `ba38be9…`。
- PyPI Integrity API 对公开 wheel 返回 `200`：publish attestation 的 subject SHA-256 为 `999b0163…77a19f`，publisher 为 `GitHub / DandreYang/DyroEngineeringFlow / pypi-publish.yml / pypi`。这与公开文件哈希及 workflow 身份一致；传统 JSON 字段 `has_sig=false` 不代表缺少 PEP 740 provenance。

### P0

无已证实 P0。以上 PyPI 制品、Release tag 和实际源码之间的独立字节比对通过，不能把已发布的 `0.5.1` 误报为制品篡改或错误包含已移出 runtime。

### P1-A1：手动发布入口没有证明“输入确为受信 tag，且该 tag 对应受保护的主线提交”

**证据：**

- [`.github/workflows/pypi-publish.yml:6-12`](../../../.github/workflows/pypi-publish.yml) 接收任意字符串 `release_tag`；[23-26](../../../.github/workflows/pypi-publish.yml) 把它直接交给 `actions/checkout` 的 `ref`。
- [42-56](../../../.github/workflows/pypi-publish.yml) 唯一的来源身份检查只是 `RELEASE_TAG == "v" + pyproject.project.version`。没有验证 `refs/tags/$RELEASE_TAG` 存在、没有验证 `GITHUB_SHA` 等于该 tag 的 commit、没有验证 annotated/signature，也没有验证该 commit 是 `origin/main` 的祖先。
- [`docs/publishing.md:47`](../../publishing.md) 却承诺手动入口“checkout 该 tag，并严格校验 tag 必须等于版本”。源码实际严格校验的只有**名字字符串**，不是 tag 类型或来源链。

**影响：** 在未来版本中，具有 Actions 调度和环境审批能力的人可以让工作流对“名字看似 `vX.Y.Z`”但来源未被流程证明的 ref 构建。即使 OIDC 不泄露长期 PyPI token，也无法由发布记录回答“这个包是否确实来自受保护主线的批准 tag”。这会使误发布或内部凭据失陷后的追责、撤销与复现变弱。

**复证方式：** 静态读取上述 shell：它没有任何 `git rev-parse refs/tags/...`、`git cat-file`、`git verify-tag`、`git merge-base` 或 `GITHUB_SHA` 比较；向 `workflow_dispatch` 输入文本只会参与 52-55 行字符串比较。当前 `v0.5.1` 恰好满足主线祖先关系，是发布者操作正确，不是工作流强制的结论。

**建议修复：** 把 ref 验证抽为受单元测试的脚本。手动入口只 checkout `refs/tags/${RELEASE_TAG}`，以完整 history 获取 `origin/main`，然后验证：tag ref 存在、解析后的 commit 等于 checkout SHA、该 commit 可达受信主线；若采用 tag 签名策略，再拒绝 lightweight/未验证签名的 tag。Release event 同样走该脚本，避免两条入口漂移。

**验收：** 在临时 Git 仓库覆盖并拒绝：同名 branch、轻量或错误 tag、tag 指向非主线 commit、tag/`pyproject` 版本不匹配；只接受受信 tag → `main` 祖先链。发布日志须输出 tag commit SHA 和主线验证结果。

### P1-A2：仓库与 PyPI 环境并未形成独立、不可绕过的生产审批边界

**远端事实（2026-07-31 只读 API）：**

- `GET /repos/DandreYang/DyroEngineeringFlow/branches/main/protection` 返回 `404 Branch not protected`，`GET .../rulesets` 返回 `[]`；即 `main` 没有可见的 PR/状态检查/推送限制规则。
- `GET .../actions/permissions/workflow` 显示默认 workflow 权限为 `write`；虽然当前两个工作流各自显式收紧权限，未来直接推入 `main` 的工作流并不继承该最小权限约束。
- `GET .../environments/pypi` 显示：唯一 required reviewer 是 `Dandre126`，`prevent_self_review=false`、`can_admins_bypass=true`、`deployment_branch_policy=null`。

**影响：** 对单一管理员而言，修改发布工作流/主线、创建 Release 和批准环境都可以由同一身份完成；人工环境门仍能阻止偶发操作，但不是独立审查，也不限制发布来源分支。若目标是生产供应链的双人或抗账号失陷控制，这一边界不足。

**复证方式：** 使用上述 GitHub REST 端点即可重现，无需审批、发布或改动仓库。

**建议修复：** 这是治理决策，须人工核：若要求职责分离，至少启用 `main` ruleset（PR、通过 CI、限制直接 push），对 `v*` 设创建/更新/删除限制，PyPI environment 启用受信来源分支/标签规则、禁止管理员 bypass、启用 prevent-self-review，并配置第二位独立 reviewer。若团队明确只有一位受信发布人，应在发布政策中明示这是“单人手工确认”而非独立审批，并把账户恢复、MFA、PAT/SSH key 轮换写入运行手册。

**验收：** 用非管理员协作者和发布者本人分别验证：不能直接改主线/发布工作流、不能移动受保护 tag、不能自行批准 PyPI deployment；另以受信 tag 成功通过一次审批演练。单人模式则须由项目所有者显式接受该剩余风险。

### P1-A3：发布流程绕过已提交的依赖锁与构建工具版本，削弱可重复性和供应链审计

**证据：**

- [`uv.lock:1-4`](../../../uv.lock) 声明锁文件；其 [`cryptography` 记录](../../../uv.lock) 固定为 `49.0.0` 且包含下载哈希，项目的直接依赖元数据见 [`pyproject.toml:11-14`](../../../pyproject.toml)。常规 CI 确实在 [`ci.yml:37-41`](../../../.github/workflows/ci.yml) 用 `uv lock --check` 和 `uv sync --locked`。
- 发布工作流却在 [`pypi-publish.yml:33-40`](../../../.github/workflows/pypi-publish.yml) 执行未约束的 `pip install --upgrade build twine` 与 `pip install --editable .`，再在 [58-59](../../../.github/workflows/pypi-publish.yml) 用该环境构建；它从不校验或安装 `uv.lock`。

**影响：** 相同 Git tag 的“发布测试通过”会随 PyPI 上的 `cryptography`、构建后端、`build`、`twine` 最新版本而变化，且发布记录不能回答实际测试/构建用了哪些解析版本和哈希。`v0.5.1` 的公开包已由字节比对确认正确；风险在于后续版本的不可重现与上游供应链漂移，而非把当前包误判为损坏。

**复证方式：** 对比上述两条 workflow 路径即可复现：CI 使用 lock，发布路径只调用裸 `pip`。在无缓存/上游发布新兼容版本的 runner 上运行 release build 即会得到不同解析结果。

**建议修复：** 发布 job 使用与 CI 相同、SHA pin 的 `setup-uv`、`uv lock --check` 和 `uv sync --locked --all-extras --dev` 执行测试；固定 PEP 517 backend 与 release tooling（或将其以受审计 constraints/lock 提供）；生成并保留 `pip inspect`/SBOM、wheel/sdist SHA-256 作为 release artifact。不要把终端用户的 `Requires-Dist >=` 改成不必要的精确锁定；目标是锁定**发布构建环境**。

**验收：** 断网或只允许 lock 中工件的环境可以完成测试/构建；构建日志与制品附带解析清单和 SHA；修改 lock 后未更新发布流程必须失败。对两个独立 runner 比较解包后的源码、METADATA 与制品哈希（或明确记录并解释时间戳导致的可接受字节差异）。

### P1-A4：已公开的 0.5.1 被同时标注为 “Unreleased” 和 “Pre-Alpha”，发布状态对用户不准确

**证据：**

- [`CHANGELOG.md:3-14`](../../../CHANGELOG.md) 仍写 `## 0.5.1 - Unreleased`。
- [`pyproject.toml:7`](../../../pyproject.toml) 是 `0.5.1`，但 [`19-27`](../../../pyproject.toml) 的 PyPI classifier 仍是 `Development Status :: 2 - Pre-Alpha`。
- 远端 Release `v0.5.1` 已发布，且 PyPI JSON 已列出 `0.5.1`，工作流 `30624818485` 的上传 job 成功；这不是预发布或待审批状态。

**影响：** 用户通过 PyPI 看到的是一个正式可安装版本，却被元数据与变更记录告知“未发布/Pre-Alpha”；这会误导上线评估、支持预期与安全修复渠道判断，也使发布者无法从仓库得到准确的事故时间线。

**复证方式：** 打开上述文件和 PyPI `https://pypi.org/pypi/dyro/0.5.1/json`；下载的 wheel METADATA 也包含该 classifier。

**建议修复：** 立即把 changelog 条目标记为实际发布日期，并由产品所有者决定真实成熟度后调整 classifier（若仍非生产级，必须调整“可生产上线”的对外文案而非伪装为稳定）。在 release checklist 中加入“changelog 状态、PyPI classifier、Release 状态三者一致”的自动检查。

**验收：** 新提交的 `CHANGELOG` 日期、wheel METADATA classifier、GitHub Release 与 PyPI 版本均一致；新增测试/脚本在版本标为 `Unreleased` 时拒绝创建正式 Release。

### P1-A5：不存在 PyPI 误发布/漏洞发布的撤回与用户通知运行手册，当前公开版本没有预先定义的止血窗口

**证据：**

- [`docs/publishing.md:24-47`](../../publishing.md) 只描述“如何发布”和发布后安装；未定义 yanking、GitHub Release 更正、受影响用户通知、替代版本、Trusted Publisher/账户失陷处置或恢复目标。对 `README*`、`docs/`、workflow 进行发布事故关键词和实际文档阅读，只找到产品内部 task/hotfix/key-revoke，而没有 PyPI release incident 流程。
- 远端 PyPI JSON 在本审查时仍显示 `0.5.1` 的两个文件 `yanked=false`；其首次公网暴露时间分别是 `2026-07-31T10:50:36Z` 与 `10:50:37Z`。这是**潜在误发布的暴露窗口起点**，不是“已发现密钥泄露”的证据。
- [PyPI yanking 文档](https://docs.pypi.org/project-management/yanking/) 明确 yanking 只能对整个 release，且精确 `==`/`===` 版本约束仍可能安装该版本；它是非破坏性的，不能撤销已经下载的制品。

**影响：** 如果未来发现错误源码、依赖供应链事故或凭据泄露，现有团队无法从仓库获知谁在几分钟内 yank、yank 后如何让 `==` 用户停止使用、是否保留/更正 GitHub Release、何时发布递增修复版本或何时轮换可信发布者。等待临时协调会扩大受影响用户的窗口。

**复证方式：** 读取上述发布文档并查询 PyPI `https://pypi.org/pypi/dyro/0.5.1/json` 的 `yanked` 字段；然后对照 PyPI 官方 yanking 行为。无需执行 yank、删除或重新发布。

**建议修复：** 在 `docs/publishing.md` 增加一页可执行的发布事故 runbook：分级、明确 incident owner 与 PyPI maintainer、在目标时限内对**整个**版本 yank 并填写原因、保留 tag/证据而非篡改历史、标记/更正 GitHub Release、发布递增修复版本、向固定版本用户/安全公告通知、以及在发布身份失陷时移除/替换 Trusted Publisher 和 GitHub 权限。演练要使用 TestPyPI 或专用演练版本，不能把生产 `0.5.1` 当作测试对象。

**验收：** 经人工批准的 runbook 中包含发布后 `5/15/60` 分钟责任与沟通动作、yank reason 模板和固定版本用户的升级说明；在 TestPyPI 完成一次“错误发布 → yank → 递增修复”桌面或自动化演练，证据链接回 release checklist。

### P2-A1：Intel macOS 的最终用户安装路径没有在发布矩阵中复证

**证据：** [`ci.yml:52-100`](../../../.github/workflows/ci.yml) 只对 wheel/sdist 做 Ubuntu 安装验证并在 Windows 做 import/fail-closed 验证，没有 macOS job。实际在本审查主机（macOS x86_64、Python 3.13）用公开的 `dyro-0.5.1` wheel 安装时，pip 选择 `cryptography-49.0.0.tar.gz` 而不是预编译 wheel，进入 source build；`pyproject.toml:11-14` 只给出了下界。

**影响：** README 的 `pipx install dyro` 在 Intel macOS 上可能要求 Rust/C 编译链，安装耗时和失败面与 Ubuntu/Windows CI 不同。当前审查没有等待本机 source build 完成，故不能声称它必然失败。

**复证方式：** 在无缓存的 Intel macOS Python 3.11–3.14 环境执行 `pipx install dyro==0.5.1 -v`，记录 resolver 是否获得 binary wheel 或进入 `cryptography` source build，并运行 `dyro --version` 与 `dyro dispatch doctor`。

**建议修复与验收：** 将 macOS arm64/x86_64 clean-install smoke 纳入发布前检查，或在安装文档明确源构建先决条件和支持范围；在干净 macOS 环境完成一次 `pipx` 安装与两个 CLI smoke 后关闭。

## 须人工核

- `0.5.1` 的 PyPI publish attestation 已通过 Integrity API 可读性核验；是否还要求更强的 SLSA source provenance、SBOM 保留期及漏洞响应 SLA，需由发布所有者确定。
- P1-A2 的职责分离与管理员 bypass 不能由代码替代。单人发布模式可以继续运行，但必须由项目所有者书面接受它不是独立审批这一风险。

## Go/No-Go

| 范围 | 结论 | 依据 |
| --- | --- | --- |
| 已发布 `v0.5.1` 制品完整性 | Go（已发布，不可回滚为“未发布”） | 远端 tag、成功 workflow、PyPI hash、wheel/sdist 对 tag 的独立逐字节比对均通过。 |
| 下一次生产发布 | No-Go | 必须先关闭 P1-A1（受信 tag/source 证明）、P1-A2（明确并落实审批治理）、P1-A3（锁定发布构建环境）和 P1-A5（误发布撤回运行手册）；P1-A4 应在随后的补丁发布前纠正对外状态。 |
| 当前用户安装兼容性 | Conditional Go | Linux/Windows release smoke 已成功；Intel macOS 路径为 P2，须完成真实 clean-install 验收或明确支持边界。 |

---

# Curie：本地 dispatch、Provider 适配与进程生命周期审查章节

审查员：Curie
时间：2026-07-31
结论：**No-Go（本地 dispatch 作为真实 Provider 工作入口）**。Core 的 gate/merge 边界未被本审查发现可绕过，但 dispatch 仍有一个 P0 机密外泄路径，以及四项会产生虚假可用性或不受限访问的 P1。不能把本地 Provider dispatch 宣称为生产可用，直至 P0 关闭且 P1 的安全默认与用户路径被修复。

## 发现

### P0-CURIE-01：任务文本、现代凭据和模型回显均可越过机密守卫进入远端 Provider 或本地持久化

证据：

- [`experiments/local_agent_dispatch/task_contract.py:58-63`](../../../experiments/local_agent_dispatch/task_contract.py) 对五段任务文本只检查非空和单字段长度；没有调用任何机密检测或脱敏逻辑。
- [`experiments/local_agent_dispatch/adapters/subprocess_cli.py:65-87`](../../../experiments/local_agent_dispatch/adapters/subprocess_cli.py) 将 `briefing`、`locations`、`objective`、`constraints`、`output_contract` 原样拼进 Provider prompt。设计文档还建议在 objective 粘贴完整错误/日志（[`docs/designs/optional-local-agent-dispatch.md:84-88`](../../designs/optional-local-agent-dispatch.md)）。
- [`experiments/local_agent_dispatch/context_guard.py:33-40`](../../../experiments/local_agent_dispatch/context_guard.py) 仅覆盖少量旧格式；本审查用非真实样例验证，`OPENAI_API_KEY=sk-proj-...` 与 `GITHUB_TOKEN=github_pat_...` 都得到 `allowed=True`。
- [`experiments/local_agent_dispatch/adapters/subprocess_cli.py:114-129`](../../../experiments/local_agent_dispatch/adapters/subprocess_cli.py) 接受 model summary/evidence 的任意文本；[`experiments/local_agent_dispatch/result_envelope.py:68-82`](../../../experiments/local_agent_dispatch/result_envelope.py) 随后把它们放入结果记录。相同的非真实样例可被 `_parse_model_json` 原样接受于 summary 和 evidence claim。

影响：用户把 CI 日志、issue 文本或源码片段交给 dispatch 时，未识别的 token 可被发送给 Codex/Claude；Provider 也可把已见内容回显，继而落盘为 run/panel 结果。现有「文件注入前机密守卫」的产品承诺不覆盖任务文本和结果文本，形成实际的机密外泄边界缺口。

复证：不启动真实 Provider，执行了下列只读构造检查：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c '... parse_task_contract(... objective="token=sk-..."); print(token in _build_prompt(contract, {}))'
# secret_accepted=True; secret_in_prompt=True

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c '... print(check_content("OPENAI_API_KEY=sk-proj-...").allowed); print(check_content("GITHUB_TOKEN=github_pat_...").allowed)'
# True; True
```

建议修复：建立唯一的、版本化且可测试的 secret scanner/redactor，并在以下边界 fail-closed 或脱敏后再流转：TaskContract 五段文本、每个 context 文件、Provider stdout 的 summary/evidence/warnings、持久化前的 error 文本。规则至少覆盖当前 Provider 与 GitHub token 格式，且采用长度/总量上限；禁止把原始命中值写入日志或错误信息。不要通过重新引入 Docker 规避此问题。

验收：新增参数化测试，证明 task 文本命中 token 时 Provider `run` 永不调用；现代 token 格式的 context 被拒绝；模拟 Provider 回显 token 后 run/panel/state 文件和 stderr 中均不含明文 token。对正常源码和无 token 结果保持兼容。

### P1-CURIE-02：`dispatch run --dry-run` 对无效契约和未知 Provider 返回成功，给出虚假预检绿灯

证据：[`experiments/local_agent_dispatch/cli.py:67-81`](../../../experiments/local_agent_dispatch/cli.py) 在读取 JSON 后，若 `args.dry_run` 就直接打印 `action=dispatch-run` 并返回 `0`；它没有调用 `parse_task_contract`、文件守卫、Provider 注册表或项目根校验。真实命令（未创建状态、未启动 Provider）已复现：

```sh
printf '{}\n' | .venv/bin/python -m experiments.local_agent_dispatch \
  --dry-run run --project . --stdin --backend definitely-not-a-provider
# 输出 dry_run=true，EXIT=0
```

影响：用户会把空任务、越界 files、strict/edit 冲突或拼错的 Provider 当作「可执行的计划」；在首次接入或发布前预检时尤其容易误判。

建议修复：dry-run 应执行纯本地的 `parse_task_contract`、project/files/context 预检和 Provider ID/能力解析，但不得启动认证探测或 Provider。输出显式的 `valid`、`resolved_backend` 与不可执行原因；不合法输入退出码必须为 `2`。

验收：空 JSON、未知 backend、零匹配 files、secret context、strict+edit 在 dry-run 均失败且不创建 state/不 spawn；有效输入输出已解析 backend 和所需人工动作。

### P1-CURIE-03：`backend=auto` 在没有真实 Provider 时会把离线 echo 模拟器包装成高置信度的已完成任务

证据：

- [`experiments/local_agent_dispatch/adapters/registry.py:44-55`](../../../experiments/local_agent_dispatch/adapters/registry.py) 的 auto 顺序包含 `echo`，且它满足 available/authenticated。
- [`experiments/local_agent_dispatch/adapters/echo.py:17-21,48-54`](../../../experiments/local_agent_dispatch/adapters/echo.py) 把 echo 声明为可用且已认证，并返回 `status="ok"`、`confidence="high"`。
- [`experiments/local_agent_dispatch/supervisor.py:776-787`](../../../experiments/local_agent_dispatch/supervisor.py) 将该 `ok` 转成 persisted `completed`；evidence locator 还能产生 1.0 的 `verified_ratio`。唯一提示在 warning，缺少机器可执行的 simulated 标记。
- 不启动真实 Provider 的 registry mock 复证：仅保留 `EchoAdapter` 时，`get_adapter("auto").id == "echo"`。

影响：没有已登录真实 Provider 时，普通用户提交默认 `auto` 任务会得到 `completed/high/verified_ratio=1.0`，但实际上没有模型分析。虽然 Core 不以此作为 gate，这是明显的用户可理解性和自动化消费风险。

建议修复：从 `auto` 候选中移除 echo；没有真实、已认证 Provider 时 fail-closed 并列出发现/登录指引。echo 仅允许显式 `--backend echo`（或显式 `--allow-offline-simulation`），且结果必须包含不可忽略的 `simulated=true` / `execution_kind=offline`，不得以可被上游误认为成功的完成结论呈现。

验收：mock 全部真实 Provider 不可用时，auto 非零退出且不创建 run；显式 echo 返回明确 simulation 类型，CLI/UI/skill 的文案不会将其与真实 Provider 并列为「已认证」。

### P1-CURIE-04：已确认的 Provider 自动发现与首次选择体验尚未实现；route 记录也不参与实际调度

证据：

- [`experiments/local_agent_dispatch/adapters/registry.py:13-19`](../../../experiments/local_agent_dispatch/adapters/registry.py) 固定注册 `echo`、`codex`、`claude`；没有可扩展 Provider descriptor 或对 Cursor、OpenCode、Grok、Hermes、Kimi 等本机 CLI 的检测。
- [`experiments/local_agent_dispatch/skill_render.py:30-35`](../../../experiments/local_agent_dispatch/skill_render.py) 的 `route add` 可以保存任意 backend 字符串；但 route 只在同文件的渲染路径读取（[`skill_render.py:38-112`](../../../experiments/local_agent_dispatch/skill_render.py)），[`panel.py:23-45`](../../../experiments/local_agent_dispatch/panel.py) 和 registry 均不消费这些偏好。
- 主 CLI 的 profile adapter 预设也只有 `codex`/`noop`（[`src/dyro/cli.py:1120-1128`](../../../src/dyro/cli.py)），与 dispatch 的 Claude 支持不一致。

影响：用户此前明确选择「自动检查本地可用 Provider，或首次使用时让用户选择」。当前只能得到两个硬编码真实 Provider；保存的“用户路由”不会影响执行，且可保存永远不可用的名称。用户会在 setup、`backends`、`route add` 和实际 run 之间遭遇不一致。

建议修复：设计受控的 Provider descriptor/adapter 入口（每个 Provider 有 command、非交互 auth probe、最小环境白名单、能力和隔离声明），先以安全的本机命令发现生成候选列表，再在首次真实 dispatch 要求选择/确认并保存**经校验**的偏好。执行时必须读取该偏好或显式 backend，并显示最终 Provider。未知或未认证 Provider 不能写入 route。保持 Dyro 为控制平面；不重新引入 Docker 或外部 semantic runtime。

验收：用临时 PATH 的假 CLI 覆盖至少 Codex、Claude、Cursor、OpenCode、Kimi 的发现/未认证/已认证三态；首次选择持久化后会真实影响 `auto` 的排序；route add 对未知/未认证名称拒绝；无真实 Provider 时给出可操作安装/登录说明而非 echo 成功。

### P1-CURIE-05：真实 Provider 的默认非 strict 路径只靠提示词限制 files，不会物理限制其读取项目中未列入的文件

证据：

- [`experiments/local_agent_dispatch/supervisor.py:656-677`](../../../experiments/local_agent_dispatch/supervisor.py) 对默认 `strict=false` 令 `work_cwd=project_root`；只有 strict 才创建 shadow。
- Codex 仅使用 read-only sandbox（[`subprocess_cli.py:286-301`](../../../experiments/local_agent_dispatch/adapters/subprocess_cli.py)），Claude 显式授予通用 `Read`（edit 模式为 `Read,Edit`，[`subprocess_cli.py:350-365`](../../../experiments/local_agent_dispatch/adapters/subprocess_cli.py)）。两者没有 files allowlist 传给工具层。
- prompt 中的 “Use only the context supplied below” 只是指令（[`subprocess_cli.py:77-80`](../../../experiments/local_agent_dispatch/adapters/subprocess_cli.py)）；设计文档也承认 Codex/Claude 不具备 strict isolation（[`docs/designs/optional-local-agent-dispatch.md:174-182`](../../designs/optional-local-agent-dispatch.md)）。

影响：`files` 只限制注入文本，不能限制真实 Provider 工具读取同一项目里的未列文件。默认 `strict=false` 容易被当作「文件白名单已生效」；一旦 context 中存在提示注入或任务含糊，敏感项目文件可被读取并回显到结果。此项不建议用 Docker 回退；当前固定决策已移出该 runtime。

建议修复：在真实 Provider dispatch 前要求显式 acknowledgement（例如 `--allow-unconfined-provider`）并将结果标识为 `isolation=best_effort`；生产默认应在可证明物理隔离前 fail-closed。长期方案是每个 Provider 的本机隔离能力经独立验证后才可声明 strict，或使用只包含批准文件的本机投影目录并明确其边界。

验收：默认真实 Provider 请求在无已验证 isolation 时明确拒绝并说明原因；带显式 acknowledgement 的结果包含 non-strict 风险标记；Provider-specific strict capability 需用独立的文件越界读取测试证明后才能启用。

### 须人工核：进程身份与 Windows/POSIX 部署基线

- 本审查的受限 macOS 沙箱禁止执行 `/bin/ps`。因此 [`process_identity.py:21-55`](../../../experiments/local_agent_dispatch/process_identity.py) 退化为 `unknown-...` token，随后 [`process_identity.py:109-118`](../../../experiments/local_agent_dispatch/process_identity.py) 返回 self-match false。focused suite 的 92 项中 91 通过、1 项失败：`ProcessIdentityTests.test_current_identity_matches_self`。这不能证明 GitHub Linux CI 或真实 macOS 终端存在同一故障，故标为**须人工核**；若产品需要在受限执行器中运行，则应把该路径升级为 P1 并改为可验证的原生进程身份来源/显式 unsupported。
- Windows 的当前承诺是 import/discovery 可用、`run`/`panel`/worker fail-closed（[`supervisor.py:64-69`](../../../experiments/local_agent_dispatch/supervisor.py)，[`README.md:40-43`](../../../experiments/local_agent_dispatch/README.md)）。须在干净 Windows 主机实际安装 wheel 后验证：`dyro dispatch --dry-run doctor` 不写状态/不启动 CLI，`dyro dispatch run` 给出清晰 POSIX 限制错误。不得把未实测的 Windows 执行能力写入发布声明。

已执行验证：

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest tests.test_local_agent_dispatch tests.test_local_agent_dispatch_l1_l4 tests.test_adversarial_remediation_dispatch -v`：92 项，91 pass、1 fail、1 skip；唯一失败如上，且受 `/bin/ps` 沙箱限制影响。
- 所有复现均为本地解析/构造或 echo 测试；未启动 Codex、Claude 或任何外部 Provider，未创建外部资源。

## Go/No-Go

- Local dispatch（真实 Provider）：**No-Go**。先关闭 P0-CURIE-01；随后完成 P1-CURIE-02 至 P1-CURIE-05 的安全默认、真实 Provider 选择与 dry-run 可信度修复，并在真实 macOS/Linux 与 Windows discovery 场景完成上述人工核。
- Local dispatch（仅显式 echo 协议测试）：**Conditional Go**。可保留为开发/测试工具，但必须在 P1-CURIE-03 修复前禁止它成为 `auto` 的成功回退。
- 进程清理与租约：源码与大部分对抗单测表明 fail-closed 方向正确；本受限沙箱无法提供生产级进程身份闭环证据，不能以本次本地结果替代目标主机验证。

---

# Turing：Core 用户流程、状态/证据/签名与 CLI 体验审查章节

审查员：Turing
时间：2026-07-31（Asia/Taipei）
结论：**No-Go（Core 任务生命周期）**。已复证 3 个 P1：默认本地任务可绕过独立 review 直接合并、真实执行失败会遗留不可恢复的 `in_progress`，以及文档承诺的外部 `QUESTION → answer → 下一份证据` 续跑由内置 CLI 生成器实际无法完成。另有 1 个会触发/放大该恢复问题的 P2 输入校验缺口。未发现可由本审查复证的 P0；但 P1 未关闭前不能把 Core 描述为可生产上线。

## 发现

### 已复证的正向事实（用于排除误报）

- `run → review → review → merge` 的标准本地路径、要求外部 sign-off 的路径，以及带签名外部 CLI 的端到端路径均通过了现有测试；问题不是正常路径完全不可用，而是 public CLI/生成器存在没有走该路径的反例。
- 使用 `UV_CACHE_DIR=/tmp/dyro-adversarial-uv-cache uv run python -m unittest discover -s tests -t . -q` 复跑全套测试，退出码为 0。Docker 相关的集成用例仍依环境预期跳过；该结果不能抵消以下用真实 Core API/CLI 语义构造出的反例。

### P1-TURING-01：`task status` 可跳过独立 review，直接将默认任务合并

**证据：**

- [`src/dyro/cli.py:530-537`](../../../src/dyro/cli.py) 的公开 `dyro task status TASK done` 直接调用 `set_status`；没有 `--force`、review receipt 或 reviewer 身份要求。
- [`src/dyro/tasks.py:37-47`](../../../src/dyro/tasks.py) 允许 `review → done`；[`423-436`](../../../src/dyro/tasks.py) 的 `set_status` 只校验图上的状态迁移。默认 profile 不要求 external sign-off 时，它不会验证 review evidence。
- [`src/dyro/tasks.py:1809-1813`](../../../src/dyro/tasks.py) 的 `merge_task` 只要求状态为 `done`，不会再次绑定/验证已接受的 review。
- 这与 [`README.md:455-457`](../../../README.md) 和 [`docs/diagrams.md:112`](../../diagrams.md) 所承诺的“独立 review PASS 后才能 done”不一致。

**最小复现：** 用测试工作区创建默认 `local` 任务，先运行至 `review`，不执行 `review_task`。随后走与 CLI 相同的 `set_status(config, task, "done")`，再调用 `merge_task`：输出为 `run=review`、`status=done review_exists=False`、`merge=ok`。任务 ledger 只记录 `phase=status, from_status=review, to_status=done`，没有 review acceptance、reviewer 或理由。该 API 是上述 CLI 命令的直接实现，不依赖篡改 state 文件。

**影响：** 单机操作员可以误操作或用脚本绕过产品反复强调的独立审查，未审查变更会进入 delivery line。该问题不是外部权限提升，但破坏了 Dyro 自己的发布完整性与可审计性承诺。

**建议修复边界：** `task status` 应只提供观察或安全恢复，禁止它进入 `review`、`review_pending_signoff`、`done` 等质量门状态；把 `done` 收敛到 `_apply_review_decision`/`_signoff_task` 等私有路径，并让 `merge_task` 重新验证有效、绑定当前 task head 的 accepted review/sign-off。若保留管理员恢复，另设显式 `task recover --force --reason`，在 ledger 记录 actor/reason/override，且不能单独解除 merge 所需的 review 证据。

**验收：** CLI 与直接 public API 对 `review → done` 均拒绝；伪造/缺失 review 时 merge 拒绝；正常 review、外部 sign-off 和签名 evidence 仍可完成；帮助、README 和状态图同一语义。

### P1-TURING-02：Agent 或 gate 的真实运行异常只失败 attempt，任务永久停在 `in_progress`

**证据：**

- [`src/dyro/tasks.py:680-719`](../../../src/dyro/tasks.py) 的 `_complete_execution_attempt` 在 executor 异常时将 execution attempt 标为 `failed`、写 `attempt_failed` 后重新抛出，但没有把 task 转为 `failed`。
- `run_task` 经 [`1143-1170`](../../../src/dyro/tasks.py) 调用该 helper，实际 Provider/gate 执行在 [`1190`](../../../src/dyro/tasks.py)；answer 后 continuation 同样经 [`1396-1423`](../../../src/dyro/tasks.py) 走此路径。
- [`src/dyro/process.py:36-49`](../../../src/dyro/process.py) 会对缺失命令和超时抛 `DyroError`。这两个正常故障面没有被 task 状态恢复处理。

**最小复现：** 配置 `noop.write = definitely-missing-dyro-agent` 后运行任务，得到 `DyroError: 找不到可执行命令…`，随后读取 state：`task.status=in_progress`、`attempt.status=failed`。将任务 timeout 配为 `0` 时，`/usr/bin/true` 抛出 `命令超时（0s）` 后也留下 `in_progress`。当前 `run_task` 只允许 `backlog/assigned/failed` 进入，`answer_task` 只接受 `waiting_answer`，因此用户不能通过正常 retry/answer 自救，只能调用前述不安全的任意状态命令。

**影响：** 常见本机安装、PATH、Provider、gate 或超时故障会产生“任务正在运行”的假象并卡死用户流程；attempt ledger 与 task 状态相互矛盾，后续操作的恢复语义不可预测。

**建议修复边界：** 对执行/continuation/gate 的异常，在确认 task 仍属于该 attempt 且状态为 `in_progress` 后原子化迁移为 `failed`，保留原始异常为主错误、避免 ledger/cleanup 异常覆盖它；不得把已进入 review 的失败错误回退成执行失败。随后提供受控 retry，新的 attempt 必须单调递增。

**验收：** 缺失 adapter、超时、gate 启动失败和 answer continuation 失败都产生 `attempt=failed` 与 `task=failed`，可重试且生成新 attempt；异常文案/退出码保留；并发 worker 不能把其他 generation 的 task 标失败。

### P1-TURING-03：外部 `QUESTION → answer → 下一份 evidence` 是文档承诺，但内置证据生成器必然产生 attempt 冲突

**证据：**

- [`README.md:363`](../../../README.md) 宣称外部 runner 返回 `QUESTION` 后，`task answer` 保留 claim、任务回到 `assigned` 并接收下一份 evidence。
- [`src/dyro/evidence.py:231-259`](../../../src/dyro/evidence.py) 构建 external execution record 时未传入任务已有 attempt 序号；[`src/dyro/provenance.py:201-230`](../../../src/dyro/provenance.py) 的 `build_external_attempt_record` 每次随机生成 run/attempt ID，却硬编码 `attempt_number: 1`。
- [`src/dyro/provenance.py:385-414`](../../../src/dyro/provenance.py) 对同一 task 的同号不同 attempt ID 正确地 fail-closed；因此它会拒绝该内置生成器的第二份 bundle。

**最小复现：** 用 external profile 的内置 `evidence build` 生成 `QUESTION` bundle，claim/import 后任务为 `waiting_answer`；调用 `answer_task` 后状态为 `assigned`。再次用同一内置生成器生成 `DONE` bundle 并 import，得到 `ValidationError: external attempt 序号冲突：TASK-…`，任务仍为 `assigned`。现有 `test_external_question_can_be_answered_by_the_claimed_runner` 仅覆盖 answer，没有覆盖下一次 import，因此未发现该断链。

**影响：** 用户完全按公开 CLI/README 操作时，外部问题续跑无法闭环；已获回答和 claim 无法让下一轮 proof 导入。若通过手改 evidence 绕过，又会削弱 provenance 的单调性保障。

**建议修复边界：** 证据 build 不能使用 runner-local随机 attempt 计数。由 Core 在 claim/answer 时保留并签发或预约下一期 attempt number，生成器带入同一 `run_id` 和严格单调的 attempt number，import 对 reservation/claim generation 绑定校验。不要为“修复”而放宽同号冲突拒绝；若暂不支持续跑，应从 CLI/README 删除该承诺并明确 fail state。

**验收：** 用实际 CLI 覆盖 unsigned 与 signed 两套 `QUESTION → answer → DONE`；第二份 bundle 成功后 lineage 为同 run、递增 attempt，重放/并行旧 bundle/跨 claim 的 bundle 均被拒绝，且失败不会改变 task 或 claim。

### P2-TURING-01：task manifest 的时限字段被隐式强转，`bool`、字符串、零和负数可在开始后才触发异常

**证据与复现：** [`src/dyro/tasks.py:148`](../../../src/dyro/tasks.py) 对 gate timeout 使用 `int(...)`；[`163-164`](../../../src/dyro/tasks.py) 同样转换 task/review timeout。将 template 替换为 `timeout_minutes = true`、`review_timeout_minutes = "0"`、`timeout_seconds = -1`，`_parse_task` 成功得到 `1, 0, -1`；其中 `timeout_minutes=0` 可直接触发 P1-TURING-02 的 stuck 状态。

**建议修复与验收：** 在 `_parse_task` 的持久化/预约前使用单一严格整数校验：拒绝 bool、字符串、零、负数、非有限数，设定合理上限；错误不得创建 attempt 或改变 task 状态。补充每个 timeout 字段的边界表和一个「无状态写入」回归测试。

## Go/No-Go

| 范围 | 结论 | 依据 |
| --- | --- | --- |
| Core 本地 run/review/merge | **No-Go** | P1-TURING-01 使默认 public CLI 绕过 review 并合并；P1-TURING-02 令常见失败不可恢复。 |
| Core 外部 evidence 续跑 | **No-Go** | P1-TURING-03 与文档/CLI 承诺相矛盾，QUESTION 后不能导入下一份内置生成的 evidence。 |
| 签名与基础 evidence 校验 | Conditional Go | 已有签名/完整性回归通过，但必须在以上状态机修复后重新跑端到端覆盖，不能以此取代 review 与 attempt lineage 绑定。 |

**关闭条件：** 先以回归测试关闭三个 P1，再重跑全套测试和 `init/setup/doctor/start/task/evidence/review/signoff/merge` 的干净单机 CLI smoke；有任一状态/证据契约变更时，同步更新 README、状态图和 machine-readable JSON 示例。

---

# Final Arbitration

仲裁者：Codex
时间：2026-07-31（Asia/Taipei）

## 1. 最终结论

源码层的 P0/P1 已完成修复并由独立代码复审复核；本轮没有重新引入 Docker 或外部 TypeScript semantic runtime。候选版本为 `0.5.2`，但**整体仍为发布 No-Go**：公开的 `dyro 0.5.1` 当前仍未 yank，且 PR 仍需独立批准。PR #14 在 `b68580a` 已完成 8 项必需 CI（Linux Python 3.11–3.14、Windows、Intel macOS、wheel/sdist 与 TypeScript）；macOS runner 已从不再列为标准 runner 的 `macos-13` 迁移至受支持的 `macos-15-intel`，保持 Intel 覆盖。2026-07-31 已实际启用 `main` 和 PyPI Environment 的远端治理；剩余发布处置必须在 PyPI 项目控制台完成，不能由本地代码替代。

已关闭的源码问题包括：任务/Provider 输出/异常的机密检测和脱敏；真实 Provider 的显式非物理隔离确认与只读上下文投影；`auto`/Panel 不再回退 echo；发现但未审计的 CLI 不可调度；公共状态命令不能跨越质量门；merge 重验 review/signoff；外部 QUESTION 续跑的同 run 单调 provenance；严格超时校验；tag 来源、锁文件和分发构建门禁。

## 2. 模块 Go/No-Go

| 模块 | 结论 | 依据 |
| --- | --- | --- |
| Core | Conditional Go | 公开状态旁路已封堵；merge 会重验当前 review/signoff 与 task HEAD；全量单测和 PR 目标平台 CI 已通过。 |
| Local dispatch | Conditional Go | 已 fail-closed、Provider 投影/显式确认/模拟标签均落地；尚未实际执行 Codex/Claude，Windows 仍只承诺 import/discovery fail-closed。 |
| 供应链与发布 | No-Go | 发布工作流与远端治理已收紧，8 项 PR CI 已通过；但 0.5.1 仍显示 `yanked=false`，且合并与 PyPI 发布均需要独立审批。 |
| 整体 | No-Go | 先 yank 0.5.1、取得独立 PR 批准并合并，再可把 0.5.2 标为可发布。 |

## 3. P0 Required Fixes

1. **已关闭（代码）：** 任务五段文本、上下文、Provider JSON、告警和 Provider 异常在持久化前统一经过机密守卫；命中时只保留通用脱敏错误。回归用例验证 token 不会写进 run JSON。
2. **发布前必须人工完成：** 在 PyPI 项目管理页 yank `0.5.1`，原因使用不含机密的“security fix available in 0.5.2”；保留 tag/Release 作为取证指针，并按 `docs/publishing.md` 通知精确版本 pin 用户升级。2026-07-31 的实时 PyPI JSON 显示该版本仍为 `yanked=false`。

## 4. P1 / P2

| 编号 | 处置 | 复证 |
| --- | --- | --- |
| CURIE-02 | 已关闭：dry-run 执行纯本地契约、上下文和 backend 预检，未知 backend 返回 2 且不创建状态。 | `CliTests.test_dry_run_validates_contract_and_known_backend_without_state_or_probe` |
| CURIE-03/04/05 | 已关闭：auto/Panel 无 echo 回退；route 只接受已认证集成 Provider；Cursor/OpenCode/Grok/Hermes/Kimi 为发现但不可调度；真实只读路径投影白名单并要求确认。 | registry/panel/route/投影回归 |
| TURING-01/02/03 | 已关闭：质量门私有化、merge 重验、异常任务转 failed、external continuation 同 run 单调递增。 | `tests.test_tasks`、`tests.test_provenance` |
| TURING-01 P2 | 已关闭：task/gate/review timeout 拒绝 bool、字符串、零、负数和超限值。 | timeout manifest 回归 |
| ATLAS-01/03/04/05 | 已关闭：验证 tag checkout/main ancestry、锁定 uv 构建、记录制品哈希、Beta classifier/released changelog、事故/yank runbook。 | `tests.test_release_source`、build、twine strict |
| ATLAS-02 | 已关闭：启用 main protection（PR、1 个批准、最新推送复审、讨论解决、管理员受约束、8 个 CI check）及 PyPI Environment 分离审批、禁 self-review/admin bypass、仅受保护分支。 | GitHub REST 更新回执：2026-07-31。 |

## 5. Requires Human Verification

1. PR #14 的 8 项 GitHub CI 已通过；在无新提交的前提下，取得独立审批后再合并，保护规则会强制复核最新推送与所有必需 check。
2. 在受控 macOS/Linux 主机分别执行一次真实 Codex 与 Claude 的只读最小任务；确认 `CODEX_HOME`/`CLAUDE_CONFIG_DIR` 等显式授权配置下的登录与 JSON 协议，并核对未列文件不在投影目录。不得以 echo 替代。
3. 维持 GitHub 远端治理：`main` 已要求 PR review/CI 且禁止直接 push/force push；PyPI Environment 已要求非发起人审批、禁止 self-review/admin bypass，并限制到受保护分支。
4. 完成 `0.5.1` PyPI yank 和事故记录；确认项目页的 yanked 标记后，再创建 `v0.5.2` Release。
5. 在 TestPyPI 先执行一次发布与 yank 通告演练；0.5.2 不能覆盖 0.5.1，必须走新 tag、完整 CI 与 Environment 审批。

已执行复证：`uv lock` / `uv sync --locked --all-extras --dev`、全量 `unittest discover`（退出码 0；受限沙箱中不可证明 process identity 的测试按设计 skip）、ruff、compileall、`python -m build`、`twine check --strict`，以及干净 CPython 3.13 环境对 wheel/sdist 的独立 `experiments.local_agent_dispatch` import/doctor smoke。独立代码审查未发现新增 P0/P1/P2。

最终签名：Codex
