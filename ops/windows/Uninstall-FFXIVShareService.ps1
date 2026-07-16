[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceId = 'FFXIVShare'
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
        throw 'Administrator privileges are required to uninstall the service.'
    }
}

function Invoke-WrapperCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    & $Executable $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

$AppRoot = Get-AbsoluteLocalPath -Path $AppRoot -Description 'AppRoot'
$serviceDirectory = Join-Path $AppRoot 'service'
$serviceExecutable = Join-Path $serviceDirectory "$wrapperBaseName.exe"
$serviceConfiguration = Join-Path $serviceDirectory "$wrapperBaseName.xml"
$service = Get-Service -Name $serviceId -ErrorAction SilentlyContinue

if ($null -eq $service) {
    Write-Output "Service is not installed: $serviceId"
    return
}
if (-not (Test-Path -LiteralPath $serviceExecutable -PathType Leaf)) {
    throw "Expected WinSW wrapper was not found: $serviceExecutable"
}
if (-not (Test-Path -LiteralPath $serviceConfiguration -PathType Leaf)) {
    throw "Expected WinSW configuration was not found: $serviceConfiguration"
}

try {
    [xml]$configuration = [System.IO.File]::ReadAllText($serviceConfiguration)
}
catch {
    throw 'The WinSW service configuration is not valid XML.'
}
if ([string]$configuration.service.id -ne $serviceId) {
    throw 'The WinSW configuration does not identify the FFXIVShare service.'
}

$target = "$serviceId at $serviceDirectory"
if (-not $PSCmdlet.ShouldProcess($target, 'Stop and unregister Windows service')) {
    return
}

Assert-Administrator
$service.Refresh()
if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
    Invoke-WrapperCommand `
        -Executable $serviceExecutable `
        -Command 'stop' `
        -Description 'FFXIVShare service stop'
}
Invoke-WrapperCommand `
    -Executable $serviceExecutable `
    -Command 'uninstall' `
    -Description 'WinSW service uninstallation'

Write-Output "Uninstalled service $serviceId."
Write-Output 'Application releases, configuration, databases, media, logs, and backups were not deleted.'
