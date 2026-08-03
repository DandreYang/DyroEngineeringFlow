# Dyro Console 本地 Web 控制台实施蓝图

状态：待实施
规划基线：`main@489caaf68bbb0417bc1fa74c43fb100b9034af2b`（v0.5.6）
发布目标：Dyro 0.6.0；`v0.5.7` 只保留维护修复
前置依赖：持久续航引擎 PR-04 的共享快照和 PR-09 的 Attention/Home 读接口
交付方式：1 个设计记录 PR，加 6 个可独立评审、可独立回滚的 Console 实现 PR；与续航 PR-01 至 PR-12 同一发布列车
默认策略：本地、前台、只读、无外部资源、无第二状态源

## 1. 最终交付结果

完成本蓝图后，用户可以从任意目录执行：

```bash
dyro console
```

并在浏览器中安全查看：

1. 所有全局登记工程的 availability、health、freshness 和活动摘要；
2. 需要修复、需要回答、可推进、暂停和等待事项；
3. 开发线、Objectives、Task 状态与依赖图；
4. attempt、gate、evidence、review、sign-off、merge 和集成摘要；
5. budget、Trigger、active action、next wake 和 uncertain 恢复状态；
6. 本机编码工具状态与 Dyro 已缓存更新信息；
7. 每个异常的一条可复制恢复命令。

Console 不创建数据库，不改 workspace、registry、Objective 或 ledger，不执行 delivery mutation，不访问外部 Web 资源。

## 2. 实施原则

- 每一步只做一个 PR，并从依赖 PR 的精确 merge SHA 创建独立 linked worktree。
- 不切换、不清理、不 stash、不重置用户当前 checkout。
- 先写失败测试，再实现最小闭环，再执行全量门禁。
- UI 只消费显式 DTO；不暴露内部 dataclass，不解析 CLI 文本。
- API 与浏览器首版无 Task、Objective、工具、更新或 Git 写操作。
- 任何新增字段必须先进入 read-model 白名单和脱敏测试。
- 部分失败必须局部可见；不能把异常转为空数组或“已完成”。
- HTTP 安全、wheel/sdist 资源和无外部请求是发布门禁，不是后补项。
- 每个实现 PR 合并后当前 main 都保持可测试、可回滚、无状态迁移负担。

## 3. 目标模块地图

```text
src/dyro/console/
  __init__.py
  models.py             展示 DTO、schema 和错误码
  redaction.py          字段白名单、路径与异常净化
  read_model.py         overview/workspace/objective/task 投影
  activity.py           ledger/action 时间线与 opaque cursor
  inspection.py         bounded worker protocol、deadline 与进程组回收
  _inspect_worker.py    exec 后调用 Core read snapshot 的内部入口
  session.py            bootstrap secret 与内存 session
  assets.py             package resource manifest 与安全解析
  server.py             bounded loopback HTTP server
  api.py                认证、路由、ETag 和错误封套
  api_overview.py       全局概览与 workspace summary
  api_details.py        line/objective/task/graph/activity 详情
  static/
    index.html
    app.js
    api.js
    router.js
    styles.css
    views/overview.js
    views/details.js
    views/system.js
    components/*.js
    icons/*.svg

src/dyro/observations.py       Core-owned WorkspaceReadSnapshot composition
```

对应测试：

```text
tests/test_console_models.py
tests/test_console_read_model.py
tests/test_console_redaction.py
tests/test_console_activity.py
tests/test_console_session.py
tests/test_console_server.py
tests/test_console_api.py
tests/test_console_assets.py
tests/test_console_cli.py
tests/test_console_packaging.py
web-tests/                 test-only browser and accessibility suite
```

## 4. 依赖图与并行策略

