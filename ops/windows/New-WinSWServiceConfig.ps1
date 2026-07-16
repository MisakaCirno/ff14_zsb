[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$AppRoot,
    [Parameter(Mandatory = $true)]
    [string]$DataRoot,
    [ValidateRange(1, 65535)]
    [int]$ListenPort = 8000,
    [ValidateRange(1, 64)]
    [int]$ThreadCount = 4,
    [string]$TemplatePath = '',
    [string]$OutputPath = '',
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

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

function Assert-Leaf {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found: $Path"
    }
}

function ConvertTo-XmlText {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return [System.Security.SecurityElement]::Escape($Value)
}

$AppRoot = Get-AbsoluteLocalPath -Path $AppRoot -Description 'AppRoot'
$DataRoot = Get-AbsoluteLocalPath -Path $DataRoot -Description 'DataRoot'

if (Test-PathInside -Candidate $DataRoot -Parent $AppRoot) {
    throw 'DataRoot must be outside AppRoot so releases cannot contain persistent data.'
}
if (Test-PathInside -Candidate $AppRoot -Parent $DataRoot) {
    throw 'AppRoot must be outside DataRoot so persistent data cannot contain releases.'
}

$currentRelease = Join-Path $AppRoot 'current'
$pythonExecutable = Join-Path $currentRelease 'venv\Scripts\python.exe'
$environmentFile = Join-Path $DataRoot 'config\ffxivshare.env'
$logDirectory = Join-Path $DataRoot 'logs'

if (-not (Test-Path -LiteralPath $currentRelease -PathType Container)) {
    throw "Current release directory was not found: $currentRelease"
}
Assert-Leaf -Path $pythonExecutable -Description 'Virtual environment Python executable'
Assert-Leaf -Path $environmentFile -Description 'External environment file'

if ([string]::IsNullOrWhiteSpace($TemplatePath)) {
    $TemplatePath = Join-Path $PSScriptRoot 'FFXIVShareService.xml.template'
}
$TemplatePath = Get-AbsoluteLocalPath -Path $TemplatePath -Description 'TemplatePath'
Assert-Leaf -Path $TemplatePath -Description 'WinSW XML template'

if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $AppRoot 'service\FFXIVShareService.xml'
}
$OutputPath = Get-AbsoluteLocalPath -Path $OutputPath -Description 'OutputPath'

$expectedOutputPath = Join-Path $AppRoot 'service\FFXIVShareService.xml'
if (-not $OutputPath.Equals(
    $expectedOutputPath,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "OutputPath must use the service directory outside releases: $expectedOutputPath"
}
if ((Test-Path -LiteralPath $OutputPath -PathType Leaf) -and -not $Force) {
    throw "OutputPath already exists. Use -Force to replace it: $OutputPath"
}

$template = [System.IO.File]::ReadAllText($TemplatePath)
$replacements = [ordered]@{
    '{{PYTHON_EXECUTABLE}}' = ConvertTo-XmlText $pythonExecutable
    '{{LISTEN_PORT}}' = [string]$ListenPort
    '{{THREAD_COUNT}}' = [string]$ThreadCount
    '{{CURRENT_RELEASE}}' = ConvertTo-XmlText $currentRelease
    '{{ENVIRONMENT_FILE}}' = ConvertTo-XmlText $environmentFile
    '{{LOG_DIRECTORY}}' = ConvertTo-XmlText $logDirectory
}

$rendered = $template
foreach ($entry in $replacements.GetEnumerator()) {
    if (-not $rendered.Contains($entry.Key)) {
        throw "Required template token is missing: $($entry.Key)"
    }
    $rendered = $rendered.Replace($entry.Key, $entry.Value)
}
if ($rendered -match '\{\{[A-Z0-9_]+\}\}') {
    throw 'The rendered WinSW configuration still contains template tokens.'
}

try {
    [void]([xml]$rendered)
}
catch {
    throw 'The rendered WinSW configuration is not valid XML.'
}

$outputDirectory = Split-Path -Parent $OutputPath
if ($PSCmdlet.ShouldProcess($OutputPath, 'Generate WinSW service configuration')) {
    if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
        [void](New-Item -ItemType Directory -Path $outputDirectory)
    }
    [System.IO.File]::WriteAllText(
        $OutputPath,
        $rendered,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Output $OutputPath
}
