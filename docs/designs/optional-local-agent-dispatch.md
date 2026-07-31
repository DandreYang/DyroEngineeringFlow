# 可选本地 Agent 派发 — 完整设计（first-party）

状态：设计已接受（ADR-0002）；**L0–L4 实现于 `experiments/local_agent_dispatch/`**  
范围：开发者侧 harness；**非** Dyro Core 交付控制；**随 `dyro` wheel 安装**（`dyro dispatch`）  
日期：2026-07-30（分发面修订同日）

## 1. 目标

在任意宿主 Agent 中，将只读调研 / 评审 / 隔离改码任务 **异步派发** 到本机已授权的后端 CLI，并回收：

- 结构化 `summary`
- 带 **locator 核验标记** 的 `evidence[]`
- 可选 `patch` 路径（edit 模式）
- 可选 `takeover` 续聊句柄（同后端原生 session，若后端支持）

相对常见「口头切换模型 / 同对话堆上下文」方案，本设计额外强制：

| 强化点 | 说明 |
| --- | --- |
| 零知识任务契约 | 被调方看不到宿主对话；上下文必须自包含 |
| 注入前机密守卫 | 路径 + 内容特征双检，fail-closed 剔除 |
| 严格影子目录 | 仅向通过严格隔离能力门禁的 adapter 提供白名单快照；普通 CLI 不因改变 `cwd` 自动升级为物理隔离 |
| 结果 locator 核验 | 文件存在、行号不越界 → `verified`；不删条目 |
| 进程身份租约 | `pid + 启动时刻` 防 PID 复用误回收 |
| 双层并发槽 | 每后端 + 全局；init grace + 心跳 |
| 异步默认 | 派发立即返回 `run_id`；禁止无理由同步空等 |
| 回收纪律 | 禁止把完整事件流灌回宿主上下文 |
| 动态宿主 skill | 只渲染本机可用后端与用户路由偏好 |
| 与 Dyro 边界 | 永不 merge/push/signoff；不替代 Core 交付控制 |

## 2. 非目标

- 不实现生产多租户隔离证明  
- 不替代 TaskGraph / claim / gates  
- 不要求用户注册第三方账号（仅使用本机已登录 CLI）  
- 不在首期绑定具体商业模型品牌名到协议层（适配器用中性 id：`backend_a`… 或探测到的命令名）

## 3. 架构

```text
宿主 Agent
   │  写 TaskContract JSON（五段式 + files + mode）
   ▼
DispatchSupervisor（本机，可移除）
   │  校验契约 / 守卫 files / 取槽位租约
   │  立即返回 run_id
   ▼
DetachedWorker
   │  组装上下文（白名单 ± 影子目录）
   │  调用 BackendAdapter（headless CLI）
   │  核验 evidence locators
   │  落盘 ResultEnvelope
   ▼
宿主 Agent
   │  result --wait（显式需要时）
   │  仅取 summary / evidence / patch_ref
   ▼
（可选）对抗评审板落盘 → 人/主 agent 仲裁
```

状态根目录建议：`~/.dyro/local-agent-dispatch/`（可配置），与 Dyro 任务状态机隔离。

## 4. 五段式任务契约（TaskContract）

```json
{
  "schema_version": 1,
  "backend": "auto",
  "mode": "read-only",
  "strict": false,
  "allow_unconfined_provider": false,
  "allow_offline_simulation": false,
  "files": ["src/**/*.py", "!**/*_test.py"],
  "task": {
    "briefing": "项目是什么、构建/测试入口",
    "locations": "相关目录与模块地图",
    "objective": "要回答/完成的问题；错误原文全文粘贴",
    "constraints": "只读/禁止改某路径/截止时间",
    "output_contract": "必须返回的 JSON 字段与 evidence 形状"
  }
}
```

| 字段 | 规则 |
| --- | --- |
| `briefing` | 项目身份与工具链；禁止依赖「你知道的上下文」 |
| `locations` | 地图，不是文件倾倒 |
| `objective` | 含 verbatim 报错/日志 |
| `constraints` | 含只读/禁止网络/禁止碰密钥路径 |
| `output_contract` | 与 `ResultEnvelope` schema 对齐 |
| `files` | 最小充分集；禁止默认 `**/*` |
| `strict` | true 时启用影子目录并要求 adapter 严格隔离能力（见 §8） |
| `mode` | `read-only` \| `edit` |
| `allow_unconfined_provider` | 真实 Provider 的非 strict 执行必须显式为 `true`；read-only 只投影白名单上下文，仍不构成 OS 隔离 |
| `allow_offline_simulation` | 仅允许显式 `echo` 测试模拟；`auto` 永不回退到它 |