```text
Continuation PR-04 Shared Scheduler
                 │
Continuation PR-09 Attention & Home
          ┌──────┴───────────────────────────────────────────────┐
          ▼                                                      ▼
Continuation PR-10 Automatic Execute/Review            Console PR-C01 Read Model
          │                                                      │
Continuation PR-11 Automatic Local Merge               Console PR-C02 Secure Runtime
          │                                                      │
          │                                       ┌──────────────┴──────────────┐
          │                                       ▼                             ▼
          │                         Console PR-C03 Overview       Console PR-C04 Details & Graph
          │                                       └──────────────┬──────────────┘
          │                                                      ▼
          │                                      Console PR-C05 Integrated UX
          │                                                      │
          │                                      Console PR-C06 Hardening
          └──────────────────────────────┬───────────────────────┘
                                         ▼
                    Continuation PR-12 Unified 0.6.0 Release Gate
```

PR-C03 与 PR-C04 可在 PR-C02 合并后并行，但必须遵守冻结文件所有权：

- C03 只拥有 `api_overview.py`、`views/overview.js` 和 overview fixtures；
- C04 只拥有 `api_details.py`、`activity.py`、`views/details.js` 和 detail fixtures；
- C02 预先冻结通用 router、API client、响应封套和 view extension contract；
- 两者不得各自修改共享 DTO 或 session 语义；发现缺口先回到 C01/C02 做前置修正。
- 两个 worktree 必须从同一个已合并 PR-C02 SHA 创建并保存 contract fixture hash；集成前分别
  rebase 到同一目标 SHA、复跑契约测试，不能在一条支线私自演进公共 schema。

C05 负责把两条支线集成成一致体验。C06 的攻击 fixtures 和 browser harness 可以在 C02 后提前准备，但正式断言只在 C05 API 冻结后合并。C06 是 Console 的专属硬化门禁；它与续航 PR-11 一起成为续航 PR-12 的前置依赖，PR-12 才是唯一的 `0.6.0` 统一发布门禁。

## 5. 分步计划

### PR-C01：展示读模型、脱敏与一致性契约

依赖：持久续航引擎 PR-04、PR-09。

目标：建立与页面无关的只读 API 内核；本 PR 不启动 HTTP server，也不添加浏览器资源。

文件所有权：

- 新增 `src/dyro/observations.py` 以及 `src/dyro/console/__init__.py`、`models.py`、
  `read_model.py`、`redaction.py`；
- 新增对应 unit tests；
- 仅为导出稳定读接口而小幅修改 `continuation/snapshot.py`、`continuation/attention.py`；
- 必要时为 `config.py`、`tasks.py`、`provenance.py` 和 `evidence_store.py` 增加可选的单次有界
  读取参数，保留现有 CLI 默认值；禁止在 Console 中复制 Core parser；
- 更新 `pyproject.toml` 显式包含 `dyro.console` package，不添加依赖。

冷启动先读：

- 已批准 ADR-0004、ADR-0005 和两份详细设计；
- `src/dyro/hub.py` 的 registry schema 和 fail-closed 行为；
- `src/dyro/graph.py` 的 TaskGraph JSON projection；
- `src/dyro/tasks.py` 的 status、decisions、board、stats 和 ledger；
- `src/dyro/provenance.py` 与 `src/dyro/evidence_store.py` 的公开读取路径；
- `continuation/snapshot.py`、`planner.py`、`attention.py`；
- `src/dyro/updates.py`、`src/dyro/tooling.py` 的本地缓存读取函数。

主要改动：

- 定义 immutable `ConsoleEnvelope`、`ConsoleOverview`、`WorkspaceSummary`、
  `ConsoleWorkspace`、`ConsoleObjective`、`ConsoleTask`、`ConsoleGraph`、
  `ConsoleActivityPage` 和错误或 freshness DTO。
- 定义 stable console reason codes、字段上限、list 上限和 schema version。
- 在 Core `observations.py` 定义 presentation-neutral `WorkspaceReadSnapshot`：capture ID、workspace revision、
  observed_at、source digests、`complete | partial | unavailable` 和结构化组件错误。所有
  endpoint 只能切片同一 capture。
- 从共享 SchedulerSnapshot 或 ContinuationSnapshot 生成 read model；无 Objective 的 workspace
  使用同一 TaskGraph scheduler primitive。
