# 生产副本迁移演练手册

本文描述 R19 的离线演练流程。它只读取线上 SQLite 数据库的不可变备份三件套和离线媒体快照，不连接活动数据库、不停止或启动线上服务，也不授权切换。工具不会产生切换授权；terminal、result、import、site-data comparison 和 deployment-candidate 等 schema 在提供 `cutover_authorized` 字段时必须固定为 false，任何 schema 没有该字段也绝不表示获得授权。正式停写、最终备份和切换属于 R20。

“小抄儿”不在本轮演练范围内。它的现有路径和数据不得因为本手册中的操作被移动或改写。

## 安全边界

- 输入数据库必须由 `backup_database` 生成，并连同同名 `.sha256`、`.metadata.json` 一起复制到离线介质；不得把活动 `db.sqlite3`、硬链接、带 `-wal`、`-shm` 或 `-journal` 的文件当作副本。
- 源数据库三件套、源媒体清单和源媒体目录在整个流程中保持只读。工具只把稳定复制后的数据库放进 RunRoot，再对私有副本执行检查和迁移。
- Proposal RunRoot、两个 Rehearsal RunRoot 必须是三个全新的本机 NTFS 目录。Bootstrap 会审查从直接父目录到卷根的完整祖先链：owner 必须可信，不可信主体不能拥有删除、修改 DACL/owner 或在直接父目录创建/继承可写子项的权限。优先使用当前用户的本机 LocalAppData 私有目录；禁止使用 UNC、映射盘、重解析点、共享临时目录、仓库目录、输入目录或媒体目录。
- RunRoot 创建后由 Bootstrap 收紧 ACL；`approval` 目录还会单独收紧。ACL 校验失败时不得通过放宽脚本或换到共享目录绕过。
- 整个 RunRoot、源输入目录、源媒体和两个 TargetMediaRoot 都含有生产用户数据，必须分别使用私有 DACL 并纳入保留、归档和安全销毁制度。RunRoot 期满后可整体安全销毁；源输入是演练权威副本时不得误删唯一备份，必须先按备份保留制度确认存在独立可恢复副本。
- 工具只强制并验证 RunRoot 与 `approval` 的 ACL；外部源输入和媒体目录的私有 DACL 由运维人员预先设置。运行前必须用 `Get-Acl` 或 `icacls` 复核并把输出归档到工单，未证明时不得放入生产副本或开始演练。
- Proposal ledger 只绑定提案阶段的本地证据。Proposal completion、Review 和 Policy 随后通过精确字节 SHA-256 与字段互相绑定，再由 Rehearsal Bootstrap 冻结；ledger 是 `self_consistent_local_chain` 且 `tamper_proof=false`，不是签名或不可篡改存储。应把 proposal/review/policy/completion/result SHA-256 和 ledger head 另行归档到受控工单。
- 操作员属于受信任边界；本地指纹、DACL 和 ledger 用于发现普通并发漂移、误操作和其他未授权主体的写入，不构成对恶意同一操作员或进程内存篡改的防护，也不是原子文件系统快照。
- 编排器不会主动请求线上服务访问，但 Django 启动、migration 和 `RunPython` 可以包含任意网络、文件或子进程副作用。工具目前不实施或证明网络隔离，也不使用 Windows Job Object 管理脱离进程或孙进程；证据会记录 `network_isolation_enforced=false` 或 `network_access_observation=not_measured`。演练机必须由外部防火墙隔离，人工审阅也必须检查所有 pending migration 的外部副作用，并拒绝会启动后台或脱离进程的 migration。任何终态之后，尤其发生中断时，必须先确认没有遗留进程仍在访问 RunRoot，并把确认结果单独记录到工单，才能归档、销毁现场，把候选物视为稳定，或使用新的 RunRoot 重跑。
- CLI 退出码约定为：0 成功；1 表示 Bootstrap、Proposal 或 Approval 拒绝/失败，也用于 Rehearsal 配置错误和普通执行失败；2 只表示 Rehearsal 已经发布 `blocked` 终态；130 表示中断。Bootstrap 和 Invoke 会原样传播已经启动的内层 CLI 退出码。任意非零、被 Ctrl+C 中断、缺少 completion/result，或证据校验失败时，保留现场用于诊断；即使中断后存在 completion/result，也不得向同一 RunRoot 补文件或续跑。确认原因后使用全新的 RunRoot 从头重跑。

