[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Condition {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

$testRoot = Join-Path (
    [System.IO.Path]::GetTempPath()
) ('ffxivshare-batch-trampoline-' + [Guid]::NewGuid().ToString('N'))
$exitCode = $null
$markerCreated = $false

try {
    [void][System.IO.Directory]::CreateDirectory($testRoot)
    $batchPath = Join-Path $testRoot 'start.bat'
    $mutatorPath = Join-Path $testRoot 'Replace-RunningBatch.ps1'
    $markerPath = Join-Path $testRoot 'unsafe-continuation.txt'
    $utf8 = [System.Text.UTF8Encoding]::new($false)

    $mutatorSource = @'
param(
    [Parameter(Mandatory = $true)][string]$BatchPath,
    [Parameter(Mandatory = $true)][string]$MarkerPath
)

$replacement = (
    "@echo off`r`n" +
    "> `"$MarkerPath`" echo unsafe`r`n" +
    "exit /b 99`r`n"
)
[System.IO.File]::WriteAllText(
    $BatchPath,
    $replacement,
    [System.Text.Encoding]::ASCII
)
exit 7
'@
    [System.IO.File]::WriteAllText($mutatorPath, $mutatorSource, $utf8)

    $batchSource = (
        '@powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' +
        $mutatorPath +
        '" -BatchPath "%~f0" -MarkerPath "' +
        $markerPath +
        '" & exit /b' +
        "`r`n"
    )
    [System.IO.File]::WriteAllText(
        $batchPath,
        $batchSource,
        [System.Text.Encoding]::ASCII
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & cmd.exe /d /c "call `"$batchPath`""
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $markerCreated = Test-Path -LiteralPath $markerPath -PathType Leaf
}
finally {
    if (Test-Path -LiteralPath $testRoot -PathType Container) {
        $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
        $resolvedTempRoot = [System.IO.Path]::GetFullPath(
            [System.IO.Path]::GetTempPath()
        )
        if (-not $resolvedTestRoot.StartsWith(
            $resolvedTempRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Refusing to remove a test directory outside the temp root.'
        }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}

Assert-Condition `
    -Condition ($exitCode -eq 7) `
    -Message "The one-line batch trampoline exited with code $exitCode."
Assert-Condition `
    -Condition (-not $markerCreated) `
    -Message 'The running batch continued into replacement file contents.'

Write-Host 'Batch trampoline immutability test passed.'