- 区分 summary capture 和 detail inspection；summary 不调用完整 Git status。
- 对一次 workspace capture 记录权威 revision 摘要，读取前后发生变化时重试一次，仍变化则
  返回 stale 或 partial。
- 对 path、remote、argv、prompt、answer、raw log、provider output、未知 ledger 字段和异常
  做显式拒绝白名单。
- recovery command 只由经过 `validate_id` 的 alias、Task、line 或 Objective 组合，不接收任意
  错误文本。
- 规范化脱敏 data，计算稳定 `snapshot_sha256`；locale 和 capture time 不参与事实 digest。
- workspace revision、completeness 和 error code 参与 digest；禁止未标记的 last-known-good fallback。
- 所有读取函数接受显式 clock、limits 和 inspection timeout policy，便于确定性测试。
- request deadline 与 cooperative cancellation 贯穿 builder；超大输入在解析前拒绝，不能先
  无界 `read_text` 后再截断。生产 HTTP 的硬中断边界由 C02 worker process 提供。
- evidence、attention、completion 和 health 由各自 Core owner 在 capture 中生成；Console
  endpoint 不得重算语义，读取也不得触发 lazy index、recent preference 或 Git lock 写入。

先写的失败测试：

- 现有 TaskGraph fixture 与 Console 节点、边、status、reason code 一致。
- Objective attention、budget、action 和 next wake 与共享 snapshot 一致。
- title 中的 HTML 只作为受长度限制的文本，控制字符被净化；错误或 ledger 中的绝对路径、
  常见凭据模式和未知字段不会越过白名单。
- 一个 corrupt attempt 或 Objective 只产生该 subject 的 partial error，不让其他 workspace 消失。
- 相同输入重复生成相同 canonical data 和 hash；语言、顺序或时间展示不改变 hash。
- builder 前后输入 revision 变化时重试；连续变化标记 stale，不拼接两次读结果。
- 调用所有 read API 前后，workspace、registry、ledger、recent preference 的 bytes 完全相同。

验证命令：

```bash
uv run --frozen python -m unittest tests.test_console_models tests.test_console_read_model tests.test_console_redaction
uv run --frozen ruff check src tests
uv run --frozen python -m unittest discover -s tests -t .
uv build
git diff --check
```

出口条件：公开 DTO 和脱敏矩阵评审通过；没有 HTTP、HTML 或 mutation；wheel 外可 import `dyro.console`。

回滚：整 PR 可回滚；没有持久文件、schema migration 或用户状态。

### PR-C02：安全 loopback runtime、session、资源打包与 CLI

依赖：PR-C01。

目标：交付一个只显示安全空壳和 `/api/v1/meta` 的本地 server，先证明网络边界与生命周期。

文件所有权：

- `src/dyro/console/session.py`、`assets.py`、`server.py`、`api.py`、`inspection.py`、
  `_inspect_worker.py`；
- 最小 `static/index.html`、通用 `app.js`、`api.js`、`router.js`、`styles.css`；
- `src/dyro/cli.py` 的 `console` parser 和前台生命周期；
- `pyproject.toml` package-data；
- session、server、asset、CLI 测试。

冷启动先读：

- ADR-0005 的安全决策和 threat model；
- `src/dyro/witness.py` 的 bounded HTTP server、timeout 和 shutdown 语义；
- `src/dyro/tooling.py` 的 `webbrowser` 使用和错误处理；
- `src/dyro/cli.py` 的 global selector、dry-run、error 和更新检查路径；
- `pyproject.toml` 显式 package 列表、CI wheel smoke 和发布文档。

主要改动：

- 只绑定 `127.0.0.1`；默认 port 0，不提供 `--host`。
- listener ready 后生成 256 bit 以上、60 秒、最多有限失败次数的一次性 bootstrap secret。
- fragment bootstrap POST 交换独立随机 bearer；constant-time compare，成功后销毁 secret。
- bootstrap script 读取 secret 后先 `history.replaceState` 清 fragment，再 POST；失败重试只用
  JS 局部内存副本。