校验失败（空 objective、files 零匹配、strict+edit 冲突等）→ **派发前** fail-closed。

## 5. 结果契约（ResultEnvelope）

```json
{
  "schema_version": 1,
  "run_id": "…",
  "status": "ok",
  "summary": "…",
  "confidence": "high|medium|low",
  "execution_kind": "provider|offline-simulation",
  "isolation": "strict|context-projection|best-effort-unconfined|not-applicable",
  "evidence": [
    {
      "file": "src/foo.py",
      "lines": "10-20",
      "claim": "…",
      "verified": true,
      "verify_note": ""
    }
  ],
  "patch_ref": null,
  "takeover": null,
  "usage": { "duration_ms": 0 },
  "warnings": []
}
```

### 5.1 Locator 核验（相对常见「只展示引用」的增强）

对每条 evidence：

1. `file` 解析到 run 的工作根内（防路径逃逸）  
2. 文件可读  
3. `lines` 形如 `N` 或 `N-M`，且 `1 ≤ N ≤ M ≤ 总行数`  
4. 通过 → `verified: true`；否则 `verified: false` + `verify_note`  

**不删除**未通过条目（可能是常识路径或生成代码）；由宿主据 `verified` 决定信任。

相对仅「展示路径」的实现，本设计额外要求：

- 核验在 worker 侧自动执行，宿主默认可见  
- dry-run / 报告可统计 `verified_ratio`  
- 与 Core evidence pack 的 hash 密封可组合（pack 内再封一层）

## 6. 对抗评审记录协议

当 `panel`（多后端并行同一任务）或用户要求交叉评审时：

1. 各后端独立 `run`，互不可见  
2. 宿主将各方 summary/evidence 写入共享板 **各自签名区**  
3. 终裁区输出共识、分歧、P0/P1/P2、Go/No-Go  
4. 执行 agent 只读终裁，禁止改他人区  

板文件建议：`docs/reviews/YYYY-MM-DD-<topic>-adversarial-board.md`  
纪律全文见 [agent-orchestration-discipline.md](../agent-orchestration-discipline.md)。

## 7. 上下文机密守卫（ContextGuard）

注入任何文件前必须：

### 7.1 路径级

- realpath 必须落在项目根（或影子根）内  
- 拒绝 symlink 逃逸  
- 拒绝已知凭据 basename / 前缀 / 扩展名（`.env`、`id_rsa`、`.pem`、`.aws` 等）  
- 拒绝敏感目录段（`.ssh`、`.kube`、`.gnupg`…）  

### 7.2 内容级

- 私钥块、`sk-…` / `sk-proj-…`、`ghp_…` / `github_pat_…`、AWS `AKIA…` 等特征 → 拒绝该文件或任务字段。
- 五段式任务文本、读取到的上下文、Provider JSON 的 `summary` / evidence / warnings 都经过同一守卫；解析失败或命中机密时只保存脱敏错误，绝不保存原始 stdout/stderr。

### 7.3 相对仅「文件名黑名单」的增强

- 路径 + 内容双检  
- 决策结构化：`{allowed, reason}` 可审计  
- 与 `strict` 影子物化共用同一守卫（先守卫再写入影子）

实现见 `experiments/local_agent_dispatch/context_guard.py`。

## 8. 严格影子目录（StrictShadow）

`strict: true` 且 `mode: read-only`：

1. 展开 files glob → 以 no-follow 文件描述符完成路径、大小、内容守卫 → 物化到 `~/.dyro/local-agent-dispatch/shadow/<run_id>/`
2. 保持相对路径结构  
3. worker cwd = 影子根  
4. adapter 还必须声明并实现 strict isolation；否则 Supervisor 在派发前拒绝

说明：后端 CLI 自带的「plan/read-only/禁用工具」权限档与 shadow `cwd` 都不是 OS 隔离证明。当前外部 Codex / Claude adapter 均标记为不支持 strict；任何 `strict` 真实 Provider 任务都会在派发前明确拒绝。`echo` 只可作为显式、已确认的离线模拟，不能充当 `auto` 或 Panel 的兜底。
`edit` 模式使用 git worktree（§10），不与 strict 影子混用。

## 9. 异步生命周期与并发

```text
accepted → running → completed|failed|timeout
              ↑
         lease heartbeat
```

| 机制 | 设计 |
| --- | --- |
| 派发 | 校验通过后立刻返回 `run_id`；worker detached |
| 收取 | `result(run_id, wait=bool, timeout=…)`；可多 id 等待 |
| 槽位 | `mkdir` 原子锁；scope=`backend_id` 与 `global` 双层 |
| 租约 | `pid` + `process_started_at` + 随机 owner token；续租/释放必须匹配所有权 |
| init grace | 槽位新建后短窗口内禁止判死 |
| 僵死回收 | rename 抢占 + 删除；失败则下轮重试 |
| GC | 超龄 run、影子目录、不活跃 thread 可回收 |