## 0. 准备不可变输入

以下变量仅作示例。RunRoot 名称应包含工单号或时间戳，且目标目录必须尚不存在。

```powershell
$Repo = 'D:\Web\FFXIVShare'
$Python = (Resolve-Path "$Repo\venv\Scripts\python.exe").Path

$SourceDatabase = 'E:\R19-Input\production.sqlite3'
$SourceChecksum = "$SourceDatabase.sha256"
$SourceMetadata = "$SourceDatabase.metadata.json"
$SourceMediaRoot = 'E:\R19-Input\media'
$SourceMediaManifest = 'E:\R19-Input\source-media-manifest.json'

$PrivateBase = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$RunParent = Join-Path $PrivateBase 'FFXIVShare\R19\Runs'
[System.IO.Directory]::CreateDirectory($RunParent) | Out-Null
$ProposalRunRoot = "$RunParent\proposal-20260717-01"
$RehearsalRunRoot1 = "$RunParent\rehearsal-20260717-01"
$RehearsalRunRoot2 = "$RunParent\rehearsal-20260717-02"
$TargetMediaRoot1 = 'D:\FFXIVSharePrivate\MediaCopy-01'
$TargetMediaRoot2 = 'D:\FFXIVSharePrivate\MediaCopy-02'
$MediaSnapshotId = 'production-media-20260717'
```

先在旧应用环境使用 SQLite Backup API 的 `backup_database` 创建在线数据库备份三件套；不要直接复制活动数据库。记录来源主机、UTC 时点、应用版本、数据库摘要和操作员，并取得与该时点一致的离线媒体或存储快照。若跨数据库与媒体的一致性必须短暂冻结写入，应使用另行批准并记录的 R19 取样窗口；它不等于 R20 的最终停写或切换窗口。然后逐字节复制数据库三件套与媒体快照到演练机。媒体清单必须针对离线媒体源生成：

```powershell
& $Python -E -s -B -X utf8 `
  "$Repo\ops\migration\MediaManifest.py" build `
  --root $SourceMediaRoot `
  --output $SourceMediaManifest `
  --snapshot-id $MediaSnapshotId `
  --confirm-offline-snapshot
```

为两次演练分别制作完整媒体副本。复制完成后将它们离线冻结，演练期间不得有任何写入者。两个副本都必须与 `$SourceMediaManifest` 的相对路径、大小和 SHA-256 完全一致，使用私有 DACL，且不能位于任何 RunRoot 内。

## 1. 生成只读迁移提案

Proposal 阶段只检查数据库备份、媒体清单、当前 migration 状态和 `migrate --plan`，不会执行 `migrate`。`PolicyId` 和 `ProposalId` 必须在工单中唯一且可追溯。

```powershell
$PolicyId = 'r19-production-20260717'
$ProposalId = 'r19-production-20260717-p01'
$Bootstrap = "$Repo\ops\migration\ProductionCopyBootstrap.py"

& $Python -I -S -B -X utf8 $Bootstrap `
  --repository-root $Repo `
  --python-executable $Python `
  --run-root $ProposalRunRoot `
  --mode policy-proposal `
  --inner-entrypoint ops/migration/Propose-ProductionCopyPolicy.py `
  -- `
  --source-database $SourceDatabase `
  --source-checksum $SourceChecksum `
  --source-metadata $SourceMetadata `
  --source-media-manifest $SourceMediaManifest `
  --policy-id $PolicyId `
  --proposal-id $ProposalId `
  --confirm-source-immutable
if ($LASTEXITCODE -ne 0) { throw "Proposal failed: $LASTEXITCODE" }
```

成功后必须同时存在：

