Set-StrictMode -Version Latest

function Write-LauncherStep {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-LauncherSuccess {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-LauncherWarning {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-LauncherError {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Write-LauncherNotice {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host $Message -ForegroundColor Cyan
}

function Write-LauncherDetail {
    param([Parameter(Mandatory = $true)][string]$Message)

    Write-Host $Message -ForegroundColor DarkGray
}

function Write-LauncherChoice {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Description
    )

    Write-Host "[$Key]" -ForegroundColor Yellow -NoNewline
    Write-Host " $Description" -ForegroundColor White
}

function Read-LauncherChoice {
    param([Parameter(Mandatory = $true)][string]$Prompt)

    Write-Host $Prompt -ForegroundColor Magenta -NoNewline
    return Read-Host
}

function Write-LauncherProcessLine {
    param([AllowEmptyString()][string]$Line)

    $color = 'Gray'
    if ($Line -match '(?i)"level"\s*:\s*"(?:error|critical)"' -or
        $Line -match '"status"\s*:\s*5\d\d' -or
        $Line -match '(?i)\b(?:error|fatal|failed|failure|traceback)\b') {
        $color = 'Red'
    }
    elseif ($Line -match '(?i)"level"\s*:\s*"warn(?:ing)?"' -or
        $Line -match '"status"\s*:\s*4\d\d' -or
        $Line -match '(?i)\b(?:warn(?:ing)?|deprecated|discouraged)\b' -or
        $Line -match '(?i)\[EVAL\]|security risk') {
        $color = 'Yellow'
    }
    elseif ($Line -match '(?i)\b(?:success(?:fully)?|completed|passed)\b' -or
        $Line -match '(?i)\bfound 0 vulnerabilities\b' -or
        $Line -match '(?i)\bbuilt in\b') {
        $color = 'Green'
    }
    elseif ($Line -match '(?i)"level"\s*:\s*"(?:info|debug)"') {
        $color = 'DarkGray'
    }

    Write-Host $Line -ForegroundColor $color
}
