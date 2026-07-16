[CmdletBinding()]
param(
    [string]$PythonExecutable = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Contract {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Invoke-Comparison {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Output,
        [Parameter(Mandatory = $true)][int]$ExpectedExitCode
    )
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $PythonExecutable -B $comparatorPath `
            --source $Source --target $Target --output $Output *> $logPath
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    # Expected negative cases must not leak their native exit code to verify.ps1.
    $global:LASTEXITCODE = 0
    Assert-Contract `
        -Condition ($exitCode -eq $ExpectedExitCode) `
        -Message "Comparator exited with $exitCode; expected $ExpectedExitCode."
}

function New-FixturePair {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Variant
    )
    $pairRoot = Join-Path $temporaryRoot $Name
    & $PythonExecutable -B $fixtureScript $pairRoot $Variant
    Assert-Contract `
        -Condition ($LASTEXITCODE -eq 0) `
        -Message "Fixture creation failed for $Name."
    return @(
        (Join-Path $pairRoot 'source'),
        (Join-Path $pairRoot 'target'),
        $pairRoot
    )
}

function Remove-TestRoot {
    param([Parameter(Mandatory = $true)][string]$Path)
    $temp = [System.IO.Path]::GetFullPath(
        [System.IO.Path]::GetTempPath()
    ).TrimEnd('\', '/')
    $resolved = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    Assert-Contract `
        -Condition ($resolved.StartsWith(
            $temp + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase
        )) `
        -Message 'Refusing to clean outside the system temp directory.'
    Assert-Contract `
        -Condition ((Split-Path -Leaf $resolved) -match '^ffxivshare-export-compare-[a-f0-9]{32}$') `
        -Message 'Refusing to clean an unexpected test directory.'
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$comparatorPath = Join-Path $PSScriptRoot 'Compare-SiteDataExports.py'
Assert-Contract `
    -Condition (Test-Path -LiteralPath $comparatorPath -PathType Leaf) `
    -Message "Comparator is missing: $comparatorPath"

$temporaryRoot = Join-Path `
    ([System.IO.Path]::GetTempPath()) `
    ('ffxivshare-export-compare-' + [Guid]::NewGuid().ToString('N'))
$fixtureScript = Join-Path $temporaryRoot 'fixture.py'
$logPath = Join-Path $temporaryRoot 'comparison.log'

$fixtureSource = @'
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
variant = sys.argv[2]
root.mkdir(parents=True)

entities = {
    "groups": ("auth.group", "groups.jsonl", "auth_group"),
    "users": ("auth.user", "users.jsonl", "auth_user"),
    "user_profiles": ("shares.userprofile", "user_profiles.jsonl", "shares_userprofile"),
    "shares": ("shares.share", "shares.jsonl", "shares_share"),
    "collections": ("shares.collection", "collections.jsonl", "shares_collection"),
    "collection_items": ("shares.collectionitem", "collection_items.jsonl", "shares_collectionitem"),
    "reports": ("shares.report", "reports.jsonl", "shares_report"),
    "share_logs": ("shares.sharelog", "share_logs.jsonl", "shares_sharelog"),
    "announcements": ("shares.announcement", "announcements.jsonl", "shares_announcement"),
    "site_messages": ("shares.sitemessage", "site_messages.jsonl", "shares_sitemessage"),
    "admin_log_entries": ("admin.logentry", "admin_log_entries.jsonl", "django_admin_log"),
}
embedded = [
    "auth_group_permissions", "auth_user_groups", "auth_user_user_permissions",
    "shares_share_favorites", "shares_share_likes",
]
regenerated = ["auth_permission", "django_content_type", "django_migrations"]

def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

def build(path, target):
    path.mkdir()
    metadata = {}
    sequences = {}
    for name, (model, filename, table) in entities.items():
        record = json.dumps(
            {"model": model, "pk": 1, "fields": {"sentinel": "record-secret-sentinel"}},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ) + "\n"
        payload = record.encode("utf-8")
        (path / filename).write_bytes(payload)
        metadata[name] = {
            "model": model, "file": filename, "count": 1,
            "sha256": sha256(payload).hexdigest(),
        }
        sequences[name] = {
            "table": table, "pk_field": "id", "max_live_pk": 1,
            "next_value_floor": 5 if target else 2,
        }

    if target:
        content_types = [["auth", "group"], ["contenttypes", "contenttype"]]
        permissions = [
            {"natural_key": ["view_contenttype", "contenttypes", "contenttype"], "name": "Can view content type"},
            {"natural_key": ["view_group", "auth", "group"], "name": "Can view group"},
        ]
        applied = [
            {"app": "shares", "name": "0001_initial", "applied_at": "2026-07-16T00:00:01.000000Z"},
            {"app": "shares", "name": "0002_forward", "applied_at": "2026-07-16T00:00:02.000000Z"},
        ]
        leaves = [["shares", "0002_forward"]]
        session = {
            "table": "django_session", "policy": "force_logout_at_cutover",
            "source_row_count": 0, "source_unexpired_count": 0,
            "source_latest_expiry": None, "target_required_row_count": 0,
        }
    else:
        content_types = [["auth", "group"]]
        permissions = [
            {"natural_key": ["view_group", "auth", "group"], "name": "Can view group"},
        ]
        applied = [
            {"app": "shares", "name": "0001_initial", "applied_at": "2026-07-15T00:00:01.000000Z"},
        ]
        leaves = [["shares", "0001_initial"]]
        session = {
            "table": "django_session", "policy": "force_logout_at_cutover",
            "source_row_count": 3, "source_unexpired_count": 2,
            "source_latest_expiry": "2026-07-17T00:00:00.000000Z",
            "target_required_row_count": 0,
        }

    manifest = {
        "format": "ffxivshare-jsonl", "format_version": 3,
        "codec": "canonical-jsonl-utc-microseconds",
        "schema_fingerprint": "5748cb65c7617cef02e2141435c80530b6736b1bd4c5ab91419772a374ad55c2",
        "model_schema_signature": "9b91a3b943d2986115508db51c216d94040053ec2c8e19b900acd2e0ddfdd685",
        "application_version": "target" if target else "source",
        "exported_at": "2026-07-16T00:00:00.000000Z",
        "source_database": "postgresql" if target else "sqlite",
        "entities": metadata,
        "dependencies": {
            "content_types": content_types,
            "permissions": permissions,
            "references": {
                "content_types": [["auth", "group"]],
                "permissions": [["view_group", "auth", "group"]],
            },
        },
        "migration_projection": {
            "table": "django_migrations", "applied": applied, "leaf_nodes": leaves,
        },
        "identity": {"sequences": sequences},
        "table_projection": {
            "direct": sorted(item[2] for item in entities.values()),
            "embedded": embedded, "regenerated": regenerated,
            "excluded": {"django_session": session},
            "internal": [] if target else ["sqlite_sequence"],
            "unknown_empty": [] if target else ["legacy_empty"],
            "unknown_nonempty": {}, "unsupported_objects": {},
        },
        "session_projection": session,
    }
    write_json(path / "manifest.json", manifest)
    write_json(path / "validation-report.json", {
        "format": "ffxivshare-jsonl", "format_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": "stale-staging-path", "valid": True,
        "entity_counts": {name: 1 for name in entities},
        "errors": [], "warnings": [], "quarantined_records": [],
    })
    return manifest

source_path = root / "source"
target_path = root / "target"
source = build(source_path, False)
target = build(target_path, True)

if variant == "bad-hash":
    with (target_path / "groups.jsonl").open("ab") as stream:
        stream.write(b"record-secret-sentinel\n")
elif variant == "extra-file":
    (target_path / "extra.jsonl").write_text("record-secret-sentinel", encoding="utf-8")
elif variant == "dependency-reference":
    target["dependencies"]["references"]["content_types"].append(["contenttypes", "contenttype"])
    write_json(target_path / "manifest.json", target)
elif variant == "sequence-lower":
    target["identity"]["sequences"]["groups"]["next_value_floor"] = 1
    write_json(target_path / "manifest.json", target)
elif variant == "target-session":
    session = target["session_projection"]
    session["source_row_count"] = 1
    session["source_unexpired_count"] = 1
    session["source_latest_expiry"] = "2026-07-17T00:00:00.000000Z"
    write_json(target_path / "manifest.json", target)
elif variant == "migration-backward":
    target["migration_projection"]["applied"] = [target["migration_projection"]["applied"][1]]
    write_json(target_path / "manifest.json", target)
elif variant == "malformed-validation":
    (target_path / "validation-report.json").write_text('{"bad":NaN}', encoding="utf-8")
elif variant == "invalid-validation-state":
    write_json(target_path / "validation-report.json", {
        "format": "ffxivshare-jsonl", "format_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset": "stale-staging-path", "valid": False,
        "entity_counts": {name: 1 for name in entities},
        "errors": ["synthetic invalid report"], "warnings": [],
        "quarantined_records": [],
    })
elif variant == "noncanonical-jsonl":
    for dataset_path, manifest in ((source_path, source), (target_path, target)):
        payload = (json.dumps({
            "model": "auth.group", "pk": 1,
            "fields": {"sentinel": "record-secret-sentinel"},
        }, ensure_ascii=False) + "\n").encode("utf-8")
        (dataset_path / "groups.jsonl").write_bytes(payload)
        manifest["entities"]["groups"]["sha256"] = sha256(payload).hexdigest()
        write_json(dataset_path / "manifest.json", manifest)
elif variant == "no-validation":
    (source_path / "validation-report.json").unlink()
    (target_path / "validation-report.json").unlink()
'@

try {
    [void](New-Item -ItemType Directory -Path $temporaryRoot)
    [System.IO.File]::WriteAllText(
        $fixtureScript,
        $fixtureSource,
        [System.Text.UTF8Encoding]::new($false)
    )

    $match = New-FixturePair -Name 'match' -Variant 'match'
    $matchOutput = Join-Path $match[2] 'comparison.json'
    Invoke-Comparison -Source $match[0] -Target $match[1] `
        -Output $matchOutput -ExpectedExitCode 0
    $matchText = Get-Content -LiteralPath $matchOutput -Raw
    $matchReport = $matchText | ConvertFrom-Json
    Assert-Contract ([bool]$matchReport.equivalent) 'Equivalent exports did not match.'
    Assert-Contract (-not [bool]$matchReport.cutover_authorized) `
        'Comparison evidence must never authorize cutover.'
    Assert-Contract (-not $matchText.Contains('record-secret-sentinel')) `
        'Comparison evidence exposed a record value.'
    $matchHash = (Get-FileHash -LiteralPath $matchOutput -Algorithm SHA256).Hash
    Invoke-Comparison -Source $match[0] -Target $match[1] `
        -Output $matchOutput -ExpectedExitCode 1
    Assert-Contract `
        -Condition ((Get-FileHash -LiteralPath $matchOutput -Algorithm SHA256).Hash -eq $matchHash) `
        -Message 'Existing evidence was overwritten.'

    $withoutValidation = New-FixturePair -Name 'no-validation' -Variant 'no-validation'
    Invoke-Comparison -Source $withoutValidation[0] -Target $withoutValidation[1] `
        -Output (Join-Path $withoutValidation[2] 'comparison.json') -ExpectedExitCode 0

    foreach ($case in @(
        'bad-hash',
        'extra-file',
        'dependency-reference',
        'sequence-lower',
        'target-session',
        'migration-backward',
        'malformed-validation',
        'invalid-validation-state',
        'noncanonical-jsonl'
    )) {
        $fixture = New-FixturePair -Name $case -Variant $case
        $output = Join-Path $fixture[2] 'comparison.json'
        Invoke-Comparison -Source $fixture[0] -Target $fixture[1] `
            -Output $output -ExpectedExitCode 2
        $text = Get-Content -LiteralPath $output -Raw
        $report = $text | ConvertFrom-Json
        Assert-Contract (-not [bool]$report.equivalent) "$case unexpectedly matched."
        Assert-Contract (-not [bool]$report.cutover_authorized) `
            "$case evidence authorized cutover."
        Assert-Contract (-not $text.Contains('record-secret-sentinel')) `
            "$case evidence exposed a record value."
    }

    $inside = New-FixturePair -Name 'inside-output' -Variant 'match'
    $insideOutput = Join-Path $inside[1] 'comparison.json'
    Invoke-Comparison -Source $inside[0] -Target $inside[1] `
        -Output $insideOutput -ExpectedExitCode 1
    Assert-Contract (-not (Test-Path -LiteralPath $insideOutput)) `
        'Comparator wrote evidence inside an immutable dataset.'

    Write-Host 'Site-data export comparison contracts passed.'
}
finally {
    Remove-TestRoot -Path $temporaryRoot
}
