# 生产副本迁移演练手册

本文描述 R19 的离线演练流程。它只读取线上 SQLite 数据库的不可变备份三件套和离线媒体快照，不连接活动数据库、不停止或启动线上服务，也不授权切换。工具不会产生切换授权；terminal、result、import、site-data comparison 和 deployment-candidate 等 schema 在提供 `cutover_authorized` 字段时必须固定为 false，任何 schema 没有该字段也绝不表示获得授权。正式停写、最终备份和切换属于 R20。

“小抄儿”不在本轮演练范围内。它的现有路径和数据不得因为本手册中的操作被移动或改写。

## 安全边界

- 输入数据库必须由 `backup_database` 或生产仓库外的四文件捕获门禁生成，并连同同名 `.sha256`、`.metadata.json` 一起复制到离线介质；不得把活动 `db.sqlite3`、硬链接、带 `-wal`、`-shm` 或 `-journal` 的文件当作副本。旧部署缺少管理命令时使用 `ProductionCopyCaptureGate.py` 固定执行 `preflight`→`capture`，由门禁在同一进程内调用受审 `database_backup.py`；不得单独执行备份脚本，不得只信任生产机自报工具摘要，也不得为取样更新生产工作区。四个工具摘要必须由可信开发机或受控制品系统另行提供，并在两个阶段重复传入。
- 源数据库三件套、源媒体清单、源媒体目录和两个演练媒体副本在整个流程中保持封存。数据库三件套必须独占一个只含三个文件的目录；源媒体清单必须是位于另一目录的独立文件；五个外部范围彼此、与仓库及任何 RunRoot 都不得重叠。工具只把稳定复制后的数据库放进 RunRoot，再对私有副本执行检查和迁移。
- Proposal RunRoot、两个 Rehearsal RunRoot 必须是三个全新的本机 NTFS 目录。Bootstrap 会审查从直接父目录到卷根的完整祖先链：owner 必须可信，不可信主体不能拥有删除、修改 DACL/owner 或在直接父目录创建/继承可写子项的权限。优先使用当前用户的本机 LocalAppData 私有目录；禁止使用 UNC、映射盘、重解析点、共享临时目录、仓库目录、输入目录或媒体目录。
- RunRoot 创建后由 Bootstrap 收紧 ACL；`approval` 目录还会单独收紧。ACL 校验失败时不得通过放宽脚本或换到共享目录绕过。
- 整个 RunRoot、源输入目录、源媒体和两个 TargetMediaRoot 都含有生产用户数据，双轮报告也包含敏感生产证据；它们必须分别使用私有 DACL 并纳入保留、归档和安全销毁制度。RunRoot 期满后可整体安全销毁；源输入是演练权威副本时不得误删唯一备份，必须先按备份保留制度确认存在独立可恢复副本。
- 外部源输入和媒体目录的 DACL 由运维人员预先封存，但不再依赖人工 `Get-Acl` 文本作为主要证据。`ProductionCopyHandoff.py` 会使用只读 Win32 handle 自动验证五个范围、完整祖先链、owner、DACL、ACE、重解析点、硬链接和节点身份，并把逐节点清单及可复算摘要写入结构化 handoff；Proposal 开始和结束以及两次 Rehearsal 的前后门禁都会复核它。任何漂移都必须使用新 handoff、ProposalId 和 RunRoot 从头重跑。
- Proposal ledger 只绑定提案阶段的本地证据。Proposal completion、Review 和 Policy 随后通过精确字节 SHA-256 与字段互相绑定，再由 Rehearsal Bootstrap 冻结；ledger 是 `self_consistent_local_chain` 且 `tamper_proof=false`，不是签名或不可篡改存储。应把 proposal/review/policy/completion/result、双轮报告的 SHA-256 和 ledger head 另行归档到受控工单。
- 操作员属于受信任边界；本地指纹、DACL、handoff 和 ledger 用于发现普通并发漂移、误操作和其他未授权主体的写入，不构成对恶意同一操作员或进程内存篡改的防护，也不是原子文件系统快照。handoff 明确记录 `tamper_proof=false`、`continuous_acl_stability_proven=false`、`offline_process_state=operator_asserted` 和 `trusted_operator_can_override_acl=true`。尤其当当前用户启用了 Administrators 组权限时，Administrators 的 Full Control 仍可能让该受信任操作员有效写入；“封存”不等于抵抗同一管理员。
- 编排器不会主动请求线上服务访问，但 Django 启动、migration 和 `RunPython` 可以包含任意网络、文件或子进程副作用。工具目前不实施或证明网络隔离，也不使用 Windows Job Object 管理脱离进程或孙进程；证据会记录 `network_isolation_enforced=false` 或 `network_access_observation=not_measured`。演练机必须由外部防火墙隔离，人工审阅也必须检查所有 pending migration 的外部副作用，并拒绝会启动后台或脱离进程的 migration。任何终态之后，尤其发生中断时，必须先确认没有遗留进程仍在访问 RunRoot，并把确认结果单独记录到工单，才能归档、销毁现场，把候选物视为稳定，或使用新的 RunRoot 重跑。
- CLI 退出码约定为：0 成功；1 表示 Bootstrap、Proposal 或 Approval 拒绝/失败，也用于 Rehearsal 配置错误和普通执行失败；2 只表示 Rehearsal 已经发布 `blocked` 终态；130 表示中断。Bootstrap 和 Invoke 会原样传播已经启动的内层 CLI 退出码。任意非零、被 Ctrl+C 中断、缺少 completion/result，或证据校验失败时，保留现场用于诊断；即使中断后存在 completion/result，也不得向同一 RunRoot 补文件或续跑。确认原因后使用全新的 RunRoot 从头重跑。