- server session 只在进程内；browser bearer 只在当前 tab 的 `sessionStorage`，通过
  Authorization header 发送。明确禁止 localhost cookie、`localStorage`、IndexedDB 和 URL token。
- bearer 固定 30 分钟 idle 和 8 小时 absolute TTL；成功 GET/304 刷新 idle，hidden 停止
  polling，absolute TTL 不延长。401 后客户端先清空旧数据和 `sessionStorage`。
- 精确 Host、Origin、Sec-Fetch-Site、method 和 Content-Type 验证；关闭 CORS 和 request log。
- 拒绝缺失或重复 Host、非 origin-form target、Transfer-Encoding、obs-fold、header CTL、重复或
  冲突 Content-Length 和 GET body；响应固定 `Connection: close`。
- 请求行、header、body、并发、read timeout 和 request deadline 采用硬上限。
- CSP 含 `worker-src 'none'` 和 `object-src 'none'`；frame、COOP、referrer、nosniff、cache
  header 全量输出；HTML 无 inline script/style。
- manifest-only package resources，验证普通文件、大小和 digest，拒绝路径拼接和 symlink。
- 禁止 cwd/source checkout resource fallback；manifest 或任一引用 asset 缺失时启动 fail-closed。
- HTTP 主进程以固定 argv、净化环境 `exec` inspection worker，绝不 fork listener/session
  memory；registry 和用户 workspace 的 `stat/open/read`、Core snapshot 与 Git 都在 worker 内
  完成。父进程复验有界 JSON，并在硬 deadline 或 shutdown 时终止整个 process group、释放
  queue/slot。
- `dyro [--workspace ALIAS | --root PATH] console [--no-open] [--port PORT]`；自动打开失败时给一次性 URL。
- `dyro --dry-run console` 不 bind、不生成 secret、不打开浏览器，也不更新 recent state。
- Ctrl-C 有界关闭，清空 session/cache；server start 或 failed bind 不写任何 Dyro 状态。
- 固定 view extension contract，为 C03/C04 提供互不修改的模块入口。

先写的失败测试：

- 无 bearer 不能读 meta；secret 错误、重放、过期和过量失败均拒绝。
- response 不设置 cookie；另一个 localhost port 收不到 bearer，跨 Origin Authorization 请求
  的 preflight 失败。
- bootstrap POST 缺失、`null` 或不精确 Origin 拒绝；fragment 在发起慢 POST 前已经清除。
- 初始 GET request 不含 fragment；server log、error 和 HTML 中不出现 secret。
- forged Host、cross Origin、OPTIONS、PUT/POST workspace route、encoded traversal、双重编码拒绝。
- duplicate Host、absolute-form、Transfer-Encoding、重复 Content-Length、obs-fold、header CTL
  和 request body ambiguity 逐项 fail-closed。
- 超大 request line/header/body、slow body 和超过 8 个并发请求受限。
- asset symlink、manifest 外文件、digest 或 size 漂移 fail-closed。
- cwd 中放置同名 asset 不能被读取；wheel 中缺 manifest 或 HTML 引用资源时 server 不启动。
- inspection fixture 在 `stat/open/read` 阶段永不返回时，父进程杀死 worker process group，
  supervisor call 和 Ctrl-C 在预算内结束且并发 slot 被释放。
- browser opener 仅在 listener ready 后调用；失败不结束 server；`--no-open` 行为明确。
- Ctrl-C 后 port 不再接受连接、session 失效、无 non-daemon request thread 泄漏。

验证命令：

```bash
uv run --frozen python -m unittest tests.test_console_session tests.test_console_server tests.test_console_assets tests.test_console_cli
uv run --frozen ruff check src tests
uv run --frozen python -m unittest discover -s tests -t .
uv build
git diff --check
```

人工 smoke：在临时 `DYRO_HOME` 下启动，检查浏览器 Network 只有 `127.0.0.1`，Ctrl-C 后连接失败。

出口条件：本地 server 安全审查通过；无 workspace data endpoint；源码树和 wheel 都能加载同一空壳。

