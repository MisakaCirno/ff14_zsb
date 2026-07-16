[CmdletBinding()]
param(
    [string]$PythonExecutable = '',
    [string]$NpmExecutable = '',
    [switch]$SkipTests,
    [switch]$SkipFrontend
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-CheckedStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    Write-Host "==> $Name"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

Push-Location $PSScriptRoot
try {
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $venvPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $venvPython) {
            $PythonExecutable = $venvPython
        }
        else {
            $PythonExecutable = 'python'
        }
    }

    if ([string]::IsNullOrWhiteSpace($NpmExecutable)) {
        $NpmExecutable = if ($env:OS -eq 'Windows_NT') { 'npm.cmd' } else { 'npm' }
    }

    Invoke-CheckedStep 'Django system check' {
        & $PythonExecutable manage.py check
    }

    Invoke-CheckedStep 'Migration drift check' {
        & $PythonExecutable manage.py makemigrations --check --dry-run
    }

    Invoke-CheckedStep 'Python dependency check' {
        & $PythonExecutable -m pip check
    }

    if ($env:OS -eq 'Windows_NT') {
        Invoke-CheckedStep 'Windows operations contract checks' {
            & (Join-Path $PSScriptRoot 'ops\windows\Test-OpsContracts.ps1') `
                -RepositoryRoot $PSScriptRoot
        }

        Invoke-CheckedStep 'WinSW service contract checks' {
            & (Join-Path $PSScriptRoot 'ops\windows\Test-WinSWServiceContract.ps1')
        }

        Invoke-CheckedStep 'Nginx compatibility and backup contract checks' {
            & (Join-Path $PSScriptRoot 'ops\nginx\Test-NginxContracts.ps1')
        }

        if (-not $SkipTests) {
            Invoke-CheckedStep 'Waitress loopback smoke test' {
                & (Join-Path $PSScriptRoot 'ops\windows\Test-WaitressSmoke.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }
        }
    }

    if (-not $SkipFrontend) {
        Invoke-CheckedStep 'Frontend type, lint, and build checks' {
            & $NpmExecutable --prefix frontend run verify
        }
    }

    if (-not $SkipTests) {
        Invoke-CheckedStep 'Django test suite' {
            & $PythonExecutable manage.py test -v 1
        }
    }

    Write-Host 'All requested checks passed.'
}
finally {
    Pop-Location
}