## 0. 准备不可变输入

以下变量仅作示例。RunRoot 名称应包含工单号或时间戳，且目标目录必须尚不存在。

```powershell
$Repo = 'D:\Web\FFXIVShare'
$Python = (Resolve-Path "$Repo\venv\Scripts\python.exe").Path

$ProductionRepositoryRoot = 'C:\Users\Administrator\Desktop\srv\ff14_zsb'
$ProductionPython = Join-Path $ProductionRepositoryRoot 'venv\Scripts\python.exe'
$ProductionDatabase = Join-Path $ProductionRepositoryRoot 'db.sqlite3'
$ToolRoot = 'C:\FFXIVShare-R19\Tools'
$CaptureRoot = 'C:\FFXIVShare-R19\Capture\capture-20260718-01'
$CaptureAuditRoot = Join-Path $CaptureRoot 'Audit'
$CaptureDatabaseRoot = Join-Path $CaptureRoot 'Database'
$SourceDatabase = Join-Path $CaptureDatabaseRoot 'production.sqlite3'
$SourceChecksum = "$SourceDatabase.sha256"
$SourceMetadata = "$SourceDatabase.metadata.json"
$SourceMediaParent = 'C:\FFXIVShare-R19\Media\production-20260718-01'
$SourceMediaRoot = Join-Path $SourceMediaParent 'SourceMedia'
$SourceMediaManifest = Join-Path $SourceMediaParent 'Manifest\source-media-manifest.json'

$PrivateBase = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
$RunParent = Join-Path $PrivateBase 'FFXIVShare\R19\Runs'
[System.IO.Directory]::CreateDirectory($RunParent) | Out-Null
$ProposalRunRoot = "$RunParent\proposal-20260717-01"
$RehearsalRunRoot1 = "$RunParent\rehearsal-20260717-01"
$RehearsalRunRoot2 = "$RunParent\rehearsal-20260717-02"
$TargetMediaParent = 'D:\FFXIVShare-R19-Targets'
$TargetMediaRoot1 = Join-Path $TargetMediaParent 'MediaCopy-01'
$TargetMediaRoot2 = Join-Path $TargetMediaParent 'MediaCopy-02'
$SourceMediaSnapshotId = 'production-media-20260717'
$TargetMediaSnapshotId1 = 'production-media-20260717-copy-01'
$TargetMediaSnapshotId2 = 'production-media-20260717-copy-02'
$SourceHost = 'production-host.example'
$Operator = 'DOMAIN\migration-operator'
$ReleaseApplicationVersion = 'release-2026.07.17'

$HandoffParent = Join-Path $PrivateBase 'FFXIVShare\R19\Handoff-20260717-01'
if (Test-Path -LiteralPath $HandoffParent) { throw 'Handoff parent must be new.' }
[System.IO.Directory]::CreateDirectory($HandoffParent) | Out-Null
$SourceHandoffManifest = Join-Path $HandoffParent 'production-20260717-handoff.json'

$PairVerificationParent = Join-Path $PrivateBase 'FFXIVShare\R19\PairVerification-20260717-01'
if (Test-Path -LiteralPath $PairVerificationParent) {
  throw 'Pair-verification parent must be new.'
}
[System.IO.Directory]::CreateDirectory($PairVerificationParent) | Out-Null
$PairVerification = Join-Path $PairVerificationParent 'production-20260717-pair.json'
```

先在旧应用环境使用 SQLite Backup API 的 `backup_database` 创建在线数据库备份三件套；旧部署没有该命令时，使用下方四文件捕获门禁。两种入口生成完全相同的三件套契约；不要直接复制活动数据库，也不要更新旧部署来取得命令。记录来源主机、UTC 时点、应用版本、数据库摘要、四个捕获工具摘要和操作员，并取得与该时点一致的离线媒体或存储快照。若跨数据库与媒体的一致性必须短暂冻结写入，应使用另行批准并记录的 R19 取样窗口；它不等于 R20 的最终停写或切换窗口。然后逐字节复制数据库三件套与媒体快照到演练机。媒体清单必须针对离线媒体源生成：