回滚：删除 `console` 命令和 package-data 即恢复；无持久状态需要清理。

### PR-C03：全局概览、工作区摘要与部分失败

依赖：PR-C02。可与 PR-C04 并行。

目标：完成最重要的用户价值——一次看到全部工程和最高优先级事项。

文件所有权：

- `src/dyro/console/api_overview.py`；
- `static/views/overview.js`、overview 专属 components；
- overview API、multi-workspace 和 responsive fixture tests；
- 不修改 `api_details.py`、`activity.py` 或 `views/details.js`。

冷启动先读：

- PR-C01 read model 和 PR-C02 route extension contract；
- `src/dyro/hub.py`、`src/dyro/home.py` 的 workspace resolution；
- `tests/test_hub.py` 的 stale、corrupt、default 和 recent 行为；
- ADR-0003 的全局 Home UX 边界。

主要改动：

- `GET /api/v1/overview` 与轻量 `GET /api/v1/workspaces/{alias}` summary。
- 一次读取 registry；按 alias 稳定聚合，最多 4 workspace 并发、单项和总预算受限。
- registry 全局损坏与 per-workspace invalid 分开建模；任何卡片失败都保留位置和 recovery。
- 当前目录 workspace 优先聚焦，其次显式 selector、default、唯一 workspace。
- overview 卡片显示 health、freshness、Task 状态分布、Objective/attention 摘要和唯一下一步。
- `not_inspected` 与 `healthy` 明确区分；Git detail 不阻塞首屏。
- 加载、空、partial、stale、session expired 和全局 registry unavailable 的完整状态。
- 工作区排序、filter 和搜索只使用安全字段；route state 不保存路径或 token。
- overview ETag 和 304；页面隐藏后暂停 polling。
- 卡片统一携带 completeness、workspace revision、observed_at 和组件错误；partial 不按空数组
  或 healthy 渲染，stale 只显示其原始观测时间与本次错误。

测试：

- 0、1、50、101 workspace；default/current focus 和稳定排序。
- 某 workspace 路径失效、Profile invalid、读取 timeout、Objective corrupt 时其他卡片完整。
- registry malformed 显示全局恢复页，不创建或覆盖 registry。
- 一个 worker 在 registry 或 workspace `stat/open/read` 永不返回时被硬终止；overview 总预算、
  其他卡片、并发 slot 和后续刷新不受影响。
- 初次响应预算、条件刷新、304、隐藏或可见轮询状态机。
- cards 不包含绝对路径、remote、raw error；recovery command 使用 alias。
- 320、768、1440 px DOM snapshot 关键结构，不用颜色作为唯一标签。

出口条件：新用户只看概览即可找到最高优先级项目和一条下一步；任何读取路径零写入。

回滚：overview route 回到安全空状态；Core 与 registry 不受影响。

### PR-C04：详情、组合图、证据链与活动时间线

依赖：PR-C02。可与 PR-C03 并行。

目标：提供工程师需要的下钻能力，同时保持脱敏、分页和 Core 语义一致。

文件所有权：

- `src/dyro/console/api_details.py`、`activity.py`；
- `src/dyro/workspace.py`、`process.py` 中仅限可注入 timeout/cancellation 的只读 inspection helper；
- `static/views/details.js`、detail 专属 components；
- detail、graph、activity 和 cursor tests；
- 不修改 `api_overview.py` 或 `views/overview.js`。

冷启动先读：

- PR-C01 DTO 和 PR-C02 API contract；
- `src/dyro/graph.py` 的 node、edge、constraint 和 issue projection；
- `src/dyro/provenance.py` 的 attempt 与 review binding；
- `src/dyro/evidence_store.py` 的 generation pointer；
- continuation Objective store、planner、actions、budgets 和 Trigger summary API；
- `src/dyro/tasks.py` ledger 的所有已知 phase。

主要改动：

