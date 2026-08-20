# Dyro Console 实时家族与信号设计

状态：提案
目标版本：`0.7.x`；扩展既有 Console，不另开 `0.8.0` / `0.9.0` / `1.0.0` 功能号
适用范围：工作区 overlay 事件、一层开发线家族、家族频道与人类可见信号；不改写 TaskGraph、Objective、merge、push 或 ADR 0005 的只读边界

本文件**扩展**而不替换：

- [本地 Web 控制台](local-web-console.md)
- [ADR 0005](../adr/0005-local-web-console.md)

既有 command-center、C01–C06 读取契约、loopback bootstrap / bearer、以及「浏览器不能交付」仍然有效。本设计只补上实时事件、一层家族图和 overlay 信号；除第 9 节列出的唯一 POST 外，Console 仍没有交付 mutation API。

---

## 1. 痛点

当前 Console 是**只读快照窗口**，不是开发线旁边的视觉孪生。已落地表面只有：

| 表面 | 现状 |
| --- | --- |
| session / meta / overview / workspace / proofs / system | 有 |
| 5 秒轮询 + ETag | 有 |
| line DTO 的 `parent` | 无。`line list` JSON 已有 `parent`，Console `LineSummary` 没有投影 |
| dispatch / 会审 / ledger | 无。页面看不到谁在跑、会审是否落下、ledger 最近一行 |
| 邮箱 / 家族频道 | 无。线与线、线与人之间的话只存在于各 harness 会话里 |

操作者要的不是第二套 IDE，而是把 harness CLI 已经能做的事摊在一个页面上：

- **计划**、**里程碑**、**阶段**、**任务**的当前态；
- 复核、图像、视频、图表等 overlay 产物；
- **看得见的跨线对话**，而不是各开一个聊天窗口互相看不见。

操作者口语里的计划 / 里程碑 / 阶段 / 任务 / 谁在跑，必须映射到已有 Dyro 对象，不得另造 backlog：

| 操作者说法 | Console 投影 | 权威来源 |
| --- | --- | --- |
| 计划 | Objective 波次泳道 | `objective_wave` 事件 + 既有 Objective 快照 |
| 里程碑 | Objective 派生结果 | `incomplete \| complete \| repair_required` |
| 阶段 | Task 状态 | 既有 Task 状态机 |
| 任务 | 既有 Task 详情 | 既有 workspace / Task DTO |
| 谁在跑 | dispatch / executor | `dispatch` 事件 + Task `executor` |

页面仍不展示 prompt、路径、argv、环境变量或未经验证的 provider 输出。

---

## 2. 原则与不变量

必须始终成立：

1. **`.dyro` + git 仍是唯一事实源。** Console 不保存 Task、Objective、attention、「完成」或聊天副本。事件与频道是 overlay 追加日志，不是第二份交付图。
2. **启动与认证不变。** 仍只绑定 `127.0.0.1`，仍用一次性 fragment bootstrap 换发 tab 内 bearer。不提供 `--host`，不用 cookie。
3. **人必须看见线信号。** 家族频道对 `operator` 没有隐藏私信。线发给线的 `--to` 对父线与人类始终可见。
4. **父线是唯一 merge / sync 点。** `line merge` 只把子线合入其直接父线；`line sync` 只把该父线合入该子线。页面不得暗示合入 git `main`、release 或 PyPI。
5. **家族只有一层。** 对父线 `P`：

   ```text
   F(P) = {P} ∪ children(P) ∪ {operator}
   ```

   - 表亲同属这个家族组。`core_pay` 与另一条子线都在 `F(core)` 里，彼此看得见广播。
   - 孙线在**子线自己的**家族里。`core_pay_fix` 的 `parent` 是 `core_pay`，因此属于 `F(core_pay)`，不属于 `F(core)`。
   - 节点只画一层：父、直接子、操作者。不画孙线，不把表亲提升成第二层树。