为减少旧 Windows 服务器上的人工操作，受控制品可以把 `Invoke-LegacyProductionCapture.ps1` 放在包根目录，并把下文列出的四个受审文件单独放进同级 `Tools` 子目录。包装脚本内置四个可信摘要，创建新的私有 CaptureRoot，并固定执行同一组 `preflight`→`capture` 门禁；它不会导入 Django、更新旧仓库、修改源数据库、停止服务或授权切换。已知旧部署可以在 PowerShell 中执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
cd C:\FFXIVShare-R19-CaptureBundle
.\Invoke-LegacyProductionCapture.ps1 `
  -ApplicationVersion '244c32734e9fab5af05bf544a654615eeab31404' `
  -ConfirmReadOnlyOnlineSnapshot
```

CaptureBundle 和 CaptureParent 必须分别是盘符根目录的直接子目录，默认使用 `C:\FFXIVShare-R19-CaptureBundle` 与 `C:\FFXIVShare-R19-Capture`；这是完整祖先链 ACL 门禁的一部分，不能从桌面、下载目录或仓库内运行。默认源仓库为 `C:\Users\Administrator\Desktop\srv\ff14_zsb`，默认源数据库为其下的 `db.sqlite3`，默认 Python 为其 `venv\Scripts\python.exe`。若实际路径不同，显式传入 `-ProductionRepositoryRoot`、`-SourceDatabase` 或 `-ProductionPython`。每次执行会生成新的 UTC CaptureId；成功输出使用私有 DACL，但为了避免在生产机传播只读 ACL，它会在复制到离线演练机后才按下文封存。失败现场禁止补文件或续跑，改用新的 `-CaptureId` 从头执行。

```powershell
& $Python -E -s -B -X utf8 `
  "$Repo\ops\migration\MediaManifest.py" build `
  --root $SourceMediaRoot `
  --output $SourceMediaManifest `
  --snapshot-id $SourceMediaSnapshotId `
  --confirm-offline-snapshot
```

为两次演练分别制作完整媒体副本。复制完成后将它们离线冻结，演练期间不得有任何写入者。两个副本都必须与 `$SourceMediaManifest` 的相对路径、大小和 SHA-256 完全一致，使用私有 DACL，且不能位于任何 RunRoot 内。备份 metadata 的 `application_version` 必须是部署时设置的不可变 release 标识，并与 `$ReleaseApplicationVersion` 完全一致；`unknown`、空值和示例占位符都会被拒绝。

`$CaptureRoot`、`$SourceMediaParent` 和 `$TargetMediaParent` 必须是本次演练专用目录，复制完成后不得再放入其它文件。CaptureRoot 在捕获期间必须精确只含 `Audit` 与 `Database`；成功后前者精确只含两份捕获报告，后者精确只含数据库三件套。handoff 还会审查从每个 scope 的直接父目录到卷根的完整祖先链；如果卷根或更高层目录向不受信任主体授予删除、修改 DACL/owner 等路径控制权限，必须改用祖先链安全的专用本机 NTFS 路径，不能放宽工具检查。

以下示例把两个专用父目录连同全部后代逐节点设置成 protected DACL，因此五个外部 scope 及其直接父目录都会只允许当前用户读/遍历，SYSTEM 和本机 Administrators 保留 Full Control。它会改 ACL 和 owner，只能在确认路径是本次离线副本后执行；不要对活动站点目录、唯一备份、共用目录或“小抄儿”路径执行。handoff 工具自身不会修改这些 ACL，也不会写入探针。

```powershell
$CurrentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentSid = $CurrentIdentity.User.Value
$SealedSddl = "D:P(A;;GRGX;;;$CurrentSid)(A;;FA;;;S-1-5-18)(A;;FA;;;S-1-5-32-544)"
$PrivateOutputSddl = "D:P(A;OICI;FA;;;$CurrentSid)(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)"

function Set-ExactTreeDacl {
  param(
    [Parameter(Mandatory)][string]$LiteralPath,
    [Parameter(Mandatory)][string]$Sddl
  )
  $Root = Get-Item -LiteralPath $LiteralPath -Force
  $Items = @($Root)
  if ($Root.PSIsContainer) {
    $Items += @(Get-ChildItem -LiteralPath $LiteralPath -Force -Recurse)
  }
  $Items = $Items | Sort-Object { $_.FullName.Length } -Descending
  foreach ($Item in $Items) {
    $Acl = Get-Acl -LiteralPath $Item.FullName
    $Acl.SetOwner($CurrentIdentity.User)
    $Acl.SetSecurityDescriptorSddlForm(
      $Sddl,
      [System.Security.AccessControl.AccessControlSections]::Access
    )
    Set-Acl -LiteralPath $Item.FullName -AclObject $Acl
  }
}

Set-ExactTreeDacl -LiteralPath $ToolRoot -Sddl $SealedSddl
Set-ExactTreeDacl -LiteralPath $CaptureRoot -Sddl $PrivateOutputSddl
Set-ExactTreeDacl -LiteralPath $SourceMediaParent -Sddl $SealedSddl
Set-ExactTreeDacl -LiteralPath $TargetMediaParent -Sddl $SealedSddl
Set-ExactTreeDacl -LiteralPath $HandoffParent -Sddl $PrivateOutputSddl
Set-ExactTreeDacl -LiteralPath $PairVerificationParent -Sddl $PrivateOutputSddl
```