- `evidence\completion.json`，其中 `inner_exit_code=0`，且 `execution_bundle_unchanged`、`bootstrap_record_unchanged`、`bundle_manifest_unchanged`、`frozen_policy_unchanged`、`frozen_proposal_unchanged`、`frozen_review_unchanged` 都为 true；Proposal 模式没有后三类输入，它们为 true 不代表源数据已校验；
- `evidence\policy-proposal.json`，其中 `state=review_required`；
- `evidence\policy-proposal-body.json`；
- `evidence\events.jsonl`，终态为 `review_required`；
- body 引用的备份集验证、SQLite 独立检查、migration 状态、migration review plan、运行时指纹和媒体清单证据。源数据库三件套不变由这些 body 引用和 ledger 的 `source_final_verified` 事件证明；原媒体清单的末次 SHA/快照 ID 在 Proposal terminal 发布前单独复核，并在 Rehearsal 中再次按 Policy hash 复核。两者都不由 completion 的 bundle manifest 字段证明。

记录提案摘要：

```powershell
$Proposal = "$ProposalRunRoot\evidence\policy-proposal.json"
$ProposalCompletion = "$ProposalRunRoot\evidence\completion.json"
$ProposalLedger = "$ProposalRunRoot\evidence\events.jsonl"
$ProposalSha256 = (Get-FileHash -LiteralPath $Proposal -Algorithm SHA256).Hash.ToLowerInvariant()
Get-FileHash -Algorithm SHA256 -LiteralPath `
  $Proposal, $ProposalCompletion, $ProposalLedger
$ProposalTerminal = Get-Content -LiteralPath $ProposalLedger |
  Select-Object -Last 1 |
  ConvertFrom-Json
[pscustomobject]@{
  ProposalSha256 = $ProposalSha256
  LedgerHead = $ProposalTerminal.event_sha256
  TerminalStage = $ProposalTerminal.stage
}
```

## 2. 人工无损审阅

审阅人必须检查 Proposal body 引用的每一份证据，而不只看 `migrate --plan` 的文本。至少确认：

- 数据库三件套验证、SQLite `integrity_check`、`foreign_key_check`、表和 migration 清点全部通过；
- `source_leaf_nodes`、`target_leaf_nodes` 和 pending migration 节点符合预期；
- migration 的 Python/SQL 操作不会删除、截断、覆盖、合并或静默重写用户数据；
- 对字段缩窄、类型变化、唯一约束和数据迁移有逐记录保留证明；任何无法证明无损的操作都停止审批；
- `migration_plan_sha256`、`migration_runtime_sha256`、`runtime_fingerprint_sha256`、`execution_bundle_sha256` 均来自本次 Proposal；
- 源数据库、三件套和媒体清单在 Proposal 完成后仍与交接摘要一致；
- ledger 连续且终态明确要求审阅，`migration_applied=false`、`cutover_authorized=false`。

审阅结论和理由写入工单。若结论不是“无损”，不要生成 Review 或 Policy；先修改迁移设计，再以新 ProposalId 和全新 RunRoot 重跑。

## 3. 记录 Review 并批准 Policy

必须使用 Proposal 冻结代码中的审批工具，避免审批实现与提案 bundle 不一致。`Reviewer` 是操作员声明，不是密码学身份认证；外部工单仍应记录真实登录身份和复核人。

```powershell
$ApprovalTool = "$ProposalRunRoot\code\ops\migration\Approve-ProductionCopyPolicy.py"
$Review = "$ProposalRunRoot\approval\lossless-review.json"
$Policy = "$ProposalRunRoot\approval\approved-policy.json"
$Reviewer = 'DOMAIN.migration-reviewer'
$ReviewId = 'r19-production-20260717-review-01'
$ReviewNotes = '已逐项核对 Proposal 引用证据和全部 pending migrations，确认逐字段无损。'

& $Python -I -S -B -X utf8 $ApprovalTool record-review `
  --proposal $Proposal `
  --proposal-run-root $ProposalRunRoot `
  --expected-proposal-sha256 $ProposalSha256 `
  --review-id $ReviewId `
  --reviewer $Reviewer `
  --notes $ReviewNotes `
  --output $Review `
  --confirm-lossless-reviewed `
  --confirm-reviewer-operator-asserted
if ($LASTEXITCODE -ne 0) { throw "Review recording failed: $LASTEXITCODE" }

$ReviewSha256 = (Get-FileHash -LiteralPath $Review -Algorithm SHA256).Hash.ToLowerInvariant()

& $Python -I -S -B -X utf8 $ApprovalTool approve `
  --proposal $Proposal `
  --proposal-run-root $ProposalRunRoot `
  --expected-proposal-sha256 $ProposalSha256 `
  --review $Review `
  --expected-review-sha256 $ReviewSha256 `
  --reviewer $Reviewer `
  --output $Policy `
  --confirm-lossless-reviewed `
  --confirm-reviewer-operator-asserted