6. **聊天不是交付门。** `shipped`、`decision`、`contract` 或任意频道行都不能代替 review、sign-off、gate、merge 或 push。信号只解释意图，不改变 Task / Objective / git。
7. **`/dyro-line-family` 不发帖。** 该斜杠仍只预检 `spawn` / `merge` / `sync`，打印一条给人亲自跑的 `--yes` 命令。它不调用 `line post`，不 ack，不写频道。

示例（只使用通用线 id）：

```text
core
├─ core_pay          F(core) 的直接子线
│  └─ core_pay_fix   属于 F(core_pay)，不出现在 F(core) 的树上
└─ <另一条子线>       与 core_pay 是表亲，同属 F(core)
operator             每个家族组的固定成员
```

---

## 3. 信息架构

不新增一级页面，不新增全局菜单。既有 command-center（全局概览、现在需要你、推荐命令、工作区列表、本机状态）保持原位。

三个新窗格只出现在**已打开的工作区详情**里，接在现有关注 / 线 / 任务 / 目标清单之后：

| 窗格 | 首要问题 | 默认内容 |
| --- | --- | --- |
| 家族树 | 这条线和谁是一层亲属？ | 以 `parent` 投影的一层图；节点徽章；可复制的 dry-run |
| 实时事件流 | 刚才发生了什么？ | `.dyro/events.jsonl` 白名单行；SSE，不可见时暂停 |
| 家族频道 | 线与人刚刚说了什么？ | `F(P)` 的 `channel.jsonl`；列表 / 时间线 / 过滤 |

宽屏三个窗格同时可见，顺序固定为树 → 事件 → 频道，不提供拖拽改序或自定义布局。窄屏（小于 768 px）改为三个 tab，标签固定为「家族」「事件」「频道」，默认「家族」。tab 只切换可见性，不改变 hash 以外的状态。

hash 只保存安全 alias 与当前 tab：`#w/<alias>`、`#w/<alias>/family`、`#w/<alias>/events`、`#w/<alias>/channel`。不把 bearer、路径或消息正文写入 URL。

详情里已有的线 / 任务 / 目标清单、独立 Proof inspect 和关闭按钮不变。点击任务仍进入既有 Task 摘要，不新开「任务工作室」。产物栏是频道 / 事件上的 overlay，不是第四个一级 tab；P3 才渲染，P1 / P2 只保留占位文案「产物尚未开放」。

---

## 4. 实时事件

### 4.1 存储

工作区 overlay 增加一份与 Objective 事件**分开**的日志：

```text
.dyro/events.jsonl
```

它不是 `.dyro/objectives/<id>/events.jsonl`，也不是 `.dyro/ledger.jsonl`。ledger 仍是交付审计；本文件只投影人类可看的直播行。一行一种 `kind`，追加写入，损坏或截断 fail-closed，不得为了「看起来连续」而补造中间行。

允许的 `kind`：

| kind | 何时写入 | 页面怎么画 |
| --- | --- | --- |
| `spawn` | `line spawn` 成功 | 树上出现子节点 |
| `merge` | `line merge` 成功 | 子 → 父边短暂点亮 |
| `sync` | `line sync` 成功 | 父 → 子边短暂点亮 |
| `task_status` | Task 状态迁移 | 阶段列更新；点进既有 Task 详情 |
| `objective_wave` | Objective 波次预览或落地 | 计划泳道 |
| `dispatch` | 已审计 dispatch 开始 / 结束 | 谁在跑 |
| `board` | 会审记录落下 | 事件行；不展示正文 |
| `signal` | 家族频道追加一行 | 频道与事件流各一条 |
| `host_seed` | `dyro host seed` 写入 overlay | 事件行；不列文件路径 |

每行最小字段：

```json
{
  "seq": 12,
  "id": "evt_12",
  "kind": "spawn",
  "at": "2026-08-20T12:00:00Z",
  "actor": "core",
  "subject": "core_pay",
  "family": "core",
  "facts": {
    "parent": "core",
    "child": "core_pay"
  }
}
```

