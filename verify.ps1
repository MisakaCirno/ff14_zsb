[CmdletBinding()]
param(
    [string]$PythonExecutable = '',
    [string]$NpmExecutable = '',
    [ValidateSet('Fast', 'Full', 'Release')]
    [string]$Profile = 'Fast',
    [ValidateRange(1, 32)]
    [int]$DjangoParallel = 4,
    [switch]$SkipTests,
    [switch]$SkipFrontend,
    [switch]$IncludeProductionCopyE2E
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$effectiveProfile = if ($IncludeProductionCopyE2E) { 'Release' } else { $Profile }
$runHeavyContracts = $effectiveProfile -in @('Full', 'Release')
$runReleaseE2E = $effectiveProfile -eq 'Release'

if ($SkipTests -and $runReleaseE2E) {
    throw 'The Release profile cannot be combined with SkipTests.'
}
if ($runReleaseE2E -and $env:OS -ne 'Windows_NT') {
    throw 'The Release profile requires Windows NTFS and DACL APIs.'
}

function Invoke-CheckedStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "==> $Name"
    try {
        & $Action
        if ($LASTEXITCODE -ne 0) {
            throw "$Name failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $stopwatch.Stop()
        Write-Host ("<== {0} ({1:N2}s)" -f $Name, $stopwatch.Elapsed.TotalSeconds)
    }
}

Push-Location $PSScriptRoot
try {
    $verificationStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    Write-Host "Verification profile: $effectiveProfile"

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

        Invoke-CheckedStep 'Current Git deployment fact contracts' {
            & (Join-Path $PSScriptRoot 'ops\release\Test-CurrentGitDeploymentFactsContract.ps1') `
                -RepositoryRoot $PSScriptRoot
        }

        Invoke-CheckedStep 'Direct Git runtime contracts' {
            & (Join-Path $PSScriptRoot 'ops\release\Test-DirectGitRuntimeContracts.ps1') `
                -RepositoryRoot $PSScriptRoot
        }

        Invoke-CheckedStep 'Direct Git update workflow tests' {
            & (Join-Path $PSScriptRoot 'ops\release\Test-DirectGitUpdateWorkflow.ps1')
        }

        Invoke-CheckedStep 'Direct Git release readiness unit tests' {
            & $PythonExecutable `
                -I `
                -B `
                (Join-Path $PSScriptRoot 'ops\release\test_direct_git_release_readiness.py')
        }

        Invoke-CheckedStep 'WinSW service contract checks' {
            & (Join-Path $PSScriptRoot 'ops\windows\Test-WinSWServiceContract.ps1')
        }

        Invoke-CheckedStep 'Nginx compatibility and backup contract checks' {
            & (Join-Path $PSScriptRoot 'ops\nginx\Test-NginxContracts.ps1')
        }

        if (-not $SkipTests -and $runHeavyContracts) {
            Invoke-CheckedStep 'Production-copy capture gate contracts' {
                & (Join-Path $PSScriptRoot 'ops\migration\Test-ProductionCopyCaptureGate.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable
            }

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

            if ($runReleaseE2E) {
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

        if (-not $SkipTests -and $runReleaseE2E -and $env:OS -eq 'Windows_NT') {
            Invoke-CheckedStep 'Browser flow and accessibility checks' {
                & (Join-Path $PSScriptRoot 'ops\testing\Test-BrowserFlows.ps1') `
                    -RepositoryRoot $PSScriptRoot `
                    -PythonExecutable $PythonExecutable `
                    -NpmExecutable $NpmExecutable
            }
        }
    }

    if (-not $SkipTests) {
        Invoke-CheckedStep "Django test suite ($DjangoParallel workers)" {
            & $PythonExecutable manage.py test -v 1 --parallel $DjangoParallel
        }
    }

    $verificationStopwatch.Stop()
    Write-Host (
        "All requested checks passed for profile {0} in {1:N2}s." -f `
            $effectiveProfile,
            $verificationStopwatch.Elapsed.TotalSeconds
    )
}
finally {
    Pop-Location
}
