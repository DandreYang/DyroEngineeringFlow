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
3. 在 GitHub 仓库 Settings → Environments 创建 `pypi`；建议设置 required reviewers，发布前人工批准。

PyPI Trusted Publishing 将 GitHub Actions 的 OIDC 身份绑定到这个仓库、工作流和 Environment；不要为该工作流创建或保存长期 `PYPI_TOKEN`。

## 发布一个版本

1. 确认 `pyproject.toml` 的 `project.version` 是未发布的新版本，例如 `X.Y.Z`。
2. 在本地运行：

   ```bash
   python3 -m unittest discover -s tests -t . -v
   python3 -m pip install --upgrade build twine
   python3 -m build
   python3 -m twine check --strict dist/*
   ```

3. 提交并推送版本变更，创建与版本严格匹配的 tag，例如 `vX.Y.Z`。
4. 在 GitHub 基于该 tag 创建并发布 Release。工作流会再次测试、构建、检查 metadata；通过 `pypi` Environment 的人工批准后才上传 PyPI。
5. 发布完成后验证：

   ```bash
   pipx install dyro
   dyro --version
   ```

PyPI 不允许覆盖同一个版本号。发布失败后，如需修改分发文件或 metadata，必须递增版本号并重新创建 Release。

若 GitHub 的 `release` 事件没有自动生成工作流运行，可在 Actions 页面选择 **Publish to PyPI** → **Run workflow**，输入既有 tag（例如 `vX.Y.Z`）。手动入口会 checkout 该 tag，并严格校验 tag 必须等于 `pyproject.toml` 的版本；它不会构建后续 `main` 提交。

## TestPyPI（可选但推荐）

先在 [TestPyPI](https://test.pypi.org/) 用独立账号和 pending publisher 演练，可降低首次正式发布风险。TestPyPI 与 PyPI 的账号、项目和包文件相互独立；测试安装时使用 `--index-url https://test.pypi.org/simple/ --no-deps`。
