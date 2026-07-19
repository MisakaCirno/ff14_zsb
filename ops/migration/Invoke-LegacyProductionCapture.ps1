[CmdletBinding()]
param(
    [string]$ProductionRepositoryRoot = 'C:\Users\Administrator\Desktop\srv\ff14_zsb',
    [string]$ProductionPython = '',
    [string]$SourceDatabase = '',
    [string]$CaptureParent = 'C:\FFXIVShare-R19-Capture',
    [string]$CaptureId = '',
    [Parameter(Mandatory = $true)][string]$ApplicationVersion,
    [Parameter(Mandatory = $true)][switch]$ConfirmReadOnlyOnlineSnapshot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($MyInvocation.InvocationName -eq '.') {
    throw 'Invoke-LegacyProductionCapture.ps1 is a top-level CLI and cannot be dot-sourced.'
}
if ($env:OS -ne 'Windows_NT') {
    throw 'Legacy production capture requires Windows NTFS and DACL APIs.'
}
if (-not $ConfirmReadOnlyOnlineSnapshot.IsPresent) {
    throw 'ConfirmReadOnlyOnlineSnapshot must be explicitly affirmed.'
}

$ProductionRepositoryRoot = (Resolve-Path -LiteralPath $ProductionRepositoryRoot).Path
if ([string]::IsNullOrWhiteSpace($ProductionPython)) {
    $ProductionPython = Join-Path $ProductionRepositoryRoot 'venv\Scripts\python.exe'
}
$ProductionPython = (Resolve-Path -LiteralPath $ProductionPython).Path
if ([string]::IsNullOrWhiteSpace($SourceDatabase)) {
    $SourceDatabase = Join-Path $ProductionRepositoryRoot 'db.sqlite3'
}
$SourceDatabase = (Resolve-Path -LiteralPath $SourceDatabase).Path
$BundleRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$BundleParent = [System.IO.Path]::GetDirectoryName($BundleRoot).TrimEnd('\', '/')
$BundleVolumeRoot = [System.IO.Path]::GetPathRoot($BundleRoot).TrimEnd('\', '/')
if (-not $BundleParent.Equals($BundleVolumeRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CaptureBundle must be extracted directly below a local drive root, for example C:\FFXIVShare-R19-CaptureBundle.'
}
$ToolRoot = (Resolve-Path -LiteralPath (Join-Path $BundleRoot 'Tools')).Path
$BundleMembers = @(Get-ChildItem -LiteralPath $BundleRoot -Force | Sort-Object Name)
$ExpectedBundleNames = @('Invoke-LegacyProductionCapture.ps1', 'Tools') | Sort-Object
$ActualBundleNames = @($BundleMembers | ForEach-Object { $_.Name })
if (($ActualBundleNames -join "`n") -cne ($ExpectedBundleNames -join "`n")) {
    throw 'CaptureBundle must contain exactly Invoke-LegacyProductionCapture.ps1 and Tools.'
}

if ([string]::IsNullOrWhiteSpace($CaptureId)) {
    $CaptureId = 'capture-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')
}
if ($CaptureId -notmatch '^capture-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
    throw 'CaptureId must start with capture- and contain only letters, digits, dot, underscore or dash.'
}
if ([string]::IsNullOrWhiteSpace($ApplicationVersion)) {
    throw 'ApplicationVersion must be the immutable deployed release identifier.'
}

$ExpectedToolHashes = [ordered]@{
    'ProductionCopyCaptureGate.py' = '7365ec6c941f95bc174f17aa47abd40001aae0bb49f6db2dbd93e8d357197f84'
    'ProductionCopyHandoff.py' = 'e5d08180b1aa39a3af77d615b8ef5b6b111b02f81e580356c3f96239acd221f6'
    'Verify-SQLiteBackupSet.py' = 'b745188292a7dd34f277ed634e74e91d335b28cf8d53e1316b87a53fb11c5f02'
    'database_backup.py' = '965e4f9b7a5e497af8b5f16241e96e5c3a8d59852995f4abed957b07ea7b15aa'
}

$ToolMembers = @(Get-ChildItem -LiteralPath $ToolRoot -Force | Sort-Object Name)
$ExpectedNames = @($ExpectedToolHashes.Keys | Sort-Object)
$ActualNames = @($ToolMembers | ForEach-Object { $_.Name })
if (($ActualNames -join "`n") -cne ($ExpectedNames -join "`n")) {
    throw 'Tools must contain exactly the four reviewed capture files.'
}
foreach ($Name in $ExpectedToolHashes.Keys) {
    $Path = Join-Path $ToolRoot $Name
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Reviewed capture tool is missing: $Name"
    }
    $ActualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -cne $ExpectedToolHashes[$Name]) {
        throw "Reviewed capture tool SHA-256 mismatch: $Name"
    }
}

$CaptureParent = [System.IO.Path]::GetFullPath($CaptureParent).TrimEnd('\', '/')
$CaptureParentDirectory = [System.IO.Path]::GetDirectoryName($CaptureParent).TrimEnd('\', '/')
$CaptureVolumeRoot = [System.IO.Path]::GetPathRoot($CaptureParent).TrimEnd('\', '/')
if (-not $CaptureParentDirectory.Equals($CaptureVolumeRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'CaptureParent must be directly below a local drive root, for example C:\FFXIVShare-R19-Capture.'
}
[System.IO.Directory]::CreateDirectory($CaptureParent) | Out-Null
$CaptureParent = (Resolve-Path -LiteralPath $CaptureParent).Path
$CaptureRoot = Join-Path $CaptureParent $CaptureId
if (Test-Path -LiteralPath $CaptureRoot) {
    throw "CaptureRoot must be new: $CaptureRoot"
}
[System.IO.Directory]::CreateDirectory($CaptureRoot) | Out-Null
$AuditRoot = Join-Path $CaptureRoot 'Audit'
$DatabaseRoot = Join-Path $CaptureRoot 'Database'
[System.IO.Directory]::CreateDirectory($AuditRoot) | Out-Null
[System.IO.Directory]::CreateDirectory($DatabaseRoot) | Out-Null

$CurrentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentSid = $CurrentIdentity.User.Value
$SealedSddl = "D:P(A;;GRGX;;;$CurrentSid)(A;;FA;;;S-1-5-18)(A;;FA;;;S-1-5-32-544)"
$PrivateOutputSddl = "D:P(A;OICI;FA;;;$CurrentSid)(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)"

function Set-ExactTreeDacl {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Sddl
    )
    $Root = Get-Item -LiteralPath $LiteralPath -Force
    $Items = @($Root)
    if ($Root.PSIsContainer) {
        $Items += @(Get-ChildItem -LiteralPath $LiteralPath -Force -Recurse)
    }
    $Items = @($Items | Sort-Object { $_.FullName.Length } -Descending)
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

function Set-ExactDacl {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$Sddl
    )
    $Item = Get-Item -LiteralPath $LiteralPath -Force
    $Acl = Get-Acl -LiteralPath $Item.FullName
    $Acl.SetOwner($CurrentIdentity.User)
    $Acl.SetSecurityDescriptorSddlForm(
        $Sddl,
        [System.Security.AccessControl.AccessControlSections]::Access
    )
    Set-Acl -LiteralPath $Item.FullName -AclObject $Acl
}

Set-ExactTreeDacl -LiteralPath $BundleRoot -Sddl $SealedSddl
Set-ExactDacl -LiteralPath $CaptureParent -Sddl $PrivateOutputSddl
Set-ExactTreeDacl -LiteralPath $CaptureRoot -Sddl $PrivateOutputSddl

$Gate = Join-Path $ToolRoot 'ProductionCopyCaptureGate.py'
$HandoffCore = Join-Path $ToolRoot 'ProductionCopyHandoff.py'
$BackupVerifier = Join-Path $ToolRoot 'Verify-SQLiteBackupSet.py'
$BackupTool = Join-Path $ToolRoot 'database_backup.py'
$OutputDatabase = Join-Path $DatabaseRoot 'production.sqlite3'
$PreflightReport = Join-Path $AuditRoot 'capture-preflight.json'
$FinalReport = Join-Path $AuditRoot 'capture-final.json'

$PreflightArguments = @(
    '-I', '-S', '-B', '-X', 'utf8', $Gate, 'preflight',
    '--expected-gate-sha256', $ExpectedToolHashes['ProductionCopyCaptureGate.py'],
    '--handoff-core', $HandoffCore,
    '--expected-handoff-core-sha256', $ExpectedToolHashes['ProductionCopyHandoff.py'],
    '--backup-verifier', $BackupVerifier,
    '--expected-backup-verifier-sha256', $ExpectedToolHashes['Verify-SQLiteBackupSet.py'],
    '--backup-tool', $BackupTool,
    '--expected-backup-tool-sha256', $ExpectedToolHashes['database_backup.py'],
    '--production-repository-root', $ProductionRepositoryRoot,
    '--source-database', $SourceDatabase,
    '--output-database', $OutputDatabase,
    '--application-version', $ApplicationVersion,
    '--output-report', $PreflightReport,
    '--confirm-dedicated-new-empty-output-directory'
)
& $ProductionPython @PreflightArguments
if ($LASTEXITCODE -ne 0) {
    throw "Capture preflight failed with exit code $LASTEXITCODE. Preserve and quarantine $CaptureRoot."
}

$ExpectedPreflightSha256 = (
    Get-FileHash -LiteralPath $PreflightReport -Algorithm SHA256
).Hash.ToLowerInvariant()
$CaptureArguments = @(
    '-I', '-S', '-B', '-X', 'utf8', $Gate, 'capture',
    '--expected-gate-sha256', $ExpectedToolHashes['ProductionCopyCaptureGate.py'],
    '--expected-handoff-core-sha256', $ExpectedToolHashes['ProductionCopyHandoff.py'],
    '--expected-backup-verifier-sha256', $ExpectedToolHashes['Verify-SQLiteBackupSet.py'],
    '--expected-backup-tool-sha256', $ExpectedToolHashes['database_backup.py'],
    '--preflight-report', $PreflightReport,
    '--expected-preflight-sha256', $ExpectedPreflightSha256,
    '--output-report', $FinalReport
)
& $ProductionPython @CaptureArguments
if ($LASTEXITCODE -ne 0) {
    throw "Capture failed with exit code $LASTEXITCODE. Preserve and quarantine $CaptureRoot."
}

$CaptureEvidence = Get-Content -LiteralPath $FinalReport -Raw -Encoding utf8 | ConvertFrom-Json
if (
    $CaptureEvidence.phase -ne 'capture' -or
    $CaptureEvidence.capture_set_complete -ne $true -or
    $CaptureEvidence.backup_set_contract_verified -ne $true -or
    $CaptureEvidence.cutover_authorized -ne $false
) {
    throw "Capture report contract failed. Preserve and quarantine $CaptureRoot."
}
$Summary = [ordered]@{
    status = 'captured'
    source_database = $SourceDatabase
    application_version = $ApplicationVersion
    capture_root = $CaptureRoot
    database = $OutputDatabase
    database_sha256 = (Get-FileHash -LiteralPath $OutputDatabase -Algorithm SHA256).Hash.ToLowerInvariant()
    checksum = "$OutputDatabase.sha256"
    metadata = "$OutputDatabase.metadata.json"
    preflight_report = $PreflightReport
    preflight_report_sha256 = $ExpectedPreflightSha256
    final_report = $FinalReport
    final_report_sha256 = (Get-FileHash -LiteralPath $FinalReport -Algorithm SHA256).Hash.ToLowerInvariant()
    database_scope_private = $true
    database_scope_sealed = $false
    source_modified = $false
    cutover_authorized = $false
}
$Summary | ConvertTo-Json -Depth 3
