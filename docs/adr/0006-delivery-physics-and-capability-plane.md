# ADR-0006：交付物理学、能力卡与宿主编译

- 状态：提案（2026-08-15）；权威投影锁定为 B；衰减语义锁定为 A1；可携带核验锁定为 B1
- 决策者：产品选择 B / A1 / B1 已写入本 ADR；其余条目仍待维护者确认合并
- 关联：
  - [交付物理学设计](../designs/delivery-physics.md)
  - [实施计划](../../plans/delivery-physics-implementation.md)
  - [架构](../architecture.md)
  - [ADR-0002 派发](0002-optional-local-agent-dispatch.md)
  - [ADR-0004 续航](0004-native-continuation-engine.md)
  - [ADR-0005 Console](0005-local-web-console.md)

## 背景

`0.6.0` 已经具备 TaskGraph、receipt-bound review、Objective 续航和只读 Console。行业同时出现三类热门形态：舰队 UI、提示词超市、工单驱动的隔离执行。

若把其中任何一类直接做进 Core，会重演「编排库 = 控制面」或「提示词仓库 = 产品」的错误，并让 Dyro 变成追随者。

真正的缺口不是「再启动一种 agent」，而是：

1. 已有证据物理学是隐式的，不能被第三方复验，也不能被编译到宿主；
2. adapter 只有 argv，没有隔离证明、证明边界和意图格；
3. 宿主 skill 若靠手写，会过期、会列出用户没有的后端、会暗示宿主自己 merge。

## 决策

1. Dyro 的产品身份锁定为 **本地优先的多仓交付物理引擎**，不是 agent、不是舰队、不是 skill 超市。
2. 抽出 **Proof Object** 作为已验证事实的统一投影。它不取代 `task.toml`、receipt、review 绑定或 Continuation journal。
3. 每个 Proof 带 **衰减函数**。substrate 变化后事实死亡；不确定不得写成通过。`decay(review_verdict)` 全量等于 `_valid_review_acceptance`；`decay(signoff)` 全量等于 `_valid_external_signoff`。`SchedulerSnapshot` 只把 merge 相关的 `live` Proof 投影进已有进展字段，不计入 trigger；journal 不把 proofs 当 PASS。生产 `BudgetUsage` 在 `0.7` 不因 Proof 新开 no-progress 耗尽。
4. 用 **Capability Card** 统一 agent / gate / reviewer / trigger / tool。`0.7` 仍只读 `[adapters.*]`；`0.8` 才运行时升级为 Card，缺省 `cannot_prove` 至少包含 `done` 与 `merge`。
5. 增加 **Host Compiler**：把定律与本机可用 Card 编译为宿主投影（`SKILL.md` 与可选拦截文件）。编译器只收缩权威，不扩大权威。
6. 所有 mutation 落入操作格 `observe | execute | review | sign | integrate | publish`。有效权威仍是策略 ∩ 合约 ∩ 租约 ∩ 任务权限 ∩ 图约束。
7. 议题跟踪器若接入，只能作为 Trigger provider，不能成为交付原子，也不能完成 Task。
8. **权威投影锁定为 B**：所有宿主必编译 skill / 规则；仅当宿主 Card 能证明拦截表面时，再投影由操作格编译的 deny hook。没有拦截表面不得拒绝 compile。Hook 不得宣传为 OS 隔离。详见设计第 8 节。
9. **`0.7` 衰减锁定为 A1**：对 merge / 下游释放的接受与拒绝，必须与 `0.6.0` 现有绑定检查同真值。Proof 只提供投影与 `PROOF_DECAYED` reason code，不是第二套门。`merge_task` / `check_dispatchable` 不读 Proof store。下游只投影 `_assert_dependency_integrated`；decayed review 不加严 ready set。任务仓 dirty：`0.6` 已拒绝，`0.7` 保持拒绝，不放松、不叠门。开发线 dirty / 错分支保持 `_prepare_merge` 现有错，不得标成 `PROOF_DECAYED`。不把 `git revert` 当成祖先断裂。
10. **`1.0` 可携带核验锁定为 B1**：`verify-bundle` 核验完整性，不核验身份，也不承诺与当前工作区 `proof verify` / `task merge` 同一套 `live` / `decayed`。输入是 Proof Bundle + 调用方提供的 git 对象。捆内不塞 git 对象库。缺 procedure、缺 substrate、缺 git 对象、或缺已声明的签名密钥 → `inconclusive`，不得写成 `live`。无 `--current-heads` 时不得报与 merge 相同的衰减结论。