if ($LASTEXITCODE -ne 0) { throw "Policy approval failed: $LASTEXITCODE" }

$PolicySha256 = (Get-FileHash -LiteralPath $Policy -Algorithm SHA256).Hash.ToLowerInvariant()
$PolicySha256
```

Review 和 Policy 都使用 create-new 发布；目标已存在时工具会拒绝覆盖。不要通过重命名旧文件后再次审批来复用同一 RunRoot。

## 4. 执行两次独立离线演练

每次演练必须使用同一个已批准 Policy、Proposal、Review、源数据库三件套和源媒体清单，但使用全新的 Rehearsal RunRoot 与独立媒体副本。提案后精确 execution bundle 闭包或已指纹化的 Python 运行时闭包有任何改变时，Policy 会失效，应回到第 1 步；bundle 外的普通文档变化不影响 Policy。

```powershell
$InvokeRehearsal = "$Repo\ops\migration\Invoke-ProductionCopyRehearsal.ps1"

& $InvokeRehearsal `
  -RepositoryRoot $Repo `
  -PythonExecutable $Python `
  -SourceDatabase $SourceDatabase `
  -SourceChecksum $SourceChecksum `
  -SourceMetadata $SourceMetadata `
  -SourceUpgradePolicy $Policy `
  -SourcePolicyProposal $Proposal `
  -SourcePolicyReview $Review `
  -SourceProposalRunRoot $ProposalRunRoot `
  -SourceMediaManifest $SourceMediaManifest `
  -TargetMediaRoot $TargetMediaRoot1 `
  -TargetMediaSnapshotId $MediaSnapshotId `
  -RunRoot $RehearsalRunRoot1 `
  -ConfirmSourceImmutable `
  -ConfirmTargetMediaOffline
if ($LASTEXITCODE -ne 0) { throw "First rehearsal failed: $LASTEXITCODE" }

& $InvokeRehearsal `
  -RepositoryRoot $Repo `
  -PythonExecutable $Python `
  -SourceDatabase $SourceDatabase `
  -SourceChecksum $SourceChecksum `
  -SourceMetadata $SourceMetadata `
  -SourceUpgradePolicy $Policy `
  -SourcePolicyProposal $Proposal `
  -SourcePolicyReview $Review `
  -SourceProposalRunRoot $ProposalRunRoot `
  -SourceMediaManifest $SourceMediaManifest `
  -TargetMediaRoot $TargetMediaRoot2 `
  -TargetMediaSnapshotId $MediaSnapshotId `
  -RunRoot $RehearsalRunRoot2 `
  -ConfirmSourceImmutable `
  -ConfirmTargetMediaOffline
