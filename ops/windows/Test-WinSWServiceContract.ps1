[CmdletBinding()]
param()

Set-StrictMode -Version Latest
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

function Assert-Equal {
    param(
        [AllowNull()]
        [object]$Actual,
        [AllowNull()]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ([string]$Actual -ne [string]$Expected) {
        throw "$Message Expected '$Expected', got '$Actual'."
    }
}

function Remove-TestDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $temporaryBase = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd('\', '/')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $requiredPrefix = $temporaryBase + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolvedPath.StartsWith(
        $requiredPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Refusing to remove a test directory outside the system temp directory.'
    }
    if ((Split-Path -Leaf $resolvedPath) -notmatch '^ffxivshare-winsw-contract-[a-f0-9]{32}$') {
        throw 'Refusing to remove a test directory with an unexpected name.'
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

$templatePath = Join-Path $PSScriptRoot 'FFXIVShareService.xml.template'
$generatorPath = Join-Path $PSScriptRoot 'New-WinSWServiceConfig.ps1'
$installerPath = Join-Path $PSScriptRoot 'Install-FFXIVShareService.ps1'
$uninstallerPath = Join-Path $PSScriptRoot 'Uninstall-FFXIVShareService.ps1'

foreach ($path in @($templatePath, $generatorPath, $installerPath, $uninstallerPath)) {
    Assert-True `
        -Condition (Test-Path -LiteralPath $path -PathType Leaf) `
        -Message "Required service-contract file is missing: $path"
}

$templateText = [System.IO.File]::ReadAllText($templatePath)
[xml]$template = $templateText
Assert-Equal $template.service.id 'FFXIVShare' 'Unexpected service ID.'
Assert-Equal $template.service.name 'FFXIVShare' 'Unexpected service name.'
Assert-Equal $template.service.startmode 'Automatic' 'Unexpected service start mode.'
Assert-Equal $template.service.delayedAutoStart 'true' 'Delayed auto-start must be enabled.'
Assert-Equal $template.service.stoptimeout '30 sec' 'Unexpected graceful stop timeout.'
Assert-Equal $template.service.resetfailure '1 hour' 'Unexpected failure reset window.'
Assert-Equal $template.service.log.mode 'roll' 'WinSW size-based rolling must be enabled.'
Assert-Equal $template.service.log.sizeThreshold '25600' 'Log threshold must be 25 MB in KB.'
Assert-Equal $template.service.log.keepFiles '14' 'WinSW must retain 14 rolled files.'

$arguments = [string]$template.service.arguments
Assert-True `
    -Condition $arguments.StartsWith('-m waitress ') `
    -Message 'WinSW must own the Python Waitress process directly.'
Assert-True `
    -Condition $arguments.Contains('--listen=127.0.0.1:{{LISTEN_PORT}}') `
    -Message 'Waitress must bind only to IPv4 loopback.'
Assert-True `
    -Condition $arguments.Contains('"--trusted-proxy-headers=x-forwarded-for x-forwarded-proto"') `
    -Message 'Trusted proxy headers must be passed as one whitespace-separated argv value.'
Assert-True `
    -Condition $arguments.Contains('--clear-untrusted-proxy-headers') `
    -Message 'Untrusted proxy headers must be cleared.'
Assert-True `
    -Condition $arguments.Contains('--no-expose-tracebacks') `
    -Message 'Waitress tracebacks must remain disabled.'
Assert-True `
    -Condition ($arguments -notmatch '0\.0\.0\.0|\[::\]|--listen=\*|--host=\*') `
    -Message 'Wildcard Waitress listeners are forbidden.'

$failureActions = @($template.service.onfailure)
Assert-Equal $failureActions.Count 4 'Unexpected number of failure actions.'
$expectedActions = @(
    @('restart', '10 sec'),
    @('restart', '30 sec'),
    @('restart', '60 sec'),
    @('none', '')
)
for ($index = 0; $index -lt $expectedActions.Count; $index++) {
    Assert-Equal `
        $failureActions[$index].GetAttribute('action') `
        $expectedActions[$index][0] `
        "Unexpected failure action at index $index."
    Assert-Equal `
        $failureActions[$index].GetAttribute('delay') `
        $expectedActions[$index][1] `
        "Unexpected failure delay at index $index."
}

Assert-True `
    -Condition ($null -eq $template.SelectSingleNode('/service/download')) `
    -Message 'The service contract must never download executable content.'
Assert-True `
    -Condition ($null -eq $template.SelectSingleNode('/service/serviceaccount')) `
    -Message 'Service credentials must not be embedded in XML.'
Assert-True `
    -Condition ($templateText -notmatch '(?i)password|secret[_-]?key') `
    -Message 'The service XML must not contain secret fields.'

$installerText = [System.IO.File]::ReadAllText($installerPath)
$generatorText = [System.IO.File]::ReadAllText($generatorPath)
$uninstallerText = [System.IO.File]::ReadAllText($uninstallerPath)
foreach ($scriptText in @($installerText, $generatorText, $uninstallerText)) {
    Assert-True `
        -Condition $scriptText.Contains('SupportsShouldProcess = $true') `
        -Message 'Every mutating service script must support WhatIf.'
    Assert-True `
        -Condition ($scriptText -notmatch '(?i)Invoke-WebRequest|Start-BitsTransfer|curl\.exe') `
        -Message 'Service scripts must not download WinSW automatically.'
}
Assert-True `
    -Condition $installerText.Contains("`$serviceAccount = 'NT SERVICE\FFXIVShare'") `
    -Message 'Installer must use the FFXIVShare virtual service account.'
Assert-True `
    -Condition $installerText.Contains('Get-FileHash') `
    -Message 'Installer must verify the operator-provided WinSW SHA256.'
Assert-True `
    -Condition $installerText.Contains("'database'") `
    -Message 'Installer must grant database-directory permissions explicitly.'
Assert-True `
    -Condition $installerText.Contains("'media'") `
    -Message 'Installer must grant media-directory permissions explicitly.'
Assert-True `
    -Condition $installerText.Contains("'logs'") `
    -Message 'Installer must grant log-directory permissions explicitly.'
Assert-True `
    -Condition (-not $installerText.Contains("Set-ServicePathAcl -Path `$DataRoot -Permission 'M'")) `
    -Message 'Installer must not grant Modify permission across DataRoot.'
Assert-True `
    -Condition $installerText.Contains('Set-ServicePathAcl -Path $environmentFile -Permission ''R''') `
    -Message 'Installer must grant read access only to the selected environment file.'
Assert-True `
    -Condition ($uninstallerText -notmatch '(?i)Remove-Item|rmdir|del\.exe') `
    -Message 'Uninstaller must never delete persistent data or service files.'

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-winsw-contract-' + [Guid]::NewGuid().ToString('N'))
$appRoot = Join-Path $temporaryRoot 'app & releases'
$dataRoot = Join-Path $temporaryRoot 'persistent & data'
$currentRelease = Join-Path $appRoot 'current'
$pythonDirectory = Join-Path $currentRelease 'venv\Scripts'
$configDirectory = Join-Path $dataRoot 'config'
$outputPath = Join-Path $appRoot 'service\FFXIVShareService.xml'

try {
    [void](New-Item -ItemType Directory -Path $pythonDirectory -Force)
    [void](New-Item -ItemType Directory -Path $configDirectory -Force)
    [System.IO.File]::WriteAllText(
        (Join-Path $pythonDirectory 'python.exe'),
        'test placeholder'
    )
    [System.IO.File]::WriteAllText(
        (Join-Path $configDirectory 'ffxivshare.env'),
        'test placeholder'
    )

    $generatedPath = & $generatorPath `
        -AppRoot $appRoot `
        -DataRoot $dataRoot `
        -ListenPort 8123 `
        -ThreadCount 7 `
        -OutputPath $outputPath `
        -Confirm:$false
    Assert-Equal $generatedPath $outputPath 'Generator returned an unexpected path.'

    [xml]$generated = [System.IO.File]::ReadAllText($outputPath)
    Assert-Equal `
        $generated.service.executable `
        (Join-Path $currentRelease 'venv\Scripts\python.exe') `
        'Generated Python path was not XML-safe.'
    Assert-Equal `
        $generated.service.workingdirectory `
        $currentRelease `
        'Generated release path was not XML-safe.'
    Assert-Equal `
        $generated.service.env[0].value `
        (Join-Path $configDirectory 'ffxivshare.env') `
        'Generated environment path was not external to the release.'
    Assert-Equal `
        $generated.service.logpath `
        (Join-Path $dataRoot 'logs') `
        'Generated log path was not external to the release.'
    Assert-True `
        -Condition ([string]$generated.service.arguments).Contains('--listen=127.0.0.1:8123') `
        -Message 'Generated Waitress listener was not loopback-only.'
    Assert-True `
        -Condition ([string]$generated.service.arguments).Contains('--threads=7') `
        -Message 'Generated thread count was not applied.'

    $overwriteRejected = $false
    try {
        & $generatorPath `
            -AppRoot $appRoot `
            -DataRoot $dataRoot `
            -OutputPath $outputPath `
            -Confirm:$false | Out-Null
    }
    catch {
        $overwriteRejected = $true
    }
    Assert-True $overwriteRejected 'Generator must reject overwrite unless -Force is explicit.'

    $outsideServiceRejected = $false
    try {
        & $generatorPath `
            -AppRoot $appRoot `
            -DataRoot $dataRoot `
            -OutputPath (Join-Path $temporaryRoot 'outside-service.xml') `
            -Confirm:$false | Out-Null
    }
    catch {
        $outsideServiceRejected = $true
    }
    Assert-True `
        $outsideServiceRejected `
        'Generator must keep service configuration outside release and data directories.'

    $nestedDataRejected = $false
    try {
        & $generatorPath `
            -AppRoot $appRoot `
            -DataRoot (Join-Path $appRoot 'data') `
            -OutputPath (Join-Path $temporaryRoot 'invalid.xml') `
            -Confirm:$false | Out-Null
    }
    catch {
        $nestedDataRejected = $true
    }
    Assert-True $nestedDataRejected 'Generator must reject persistent data inside AppRoot.'

    Remove-Item -LiteralPath $outputPath -Force
    $whatIfOutput = $outputPath
    & $generatorPath `
        -AppRoot $appRoot `
        -DataRoot $dataRoot `
        -OutputPath $whatIfOutput `
        -WhatIf | Out-Null
    Assert-True `
        -Condition (-not (Test-Path -LiteralPath $whatIfOutput)) `
        -Message 'Generator -WhatIf must not write a configuration file.'
}
finally {
    Remove-TestDirectory -Path $temporaryRoot
}

Write-Output 'WinSW service contract checks passed.'
