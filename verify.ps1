[CmdletBinding()]
param(
    [string]$PythonExecutable = '',
    [switch]$SkipTests
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

    Invoke-CheckedStep 'Django system check' {
        & $PythonExecutable manage.py check
    }

    Invoke-CheckedStep 'Migration drift check' {
        & $PythonExecutable manage.py makemigrations --check --dry-run
    }

    Invoke-CheckedStep 'Python dependency check' {
        & $PythonExecutable -m pip check
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