if ($LASTEXITCODE -ne 0) { throw "Second rehearsal failed: $LASTEXITCODE" }
```

## 5. 验收证据

两次 RunRoot 都必须独立满足以下条件：

- `evidence\completion.json` 的 `inner_exit_code=0`，冻结 policy/proposal/review、execution bundle、Bootstrap 和 manifest 均 unchanged；
- `evidence\result.json` 的 `status=completed`、`issues=[]`、`source_database_unchanged=true`、`cutover_authorized=false`；
- `evidence\target-import.json` 为 `status=imported`、`target_state=empty`（记录导入前状态），`evidence\target-import-idempotence.json` 为 `status=already_imported`、`target_state=complete`；
- `evidence\site-data-comparison.json` 和 `evidence\final-target-site-data-comparison.json` 证明源导出、目标导出和最终备份恢复后的再次导出逐实体等价；
- `evidence\restriction-preflight.json` 和 `evidence\final-target-restriction-preflight.json` 都没有 blocking error 或 manual review；
- preflight 中的 `ready_for_cutover=true` 只表示“内容限制数据”这一项检查已就绪，不是整体切换授权，绝不覆盖 result 与 deployment-candidate 的 `cutover_authorized=false`；正式停写、最终备份、冒烟和授权仍属于 R20；
- `evidence\target-backup-set.json`、`evidence\target-backup-inspection.json` 和 `evidence\target-backup-set-final.json` 证明目标数据库备份三件套通过初次、独立 SQLite 检查和最终复核；
- 目标媒体在数据库验证完成后被重新扫描，`artifacts\target-media-manifest-final.json` 与 `evidence\media-comparison-final.json` 仍证明它与源媒体清单一致；
- `evidence\runtime-fingerprint-initial.json` 在任何审批或业务子进程前建立全量内容指纹，`evidence\runtime-fingerprint-final.json` 在所有最终验证后再次执行全量内容哈希；pre/post migrate 等中间门禁只做绑定原始报告 SHA 的 metadata identity+closure checkpoint，必须明确记录 `content_rehashed=false`。它用于发现普通漂移，不证明抵抗同一受信任操作员刻意等长改写并恢复时间戳；
- `evidence\events.jsonl` 连续、终态为 `completed`，且 `deployment_candidate_verified` 早于终态；该事件仍包含 `cutover_authorized=false`；
- 两次运行的关键业务计数、实体摘要、源/目标导出比较结果和最终备份摘要一致。RunId、时间戳和独立目标备份文件摘要以外的差异都必须解释。

任何一项不满足都表示 R19 未通过。不要删除失败证据，也不要进入 R20。

把两次运行的 completion、result 和 ledger 字节摘要以及 ledger head 归档到工单：

```powershell
foreach ($RunRoot in @($RehearsalRunRoot1, $RehearsalRunRoot2)) {
  Get-FileHash -Algorithm SHA256 -LiteralPath `
    "$RunRoot\evidence\completion.json", `
    "$RunRoot\evidence\result.json", `
    "$RunRoot\evidence\events.jsonl"
  $Terminal = Get-Content -LiteralPath "$RunRoot\evidence\events.jsonl" |
    Select-Object -Last 1 |
    ConvertFrom-Json
  [pscustomobject]@{
    RunRoot = $RunRoot
    LedgerHead = $Terminal.event_sha256
    TerminalStage = $Terminal.stage
  }
}
```

## 6. 仓库侧自动化验证

这些检查不读取线上副本，也不使用浏览器：

```powershell
& "$Repo\ops\migration\Test-ProductionCopyBootstrap.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyPolicyApproval.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyPolicyProposal.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyRehearsal.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyEndToEnd.ps1" `
  -IncludeSlow `
  -RepositoryRoot $Repo `
  -PythonExecutable $Python `
  -RunParent $RunParent
```

带 `-IncludeSlow` 的 Proposal 测试会创建真实 SQLite 备份并执行完整只读提案。`Test-ProductionCopyEndToEnd.ps1 -IncludeSlow` 执行完整 Proposal→Review→Approval→两次 Rehearsal 离线测试；它耗时较长，只使用自己创建的唯一测试根，不应指向任何活动生产输入目录。

前四个快速合同的成功路径为了可重复测试会使用严格限域的 ACL 回调，不能单独证明目标机能够写入和复核生产 DACL。端到端脚本不使用该回调：它在指定的真实 RunParent 下调用生产 `run_bootstrap` 三次（一次 Proposal、两次 approved rehearsal），并使用冻结 Approval CLI；Bootstrap CLI 参数解析和 Invoke wrapper 另由快速合同覆盖。必须先用非敏感合成数据在计划使用的演练机与 RunParent 上让该命令完整通过，再放入生产副本；若 `SetFileSecurityW`、祖先链或 approval 输出 DACL 校验失败，不得绕过。

需要连同 Django、前端静态检查和其它运维合同一起执行时，可运行 `verify.ps1 -IncludeProductionCopyE2E`。该开关会显式启用耗时的真实离线端到端测试，但仍不读取生产输入，也不启动浏览器。

## R19 完成门禁

自动化测试通过只证明工具链和测试数据有效。只有使用可证明来源及时间点的线上不可变数据库备份和对应媒体快照完成上述两次演练、归档全部摘要与人工审阅记录后，R19 才能标记完成。在此之前，`document\REFACTORING_PLAN.md` 中的 R19 必须保持未完成状态，也不得进入 R20 正式切换。
