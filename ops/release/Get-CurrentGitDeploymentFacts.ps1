[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputPath,
    [string]$DatabasePath = '',
    [string]$MediaRoot = '',
    [string]$EnvironmentFile = '',
    [string]$NginxRoot = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-AbsolutePath {
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
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
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

function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [switch]$AllowFailure
    )

    $lines = @(& $FilePath @ArgumentList 2>&1 | ForEach-Object { $_.ToString() })
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "$Description failed with exit code $exitCode."
    }
    return [pscustomobject]@{
        exit_code = $exitCode
        lines = $lines
    }
}

function Protect-CommandLine {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $Value
    }

    $protected = $Value
    $assignmentPattern = '(?i)(SECRET_KEY|DATABASE_PASSWORD|PASSWORD|TOKEN|API_KEY)(\s*=\s*|\s+)("[^"]*"|''[^'']*''|\S+)'
    $protected = [regex]::Replace($protected, $assignmentPattern, '$1=<redacted>')
    $protected = [regex]::Replace(
        $protected,
        '(?i)(https?://)[^\s/@:]+:[^\s/@]+@',
        '$1<redacted>@'
    )
    return $protected
}

function Get-FileObservation {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [switch]$IncludeSha256
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
        }
    }

    $item = Get-Item -LiteralPath $Path -Force
    $result = [ordered]@{
        path = $item.FullName
        exists = $true
        size = [long]$item.Length
        last_write_utc = $item.LastWriteTimeUtc.ToString('o')
    }
    if ($IncludeSha256) {
        $result.sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return [pscustomobject]$result
}

function Get-DirectoryObservation {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        return [pscustomobject]@{
            path = $Path
            exists = $false
        }
    }

    $files = @(Get-ChildItem -LiteralPath $Path -File -Recurse -Force -ErrorAction Stop)
    $totalBytes = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $totalBytes) {
        $totalBytes = 0
    }
    return [pscustomobject]@{
        path = (Resolve-Path -LiteralPath $Path).Path
        exists = $true
        file_count = $files.Count
        total_bytes = [long]$totalBytes
    }
}

function Get-OptionalCimData {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action
    )

    try {
        return [pscustomobject]@{
            available = $true
            error = $null
            values = @(& $Action)
        }
    }
    catch {
        return [pscustomobject]@{
            available = $false
            error = $_.Exception.Message
            values = @()
        }
    }
}

if ($env:OS -ne 'Windows_NT') {
    throw 'This inventory tool supports the current Windows deployment only.'
}

$RepositoryRoot = Get-AbsolutePath -Path $RepositoryRoot -Description 'RepositoryRoot'
$OutputPath = Get-AbsolutePath -Path $OutputPath -Description 'OutputPath'
if (-not (Test-Path -LiteralPath $RepositoryRoot -PathType Container)) {
    throw "RepositoryRoot was not found: $RepositoryRoot"
}
if (Test-PathInside -Candidate $OutputPath -Parent $RepositoryRoot) {
    throw 'OutputPath must be outside RepositoryRoot so inventory does not dirty production Git state.'
}
if (Test-Path -LiteralPath $OutputPath) {
    throw "OutputPath already exists: $OutputPath"
}

$outputParent = Split-Path -Parent $OutputPath
if ([string]::IsNullOrWhiteSpace($outputParent)) {
    throw 'OutputPath must have a parent directory.'
}
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    [void][System.IO.Directory]::CreateDirectory($outputParent)
}

$gitCommand = Get-Command -Name 'git.exe' -CommandType Application -ErrorAction Stop |
    Select-Object -First 1
$gitPrefix = @('-C', $RepositoryRoot)
$inside = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('rev-parse', '--is-inside-work-tree')) `
    -Description 'Git worktree check'
if ($inside.lines.Count -ne 1 -or $inside.lines[0].Trim() -ne 'true') {
    throw 'RepositoryRoot is not a Git worktree.'
}

$gitTop = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('rev-parse', '--show-toplevel')) `
    -Description 'Git root lookup'
$resolvedGitTop = Get-AbsolutePath -Path $gitTop.lines[0].Trim() -Description 'Git root'
if (-not $resolvedGitTop.Equals($RepositoryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'RepositoryRoot must be the Git worktree root.'
}

$head = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('rev-parse', 'HEAD')) `
    -Description 'Git HEAD lookup'
$branch = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('symbolic-ref', '--quiet', '--short', 'HEAD')) `
    -Description 'Git branch lookup' `
    -AllowFailure
$upstream = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')) `
    -Description 'Git upstream lookup' `
    -AllowFailure
$status = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('status', '--porcelain=v1', '--untracked-files=all')) `
    -Description 'Git status lookup'