`$ToolRoot` 在设置 DACL 前必须精确只放入来自同一受审制品的四个文件：`ProductionCopyCaptureGate.py`、`ProductionCopyHandoff.py`、`Verify-SQLiteBackupSet.py` 和 `database_backup.py`。四个 expected 值不得在生产机上根据刚收到的文件自行生成；下面的占位符必须替换为可信开发机或受控制品系统提供的精确 SHA-256。CaptureRoot 必须预先新建，且只含使用 private protected DACL 的空 `Audit` 与空 `Database`。

```powershell
$Gate = Join-Path $ToolRoot 'ProductionCopyCaptureGate.py'
$HandoffCore = Join-Path $ToolRoot 'ProductionCopyHandoff.py'
$BackupVerifier = Join-Path $ToolRoot 'Verify-SQLiteBackupSet.py'
$BackupTool = Join-Path $ToolRoot 'database_backup.py'
$PreflightReport = Join-Path $CaptureAuditRoot 'capture-preflight.json'
$FinalReport = Join-Path $CaptureAuditRoot 'capture-final.json'
# 以下四值绑定受审工具提交 b1360d9a39a18509e417493925af8b7419b7ba1a。
$ExpectedGateSha256 = '7365ec6c941f95bc174f17aa47abd40001aae0bb49f6db2dbd93e8d357197f84'
$ExpectedHandoffSha256 = 'e5d08180b1aa39a3af77d615b8ef5b6b111b02f81e580356c3f96239acd221f6'
$ExpectedVerifierSha256 = 'b745188292a7dd34f277ed634e74e91d335b28cf8d53e1316b87a53fb11c5f02'
$ExpectedBackupToolSha256 = '965e4f9b7a5e497af8b5f16241e96e5c3a8d59852995f4abed957b07ea7b15aa'

& $ProductionPython -I -S -B -X utf8 $Gate preflight `
  --expected-gate-sha256 $ExpectedGateSha256 `
  --handoff-core $HandoffCore `
  --expected-handoff-core-sha256 $ExpectedHandoffSha256 `
  --backup-verifier $BackupVerifier `
  --expected-backup-verifier-sha256 $ExpectedVerifierSha256 `
  --backup-tool $BackupTool `
  --expected-backup-tool-sha256 $ExpectedBackupToolSha256 `
  --production-repository-root $ProductionRepositoryRoot `
  --source-database $ProductionDatabase `
  --output-database $SourceDatabase `
  --application-version $ReleaseApplicationVersion `
  --output-report $PreflightReport `
  --confirm-dedicated-new-empty-output-directory
if ($LASTEXITCODE -ne 0) { throw "Capture preflight failed: $LASTEXITCODE" }

$ExpectedPreflightSha256 = (
  Get-FileHash -LiteralPath $PreflightReport -Algorithm SHA256
).Hash.ToLowerInvariant()
& $ProductionPython -I -S -B -X utf8 $Gate capture `
  --expected-gate-sha256 $ExpectedGateSha256 `
  --expected-handoff-core-sha256 $ExpectedHandoffSha256 `
  --expected-backup-verifier-sha256 $ExpectedVerifierSha256 `
  --expected-backup-tool-sha256 $ExpectedBackupToolSha256 `
  --preflight-report $PreflightReport `
  --expected-preflight-sha256 $ExpectedPreflightSha256 `
  --output-report $FinalReport
if ($LASTEXITCODE -ne 0) { throw "Capture failed: $LASTEXITCODE" }

$CaptureEvidence = Get-Content -LiteralPath $FinalReport -Raw | ConvertFrom-Json
if (
  $CaptureEvidence.phase -ne 'capture' -or
  $CaptureEvidence.capture_set_complete -ne $true -or
  $CaptureEvidence.backup_set_contract_verified -ne $true -or
  $CaptureEvidence.cutover_authorized -ne $false
) {
  throw 'Capture final report did not publish a complete non-cutover result.'
}
Get-FileHash -Algorithm SHA256 -LiteralPath $PreflightReport, $FinalReport
```

preflight 与 capture 必须使用同一个 Python/SQLite 运行时；门禁会绑定版本和 `-I -S -B -X utf8` 标志。活动库允许继续写入，门禁冻结和复核的是源路径身份而不是全过程内容；SQLite Backup API 生成一致备份，但捕获报告不证明数据库与媒体同窗一致。final 报告也不独立重跑 metadata 声称的 integrity/foreign-key PRAGMA，后续 Proposal/Inspector 仍必须完成独立检查。任意失败或中断都要隔离并保留整个 CaptureRoot，换全新根从 preflight 重跑，禁止补文件或复用部分输出。

