# 生产验收操作员手册

本手册把真实环境的 `PROD-01`、`PROD-02`、`PROD-09` 证据转换为 Dyro
可验证记录。操作员工具只做五件事：

1. 定位或导出当前安装版本的 JSON Schema；
2. 对真实普通文件做稳定 SHA-256；
3. create-only 地准备未签名 JSON；
4. 导出外部 Ed25519 signer/HSM 应签署的精确 bytes；
5. 用 trust store 公钥验证返回的签名后，create-only 地附加 signature。

它不会读取生产私钥，不会自动生成 `pass` 断言，不会批准发布，也不会执行
deployment、Core import、review、signoff、merge 或 push。
每个子命令的 JSON 报告都会显式返回
`private_key_loaded=false`、`release_approval_granted=false` 与
`deployment_attempted=false`，便于流水线持续校验这三条边界。

## 0. 准备目录与信任根

所有 `--output` 的父目录必须已经存在。输出文件和 schema 导出目录必须是全新
路径；dry-run 与真实执行都拒绝覆盖普通文件、目录、符号链接或 dangling
symlink。

发布、安全、provider 与配额四个用途必须分别登记不同公钥：

```bash
dyro --root /control/dyro-profile key trust release-2026 \
  --purpose production-release \
  --public-key /trust/release-2026.public.pem
dyro --root /control/dyro-profile key trust security-2026 \
  --purpose production-security \
  --public-key /trust/security-2026.public.pem
dyro --root /control/dyro-profile key trust provider-2026 \
  --purpose production-provider \
  --public-key /trust/provider-2026.public.pem
dyro --root /control/dyro-profile key trust quota-2026 \
  --purpose production-quota \
  --public-key /trust/quota-2026.public.pem
```

生产私钥留在各职责主体的 HSM 或远程签名系统中，不放入 Dyro Profile、
runner workspace、evidence 目录或命令行。

## 1. 定位并保存精确契约

```bash
dyro runtime production-acceptance schemas --human

dyro runtime production-acceptance schemas \
  --output-dir /release/contracts/dyro-0.5.1
```

无 `--output-dir` 时只返回安装包内的绝对路径、字节数与 SHA-256。提供
`--output-dir` 时创建一个全新目录并复制两份 schema。wheel 与 sdist smoke
都会验证这些路径来自当前安装制品，而不是当前 Git checkout。

## 2. 准备发布清单

先用 dry-run 检查全部真实输入和输出冲突：

```bash
dyro --dry-run runtime production-acceptance release-prepare \
  --release-id dyro-0.5.1-prod.1 \
  --environment-id prod/tw-primary \
  --source-commit 0123456789abcdef0123456789abcdef01234567 \
  --wheel /release/dist/dyro-0.5.1-py3-none-any.whl \
  --sdist /release/dist/dyro-0.5.1.tar.gz \
  --sbom /release/supply-chain/sbom.cdx.json \
  --provenance /release/supply-chain/provenance.intoto.jsonl \
  --provider codex=/opt/dyro/providers/codex \
  --provider claude=/opt/dyro/providers/claude \
  --deployment /release/operations/deployment.yaml \
  --canary-plan /release/operations/canary.md \
  --rollback-plan /release/operations/rollback.md \
  --observability-plan /release/operations/observability.md \
  --runbook /release/operations/runbook.md \
  --output /release/acceptance/release.unsigned.json
```

确认 JSON 输出中的每个 `path`、`size_bytes` 与 `sha256` 后，移除
`--dry-run`。工具会再次读取并稳定哈希所有输入，再创建未签名清单。任何输入
是空文件、符号链接、FIFO、超限文件，或在读取期间被替换/修改，都会失败且不
产生输出。

该步骤不生成 SBOM 或 provenance；它只绑定发布流水线已经生成并审核的真实
文件。canary、rollback、observability 与 runbook 也必须是该发布实际采用的
版本。

## 3. 由外部 release signer 签署

```bash
dyro runtime production-acceptance signing-payload \
  --record /release/acceptance/release.unsigned.json \
  --output /release/acceptance/production-release.payload
```

JSON 输出同时提供 `payload_base64`、`payload_sha256` 与 raw payload 路径。
外部 signer 必须直接用 Ed25519 签署 raw bytes，不能先把 payload SHA-256
当作待签消息。消息格式为：

```text
ASCII("dyro/production-release/v1") || NUL || RFC8785(unsigned_record)
```

HSM 返回 64 字节 Ed25519 signature 的规范单行 Base64。将其放进普通文件，
然后附加：

