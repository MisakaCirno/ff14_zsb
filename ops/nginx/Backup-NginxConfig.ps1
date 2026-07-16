[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$NginxRoot,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BackupRoot
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ($fullPath.Equals($pathRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $fullPath
    }

    return $fullPath.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-IsEqualOrChildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate,

        [Parameter(Mandatory = $true)]
        [string]$Parent
    )

    $candidatePath = Get-NormalizedPath -Path $Candidate
    $parentPath = Get-NormalizedPath -Path $Parent

    if ($candidatePath.Equals($parentPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }

    $prefix = $parentPath
    if (-not $prefix.EndsWith([System.IO.Path]::DirectorySeparatorChar.ToString())) {
        $prefix += [System.IO.Path]::DirectorySeparatorChar
    }
    return $candidatePath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

if (-not [System.IO.Path]::IsPathRooted($NginxRoot)) {
    throw 'NginxRoot must be an absolute path.'
}

if (-not [System.IO.Path]::IsPathRooted($BackupRoot)) {
    throw 'BackupRoot must be an absolute path.'
}

$nginxRootPath = Get-NormalizedPath -Path $NginxRoot
if (-not (Test-Path -LiteralPath $nginxRootPath -PathType Container)) {
    throw 'NginxRoot must be an existing directory.'
}

$confPath = Join-Path $nginxRootPath 'conf'
$nginxConfigPath = Join-Path $confPath 'nginx.conf'
if (-not (Test-Path -LiteralPath $confPath -PathType Container)) {
    throw 'NginxRoot must contain an existing conf directory.'
}
if (-not (Test-Path -LiteralPath $nginxConfigPath -PathType Leaf)) {
    throw 'NginxRoot must contain the file conf/nginx.conf.'
}

$backupRootPath = Get-NormalizedPath -Path $BackupRoot
if (Test-IsEqualOrChildPath -Candidate $backupRootPath -Parent $nginxRootPath) {
    throw 'BackupRoot must be outside NginxRoot.'
}

if (-not $PSCmdlet.ShouldProcess($backupRootPath, 'Create a versioned Nginx configuration backup')) {
    return
}

if (-not (Test-Path -LiteralPath $backupRootPath)) {
    [void][System.IO.Directory]::CreateDirectory($backupRootPath)
}
elseif (-not (Test-Path -LiteralPath $backupRootPath -PathType Container)) {
    throw 'BackupRoot must be a directory.'
}

$timestamp = [System.DateTime]::UtcNow.ToString(
    'yyyyMMddTHHmmssfffffffZ',
    [System.Globalization.CultureInfo]::InvariantCulture
)
$uniqueSuffix = [System.Guid]::NewGuid().ToString('N').Substring(0, 12)
$baseName = 'ffxivshare-nginx-conf-{0}-{1}' -f $timestamp, $uniqueSuffix
$archivePath = Join-Path $backupRootPath ($baseName + '.zip')
$checksumPath = $archivePath + '.sha256'
$partialArchivePath = Join-Path $backupRootPath ($baseName + '.partial.zip')
$partialChecksumPath = $checksumPath + '.partial'
$archiveCreated = $false
$checksumCreated = $false

foreach ($path in @($archivePath, $checksumPath, $partialArchivePath, $partialChecksumPath)) {
    if (Test-Path -LiteralPath $path) {
        throw 'A generated backup destination already exists; no file was overwritten.'
    }
}

try {
    Compress-Archive `
        -LiteralPath $confPath `
        -DestinationPath $partialArchivePath `
        -CompressionLevel Optimal `
        -ErrorAction Stop

    $hash = (Get-FileHash -LiteralPath $partialArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumLine = '{0}  {1}{2}' -f $hash, [System.IO.Path]::GetFileName($archivePath), "`n"
    $ascii = New-Object System.Text.ASCIIEncoding
    $stream = New-Object System.IO.FileStream(
        $partialChecksumPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $bytes = $ascii.GetBytes($checksumLine)
        $stream.Write($bytes, 0, $bytes.Length)
    }
    finally {
        $stream.Dispose()
    }

    [System.IO.File]::Move($partialArchivePath, $archivePath)
    $archiveCreated = $true
    [System.IO.File]::Move($partialChecksumPath, $checksumPath)
    $checksumCreated = $true

    [pscustomobject]@{
        ArchivePath = $archivePath
        ChecksumPath = $checksumPath
        Sha256 = $hash
    }
}
catch {
    foreach ($path in @($partialArchivePath, $partialChecksumPath)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        }
    }
    if ($archiveCreated -and -not $checksumCreated -and (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
    }
    throw
}
