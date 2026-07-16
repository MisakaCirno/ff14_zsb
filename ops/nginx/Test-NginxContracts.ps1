[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $didThrow = $false
    try {
        & $Action
    }
    catch {
        $didThrow = $true
    }

    Assert-True -Condition $didThrow -Message $Message
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backupScript = Join-Path $scriptRoot 'Backup-NginxConfig.ps1'
$includeExample = Join-Path $scriptRoot 'ffxivshare.locations.conf.example'
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'ffxivshare-nginx-contract-' + [System.Guid]::NewGuid().ToString('N')
)

try {
    $nginxRoot = Join-Path $testRoot 'nginx'
    $confRoot = Join-Path $nginxRoot 'conf'
    $includedRoot = Join-Path $confRoot 'includes'
    $backupRoot = Join-Path $testRoot 'backups'
    [void][System.IO.Directory]::CreateDirectory($includedRoot)

    $ascii = New-Object System.Text.ASCIIEncoding
    [System.IO.File]::WriteAllText(
        (Join-Path $confRoot 'nginx.conf'),
        "events {}`nhttp { include includes/site.conf; }`n",
        $ascii
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $includedRoot 'site.conf'),
        "server { listen 443 ssl; }`n",
        $ascii
    )

    $first = & $backupScript `
        -NginxRoot $nginxRoot `
        -BackupRoot $backupRoot `
        -Confirm:$false
    Assert-True -Condition (Test-Path -LiteralPath $first.ArchivePath -PathType Leaf) `
        -Message 'The backup archive was not created.'
    Assert-True -Condition (Test-Path -LiteralPath $first.ChecksumPath -PathType Leaf) `
        -Message 'The checksum sidecar was not created.'

    $actualHash = (Get-FileHash -LiteralPath $first.ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-True -Condition ($actualHash -eq $first.Sha256) `
        -Message 'The reported SHA256 does not match the archive.'
    $sidecar = [System.IO.File]::ReadAllText($first.ChecksumPath, $ascii).Trim()
    $expectedSidecar = '{0}  {1}' -f $actualHash, [System.IO.Path]::GetFileName($first.ArchivePath)
    Assert-True -Condition ($sidecar -eq $expectedSidecar) `
        -Message 'The SHA256 sidecar does not match the archive.'

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($first.ArchivePath)
    try {
        $entryNames = @($zip.Entries | ForEach-Object { $_.FullName.Replace('\', '/') })
        Assert-True -Condition ($entryNames -contains 'conf/nginx.conf') `
            -Message 'The archive does not contain conf/nginx.conf.'
        Assert-True -Condition ($entryNames -contains 'conf/includes/site.conf') `
            -Message 'The archive does not contain nested configuration files.'

        $nginxEntry = $zip.Entries | Where-Object { $_.FullName.Replace('\', '/') -eq 'conf/nginx.conf' }
        $reader = New-Object System.IO.StreamReader($nginxEntry.Open(), $ascii)
        try {
            $archivedConfig = $reader.ReadToEnd()
        }
        finally {
            $reader.Dispose()
        }
        Assert-True -Condition ($archivedConfig -eq "events {}`nhttp { include includes/site.conf; }`n") `
            -Message 'The archived nginx.conf content changed.'
    }
    finally {
        $zip.Dispose()
    }

    $firstArchiveHash = (Get-FileHash -LiteralPath $first.ArchivePath -Algorithm SHA256).Hash
    $second = & $backupScript `
        -NginxRoot $nginxRoot `
        -BackupRoot $backupRoot `
        -Confirm:$false
    Assert-True -Condition ($second.ArchivePath -ne $first.ArchivePath) `
        -Message 'Two backups used the same destination name.'
    Assert-True -Condition (Test-Path -LiteralPath $first.ArchivePath -PathType Leaf) `
        -Message 'A later backup removed the earlier archive.'
    Assert-True `
        -Condition ((Get-FileHash -LiteralPath $first.ArchivePath -Algorithm SHA256).Hash -eq $firstArchiveHash) `
        -Message 'A later backup overwrote the earlier archive.'
    Assert-True -Condition ((Get-ChildItem -LiteralPath $backupRoot -Filter '*.zip').Count -eq 2) `
        -Message 'The backup directory does not contain two versioned archives.'
    Assert-True -Condition ((Get-ChildItem -LiteralPath $backupRoot -Filter '*.sha256').Count -eq 2) `
        -Message 'The backup directory does not contain two checksum sidecars.'

    $whatIfRoot = Join-Path $testRoot 'what-if-backups'
    & $backupScript `
        -NginxRoot $nginxRoot `
        -BackupRoot $whatIfRoot `
        -WhatIf | Out-Null
    Assert-True -Condition (-not (Test-Path -LiteralPath $whatIfRoot)) `
        -Message 'WhatIf created the backup directory.'

    $unsafeRoot = Join-Path $confRoot 'backups'
    Assert-Throws -Message 'BackupRoot inside NginxRoot was not rejected.' -Action {
        & $backupScript `
            -NginxRoot $nginxRoot `
            -BackupRoot $unsafeRoot `
            -Confirm:$false | Out-Null
    }
    Assert-True -Condition (-not (Test-Path -LiteralPath $unsafeRoot)) `
        -Message 'The rejected in-tree backup destination was created.'

    $missingConfigRoot = Join-Path $testRoot 'missing-config'
    [void][System.IO.Directory]::CreateDirectory((Join-Path $missingConfigRoot 'conf'))
    Assert-Throws -Message 'A missing conf/nginx.conf was not rejected.' -Action {
        & $backupScript `
            -NginxRoot $missingConfigRoot `
            -BackupRoot (Join-Path $testRoot 'missing-config-backups') `
            -Confirm:$false | Out-Null
    }

    $includeText = [System.IO.File]::ReadAllText($includeExample)
    Assert-True -Condition ($includeText.Contains('Include this file inside the existing HTTPS server')) `
        -Message 'The include placement contract is missing.'
    Assert-True -Condition ($includeText.Contains('RESERVED: /n/')) `
        -Message 'The /n/ renderer reservation is missing.'
    Assert-True -Condition ($includeText.Contains('intentionally does not define /n/')) `
        -Message 'The /n/ no-change contract is missing.'
    Assert-True -Condition (-not $includeText.Contains('location /n/')) `
        -Message 'The compatibility include must not replace the /n/ renderer location.'
    Assert-True -Condition ($includeText.Contains('alias D:/FFXIVShareApp/current/staticfiles/;')) `
        -Message 'The static alias does not point at AppRoot/current/staticfiles.'
    Assert-True -Condition ($includeText.Contains('alias D:/FFXIVShareData/media/;')) `
        -Message 'The media alias does not point at DataRoot/media.'
    Assert-True -Condition ($includeText.Contains('location = /health/live/')) `
        -Message 'The exact liveness boundary is missing.'
    Assert-True -Condition ($includeText.Contains('location = /health/ready/')) `
        -Message 'The exact readiness boundary is missing.'
    Assert-True -Condition (-not $includeText.Contains('location ^~ /health/')) `
        -Message 'The health boundary must not expose a broad prefix.'
    Assert-True -Condition ($includeText.Contains('allow 127.0.0.1;')) `
        -Message 'Local health access is missing.'
    Assert-True -Condition ($includeText.Contains('deny all;')) `
        -Message 'External health access is not denied by default.'
    Assert-True -Condition ($includeText.Contains('proxy_set_header Host $host;')) `
        -Message 'The Host header is not overwritten.'
    Assert-True -Condition ($includeText.Contains('proxy_set_header X-Forwarded-Proto $scheme;')) `
        -Message 'X-Forwarded-Proto is not overwritten.'
    Assert-True -Condition ($includeText.Contains('proxy_set_header X-Forwarded-For $remote_addr;')) `
        -Message 'X-Forwarded-For is not overwritten with the direct client address.'
    Assert-True -Condition (-not $includeText.Contains('$proxy_add_x_forwarded_for')) `
        -Message 'The include appends untrusted X-Forwarded-For data.'

    $proxyTargets = [System.Text.RegularExpressions.Regex]::Matches(
        $includeText,
        'proxy_pass\s+([^;]+);'
    )
    Assert-True -Condition ($proxyTargets.Count -gt 0) `
        -Message 'The include does not define a Waitress proxy target.'
    foreach ($target in $proxyTargets) {
        Assert-True -Condition ($target.Groups[1].Value.Trim() -eq 'http://127.0.0.1:8000') `
            -Message 'A proxy target is not the loopback-only Waitress endpoint.'
    }
    Assert-True -Condition (-not $includeText.Contains('0.0.0.0')) `
        -Message 'The include contains a wildcard IPv4 endpoint.'
    Assert-True -Condition (-not $includeText.Contains('[::]')) `
        -Message 'The include contains a wildcard IPv6 endpoint.'

    Write-Host 'Nginx compatibility and backup contracts passed.'
}
finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        Assert-True `
            -Condition ($resolvedTestRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) `
            -Message 'Refusing to clean a test directory outside the system temp directory.'
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