```bash
dyro runtime production-acceptance signature-attach \
  --root /control/dyro-profile \
  --record /release/acceptance/release.unsigned.json \
  --key-id release-2026 \
  --signature-file /secure-transfer/production-release.signature.b64 \
  --output /release/acceptance/release.signed.json
```

`signature-attach` 会先用 trust store 验证 exact payload 与 key ID，验证失败
时不写输出。成功只表示签名可验证，返回值仍包含：

```json
{
  "private_key_loaded": false,
  "release_approval_granted": false,
  "deployment_attempted": false
}
```

## 4. 准备三份环境验收证明

每个职责主体都必须提供一份 assertions JSON。字段以已导出的
`production-attestation.schema.json` 为准。工具不会创建模板或把字段自动
设为 `true`。当 `--verdict pass` 时，所有关键布尔断言必须显式为 `true`，
开放 high/critical findings 必须为 0；`PROD-02.canary_runs` 与
`PROD-09.writable_mount_count` 必须至少为 1。

以下命令展示 `PROD-01` 的输入形状：

```bash
dyro --root /control/dyro-profile \
  runtime production-acceptance attestation-prepare \
  --release-manifest /release/acceptance/release.signed.json \
  --check PROD-01 \
  --verdict pass \
  --assertions /security/prod-01.assertions.json \
  --evidence \
    https://evidence.example/releases/dyro-0.5.1/prod-01/report.json=/security/report.json \
  --evidence-summary "独立多宿主逃逸与租户边界评审" \
  --evidence \
    s3://immutable-audit/dyro-0.5.1/prod-01/findings.json=/security/findings.json \
  --evidence-summary "关闭后的高危与严重发现清单" \
  --expires-at 2026-08-07T12:00:00+00:00 \
  --output /release/acceptance/prod-01.unsigned.json
```

每个 `--evidence URI=PATH` 必须按顺序对应一个 `--evidence-summary`。URI 必须
是无凭据、无 query/fragment 的 `https`、`s3`、`gs`、`az` 或 `urn` 地址；
PATH 是本地只读快照，工具会把其真实 SHA-256 写入证明。证明绑定已验证的
**signed release manifest canonical SHA-256** 与同一 environment ID。

对 `PROD-02`、`PROD-09` 重复该流程，并分别使用 `--check PROD-02`、
`--check PROD-09`。

## 5. 分别签署环境证明

以 `PROD-01` 为例：

```bash
dyro --root /control/dyro-profile \
  runtime production-acceptance signing-payload \
  --record /release/acceptance/prod-01.unsigned.json \
  --release-manifest /release/acceptance/release.signed.json \
  --output /release/acceptance/production-security.payload

dyro --root /control/dyro-profile \
  runtime production-acceptance signature-attach \
  --record /release/acceptance/prod-01.unsigned.json \
  --release-manifest /release/acceptance/release.signed.json \
  --key-id security-2026 \
  --signature-file /secure-transfer/production-security.signature.b64 \
  --output /release/acceptance/prod-01.signed.json
```

`PROD-02` 使用 `production-provider` key，`PROD-09` 使用
`production-quota` key。附加 attestation 签名时会重新验证 signed release
manifest，拒绝跨 release/environment 漂移，并拒绝与 release signer 共用同一
公钥。最终门禁还会拒绝三份证明之间的公钥复用。

## 6. 运行只读门禁

```bash
dyro --root /control/dyro-profile runtime production-gate \
  --release-manifest /release/acceptance/release.signed.json \
  --security-attestation /release/acceptance/prod-01.signed.json \
  --provider-attestation /release/acceptance/prod-02.signed.json \
  --quota-attestation /release/acceptance/prod-09.signed.json \
  --human
```

- `READY`：退出码 0，但仍需独立发布控制面的最终批准；
- `NOT_READY`：退出码 3，输出未关闭阻断项和下一条操作员命令；
- 输入/签名/契约错误：退出码 2；
- 输出中的 `checked_at` 是本次验证时间，不是证据生成时间。

门禁不会缓存成功结果。发布前必须针对即将部署的同一组文件重新运行；任何
attestation 过期、密钥撤销、文件/环境切换或发布清单变化都必须重新验收。

## 故障恢复

- 输出已存在：选择新路径并重新执行；不要删除或覆盖既有审计记录。
- 输入读取期间变化：停止发布，冻结制品后从 `release-prepare` 重做。
- HSM 验签失败：核对 `purpose`、raw payload、key ID 与 Base64；不要在本地
  生成替代生产签名。
- 证明过期：由原职责主体基于当前环境和当前 release 重新验收、重新签署。
- 门禁 `READY` 但发布审批未完成：保持不部署；`READY` 不是 signoff。