- line、Task、Objective、graph、activity endpoints，全部认证 GET。
- Task 详情按 contract → scheduler → attempt → gate/heads → review → sign-off → integration 排列。
- Objective 详情严格区分 operator state、derived result、mode 和 effective authority。
- graph 直接使用共享组合图；增加展示 hint，不重算 readiness 或伪造 constraint edge。
- 图默认过滤到 250 可见节点，超大图给筛选建议；API 4 MiB hard limit。
- SVG graph 和始终可用的等价表格；invalid graph 只显示 Core issues。
- activity 逐 phase 白名单化、倒序读取、50/100 条限制、带完整性保护的 opaque cursor。
- ledger truncate/replace、cursor tamper 和 unknown phase 产生明确 code，不回传 raw JSON。
- Git 和集成 detail 通过有 timeout 的 subprocess；超时只让当前 section partial。
- server shutdown 会取消尚未开始的 inspection，并终止未退出的 inspection worker process
  group（包括其 Git 子进程）；不留下无法回收的 workspace thread、process 或 queue slot。

测试：

- Core graph JSON 与 Console node/edge/constraint 集合逐项相同。
- Task 各状态、无 attempt、多 attempt、review pending、sign-off、integrated、invalid evidence。
- Objective paused/stopped/complete/repair、budget exhausted、Trigger error、uncertain action。
- 1,000 Task graph 的过滤、响应大小和时间复杂度；cycle/invalid graph fail-closed。
- activity 正常翻页、append 后翻页、truncate、replace、cursor tamper、超长行和 unknown phase。
- malicious title/event/reason fact 在真实 DOM 中只成为文本。

出口条件：每个 detail 事实可追溯到同一 Core snapshot 或证据摘要；无 raw file 浏览器和无 mutation endpoint。

回滚：移除 detail routes；overview 仍可独立使用；没有数据迁移。

### PR-C05：整合体验、可访问性、本地化与自适应刷新

依赖：PR-C03、PR-C04。

目标：把功能页面整合成新人也能快速上手的完整产品体验。

文件所有权：

- 共享 frontend app shell、router、styles、components 和 i18n catalogs；
- `home.py` 的“查看全部项目控制台”入口；
- `system` view 与只读 tool/update cached endpoint；
- UX、accessibility、polling 和 browser-level tests。

冷启动先读：

- ADR-0003、ADR-0005 的用户体验合同；
- PR-C03/C04 页面和全部 error states；
- `home.py` 的工具选择、最近目标和零写入取消路径；
- `tooling.py` 的 ToolState 与排序；
- `updates.py` 的 cache 和 daily-check 边界。

主要改动：

- 完成 overview → workspace → objective/task → graph/activity 的一致导航和 breadcrumbs。
- 全局 attention-first 排序、搜索、状态 filter 和安全 hash route。
- system 页面只显示本机 tool detection 和已有 update cache；不安装、不检查网络、不更新。
- `dyro` Home 增加 Console 入口，不改变既有默认目标和编码工具 picker。
- 活跃页面 5 秒、等待页面 30 秒 ETag polling；hidden pause、visible single refresh、错误退避。
- 简体中文和英文 catalogs；API code 与事实不本地化。
- semantic landmarks、visible focus、live region、reduced motion、AA contrast、键盘图表格替代。
- 所有恢复入口统一为“原因 + 影响 + 一条复制命令”；clipboard 失败有文本 fallback。
- onboarding 空状态覆盖 setup、join、add；不在浏览器中执行这些操作。

测试与可用性验收：

- keyboard-only：启动后 overview → repair card → Task → 复制命令，全程无焦点陷阱。
- screen-reader semantic snapshot；graph table 与 SVG facts 一致。
- locale switch、unknown code fallback、UTC 与本地时间并存。
- missing tool、stale update cache、无 update cache 都不触发网络。
- bare `dyro` 原有 line/task/tool 流程 golden tests 不回归，Console 取消零写入。
- 至少 3 名不了解内部术语的试用者：30 秒内找到最需关注项目，3 分钟内解释其阻塞并复制正确恢复命令；记录失败点并在本 PR 修正。

出口条件：产品旅程和 accessibility gate 通过；Console 不依赖 README 才能让新人找到下一步。