worker 记录会持久化 `pid + process_started_at + owner token`。短命的派发 CLI
退出后，新 Supervisor、`result --wait` 与 GC 都会重建监控：只有操作系统证明该
进程已消失、成为 zombie 或 PID 代际不符时，才以 token-CAS 将遗留 `running`
转为 `failed`。`ps` 不可用但 PID 仍存活时保持 fail-safe，不误杀运行中的任务。

相对「仅 TTL 锁」：增加 **进程启动时刻** 防 PID 复用；相对「无 grace」：避免 init 竞态误杀。

当前 `run` / `panel` / worker 的进程树监管限定 POSIX（Linux/macOS）；Windows
允许导入与只读 discovery，但执行会 fail-closed，直到提供并验证 Windows 原生
process-tree 与 pipe 后端。

## 10. Edit 模式：worktree + patch

1. 从当前 Git `HEAD` 创建 detached 隔离 worktree；源工作区未提交变更不会隐式复制
2. 后端仅在该 worktree 写  
3. 产出有界 `git diff --binary` → 带 SHA-256 的 `patch_ref` 落盘
4. **默认不** 写回主工作区、不 commit、不 push  

主 agent / 人择优 apply。

## 11. 回收纪律（宿主 skill 强制）

1. 可外包任务：**先派后干再收**  
2. 非用户要求，禁止派完立即无限 wait  
3. 只把 `summary` / `evidence`（可截断）/ `patch_ref` / `warnings` 带回对话  
4. **禁止** 读取完整 event 日志灌入宿主上下文  
5. 长任务超时：优先 followup/续聊，禁止无脑重复全量派发  

## 12. Provider 发现、选择与动态宿主 skill

安装/刷新时：

1. `dyro dispatch backends` 探测本机命令与登录态；当前受审计、可执行的集成为 `codex` 和 `claude`。
2. `cursor-agent`、`opencode`、`grok`、`hermes`、`kimi` 会被展示为“已发现但未集成”，不得被自动或手动路由，直到各自拥有经过审计的非交互协议 adapter。
3. 当只有一个已认证 Provider 时，`backend: auto` 可以选择它；当有多个时，用户必须使用 `dyro dispatch route add default <provider>` 选择默认路由；一个也没有时 fail-closed 并给出发现结果。
4. `echo` 仅用于显式 `--backend echo --allow-offline-simulation` 的确定性测试，输出的 `execution_kind=offline-simulation`、低置信度和非生产警告不得被上游当作真实模型结论。
5. 渲染 skill 正文只含已准备 Provider、发现但未集成的命令、路由表与上述限制，再分发到各宿主 skills 目录。

禁止静态 skill 列出用户没有的后端。

## 13. 使用边界

| 场景 | 用哪条路径 |
| --- | --- |
| 开发者要第二意见 / 大调研 / patch 竞赛 | ADR-0002 本设计 |
| 高风险设计评审 | 本设计 panel + 对抗评审板 |
| 生产 evidence / merge | 仅 Dyro 控制面；派发 harness 不可越权 |

## 14. 分阶段实现

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| **L0** | 纪律文档 + ADR + 本设计 + `TaskContract`/`ContextGuard`/`LocatorVerify` | 单测绿 |
| **L1** | `RunStore` + 双层槽位租约 + strict 影子接入 | 单测绿 |
| **L2** | `echo`/`codex`/`claude` 适配器 + CLI `run`/`result` | 单测（echo）+ 本机可选真 CLI |
| **L3** | `panel`、skill 渲染、routes、`gc` | 单测绿 |

## 15. 测试矩阵（L0）

- 守卫：逃逸路径、`.env`、私钥内容、合法源文件  
- 契约：缺字段、空 files、strict+edit  
- locator：越界行号、缺文件、合法区间、路径逃逸  

## 16. 安全声明

本设计提升的是 **本机开发协作** 的可控性，不声称：

- 容器逃逸免疫  
- 恶意后端 CLI 完全不可信环境下的机密安全  
- 可替代生产 evidence 链  

真实 Provider 的 read-only 路径只得到经过守卫的白名单上下文投影；它不是物理隔离，调用者必须显式确认该风险。编辑路径是隔离 Git worktree，但同样不等同于对恶意 CLI 的 OS 沙箱。

派发结果始终是建议性产物，不能构成生产放行依据。