## 否决项

- 把 dispatch、模型投票、视频、截图、Trigger 摘要升级为 Proof。
- 把 issue 跟踪器或看板当作 TaskGraph 的真源。
- 在 Core 内置角色技能包或提示词市场。
- 给 Console 或编译后的宿主 skill 以 merge / push / signoff 实权。
- 将 `cwd`、CLI 只读档或宿主拦截 hook 宣传为 OS 隔离。
- 因宿主缺少拦截表面而拒绝 `host compile`。
- 用命令名黑名单代替操作格来生成 deny hook。
- 未审计命令自动获得 `execute` intent。
- 在 Proof Bundle 中写入绝对路径、凭据、prompt、adapter 环境或 git 对象库。
- 把 `0.7` 衰减做成与现有 merge / 下游检查不同真值的第二套门。
- 把 `0.7` 写成「不拒绝任务仓 dirty」（那是放松 `0.6`，不是「不加严」）。
- 按 decayed review 加严下游 ready set，或让 `merge_task` / `check_dispatchable` 读取 Proof store。
- 把 `git revert` 宣传或实现成祖先断裂。
- 把 `verify-bundle` 写成身份证明，或在缺签名密钥时返回 `live`。
- 把 `verify-bundle` 的完整性结论写成与控制面当前 `live` / `decayed` 相同。
- 把 `task evidence` ZIP 或 git 对象库当作 Proof Bundle。
- 为对齐外部产品而复制其领域语言或角色剧场。
- 在仓库内保存「我们学了谁 / 对标谁」的对照附录。

## 后果

- 产品叙事从「启动 agent」转为「核验完成」。
- `0.7` 起增加 `dyro proof list/show/verify` 与衰减 reason code，不要求用户改 Task 清单。Console Proof 展示默认进 `0.8`。`export` 可在 `0.7` 以 experimental 提供；`verify-bundle` 硬门禁与 `schema_version = 1` 锁在 `1.0`。
- `0.8` 起 adapter 配置向 Card 迁移，旧 Profile 仍可加载。
- `0.9` 起宿主投影可重算、可 doctor；过期投影阻断自动 mutation。默认只写当前工作区；`--user` 才写用户级目录。`tools.json` / PATH 发现不是可执行 Card。
- `1.0` 的对外承诺是：陌生人拿着 Proof Bundle 和自己提供的 git 对象，能得到与源机**相同的完整性结论**（字节仍在、钉死 SHA 可解析）。这不是身份证明，也不是「现在工作区还能 merge」。
- 实施成本是新的投影层与兼容层，而不是第二套调度器。

## 兼容

- 无 Proof 命令的工作区行为与 `0.6.0` 相同。`0.7` 的 `task merge` 与下游释放对错不变；只多可查询的 Proof 与 `PROOF_DECAYED` 人话。任务仓 dirty 在 `0.6` 已拒绝，`0.7` 保持。
- 历史 review 仍按既有绑定字段核验；`proof list` / `verify` **每次**从现有文件重派生，不改写原件。缺绑定字段 → `inconclusive`，不伪造 `live`。store 可重建，不是展示真源。
- Proof `contract_hash` 按 subject 拆：task 面 kind 用 attempt `task_contract_sha256`（缺则空）；`action_receipt` 用 Objective `contract_sha256`。不把两个合约哈希混成一个字段。
- `v0.6.x` 只接受与本 ADR 不冲突的维护修复，不提前合并 Host Compiler。