捕获完成后，对 `$CaptureDatabaseRoot` 设置 `$SealedSddl`，再创建结构化 handoff。输出使用 create-new 语义，已存在时拒绝覆盖；创建过程会重复校验数据库三件套、源媒体和两个目标媒体副本，并自动归档五个范围及祖先链的路径身份、owner、DACL 和 ACE 投影。handoff 不会自动绑定两份捕获报告，因此必须把报告原件及 SHA-256 另行附到同一受控工单。

```powershell
Set-ExactTreeDacl -LiteralPath $CaptureDatabaseRoot -Sddl $SealedSddl
```

```powershell
$HandoffTool = "$Repo\ops\migration\ProductionCopyHandoff.py"

& $Python -I -S -B -X utf8 $HandoffTool create `
  --repository-root $Repo `
  --source-database $SourceDatabase `
  --source-checksum $SourceChecksum `
  --source-metadata $SourceMetadata `
  --source-media-root $SourceMediaRoot `
  --source-media-manifest $SourceMediaManifest `
  --target-media-root-one $TargetMediaRoot1 `
  --target-media-root-one-snapshot-id $TargetMediaSnapshotId1 `
  --target-media-root-two $TargetMediaRoot2 `
  --target-media-root-two-snapshot-id $TargetMediaSnapshotId2 `
  --source-host $SourceHost `
  --operator $Operator `
  --expected-application-version $ReleaseApplicationVersion `
  --output $SourceHandoffManifest `
  --confirm-source-immutable `
  --confirm-target-media-offline `
  --confirm-database-media-consistent `
  --confirm-operator-identity-asserted
if ($LASTEXITCODE -ne 0) { throw "Handoff creation failed: $LASTEXITCODE" }

& $Python -I -S -B -X utf8 $HandoffTool verify `
  --handoff $SourceHandoffManifest `
  --repository-root $Repo `
  --check-live
if ($LASTEXITCODE -ne 0) { throw "Handoff live verification failed: $LASTEXITCODE" }
```

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
  --source-handoff-manifest $SourceHandoffManifest `
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
- `artifacts\source-handoff-manifest.json`，其 size/SHA-256 与外部 handoff 完全一致并进入 Proposal v2 的十项 evidence；
- body 引用的备份集验证、SQLite 独立检查、migration 状态、migration review plan、运行时指纹和媒体清单证据。ledger 必须依次包含唯一的 `source_handoff_verified`、`policy_proposal_body_created`、`source_handoff_final_verified`、`source_final_verified`、`execution_bundle_final_verified` 和 terminal；终检会重新读取数据库三件套、源媒体、两个目标媒体副本及五范围访问快照。两者都不由 completion 的 bundle manifest 字段证明。
- `evidence\source-inspection.json` 除 migration 外还冻结规范化 `sqlite_schema`、`table_structures` 和完整 `sqlite_sequence` 清点；`table_structures` 包含列、外键与唯一约束，并具有可复算 SHA-256；
- Approval 会从 Proposal 冻结的 inspection artifact 独立校验上述清点的精确形状、摘要、表集合和 SQLite ASCII 标识符规则，不能只信任 Inspector 的通过声明。`table_structures` 通过 inspection artifact 进入审批证据，不是 Policy 顶层字段。

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
- 允许变化的六张表 `shares_announcement`、`shares_collectionitem`、`shares_report`、`shares_share`、`shares_sharelog`、`shares_userprofile` 必须分别匹配冻结代码中的精确 source/destination SQL SHA-256 对；其中公告表只规范化 `content` 的物理列顺序并保护 AUTOINCREMENT 高水位。只允许一个精确旧唯一索引移除、20 个精确对象新增，以及 `shares_report.reporter_id`、`shares_sharelog.user_id` 两个精确 `notnull 1→0` 变化；
- 所有声明例外必须在升级源库和最终目标中分别被完整消费；任何未消费例外、额外对象、未知 SQL 或其它列属性变化都阻断。即使哈希门禁通过，审阅人仍须逐项记录六张表 SQL 转换为何无损；
- `migration_plan_sha256`、`migration_runtime_sha256`、`runtime_fingerprint_sha256`、`execution_bundle_sha256` 均来自本次 Proposal；
- handoff 中的来源主机、UTC、操作员、release application version、数据库三件套、源媒体、两个目标副本和五范围逐节点 DACL/owner 清单符合本次受控工单；
- 源数据库、三件套、媒体和 ACL 在 Proposal 完成后仍与 handoff 一致，`source_handoff_final_verified.content_verified=true`；
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

