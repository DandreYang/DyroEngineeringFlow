# 发布 DyroEngineeringFlow 到 PyPI

`dyro` 采用 GitHub Actions 与 PyPI Trusted Publishing 发布。GitHub 不保存长期 PyPI Token；PyPI 仅在受信工作流运行时发放短期 OIDC 凭据。

## 发布前状态

- 已完成：MIT `LICENSE`、PyPI 包元数据、构建与测试、`.github/workflows/pypi-publish.yml`，以及 PyPI 正式发布链路。
- 已完成：PyPI pending publisher 与 GitHub `pypi` Environment。
- 每次新版本仍需创建严格匹配的 Git tag、GitHub Release，并在 `pypi` Environment 审批发布。

## 首次配置（仅项目所有者）

1. 注册并验证 [PyPI](https://pypi.org/) 账号。
2. 进入 PyPI 的 **Publishing** 页面，添加 GitHub Actions 的 **pending publisher**：
   - PyPI project name：`dyro`
   - Owner：`DandreYang`
   - Repository：`DyroEngineeringFlow`
   - Workflow：`pypi-publish.yml`
   - Environment：`pypi`
3. 在 GitHub 仓库 Settings → Environments 创建 `pypi`，并设置至少一名不等于发起人的 required reviewer；关闭管理员绕过，限制为受保护的 release tag。仓库还应对 `main` 启用 required pull request review 与 required CI checks。

PyPI Trusted Publishing 将 GitHub Actions 的 OIDC 身份绑定到这个仓库、工作流和 Environment；不要为该工作流创建或保存长期 `PYPI_TOKEN`。

## 发布一个版本

1. 确认 `pyproject.toml` 的 `project.version` 是未发布的新版本，例如 `X.Y.Z`。
2. 在本地使用锁定环境运行（不要临时 `pip install --upgrade` 构建工具）：

   ```bash
   uv lock --check
   uv sync --locked --all-extras --dev
   uv run python -m unittest discover -s tests -t . -v
   uv run ruff check src tests experiments
   uv run python -m build
   uv run python -m twine check --strict dist/dyro-*.whl dist/dyro-*.tar.gz
   ```

3. 提交并推送版本变更，创建与版本严格匹配的 tag，例如 `vX.Y.Z`。
4. 在 GitHub 基于该 tag 创建并发布 Release。工作流会验证 checkout 恰为该 tag、tag commit 是 `origin/main` 的祖先、`uv.lock` 未漂移，再测试、构建、检查 metadata；通过 `pypi` Environment 的人工批准后才上传 PyPI。
5. 发布完成后验证：

   ```bash
   pipx install dyro
   dyro --version
   ```

PyPI 不允许覆盖同一个版本号。发布失败后，如需修改分发文件或 metadata，必须递增版本号并重新创建 Release。

若 GitHub 的 `release` 事件没有自动生成工作流运行，可在 Actions 页面选择 **Publish to PyPI** → **Run workflow**，输入既有 tag（例如 `vX.Y.Z`）。手动入口会 checkout 该 tag，并严格校验 tag 必须等于 `pyproject.toml` 的版本且位于受信 `main` 历史上；它不会构建后续 `main` 提交。

## TestPyPI（可选但推荐）

先在 [TestPyPI](https://test.pypi.org/) 用独立账号和 pending publisher 演练，可降低首次正式发布风险。TestPyPI 与 PyPI 的账号、项目和包文件相互独立；测试安装时使用 `--index-url https://test.pypi.org/simple/ --no-deps`。

## 发布事故与 Yank Runbook

发布包一旦发现安全、完整性或关键功能问题，发布负责人先停止后续 Release 和推广；不要删除 tag、GitHub Release 或 PyPI 文件来掩盖事件，它们是取证所需的不可变指针。

1. **0–5 分钟：** 项目所有者在 PyPI 项目管理页对受影响的**整个版本**执行 yank，并写明简洁、无机密的原因；在 GitHub Release 标题和正文标记 `YANKED`，记录版本、tag commit、发现时间和负责人。
2. **5–15 分钟：** 创建限制可见度的事故记录，保存测试失败、制品 SHA-256、受影响范围与缓解建议；公开说明必须提示用户，精确 `==` / `===` 约束仍可能安装已 yank 版本。
3. **15–60 分钟：** 判断是否需要撤销 Trusted Publisher、轮换凭据或收紧 Environment；修复后递增版本、重新走完整锁定验证和人工批准，绝不复用或覆盖旧版本。
4. **恢复：** 使用 TestPyPI 先演练安装和 yank 通告；仅在修复版可安装、回归测试通过且事故记录写清升级路径后恢复推荐安装。

单人维护者无法自行提供独立审批：每次正式发布前必须在 Release/PR 留下版本、tag SHA、锁文件检查和风险接受记录，并启用 MFA 与可恢复的项目所有者账户。PyPI 的 yank 是可逆的索引标记，不会撤回已经下载的制品；具体操作和约束以 [PyPI 官方 yanking 文档](https://docs.pypi.org/project-management/yanking/) 为准。
