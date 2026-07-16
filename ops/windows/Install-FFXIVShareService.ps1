[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppRoot,
    [Parameter(Mandatory = $true)]
    [string]$DataRoot,
    [Parameter(Mandatory = $true)]
    [string]$WinSWBinaryPath,
    [Parameter(Mandatory = $true)]
    [string]$WinSWSha256,
    [ValidateRange(1, 65535)]
    [int]$ListenPort = 8000,
    [ValidateRange(1, 64)]
    [int]$ThreadCount = 4,
    [switch]$ReplaceServiceFiles,
    [switch]$StartService
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceId = 'FFXIVShare'
$serviceAccount = 'NT SERVICE\FFXIVShare'
$wrapperBaseName = 'FFXIVShareService'

function Get-AbsoluteLocalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Description must not be empty."
    }
    if ($Path.StartsWith('\\')) {
        throw "$Description must be on a local drive."
    }
    if ($Path -notmatch '^[A-Za-z]:[\\/]') {
        throw "$Description must be an absolute Windows path."
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $normalizedPath = $fullPath.TrimEnd('\', '/')
    $driveRoot = [System.IO.Path]::GetPathRoot($fullPath).TrimEnd('\', '/')
    if ($normalizedPath.Equals($driveRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Description must not be a drive root."
    }
    return $normalizedPath
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $role = [Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not $principal.IsInRole($role)) {
        throw 'Administrator privileges are required to install the service.'
    }
}

function Test-PathInside {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Candidate,
        [Parameter(Mandatory = $true)]
        [string]$Parent
    )

    $comparison = [System.StringComparison]::OrdinalIgnoreCase
    if ($Candidate.Equals($Parent, $comparison)) {
        return $true
    }
    $prefix = $Parent.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    return $Candidate.StartsWith($prefix, $comparison)
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Set-ServicePathAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Permission,
        [switch]$InheritToChildren
    )

    $grant = "${serviceAccount}:($Permission)"
    if ($InheritToChildren) {
        $grant = "${serviceAccount}:(OI)(CI)($Permission)"
    }
    Invoke-NativeCommand `
        -FilePath (Join-Path $env:SystemRoot 'System32\icacls.exe') `
        -ArgumentList @($Path, '/grant:r', $grant, '/C') `
        -Description "ACL update for $Path"
}

$AppRoot = Get-AbsoluteLocalPath -Path $AppRoot -Description 'AppRoot'
$DataRoot = Get-AbsoluteLocalPath -Path $DataRoot -Description 'DataRoot'
$WinSWBinaryPath = Get-AbsoluteLocalPath `
    -Path $WinSWBinaryPath `
    -Description 'WinSWBinaryPath'

if (Test-PathInside -Candidate $DataRoot -Parent $AppRoot) {
    throw 'DataRoot must be outside AppRoot so releases cannot contain persistent data.'
}
if (Test-PathInside -Candidate $AppRoot -Parent $DataRoot) {
    throw 'AppRoot must be outside DataRoot so persistent data cannot contain releases.'
}

if (-not (Test-Path -LiteralPath $WinSWBinaryPath -PathType Leaf)) {
    throw "Operator-provided WinSW binary was not found: $WinSWBinaryPath"
}
if ($WinSWSha256 -notmatch '^[A-Fa-f0-9]{64}$') {
    throw 'WinSWSha256 must be exactly 64 hexadecimal characters.'
}
$actualHash = (Get-FileHash -LiteralPath $WinSWBinaryPath -Algorithm SHA256).Hash
if (-not $actualHash.Equals($WinSWSha256, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The operator-provided WinSW binary failed SHA256 verification.'
}

$versionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($WinSWBinaryPath)
$reportedVersions = @($versionInfo.ProductVersion, $versionInfo.FileVersion) |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
if (-not ($reportedVersions | Where-Object { $_ -match '^2\.12\.0(?:\.0)?(?:[+-].*)?$' })) {
    throw 'The verified binary is not WinSW 2.12.0.'
}

$currentRelease = Join-Path $AppRoot 'current'
$pythonExecutable = Join-Path $currentRelease 'venv\Scripts\python.exe'
$environmentFile = Join-Path $DataRoot 'config\ffxivshare.env'
if (-not (Test-Path -LiteralPath $currentRelease -PathType Container)) {
    throw "Current release directory was not found: $currentRelease"
}
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Virtual environment Python executable was not found: $pythonExecutable"
}
if (-not (Test-Path -LiteralPath $environmentFile -PathType Leaf)) {
    throw "External environment file was not found: $environmentFile"
}

$serviceDirectory = Join-Path $AppRoot 'service'
$serviceExecutable = Join-Path $serviceDirectory "$wrapperBaseName.exe"
$serviceConfiguration = Join-Path $serviceDirectory "$wrapperBaseName.xml"
$generator = Join-Path $PSScriptRoot 'New-WinSWServiceConfig.ps1'
if (-not (Test-Path -LiteralPath $generator -PathType Leaf)) {
    throw "Service configuration generator was not found: $generator"
}
if ($WinSWBinaryPath.Equals($serviceExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'WinSWBinaryPath must be a staging file outside the service directory.'
}
if (-not $ReplaceServiceFiles -and (
    (Test-Path -LiteralPath $serviceExecutable) -or
    (Test-Path -LiteralPath $serviceConfiguration)
)) {
    throw 'Existing service files require the explicit -ReplaceServiceFiles switch.'
}
if ($null -ne (Get-Service -Name $serviceId -ErrorAction SilentlyContinue)) {
    throw "Service is already installed: $serviceId"
}

$target = "$serviceId at $serviceDirectory"
if (-not $PSCmdlet.ShouldProcess($target, 'Install Windows service')) {
    return
}

Assert-Administrator

$databaseDirectory = Join-Path $DataRoot 'database'
$mediaDirectory = Join-Path $DataRoot 'media'
$logDirectory = Join-Path $DataRoot 'logs'
$requiredDirectories = @(
    $serviceDirectory,
    $databaseDirectory,
    $mediaDirectory,
    $logDirectory
)
foreach ($directory in $requiredDirectories) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $directory)
    }
}

& $generator `
    -AppRoot $AppRoot `
    -DataRoot $DataRoot `
    -ListenPort $ListenPort `
    -ThreadCount $ThreadCount `
    -OutputPath $serviceConfiguration `
    -Force:$ReplaceServiceFiles `
    -Confirm:$false | Out-Null

Copy-Item -LiteralPath $WinSWBinaryPath -Destination $serviceExecutable -Force
$copiedHash = (Get-FileHash -LiteralPath $serviceExecutable -Algorithm SHA256).Hash
if (-not $copiedHash.Equals($actualHash, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'The copied WinSW service wrapper failed SHA256 verification.'
}

$serviceInstalled = $false
try {
    Invoke-NativeCommand `
        -FilePath $serviceExecutable `
        -ArgumentList @('install') `
        -Description 'WinSW service installation'
    $serviceInstalled = $true

    $scExecutable = Join-Path $env:SystemRoot 'System32\sc.exe'
    Invoke-NativeCommand `
        -FilePath $scExecutable `
        -ArgumentList @('config', $serviceId, 'obj=', $serviceAccount) `
        -Description 'Virtual service account configuration'
    Invoke-NativeCommand `
        -FilePath $scExecutable `
        -ArgumentList @('sidtype', $serviceId, 'unrestricted') `
        -Description 'Service SID configuration'

    Set-ServicePathAcl -Path $AppRoot -Permission 'RX' -InheritToChildren
    Set-ServicePathAcl -Path $DataRoot -Permission 'RX'
    Set-ServicePathAcl `
        -Path (Join-Path $DataRoot 'config') `
        -Permission 'RX'
    Set-ServicePathAcl -Path $environmentFile -Permission 'R'
    Set-ServicePathAcl -Path $databaseDirectory -Permission 'M' -InheritToChildren
    Set-ServicePathAcl -Path $mediaDirectory -Permission 'M' -InheritToChildren
    Set-ServicePathAcl -Path $logDirectory -Permission 'M' -InheritToChildren

    if ($StartService) {
        Invoke-NativeCommand `
            -FilePath $serviceExecutable `
            -ArgumentList @('start') `
            -Description 'FFXIVShare service start'
    }
}
catch {
    if ($serviceInstalled) {
        try {
            & $serviceExecutable uninstall | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'Automatic service-registration cleanup returned a failure code.'
            }
        }
        catch {
            Write-Warning 'Automatic service-registration cleanup failed.'
        }
    }
    throw
}

Write-Output "Installed service $serviceId with account $serviceAccount."
if (-not $StartService) {
    Write-Output 'The service remains stopped until an operator starts it.'
}
