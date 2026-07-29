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
| 严格影子目录 | 物理上仅见白名单文件（可选，敏感仓默认开） |
| 结果 locator 核验 | 文件存在、行号不越界 → `verified`；不删条目 |
| 进程身份租约 | `pid + 启动时刻` 防 PID 复用误回收 |
| 双层并发槽 | 每后端 + 全局；init grace + 心跳 |
| 异步默认 | 派发立即返回 `run_id`；禁止无理由同步空等 |
| 回收纪律 | 禁止把完整事件流灌回宿主上下文 |
| 动态宿主 skill | 只渲染本机可用后端与用户路由偏好 |
| 与 Dyro 边界 | 永不 merge/push/signoff；与 ADR-0001 实验并列 |

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
| `strict` | true 时启用影子目录（见 §7） |
| `mode` | `read-only` \| `edit` |

校验失败（空 objective、files 零匹配、strict+edit 冲突等）→ **派发前** fail-closed。

## 5. 结果契约（ResultEnvelope）

```json
{
  "schema_version": 1,
  "run_id": "…",
  "status": "ok",
  "summary": "…",
  "confidence": "high|medium|low",
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
- 与 ADR-0001 evidence pack 的 hash 密封可组合（pack 内再封一层）

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

- 私钥块、`sk-…`、`ghp_…`、AWS `AKIA…` 等特征 → 拒绝该文件  

### 7.3 相对仅「文件名黑名单」的增强

- 路径 + 内容双检  
- 决策结构化：`{allowed, reason}` 可审计  
- 与 `strict` 影子物化共用同一守卫（先守卫再写入影子）

实现见 `experiments/local_agent_dispatch/context_guard.py`。

## 8. 严格影子目录（StrictShadow）

`strict: true` 且 `mode: read-only`：

1. 展开 files glob → 守卫 → 物化到 `~/.dyro/local-agent-dispatch/shadow/<run_id>/`  
2. 保持相对路径结构  
3. worker cwd = 影子根  
4. 被调进程 **物理上** 看不到白名单外文件  

说明：后端 CLI 自带的「plan/read-only 权限档」仅是行为约束；**严格模式是硬隔离补强**。  
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
| 租约 | `pid` + `process_started_at`；存活用身份匹配，不只看 TTL |
| init grace | 槽位新建后短窗口内禁止判死 |
| 僵死回收 | rename 抢占 + 删除；失败则下轮重试 |
| GC | 超龄 run、影子目录、不活跃 thread 可回收 |

相对「仅 TTL 锁」：增加 **进程启动时刻** 防 PID 复用；相对「无 grace」：避免 init 竞态误杀。

## 10. Edit 模式：worktree + patch

1. 创建隔离 git worktree（含未提交变更同步策略：以当前 index/worktree 快照为准，设计实现时显式记录）  
2. 后端仅在该 worktree 写  
3. 产出 `git diff` → `patch_ref` 落盘  
4. **默认不** 写回主工作区、不 commit、不 push  

主 agent / 人择优 apply。

## 11. 回收纪律（宿主 skill 强制）

1. 可外包任务：**先派后干再收**  
2. 非用户要求，禁止派完立即无限 wait  
3. 只把 `summary` / `evidence`（可截断）/ `patch_ref` / `warnings` 带回对话  
4. **禁止** 读取完整 event 日志灌入宿主上下文  
5. 长任务超时：优先 followup/续聊，禁止无脑重复全量派发  

## 12. 动态宿主 skill

安装/刷新时：

1. 探测本机后端命令与登录态  
2. 读取用户路由偏好  
3. 渲染 skill 正文：仅含可用后端、默认模型、路由表  
4. 分发到各宿主 skills 目录  

禁止静态 skill 列出用户没有的后端。

## 13. 与 ADR-0001 外部语义运行时的组合

| 场景 | 用哪条路径 |
| --- | --- |
| 任务内固定 TS 语义流 + Docker 隔离 | ADR-0001 Stage0–5 |
| 开发者要第二意见 / 大调研 / patch 竞赛 | ADR-0002 本设计 |
| 高风险设计评审 | 本设计 panel + 对抗评审板 |
| 生产 evidence / merge | 仅 Dyro 控制面；两侧 harness 皆不可越权 |

Stage5 dry-run 的 pack 核验与本设计的 locator 核验可共享库思想，但 **状态目录与生命周期分离**。

## 14. 分阶段实现

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| **L0** | 纪律文档 + ADR + 本设计 + `TaskContract`/`ContextGuard`/`LocatorVerify` | 单测绿 |
| **L1** | `RunStore` + 双层槽位租约 + strict 影子接入 | 单测绿 |
| **L2** | `echo`/`codex`/`claude` 适配器 + CLI `run`/`result` | 单测（echo）+ 本机可选真 CLI |
| **L3** | `panel`、skill 渲染、routes、`gc` | 单测绿 |
| **L4** | `stage5-bridge` dry-run（不替代 Core import） | 模块导入 + `dyro dispatch` |

## 15. 测试矩阵（L0）

- 守卫：逃逸路径、`.env`、私钥内容、合法源文件  
- 契约：缺字段、空 files、strict+edit  
- locator：越界行号、缺文件、合法区间、路径逃逸  

## 16. 安全声明

本设计提升的是 **本机开发协作** 的可控性，不声称：

- 容器逃逸免疫  
- 恶意后端 CLI 完全不可信环境下的机密安全  
- 可替代生产 evidence 链  

生产门槛仍以 Stage5 `PRODUCTION_NOT_READY` 与 ADR-0001 为准。