回滚：移除 Home 导航和共享 UI 增强，C03/C04 API 仍保持只读；无工作区状态丢失。

### PR-C06：攻击验证、压力、浏览器、产物与发布门禁

依赖：PR-C05。

目标：在前序 PR 已满足正确性安全门禁的基础上，增加独立攻击、压力、浏览器与产物证据，
证明 Console 可安全进入 `0.6.0` 的统一发布门禁，而不是只在 source checkout 中工作。C06 不接收首次实现的
Host、Origin、session、CSP、path、request bound 或 asset correctness 修复；发现缺口必须先
回到拥有该契约的 C02–C05 修正并合并。

文件所有权：

- HTTP adversarial tests、property/fuzz fixtures、performance fixtures；
- `web-tests/` 的 pinned test-only browser 和 accessibility harness；
- CI 的 Console security、browser 和 clean-install jobs；
- publishing、architecture、README 和 operator documentation；
- 不扩大 API 功能或引入 browser mutation。

冷启动先读：

- 全部 Console ADR、设计、实现与 tests；
- `.github/workflows/ci.yml`、publish workflow、`docs/publishing.md`；
- `tests/test_release_metadata.py` 和当前 wheel/sdist smoke；
- 现有 terminology-policy scanner 与仓库外 policy input 约定。

主要改动：

- 攻击矩阵：Host/DNS rebinding、Origin/CSRF、bootstrap theft/replay、bearer storage、
  localhost cross-port、XSS、
  CSP、request smuggling edge、path traversal、symlink、header/body/slowloris、concurrency exhaustion。
- 使用真实浏览器验证 fragment 不进入 request/Referer/log、CSP 无 violation、无 external origin。
- pinned test-only browser harness 与 accessibility scanner；Node 只属于 web test，不进入 wheel runtime。
- 50 workspace、1,000 Task、large ledger、slow Git、并发 append 的基准和 regression thresholds。
- fault injection：registry/Profile/Objective/attempt/ledger/asset 各层损坏与 server shutdown race。
- CI 在 Linux Python 3.11–3.14 运行 unit/security；Windows/macOS clean install 启动 `--no-open`
  smoke；至少一个平台运行 browser E2E。
- 每次用新的 `mktemp -d` 构建，记录 wheel 与 sdist 的精确路径和 SHA-256；分别在 checkout
  外两个全新环境按精确文件安装，遍历 HTML 引用的 JS/CSS/icon，校验 manifest、API、CSP、
  无 source fallback、断网启动和 Ctrl-C shutdown。
- 更新架构图、命令说明、隐私边界、故障恢复；明确本地 server 不适合远程暴露。
- 运行仓库外 deny list 对源码、文档、diff、分支、提交候选、生成 help、wheel/sdist metadata
  和静态 assets 扫描，必须零命中。

发布验收命令至少包含：

```bash
uv lock --check
uv sync --locked --all-extras --dev
uv run ruff check src tests experiments
uv run python -m unittest discover -s tests -t . -v
uv run python -m compileall -q src tests experiments
dist_root="$(mktemp -d)"
uv run python -m build -o "$dist_root"
shasum -a 256 "$dist_root"/*
git diff --check
```

还必须保存：browser E2E、accessibility、security matrix、performance、wheel install、sdist install 和术语策略的独立证据。

出口条件：所有 P0/P1 清零；只读与脱敏不变量、localhost 安全、三平台安装、浏览器、性能和 accessibility 全绿；之后将证据交给续航 PR-12 的 `0.6.0` 统一门禁，版本、CHANGELOG、tag、Release 和 PyPI 仍走各自独立确认门禁。

回滚：发布前整 PR 回滚；发布后可在补丁版本移除 `console` command，旧包没有 Console durable state 需要迁移或删除，CLI Core 完整保留。

## 6. 阶段里程碑