`facts` 只含安全 ID、枚举状态、计数、短哈希和稳定 reason code。禁止 prompt、answer、handoff、argv、绝对路径、remote URL、环境变量、完整日志。未知 `kind` 只显示时间、`id` 和 `EVENT_REDACTED`。

### 4.2 传输

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v1/workspaces/{alias}/events?after=<cursor>` | 游标分页的白名单事件。默认 50、最大 100 |
| `GET /api/v1/workspaces/{alias}/events/stream?after=<cursor>` | 同一白名单的 SSE。`text/event-stream`，每条 `data` 是一条事件 JSON；注释心跳保持连接 |

- `after=` 是带完整性校验的 opaque cursor，语义是「该 seq 之后」。截断、替换或篡改返回 `EVENT_CURSOR_INVALID`，客户端从空 cursor 重读，不猜测偏移。
- SSE 与 JSON 都需要 bearer，遵守既有 Host / Origin / Fetch Metadata / 无 CORS 规则。
- 页面 `document.hidden` 时关闭 EventSource，并停止 5 秒回退轮询；可见后用当前 cursor 先做一次条件 GET，再重开 SSE。
- SSE 不可用、被代理断开或 405 时，回退到既有 5 秒条件轮询，轮询同一个 `GET .../events`。不引入 WebSocket。
- overview 的 5 秒轮询不拉事件流。只有打开的工作区详情订阅该 workspace。

### 4.3 映射，不新造对象

页面把事件排进既有列，不增加「计划」「里程碑」「阶段」菜单：

- `objective_wave` → 计划泳道。一列一个当前波次，格子是该波 Task id。
- Objective `derived_result` → 里程碑徽章，挂在泳道标题，不单独开里程碑页。
- `task_status` → 阶段。状态枚举仍是 backlog / assigned / in_progress / waiting_answer / review / review_pending_signoff / done / failed。
- 任务标题 → 既有详情。点击只打开已经设计过的 Task 摘要。
- `dispatch` + Task `executor` → 「谁在跑」。只显示安全 executor id 与 `running | idle | unknown`。

---

## 5. 家族图

### 5.1 投影

`LineSummary` 增加一个可选字段 `parent`：空字符串或安全线 id。来源是 line manifest 的 `parent`，与 `line list --format json` 同值。Console 不得自己猜父子，不得把 `base` Git ref 当成父线。

`GET /api/v1/workspaces/{alias}/families` 返回该 workspace 里每个一层家族的轻量卡：`parent`、直接子线、未读数、dirty / missing-origin / in-progress 计数。

`GET /api/v1/workspaces/{alias}/families/{parent}` 返回 `F(parent)` 的图：

```json
{
  "parent": "core",
  "members": ["core", "core_pay", "operator"],
  "nodes": [
    {
      "id": "core",
      "role": "parent",
      "dirty": false,
      "missing_origin": false,
      "in_progress": false,
      "unread": 0
    }
  ],
  "edges": [
    {"from": "core", "to": "core_pay", "kind": "parent"}
  ]
}
```

- 节点徽章：`dirty`、`missing-origin`、`in-progress`、`unread`。四者同时具备图标、文字和含义，不靠颜色单独表达。
- `operator` 是固定节点，没有 git 徽章，只显示未读。
- 边在对应 `merge` / `sync` 事件进入当前事件窗口时点亮，随后随该行滚出窗口而熄灭。页面不自己推导「应该合并」。
- 孙线 `core_pay_fix` 出现在 `families/core_pay`，不出现在 `families/core`。

### 5.2 页面动作

图上只提供**复制 dry-run**。复制内容必须带 `--workspace` 与 `--dry-run`，且不得带 `--yes` 或 `--push`：

```text
dyro --workspace example --dry-run line spawn core core_pay
dyro --workspace example --dry-run line merge core_pay --into core
dyro --workspace example --dry-run line sync core_pay
```

浏览器不得执行 spawn / merge / sync，不得弹出「确认后执行」，不得把 `--yes` 写进剪贴板。真正的 `--yes` 仍只存在于终端，或由 `/dyro-line-family` 预检后打印给人亲自跑。

---

## 6. 产物栏

P3 才实现。P1 / P2 的频道行若带 `artifact` kind，只显示「产物尚未开放」和安全 id，不取文件。

权威位置是 overlay，不是产品 git worktree，也不是 Proof store：

```text
.dyro/families/<parent>/artifacts/<artifact-id>
.dyro/families/<parent>/artifacts.jsonl
```

允许展示的类型：复核摘要卡、图像、图表。视频只出卡片，不出嵌入播放器。

| 类型 | 页面 | 传输 |
| --- | --- | --- |
| 复核 | 标题、结论枚举、绑定哈希前 12 位 | JSON DTO |
| 图像 | `<img>` | 先用 bearer `fetch` 同源字节，再赋 `blob:` URL。`img-src 'self'` 与 `blob:`；不把 bearer 放进 query，不设 cookie |
| 图表 | 与图像相同，或纯 JSON 点列由本页画 SVG | 同源；无第三方图表库 |
| 视频 | 卡片：标题、时长或大小、一条可复制的只读打开命令 | 不返回媒体流，不使用 `<video>`，不增加 `media-src` |

`GET /api/v1/workspaces/{alias}/families/{parent}/artifacts/{id}` 只读。图像响应有大小上限，校验 overlay 路径，拒绝 symlink 与 `..`。页面不扫描 `outputs/images/`，不扫描 harness home，不把 sidecar 生成目录当成家族产物。

产物引用可以出现在 `artifact` 频道行的 `facts.artifact_id` 里。引用不是核验；衰减与 merge 仍走既有 Proof / Task 路径。

---

## 7. 家族组与频道

### 7.1 成员与可见性

`F(P) = {P} ∪ children(P) ∪ {operator}`。

- 默认帖进入家族广播，`to` 为空，组内全员可见。
- `--to <id>` 必须落在同一个 `F(P)` 内，否则 CLI 与 API 都拒绝。
- 定向帖对发送者、接收者、**父线**和 **operator** 可见。表亲看不到别人的定向帖。
- 父线与人类始终能看见该家族里的定向帖。人类控制台没有「仅线可见」的隐藏私信。
- `core_pay` 写给 `core_pay_fix` 的帖属于 `F(core_pay)`，`core` 在自己的家族频道里看不到它；人要看孙线对话，打开 `core_pay` 那一家族。

### 7.2 频道 kind

| kind | 含义 | 谁可以写 |
| --- | --- | --- |
| `contract` | 约定或范围声明 | 线 CLI；人类 POST |
| `blocked` | 阻塞 | 线 CLI |
| `shipped` | 声称已交付（不是 merge 证据） | 线 CLI |
| `ask_sync` | 请求从父线同步 | 线 CLI |
| `decision` | 人类决定 | 人类 POST；线 CLI 也可记 |
| `artifact` | 指向 overlay 产物 | 线 CLI |
| `retract` | 撤回先前列，原行仍在 | 线 CLI |

`ack` 不是频道 kind。它是收件状态，写在旁路索引里，并追加一条 `signal` 事件。

### 7.3 存储

```text
.dyro/families/<parent>/channel.jsonl
.dyro/families/<parent>/acks.json
```

追加频道行时必须同时追加 `.dyro/events.jsonl` 的 `signal` 行，`facts.channel_id` 指向频道行 id。两处写入放在同一 overlay 锁下；只写成一边则 fail-closed，恢复时重放或进入 repair，不得假装已广播。

频道行：

```json
{
  "id": "msg_9",
  "seq": 9,
  "at": "2026-08-20T12:01:00Z",
  "family": "core",
  "from": "core_pay",
  "to": "",
  "kind": "ask_sync",
  "body": "请把 core 同步进 core_pay",
  "retracts": ""
}
```

`body` 受长度和控制字符净化，默认拒绝路径与凭据模式。`retract` 行的 `retracts` 指向被撤回 id；原行不删除，页面标成已撤回。

### 7.4 CLI

不新增顶级命令。三条子命令挂在既有 `dyro line` 下：

```text
dyro --workspace example line post <from> --kind ask_sync [--to <id>] [--body TEXT]
dyro --workspace example line inbox [--family core] [--unacked]
dyro --workspace example line ack <id>
```

- `post` 的 `<from>` 必须是该家族里的线 id。人类身份用保留 id `operator`，且 kind 只能是 `decision` 或 `contract`（ack 走 `line ack`）。
- 省略 `--to` 即广播。`--to` 只接受 `F(family)` 成员。
- `inbox` 默认看当前线或 `--family`。`--unacked` 只列出操作者尚未 ack 的行。
- `ack` 只标记人类已读，不改变 Task / git，不视为同意 merge。
- 三条命令都支持 `--dry-run` 与 `--format json`。`post` / `ack` 的真正写入仍要现有确认纪律；没有静默成功。
- `/dyro-line-family` 的允许列表不增加 `line post` / `inbox` / `ack`。

### 7.5 `next` 与控制面

`dyro next` 与 `dyro-control-plane` **读取**未 ack 信号，不代写。

- `next --format json` 增加只读字段 `family_unacked`：`count`、最高优先级 `kind`、`family`、一条安全摘要。`count > 0` 时事项里出现「家族频道有未读信号」；`next.commands` 仍为空或只含既有只读 / 修复命令，不得出现 `line post` 或 `line ack --yes`。
- 控制面座位可运行 `line inbox --unacked --format json`。Observed 里原样报告未读；User action 不得发明 ack / post / merge。
- 未读信号不是 `repair_required`，也不阻塞 `line spawn` 预检。

---

## 8. 人类控制台模块

工作区详情的「频道」窗格就是人类模块。不另做「消息中心」页面。

必须具备：

1. **完整历史。** 打开某家族即看到该 `channel.jsonl` 的全量（分页），包括定向帖和已撤回行。
2. **操作者身份。** 人类发帖的 `from` 恒为 `operator`。页面不提供假扮某条线的选择器。
3. **没有隐藏私信。** 凡 `F(P)` 内的定向帖，人类模块都能看到，并标明 `from` → `to`。
4. **只追加，可撤回。** 页面不能改写或删除旧行。撤回只能复制 `line post operator --kind retract --body <id>` 的 CLI；人类 POST 集合不含 `retract`。
5. **列表 / 时间线 / 过滤。** 同一窗格内切换：列表（未读优先）、时间线（seq 正序）、过滤（kind、from、未读）。不是三个新页面。
6. **人类 POST 只允许 `decision`、`contract`、`ack`。** 其它 kind 返回 403，稳定 code `FAMILY_POST_FORBIDDEN`。
7. **页面不能 merge / push / `--yes` / 改 Task 状态。** 这些控件不存在。复制区只出 dry-run 或只读命令。

### 8.1 HTTP

家族相关 API 都挂在 `/api/v1/workspaces/{alias}/families` 下。路径参数只解码一次，再走既有安全 ID 校验。

| 方法与路径 | 认证 | 行为 |
| --- | --- | --- |
| `GET /api/v1/workspaces/{alias}/families` | bearer | 一层家族列表 |
| `GET /api/v1/workspaces/{alias}/families/{parent}` | bearer | `F(parent)` 图与未读摘要 |
| `GET /api/v1/workspaces/{alias}/families/{parent}/channel?after=&filter=` | bearer | 频道页。`filter` 为 `unacked \| kind \| from` 的白名单组合 |
| `POST /api/v1/workspaces/{alias}/families/{parent}/channel` | bearer | **唯一浏览器写入。** body 只接受下面的 JSON |
| `GET /api/v1/workspaces/{alias}/families/{parent}/artifacts` | bearer | P3：overlay 产物清单 |
| `GET /api/v1/workspaces/{alias}/families/{parent}/artifacts/{id}` | bearer | P3：单件元数据或图像字节 |

`POST` body：

```json
{
  "kind": "decision",
  "to": "",
  "body": "先同步 core_pay，再谈合入",
  "ack_id": ""
}
```

| `kind` | 必填 | 效果 |
| --- | --- | --- |
| `decision` | `body` | 追加频道行，`from=operator` |
| `contract` | `body` | 同上 |
| `ack` | `ack_id` | 标记该行已读，不追加 body 文案 |

其它字段、未知 kind、`to` 落在家族外、空 body 的 `decision` / `contract`，全部拒绝。成功响应是统一 Console 封套，`data` 只含新行 id 与 seq。该 POST 走既有 session、Host、Origin、Content-Length、并发和 10 秒 deadline；body 上限 4 KiB。

meta `surfaces` 增加 `events`（P1）与 `families`（P2）。页面按能力拉取，overview 轮询仍不带这些列表。

---

## 9. ADR 0005 的唯一例外

[ADR 0005](../adr/0005-local-web-console.md) 第 4 条：「Console 首发没有交付 mutation API」。本设计**不改写**该 ADR 正文，只增加一条兼容例外：

> `POST /api/v1/workspaces/{alias}/families/{parent}/channel` 是 Console 唯一允许的非 GET 数据面。它只把 overlay 信号追加到 `.dyro/families/<parent>/` 与 `.dyro/events.jsonl`。它不能执行、复核、签收、合并、推送、导入证据、回答任务、改变 Objective、安装工具或更新 Dyro。

因此：

1. 例外对象只有这一条 POST。`ack` 也走它，不另开第二条写入路径。
2. 写入面只是 overlay 信号，不是 git 对象、不是 Task 状态、不是 Objective journal、不是 ledger 交付行、不是 Proof。
3. 既有「所有 workspace mutation method 返回 405」对 `task`、`objective`、`line merge`、`push`、`--yes` 仍然成立。
4. 未来若要在浏览器里预览 merge 或改 Task，仍须按 ADR 0005 另开 ADR：不可变命令预览 → 权限与影响 → 一次性 nonce → Core 重验 → 现有 mutation API。本文件不授予那些能力。
5. 打开、轮询、SSE、隐藏 tab、停止 server，除上述 POST 外仍不写 workspace、registry、Task、Objective 或 recent preference。

---

## 10. 阶段与验收

同一 `0.7.x` 列车，分三个可独立验收的阶段。未完成阶段不得假装已具备能力：meta 不宣告对应 `surface`，窗格显示「尚未开放」。

### P1 · 事件 + 图

P1 已在本 PR 落地（事件尾、`parent` 投影、一层家族图、SSE `after=`）。P2 / P3 仍未实现。

- 写入并读取 `.dyro/events.jsonl`。
- line DTO 投影 `parent`。
- 工作区详情出现家族树与事件流。
- SSE + `after=`；隐藏 tab 暂停；失败回退 5 秒轮询。
- 边在 merge / sync 时点亮。
- 复制区只有 dry-run，没有 `--yes`。

### P2 · 家族频道 + 人类模块

- 计算 `F(P)`，写入 `channel.jsonl` 与对应 `signal` 事件。
- CLI：`line post` / `inbox` / `ack`。
- `next` 与控制面读取未 ack。
- Console 频道窗格：完整历史、`operator` 身份、列表 / 时间线 / 过滤。
- 唯一 POST：`decision` \| `contract` \| `ack`。
- `/dyro-line-family` 行为不变，不发帖。

### P3 · 产物

- overlay 产物清单与同源图像。
- 视频只出卡片。
- 不扫描 harness home，不把 sidecar 输出目录当成家族产物。

### 验收

- 操作者打开已登记工作区详情，能在同一页看到一层家族、直播事件和（P2 后）频道，而不需要新的一级导航。
- `core` / `core_pay` / `core_pay_fix` fixture：`F(core)` 含 `core`、`core_pay`、`operator`，不含 `core_pay_fix`；`F(core_pay)` 含 `core_pay_fix`。
- 表亲看得到广播，看不到对方定向帖；父线与 `operator` 看得到该家族全部定向帖。
- 隐藏详情或切换到后台后，SSE 与轮询停止；回到前台只补拉 cursor 之后的行。
- 事件与频道 JSON 不含 prompt、argv、绝对路径、remote 或未知字段原文。
- 页面复制的 spawn / merge / sync 命令都含 `--dry-run` 且不含 `--yes`。
- 人类 POST `blocked` / `shipped` / `ask_sync` / `artifact` / `retract` / `merge` 均被拒绝；`decision` / `contract` / `ack` 只改 overlay。
- `shipped` 或 `decision` 之后，Task 仍未 merge，`next` 不得把它说成已集成。
- `/dyro-line-family` 预检前后都不调用 `line post`。
- `next --format json` 在有未 ack 时给出 `family_unacked`，且 `commands` 不含发帖或 ack。
- 停止 Console、过期 session、伪造 Host / Origin、无 bearer 的 POST，都不能留下半行频道或事件。
- 键盘可在三个窗格 / tab 间移动；320 px 宽仍能看树、读事件、发一条 `decision`。
- 公开版本号仍是当前列车；本设计落地不要求改成 `0.8.0`。

### 非目标

- 第二套 IDE、编辑器或 harness 会话镜像。
- 离开 loopback：LAN、`--host 0.0.0.0`、SSH 转发当产品、远程团队账号。
- 把聊天当成 SSOT 或交付门。
- 扫描 harness home、个人 skill 目录或 sidecar 输出目录来发现对话 / 图像。
- 在浏览器里执行 spawn / merge / sync / push / task-status / `--yes`。
- WebSocket、第三方字体 / 图表 / 播放器、service worker。
- 把 Objective `events.jsonl` 与工作区 `.dyro/events.jsonl` 合成一份日志。
- 让 `/dyro-line-family` 发帖或 ack。
- 多级家族树、隔代 merge、表亲直接 merge。

---

## 11. 模块边界

建议只加读取与 overlay 信号边界，不改 TaskGraph / Objective / merge 实现：

```text
src/dyro/events.py              工作区 .dyro/events.jsonl 追加与游标读取
src/dyro/families.py            F(P) 计算、channel.jsonl、acks、产物清单
src/dyro/console/events.py      SSE / after= DTO
src/dyro/console/families.py    家族图与频道 DTO、唯一 POST 校验
src/dyro/console/assets/        详情里三个窗格；无新的顶级 shell
```

`hub.py`、`graph.py`、`tasks.py`、`continuation/`、`home.py` 的职责不变。`cli.py` 只解析 `line post|inbox|ack`。inspection worker 仍只走 Core 只读快照；频道 POST 在 listener 进程内写 overlay，并受既有请求上限约束，不得在 worker 里执行 git。

静态资源仍走 wheel manifest。新增 JS / CSS 必须列入 manifest 并做 digest 校验；不得从 cwd 回退。

---

## 12. 与既有 Console 契约的关系

第 4–11 节是增量。下列条目继续以 [本地 Web 控制台](local-web-console.md) 与 [ADR 0005](../adr/0005-local-web-console.md) 为准：

- 统一响应封套、ETag 不含纯 `captured_at`、错误封套只用稳定 code；
- 脱敏白名单与拒绝列表；
- loopback、精确 Host、无 CORS、无 cookie、`sessionStorage` bearer、CSP；
- inspection 硬 deadline 与进程组回收；
- 页面用 `textContent`，不用 `innerHTML`；
- 推荐动作是可复制 CLI，真正执行仍走现有权限、确认、锁、证据和审计。

本文件只回答一件事：在不把 Console 变成第二套操作系统的前提下，让人看见一层家族里正在发生的事，并让人留下 overlay 决定。