$remotes = Invoke-NativeCapture `
    -FilePath $gitCommand.Source `
    -ArgumentList ($gitPrefix + @('remote')) `
    -Description 'Git remote-name lookup'

$DatabasePath = if ([string]::IsNullOrWhiteSpace($DatabasePath)) {
    Join-Path $RepositoryRoot 'db.sqlite3'
} else {
    Get-AbsolutePath -Path $DatabasePath -Description 'DatabasePath'
}
$MediaRoot = if ([string]::IsNullOrWhiteSpace($MediaRoot)) {
    Join-Path $RepositoryRoot 'media'
} else {
    Get-AbsolutePath -Path $MediaRoot -Description 'MediaRoot'
}
$EnvironmentFile = if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    Join-Path $RepositoryRoot '.env'
} else {
    Get-AbsolutePath -Path $EnvironmentFile -Description 'EnvironmentFile'
}

$processResult = Get-OptionalCimData -Action {
    Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
        Where-Object {
            $name = [string]$_.Name
            $commandLine = [string]$_.CommandLine
            $executablePath = [string]$_.ExecutablePath
            $name -match '^(?i:pythonw?|waitress-serve|nginx|bun|node)(?:\.exe)?$' -or
            $commandLine.IndexOf($RepositoryRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $executablePath.IndexOf($RepositoryRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        } |
        Sort-Object -Property ProcessId |
        ForEach-Object {
            [pscustomobject]@{
                process_id = [int]$_.ProcessId
                parent_process_id = [int]$_.ParentProcessId
                name = [string]$_.Name
                executable_path = [string]$_.ExecutablePath
                command_line = Protect-CommandLine -Value ([string]$_.CommandLine)
            }
        }
}

$serviceResult = Get-OptionalCimData -Action {
    Get-CimInstance -ClassName Win32_Service -ErrorAction Stop |
        Where-Object {
            $pathName = [string]$_.PathName
            $name = [string]$_.Name
            $displayName = [string]$_.DisplayName
            $pathName.IndexOf($RepositoryRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $name -match '(?i)ffxiv|nginx|waitress' -or
            $displayName -match '(?i)ffxiv|nginx|waitress' -or
            $pathName -match '(?i)nginx|waitress|manage\.py|wsgi'
        } |
        Sort-Object -Property Name |
        ForEach-Object {
            [pscustomobject]@{
                name = [string]$_.Name
                display_name = [string]$_.DisplayName
                state = [string]$_.State
                start_mode = [string]$_.StartMode
                start_name = [string]$_.StartName
                path_name = Protect-CommandLine -Value ([string]$_.PathName)
            }
        }
}

$listenerResult = Get-OptionalCimData -Action {
    $processIds = @($processResult.values | ForEach-Object { $_.process_id })
    Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object {
            $_.LocalPort -in @(80, 443, 8000) -or
            $processIds -contains [int]$_.OwningProcess
        } |
        Sort-Object -Property LocalPort, LocalAddress, OwningProcess |
        ForEach-Object {
            [pscustomobject]@{
                local_address = [string]$_.LocalAddress
                local_port = [int]$_.LocalPort
                owning_process_id = [int]$_.OwningProcess
            }
        }
}

$nginxRoots = New-Object System.Collections.Generic.List[string]
if (-not [string]::IsNullOrWhiteSpace($NginxRoot)) {
    $nginxRoots.Add((Get-AbsolutePath -Path $NginxRoot -Description 'NginxRoot'))
}
$defaultNginxRoot = 'C:\nginx'
if (Test-Path -LiteralPath $defaultNginxRoot -PathType Container) {
    $nginxRoots.Add($defaultNginxRoot)
}
foreach ($process in $processResult.values) {
    if ($process.name -match '^(?i:nginx)(?:\.exe)?$' -and
        -not [string]::IsNullOrWhiteSpace($process.executable_path)) {
        $nginxRoots.Add((Split-Path -Parent $process.executable_path))
    }
}

$nginxObservations = @($nginxRoots |
    Select-Object -Unique |
    Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
    ForEach-Object {
        $root = (Resolve-Path -LiteralPath $_).Path
        $confRoot = Join-Path $root 'conf'
        $configFiles = @()
        if (Test-Path -LiteralPath $confRoot -PathType Container) {
            $configFiles = @(Get-ChildItem -LiteralPath $confRoot -File -Recurse -Force |
                Where-Object { $_.Extension -eq '.conf' } |
                Sort-Object -Property FullName |
                ForEach-Object {
                    [pscustomobject]@{
                        relative_path = $_.FullName.Substring($root.Length).TrimStart('\', '/')
                        size = [long]$_.Length
                        last_write_utc = $_.LastWriteTimeUtc.ToString('o')
                        sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                    }
                })
        }
        [pscustomobject]@{
            root = $root
            config_contents_collected = $false
            config_files = $configFiles
        }
    })

$startupFiles = @(Get-ChildItem -LiteralPath $RepositoryRoot -File -Force |
    Where-Object { $_.Extension -in @('.bat', '.cmd', '.ps1', '.sh') } |
    Sort-Object -Property Name |
    ForEach-Object {
        [pscustomobject]@{
            name = $_.Name
            size = [long]$_.Length
            last_write_utc = $_.LastWriteTimeUtc.ToString('o')
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            content_collected = $false
        }
    })

$osResult = Get-OptionalCimData -Action {
    Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop |
        Select-Object -First 1 |
        ForEach-Object {
            [pscustomobject]@{
                caption = [string]$_.Caption
                version = [string]$_.Version
                build_number = [string]$_.BuildNumber
                os_architecture = [string]$_.OSArchitecture
                last_boot_utc = $_.LastBootUpTime.ToUniversalTime().ToString('o')
            }
        }
}

$pythonPath = Join-Path $RepositoryRoot 'venv\Scripts\python.exe'
$pythonVersion = $null
if (Test-Path -LiteralPath $pythonPath -PathType Leaf) {
    $pythonResult = Invoke-NativeCapture `
        -FilePath $pythonPath `
        -ArgumentList @('--version') `
        -Description 'Virtual-environment Python version'
    $pythonVersion = ($pythonResult.lines -join ' ').Trim()
}