| 里程碑 | 包含 PR | 用户可见结果 | 权限 |
| --- | --- | --- | --- |
| W0 数据边界 | C01 | 稳定、脱敏、可复用 read model | 进程内只读 |
| W1 安全 runtime | C02 | 可启动的本地安全空壳 | session + meta GET |
| W2 可观察 | C03–C04 | 全局概览、详情、图和活动 | authenticated GET |
| W3 易上手 | C05 | 完整导航、新手入口、i18n、a11y | authenticated GET |
| W4 Console 硬化 | C06 | 攻击、压力、三平台和产物证据，交给 PR-12 | 仍无 delivery mutation |
| W5 统一可发布 | PR-12（依赖 C06、PR-11） | 续航与 Console 的 `0.6.0` 发布候选 | Console 只读；自动运行默认关闭；仍无自动 push |

单一有经验工程师粗略工作量为 18–26 个工程日；C03/C04 并行时双人可把主线压缩约 4–6 个工作日。估算不替代每个里程碑出口条件。

## 7. 全局验收矩阵

| 维度 | 必须证明 |
| --- | --- |
| 唯一事实源 | Console 无数据库、无状态副本、无 CLI text scraping |
| 权限 | 除 session exchange 外没有 POST；无 execute/review/signoff/merge/push/install/update |
| 一致性 | 每 workspace 独立 snapshot/digest；并发变化显式 stale/partial |
| 部分失败 | 单 workspace、单 section 或 Git timeout 不影响其他内容 |
| 隐私 | 无 raw path、remote、argv、prompt、answer、log、credential 或未知 event 字段 |
| localhost 安全 | session、Host、Origin、CSP、request bounds、path safety 全绿 |
| 易用性 | 任意目录一条命令；30 秒发现重点；每个错误一条恢复命令 |
| 可访问性 | keyboard、focus、AA、reduced motion、semantic graph alternative |
| 性能 | 50 workspace summary、1,000 Task graph、large ledger 有界 |
| 兼容性 | 无 Objective 和无 workspace 都可用；bare Home 与工具选择不回归 |
| 产物 | wheel/sdist checkout 外资源完整；断网无 external request |
| 开源卫生 | 源码、文档、assets、help、branch 和构建 metadata 通过术语策略 |

## 8. 每个 PR 的统一执行协议

1. 记录 source branch、精确 HEAD、dirty 状态、目标 branch 和 linked worktree。
2. 从依赖 merge SHA 创建隔离工作树；验证未改变用户 source checkout。
3. 先运行依赖基线和相关现有 Home/Task/continuation tests。
4. 按本 PR 文件所有权先写失败测试，再实现；并行支线不修改共享 frozen contracts。
5. 运行 focused tests、全量 unittest、ruff、compile、build 和 diff check。
6. 由独立 reviewer 检查架构、browser security、隐私、silent failure、a11y 和测试覆盖。
7. 用仓库外 policy input 扫描工作树、diff、branch 和提交候选；零命中才能继续。
8. 展示 diff、测试证据、剩余风险和回滚方式；单独取得 commit 授权。
9. commit 后复跑关键门禁；单独取得 push 或 PR 授权。
10. 合并后记录 merge SHA，下一步只从该 SHA 开始；不在旧 branch 叠加后续 PR。

Console C06 合并后不得单独进入版本、tag、Release 或 PyPI 流程；必须把其证据与续航 PR-11
一起交给续航 PR-12。PR-12 通过后，`0.6.0` 的版本号、CHANGELOG、tag、Release 和 PyPI 仍
分别需要独立授权。

## 9. 本轮设计文档落库范围

当前仅文档分支应包含：

- `docs/adr/0004-native-continuation-engine.md`
- `docs/designs/native-continuation-engine.md`
- `plans/native-continuation-engine-implementation.md`
- `docs/adr/0005-local-web-console.md`
- `docs/designs/local-web-console.md`
- `plans/local-web-console-implementation.md`

本轮不修改运行时代码、版本号或 CHANGELOG，不 stage、不 commit、不 push，直到分别获得对应授权。六份文档、路径、branch 和提交标题候选都必须在落库前通过仓库外术语策略零命中扫描。
