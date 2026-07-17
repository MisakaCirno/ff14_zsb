[CmdletBinding()]
param(
    [string]$PythonExecutable = '',
    [string]$NpmExecutable = '',
    [switch]$SkipTests,
    [switch]$SkipFrontend,
    [switch]$IncludeProductionCopyE2E
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($SkipTests -and $IncludeProductionCopyE2E) {
    throw 'IncludeProductionCopyE2E cannot be combined with SkipTests.'
}
if ($IncludeProductionCopyE2E -and $env:OS -ne 'Windows_NT') {
    throw 'IncludeProductionCopyE2E requires Windows NTFS and DACL APIs.'
}

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
    if (Test-Path -LiteralPath $PythonExecutable -PathType Leaf) {
        $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
    }
    else {
        $resolvedPython = Get-Command `
            -Name $PythonExecutable `
            -CommandType Application `
            -ErrorAction Stop |
            Select-Object -First 1
        $PythonExecutable = $resolvedPython.Source
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
            Invoke-CheckedStep 'SQLite backup-set verification contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-SQLiteBackupSet.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'SQLite migration snapshot inspection contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-SQLiteSnapshotInspection.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Media snapshot manifest contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-MediaManifest.ps1') `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Site-data export comparison contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-SiteDataExportComparison.ps1') `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy handoff contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyHandoff.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy bootstrap contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyBootstrap.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy policy proposal contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyPolicyProposal.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy policy approval contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyPolicyApproval.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy rehearsal contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyRehearsal.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            Invoke-CheckedStep 'Production-copy rehearsal pair verification contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyRehearsalPairVerifier.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

            if ($IncludeProductionCopyE2E) {
                Invoke-CheckedStep 'Production-copy real offline end-to-end contracts' {
                    & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyEndToEnd.ps1') `
                        -IncludeSlow `
                        -RepositoryRoot $PSScriptRoot `
                        -PythonExecutable $PythonExecutable
                }
            }

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