$databaseObservation = Get-FileObservation -Path $DatabasePath
$databaseSidecars = @(
    Get-FileObservation -Path ($DatabasePath + '-wal')
    Get-FileObservation -Path ($DatabasePath + '-shm')
    Get-FileObservation -Path ($DatabasePath + '-journal')
)

$report = [ordered]@{
    format = 'ffxivshare-current-git-deployment-facts'
    format_version = 1
    status = 'captured'
    generated_at = [DateTime]::UtcNow.ToString('o')
    read_only_observation = $true
    output_report_is_only_intentional_write = $true
    cutover_authorized = $false
    secrets_collected = $false
    contains_operational_metadata = $true
    host = [ordered]@{
        computer_name = $env:COMPUTERNAME
        operator = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        powershell_version = $PSVersionTable.PSVersion.ToString()
        operating_system = $osResult
    }
    repository = [ordered]@{
        root = $RepositoryRoot
        head = $head.lines[0].Trim()
        branch = if ($branch.exit_code -eq 0) { ($branch.lines -join '').Trim() } else { $null }
        upstream = if ($upstream.exit_code -eq 0) { ($upstream.lines -join '').Trim() } else { $null }
        remote_names = @($remotes.lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        dirty = $status.lines.Count -gt 0
        status_porcelain = @($status.lines)
        startup_file_inventory = $startupFiles
    }
    runtime = [ordered]@{
        virtual_environment_python = Get-FileObservation -Path $pythonPath
        python_version = $pythonVersion
        manage_py = Get-FileObservation -Path (Join-Path $RepositoryRoot 'manage.py') -IncludeSha256
        environment_file = Get-FileObservation -Path $EnvironmentFile
        environment_file_contents_collected = $false
    }
    data = [ordered]@{
        database = $databaseObservation
        database_sidecars = $databaseSidecars
        database_contents_opened = $false
        database_sha256_computed = $false
        media = Get-DirectoryObservation -Path $MediaRoot
        media_contents_collected = $false
    }
    processes = $processResult
    services = $serviceResult
    listeners = $listenerResult
    nginx = [ordered]@{
        roots = $nginxObservations
        config_contents_collected = $false
    }
    limitations = @(
        'No environment-file contents or process environment blocks are collected.',
        'The active SQLite database is not opened or hashed; use the approved backup tool for a consistent snapshot.',
        'Nginx configuration contents are not collected; only .conf file metadata and SHA256 values are recorded.',
        'Absence from process and service listings does not prove that no external proxy or scheduler exists.'
    )
}

$json = $report | ConvertTo-Json -Depth 12
$encoding = New-Object System.Text.UTF8Encoding($false)
$stream = New-Object System.IO.FileStream(
    $OutputPath,
    [System.IO.FileMode]::CreateNew,
    [System.IO.FileAccess]::Write,
    [System.IO.FileShare]::None
)
try {
    $writer = New-Object System.IO.StreamWriter($stream, $encoding)
    try {
        $writer.WriteLine($json)
    }
    finally {
        $writer.Dispose()
    }
}
finally {
    $stream.Dispose()
}

$outputHash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
[pscustomobject]@{
    status = 'captured'
    report = $OutputPath
    sha256 = $outputHash
    cutover_authorized = $false
} | ConvertTo-Json -Compress