每次演练必须使用同一个已批准 Policy、Proposal、Review、源数据库三件套和源媒体清单，但使用全新的 Rehearsal RunRoot 与 handoff 中对应 slot 的独立媒体副本。Rehearsal 不接受新的 handoff CLI 参数，而是只从已批准 Proposal RunRoot 的 `artifacts\source-handoff-manifest.json` 读取被冻结且由 Policy 间接绑定的副本；这避免在审批后替换交接权威。提案后精确 execution bundle 闭包或已指纹化的 Python 运行时闭包有任何改变时，Policy 会失效，应回到第 1 步；bundle 外的普通文档变化不影响 Policy。

每轮会依次验证原始 `evidence\source-inspection.json`，在 migration 后生成 `evidence\upgraded-source-inspection.json`，再从最终目标备份生成 `evidence\target-backup-inspection.json`，最后以 create-new 语义发布 `evidence\database-structure-preservation.json`。只有结构报告同时满足 `preserved=true`、`issues=[]`，ledger 才会记录通过的 `database_structure_preserved` 并继续生成 deployment candidate。

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
  -TargetMediaSnapshotId $TargetMediaSnapshotId1 `
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
  -TargetMediaSnapshotId $TargetMediaSnapshotId2 `
  -RunRoot $RehearsalRunRoot2 `
  -ConfirmSourceImmutable `
  -ConfirmTargetMediaOffline
if ($LASTEXITCODE -ne 0) { throw "Second rehearsal failed: $LASTEXITCODE" }
```

两次演练完成后，必须使用第一轮 RunRoot 中被冻结的 verifier 生成一份 create-new 双轮验证报告；不得改用审批后工作区中的同名脚本。该报告重新绑定 Proposal、Review、Policy 和两轮 RunRoot，校验每轮完成门禁，并把业务语义一致性、允许差异及未解释差异汇总为可归档 JSON。

```powershell
$PairVerifier = Join-Path `
  $RehearsalRunRoot1 `
  'code\ops\migration\Verify-ProductionCopyRehearsalPair.py'

& $Python -I -S -B -X utf8 $PairVerifier `
  --first-run-root $RehearsalRunRoot1 `
  --second-run-root $RehearsalRunRoot2 `
  --proposal-run-root $ProposalRunRoot `
  --policy $Policy `
  --proposal $Proposal `
  --review $Review `
  --expected-policy-sha256 $PolicySha256 `
  --expected-proposal-sha256 $ProposalSha256 `
  --expected-review-sha256 $ReviewSha256 `
  --output $PairVerification
if ($LASTEXITCODE -ne 0) { throw "Pair verification failed: $LASTEXITCODE" }

$PairReport = Get-Content -LiteralPath $PairVerification -Raw | ConvertFrom-Json
if (
  $PairReport.status -ne 'verified' -or
  $PairReport.comparison.matched -ne $true -or
  $PairReport.comparison.issues.Count -ne 0 -or
  $PairReport.comparison.unexplained_differences.Count -ne 0 -or
  $PairReport.verification -ne 'self_consistent_local_chain' -or
  $PairReport.tamper_proof -ne $false -or
  $PairReport.contains_production_user_data -ne $true -or
  $PairReport.retained_on_success -ne $true -or
  $PairReport.secure_disposal_required -ne $true -or
  $PairReport.live_handoff_final_verification.content_reverified -ne $true -or
  $PairReport.live_handoff_final_verification.access_baseline_matches_approved_handoff -ne $true -or
  $PairReport.cutover_authorized -ne $false
) {
  throw 'Pair verification report did not publish a verified non-cutover result.'
}
```

## 5. 验收证据

两次 RunRoot 都必须独立满足以下条件：

- `evidence\completion.json` 的 `inner_exit_code=0`，冻结 policy/proposal/review、execution bundle、Bootstrap 和 manifest 均 unchanged；
- `evidence\result.json` 的 `status=completed`、`issues=[]`、`source_database_unchanged=true`、`cutover_authorized=false`；
- `evidence\external-handoff-preflight.json` 和 `evidence\external-handoff-final.json` 均绑定同一 handoff SHA-256 和五个 scope，对应 ledger event 的 `details.active_target_slot` 绑定本次目标副本；final 位于最后一次外部内容读取之后，且访问快照与 preflight 完全一致；
- `evidence\target-import.json` 为 `status=imported`、`target_state=empty`（记录导入前状态），`evidence\target-import-idempotence.json` 为 `status=already_imported`、`target_state=complete`；
- `evidence\site-data-comparison.json` 和 `evidence\final-target-site-data-comparison.json` 证明源导出、目标导出和最终备份恢复后的再次导出逐实体等价；
- `evidence\restriction-preflight.json` 和 `evidence\final-target-restriction-preflight.json` 都具有完整计数、状态分布与 manual-review 结构，没有 blocking error 或待人工处理项，且去除生成时间后的语义投影完全一致；
- preflight 中的 `ready_for_cutover=true` 只表示“内容限制数据”这一项检查已就绪，不是整体切换授权，绝不覆盖 result 与 deployment-candidate 的 `cutover_authorized=false`；正式停写、最终备份、冒烟和授权仍属于 R20；
- `evidence\target-backup-set.json`、`evidence\target-backup-inspection.json` 和 `evidence\target-backup-set-final.json` 证明目标数据库备份三件套通过初次、独立 SQLite 检查和最终复核；双轮 verifier 还会重新绑定 database/checksum/metadata，并要求四个 backup checks 全为 true；
- `evidence\source-inspection.json`、`evidence\upgraded-source-inspection.json`、`evidence\target-backup-inspection.json` 和 `evidence\database-structure-preservation.json` 必须被 ledger 与 deployment candidate 精确绑定，且结构报告为 `preserved=true`、`issues=[]`、`cross_destination_schema_equal=true`。门禁检查 `sqlite_schema` 的对象身份和精确 SQL、列的类型/`notnull`/默认值/主键序号/hidden、已有外键语义和唯一约束语义；普通列 `cid` 变化只作诊断，不豁免其它属性。唯一约束保留列顺序、名称、降序、collation、partial，并区分普通列、rowid 与表达式；升级源库与最终目标的 `sqlite_schema` 和 `table_structures` 必须精确相等；
- 升级源库必须保留全部原始 `sqlite_sequence` 下限。最终目标对 11 张 v3 直接可移植实体表使用 `max(original source, upgraded source)` 作为有效下限：`auth_group`、`auth_user`、`shares_userprofile`、`shares_share`、`shares_collection`、`shares_collectionitem`、`shares_report`、`shares_sharelog`、`shares_announcement`、`shares_sitemessage`、`django_admin_log`。高水位保持相等完全合法，不要求 migration 插入记录后必然增加；
- 最终目标报告必须逐项记录未进入上述范围的已观察序列及原因：内嵌桥接表代理 ID 在导入时重建，`auth_permission`、`django_content_type`、`django_migrations` 属于框架元数据重建，`django_session` 有意清空以强制重新登录，其它表明确标记为不属于 v3 直接可移植实体序列范围；
- 目标媒体在数据库验证完成后被重新扫描，`artifacts\target-media-manifest-final.json` 与 `evidence\media-comparison-final.json` 仍证明它与源媒体清单一致；
- `evidence\runtime-fingerprint-initial.json` 在任何审批或业务子进程前建立全量内容指纹，`evidence\runtime-fingerprint-final.json` 在最后一个业务子进程结束后再次执行全量内容哈希，随后仍须通过 source final、external handoff final 和 deployment candidate 门禁；pre/post migrate 等中间门禁只做绑定原始报告 SHA 的 metadata identity+closure checkpoint，必须明确记录 `content_rehashed=false`。指纹覆盖解释器、基础标准库、实际 `sys.path` 导入闭包和 venv 自身 `site-packages`；同一信任根下相互包含的全递归根会归一为不重叠物理根，closure 条目必须全局唯一，closure 文件必须属于 identity inventory，未落入 closure 的少量独立 runtime 文件仍会逐组件单独复验。quick checkpoint 会在一次不重叠遍历中同时核对闭包和文件身份；full producer 在输出 `fsync` 后也会终检闭包目录身份及全部已哈希文件，晚到条目会失败关闭并清理本次输出。两者均不使用跨运行内容缓存，也不减少 initial/final 的全量内容哈希。基础 Python 中未进入 `sys.path` 的全局 `purelib/platlib` 内容会作为 `excluded_inactive_site_package_roots` 明示排除，若该根或其子路径实际进入 `sys.path` 则直接阻断。它用于发现普通漂移，不证明抵抗同一受信任操作员刻意等长改写并恢复时间戳；
- `evidence\events.jsonl` 连续、终态为 `completed`，且 `deployment_candidate_verified` 早于终态；该事件仍包含 `cutover_authorized=false`；
- 两次运行的关键业务计数、实体摘要、源/目标导出比较结果和最终备份摘要一致。RunId、时间戳和独立目标备份文件摘要以外的差异都必须解释。
- 双轮报告的 `status=verified`、`comparison.matched=true`、`comparison.issues=[]`、`comparison.unexplained_differences=[]`、`cutover_authorized=false`；`authority` 必须绑定本次 Proposal/Review/Policy，`runs.first` 和 `runs.second` 必须分别绑定两轮 completion、result、ledger 及 ledger head。verifier 会重新读取三份 inspection、独立复算结构投影并验证结构报告、ledger 和 deployment candidate，而不信任单轮的通过声明；`comparison.matched_projections.database_structure_preservation_sha256` 必须与 `authority.database_structure_preservation_sha256` 一致并进入跨轮语义比较。verifier 还会在两轮结束后再次复核 live handoff 的内容与访问基线；`comparison.allowed_differences` 中每一项都必须在 `allowed_difference_values` 逐值归档，不能用于豁免业务语义或审批权威漂移。报告本身明确标记含生产证据、成功后保留并要求安全销毁。

任何一项不满足都表示 R19 未通过。不要删除失败证据，也不要进入 R20。

把两次运行的 completion、result 和 ledger 字节摘要以及 ledger head 归档到工单：

```powershell
foreach ($RunRoot in @($RehearsalRunRoot1, $RehearsalRunRoot2)) {
  Get-FileHash -Algorithm SHA256 -LiteralPath `
    "$RunRoot\evidence\completion.json", `
    "$RunRoot\evidence\result.json", `
    "$RunRoot\evidence\events.jsonl", `
    "$RunRoot\evidence\source-inspection.json", `
    "$RunRoot\evidence\upgraded-source-inspection.json", `
    "$RunRoot\evidence\target-backup-inspection.json", `
    "$RunRoot\evidence\database-structure-preservation.json"
  $Terminal = Get-Content -LiteralPath "$RunRoot\evidence\events.jsonl" |
    Select-Object -Last 1 |
    ConvertFrom-Json
  [pscustomobject]@{
    RunRoot = $RunRoot
    LedgerHead = $Terminal.event_sha256
    TerminalStage = $Terminal.stage
  }
}

Get-FileHash -Algorithm SHA256 -LiteralPath $PairVerification
```

工单必须附上完整双轮验证报告及其 SHA-256，而不只是控制台中的“通过”文字；同时保留两轮 RunRoot，才能复算报告内的 artifact 引用。双轮报告验证的仍是 `self_consistent_local_chain` 本地证据，不是签名、不可篡改存储或 R20 切换授权。归档时还要附上人工无损审阅记录，以及每轮终态后“没有遗留进程访问 RunRoot”的外部确认。

## 6. 仓库侧自动化验证

这些检查不读取线上副本，也不使用浏览器：

```powershell
& "$Repo\ops\migration\Test-ProductionCopyCaptureGate.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-SQLiteBackupSet.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyHandoff.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyBootstrap.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyPolicyApproval.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyPolicyProposal.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyRehearsal.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyRehearsalPairVerifier.ps1" -RepositoryRoot $Repo -PythonExecutable $Python
& "$Repo\ops\migration\Test-ProductionCopyEndToEnd.ps1" `
  -IncludeSlow `
  -RepositoryRoot $Repo `
  -PythonExecutable $Python `
  -RunParent $RunParent
```

带 `-IncludeSlow` 的 Proposal 测试会创建真实 SQLite 备份并执行完整只读提案。`Test-ProductionCopyEndToEnd.ps1 -IncludeSlow` 执行完整 Proposal→Review→Approval→两次 Rehearsal 离线测试；它耗时较长，只使用自己创建的唯一测试根，不应指向任何活动生产输入目录。

E2E 源库从空库严格前向构建到 `shares/0018`，并断言 `django_migrations` ID 连续且 `sqlite_sequence` 等于最大 ID，防止重新引入“先迁到最新再回滚”的伪源库。测试还对五张会重建的表及一张不重建的控制表注入显著高于 `MAX(id)` 的序列高水位，贯穿源备份、Proposal、升级源、目标、最终备份和 Pair 验证。当前性能基线及三类语义摘要以 `REFACTORING_PLAN.md` 的 R19 状态为唯一事实源。

成功时端到端脚本还会输出单行 `PRODUCTION_COPY_E2E_TIMING_JSON=...`，分别记录 Proposal、Review/Approval、两轮 rehearsal、双轮 verifier 和总耗时。该数据明确标记为 `diagnostic_only=true`，只用于发现性能回归，不参与任何证据或通过判定。

handoff 合同和端到端脚本使用真实 NTFS/DACL；其余快速合同的部分成功路径为了可重复测试会使用严格限域的 ACL 回调，不能单独证明目标机能够写入和复核生产 DACL。端到端脚本在指定的真实 RunParent 下调用生产 `run_bootstrap` 三次（一次 Proposal、两次 approved rehearsal），并使用冻结 Approval CLI；Bootstrap CLI 参数解析和 Invoke wrapper 另由快速合同覆盖。必须先用非敏感合成数据在计划使用的演练机与 RunParent 上让这些命令完整通过，再放入生产副本；若 `SetFileSecurityW`、祖先链、handoff 或 approval 输出 DACL 校验失败，不得绕过。

需要连同 Django、前端静态检查和其它运维合同一起执行时，可运行 `verify.ps1 -Profile Release`。该档位会显式启用耗时的真实离线端到端测试，但仍不读取生产输入，也不启动浏览器。旧的 `-IncludeProductionCopyE2E` 参数仍作为等价兼容入口保留。

## R19 完成门禁

自动化测试通过只证明工具链和测试数据有效。只有使用可证明来源及时间点的线上不可变数据库备份和对应媒体快照完成上述两次演练，归档捕获 preflight/final、handoff、Proposal/Review/Policy、双轮报告的原件与摘要以及人工审阅记录后，R19 才能标记完成。在此之前，`document\REFACTORING_PLAN.md` 中的 R19 必须保持未完成状态，也不得进入 R20 正式切换。
