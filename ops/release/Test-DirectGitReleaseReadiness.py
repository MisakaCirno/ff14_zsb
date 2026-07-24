from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any


FORMAT = "ffxivshare-direct-git-release-readiness"
FORMAT_VERSION = 1
PYTHON_MINIMUM = (3, 11, 0)
PYTHON_MAXIMUM = (3, 12, 0)
PLACEHOLDERS = {
    "change-me",
    "changeme",
    "example",
    "replace-me",
    "replace-with-deployed-release-id",
    "unknown",
}
REQUIRED_ENVIRONMENT_KEYS = {
    "ALLOWED_HOSTS",
    "APP_ENV",
    "CSRF_TRUSTED_ORIGINS",
    "DATABASE_ENGINE",
    "DATABASE_PATH",
    "DEBUG",
    "MEDIA_ROOT",
    "SECRET_KEY",
}
RECOMMENDED_ENVIRONMENT_KEYS = {
    "APP_VERSION",
    "CSRF_COOKIE_SECURE",
    "RATE_LIMIT_ENABLED",
    "REQUEST_LOG_ENABLED",
    "SECURE_HSTS_SECONDS",
    "SECURE_SSL_REDIRECT",
    "SESSION_COOKIE_SECURE",
    "SQLITE_JOURNAL_MODE",
    "SQLITE_SYNCHRONOUS",
    "SQLITE_TIMEOUT",
    "SQLITE_TRANSACTION_MODE",
    "TRUST_X_FORWARDED_FOR",
}
KNOWN_ENVIRONMENT_KEYS = REQUIRED_ENVIRONMENT_KEYS | RECOMMENDED_ENVIRONMENT_KEYS | {
    "APP_VERSION",
    "CSP_REPORT_ONLY",
    "DATABASE_CONNECT_TIMEOUT",
    "DATABASE_CONN_MAX_AGE",
    "DATABASE_HOST",
    "DATABASE_NAME",
    "DATABASE_PASSWORD",
    "DATABASE_PORT",
    "DATABASE_SSLMODE",
    "DATABASE_USER",
    "FFXIVSHARE_ENV_FILE",
    "RENDERER_PROXY_MAX_BYTES",
    "RENDERER_PROXY_TIMEOUT_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS",
    "SECURE_HSTS_PRELOAD",
}
CRITICAL_DIRTY_PREFIXES = (
    "ffxivshare/",
    "frontend/",
    "ops/release/",
    "shares/",
    "static/",
    "templates/",
)
CRITICAL_DIRTY_FILES = {
    ".env.production.sample",
    "manage.py",
    "preflight_ffxivshare.bat",
    "requirements.txt",
    "start_ffxivshare.bat",
    "verify.ps1",
}


class CheckCollector:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        check_id: str,
        status: str,
        message: str,
        **details: Any,
    ) -> None:
        item: dict[str, Any] = {
            "id": check_id,
            "status": status,
            "message": message,
        }
        if details:
            item["details"] = details
        self.items.append(item)

    def passed(self, check_id: str, message: str, **details: Any) -> None:
        self.add(check_id, "pass", message, **details)

    def failed(self, check_id: str, message: str, **details: Any) -> None:
        self.add(check_id, "fail", message, **details)

    def warned(self, check_id: str, message: str, **details: Any) -> None:
        self.add(check_id, "warn", message, **details)

    @property
    def blocker_ids(self) -> list[str]:
        return [item["id"] for item in self.items if item["status"] == "fail"]

    @property
    def warning_ids(self) -> list[str]:
        return [item["id"] for item in self.items if item["status"] == "warn"]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a redacted, read-only readiness report for direct Git deployment."
    )
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--environment-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--target-commit", required=True)
    return parser.parse_args()


def is_inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def run_command(
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run_command(["git", "-C", str(repository), *arguments], cwd=repository)


def canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_requirement_pins(path: Path) -> tuple[dict[str, str], list[int]]:
    pins: dict[str, str] = {}
    unsupported_lines: list[int] = []
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")
    for line_number, source_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match:
            unsupported_lines.append(line_number)
            continue
        pins[canonical_package_name(match.group(1))] = match.group(2)
    return pins, unsupported_lines


def strip_dotenv_value(raw_value: str) -> tuple[str, bool]:
    value = raw_value.strip()
    if not value:
        return "", True
    if value[0] in {"'", '"'}:
        quote = value[0]
        if len(value) < 2 or value[-1] != quote:
            return "", False
        inner = value[1:-1]
        if quote == '"':
            inner = (
                inner.replace(r"\n", "\n")
                .replace(r"\r", "\r")
                .replace(r"\t", "\t")
                .replace(r'\"', '"')
                .replace(r"\\", "\\")
            )
        return inner, True
    return re.split(r"\s+#", value, maxsplit=1)[0].strip(), True


def parse_environment_file(
    path: Path,
) -> tuple[dict[str, str], list[str], list[int]]:
    values: dict[str, str] = {}
    duplicates: list[str] = []
    malformed_lines: list[int] = []
    pattern = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    for line_number, source_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if not match:
            malformed_lines.append(line_number)
            continue
        name, raw_value = match.groups()
        value, valid = strip_dotenv_value(raw_value)
        if not valid:
            malformed_lines.append(line_number)
            continue
        if name in values:
            duplicates.append(name)
        values[name] = value
    return values, sorted(set(duplicates)), malformed_lines


def parse_bool(value: str | None, default: bool) -> bool | None:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def parse_integer(value: str | None, default: int) -> int | None:
    if value is None or not value.strip():
        return default
    try:
        return int(value.strip())
    except ValueError:
        return None


def parse_semver(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?", value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def parse_node_range(value: str) -> tuple[tuple[int, int, int], int] | None:
    match = re.fullmatch(
        r">=(\d+)\.(\d+)\.(\d+)\s+<(\d+)", value.strip()
    )
    if not match:
        return None
    return (tuple(int(part) for part in match.groups()[:3]), int(match.group(4)))


def extract_dirty_paths(status_output: str) -> list[str]:
    paths: set[str] = set()
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            old_path, new_path = value.split(" -> ", maxsplit=1)
            paths.update((old_path.strip('"'), new_path.strip('"')))
        elif value:
            paths.add(value.strip('"'))
    return sorted(path.replace("\\", "/") for path in paths)


def is_critical_dirty_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized in CRITICAL_DIRTY_FILES or normalized.startswith(
        CRITICAL_DIRTY_PREFIXES
    )


def inspect_manifest(path: Path, asset_root: Path) -> tuple[bool, list[str]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, []
    entry = manifest.get("src/main.ts")
    if not isinstance(entry, dict) or entry.get("isEntry") is not True:
        return False, []
    relative_assets = [entry.get("file"), *(entry.get("css") or [])]
    if not all(isinstance(item, str) and item for item in relative_assets):
        return False, []
    missing = [item for item in relative_assets if not (asset_root / item).is_file()]
    return not missing, missing


def inspect_port_8000() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.2)
        return connection.connect_ex(("127.0.0.1", 8000)) == 0


def main() -> int:
    arguments = parse_arguments()
    checks = CheckCollector()
    repository = Path(arguments.repository_root).resolve()
    environment_file = Path(arguments.environment_file).resolve()
    output_path = Path(arguments.output).resolve()

    if os.name != "nt":
        raise RuntimeError("The direct Git readiness check supports Windows only.")
    if not repository.is_dir() or not (repository / "manage.py").is_file():
        raise RuntimeError("RepositoryRoot is not a Django deployment root.")
    if is_inside(output_path, repository):
        raise RuntimeError("The readiness report must be written outside the repository.")
    if output_path.exists():
        raise RuntimeError("The readiness report already exists.")
    if not environment_file.is_file():
        raise RuntimeError("The environment file does not exist.")

    environment_stat = environment_file.stat()
    if environment_file.is_symlink() or environment_stat.st_nlink != 1:
        checks.failed(
            "environment_file_identity",
            "The environment file must be one regular, single-link file.",
        )
    else:
        checks.passed(
            "environment_file_identity",
            "The environment file has an unambiguous filesystem identity.",
        )

    inside = git(repository, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise RuntimeError("RepositoryRoot is not a Git worktree.")
    head_result = git(repository, "rev-parse", "HEAD")
    if head_result.returncode != 0:
        raise RuntimeError("Git HEAD could not be resolved.")
    head = head_result.stdout.strip().lower()
    requested_target = arguments.target_commit.strip()
    target_result = git(repository, "rev-parse", "--verify", f"{requested_target}^{{commit}}")
    if target_result.returncode != 0:
        checks.failed("target_commit", "The requested target commit is not available locally.")
        target_commit = requested_target.lower()
    else:
        target_commit = target_result.stdout.strip().lower()
        if not re.fullmatch(r"[a-f0-9]{40}", requested_target.lower()):
            checks.failed(
                "target_commit",
                "TargetCommit must be supplied as a full 40-character commit SHA.",
            )
        elif target_commit != head:
            checks.failed(
                "target_commit",
                "The checked-out worktree is not the requested target commit.",
            )
        else:
            checks.passed(
                "target_commit",
                "The worktree is pinned to the requested immutable commit.",
            )

    status_result = git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    if status_result.returncode != 0:
        raise RuntimeError("Git status could not be read.")
    dirty_paths = extract_dirty_paths(status_result.stdout)
    critical_dirty_paths = [path for path in dirty_paths if is_critical_dirty_path(path)]
    noncritical_dirty_paths = [path for path in dirty_paths if path not in critical_dirty_paths]
    if critical_dirty_paths:
        checks.failed(
            "critical_worktree_changes",
            "Runtime-critical files contain local changes.",
            paths=critical_dirty_paths,
        )
    else:
        checks.passed(
            "critical_worktree_changes",
            "Runtime-critical files match the checked-out commit.",
        )
    if noncritical_dirty_paths:
        checks.warned(
            "noncritical_worktree_changes",
            "Non-runtime local files remain and must be preserved during deployment.",
            paths=noncritical_dirty_paths,
        )
    else:
        checks.passed(
            "noncritical_worktree_changes",
            "No additional local worktree files were observed.",
        )

    file_values, duplicate_keys, malformed_lines = parse_environment_file(environment_file)
    configured_keys = sorted(file_values)
    process_override_keys = sorted(KNOWN_ENVIRONMENT_KEYS.intersection(os.environ))
    effective_values = dict(file_values)
    for name in process_override_keys:
        if name != "FFXIVSHARE_ENV_FILE":
            effective_values[name] = os.environ[name]

    if duplicate_keys or malformed_lines:
        checks.failed(
            "environment_syntax",
            "The environment file contains duplicate keys or malformed lines.",
            duplicate_keys=duplicate_keys,
            malformed_line_numbers=malformed_lines,
        )
    else:
        checks.passed(
            "environment_syntax",
            "The environment file has unique, parseable assignments.",
        )
    missing_required = sorted(REQUIRED_ENVIRONMENT_KEYS.difference(file_values))
    if missing_required:
        checks.failed(
            "required_environment_keys",
            "Required production environment keys are missing.",
            missing_keys=missing_required,
        )
    else:
        checks.passed(
            "required_environment_keys",
            "All required production environment keys are configured.",
        )
    missing_recommended = sorted(RECOMMENDED_ENVIRONMENT_KEYS.difference(file_values))
    if missing_recommended:
        checks.warned(
            "recommended_environment_keys",
            "Safe defaults exist, but explicit production keys are recommended.",
            missing_keys=missing_recommended,
        )
    else:
        checks.passed(
            "recommended_environment_keys",
            "Recommended production settings are explicit.",
        )

    app_env = effective_values.get("APP_ENV", "").strip().lower()
    debug = parse_bool(effective_values.get("DEBUG"), app_env != "production")
    secret_key = effective_values.get("SECRET_KEY", "").strip()
    allowed_hosts = [
        item.strip()
        for item in effective_values.get("ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    csrf_origins = [
        item.strip()
        for item in effective_values.get("CSRF_TRUSTED_ORIGINS", "").split(",")
        if item.strip()
    ]
    production_values_valid = (
        app_env == "production"
        and debug is False
        and len(secret_key) >= 50
        and len(set(secret_key)) >= 5
        and not secret_key.startswith("django-insecure-")
        and bool(allowed_hosts)
        and "*" not in allowed_hosts
        and bool(csrf_origins)
        and all(origin.lower().startswith("https://") for origin in csrf_origins)
    )
    if production_values_valid:
        checks.passed(
            "production_environment_values",
            "Core production values pass redacted validation.",
        )
    else:
        checks.failed(
            "production_environment_values",
            "Core production values failed redacted validation; no values were recorded.",
        )

    secure_booleans = {
        "CSRF_COOKIE_SECURE": parse_bool(
            effective_values.get("CSRF_COOKIE_SECURE"), True
        ),
        "RATE_LIMIT_ENABLED": parse_bool(
            effective_values.get("RATE_LIMIT_ENABLED"), True
        ),
        "REQUEST_LOG_ENABLED": parse_bool(
            effective_values.get("REQUEST_LOG_ENABLED"), True
        ),
        "SECURE_SSL_REDIRECT": parse_bool(
            effective_values.get("SECURE_SSL_REDIRECT"), True
        ),
        "SESSION_COOKIE_SECURE": parse_bool(
            effective_values.get("SESSION_COOKIE_SECURE"), True
        ),
        "TRUST_X_FORWARDED_FOR": parse_bool(
            effective_values.get("TRUST_X_FORWARDED_FOR"), True
        ),
    }
    invalid_secure_booleans = sorted(
        name for name, value in secure_booleans.items() if value is not True
    )
    hsts_seconds = parse_integer(effective_values.get("SECURE_HSTS_SECONDS"), 31536000)
    if invalid_secure_booleans or hsts_seconds is None or hsts_seconds <= 0:
        checks.failed(
            "production_security_settings",
            "Required HTTPS, proxy, logging, or rate-limit settings are not safely enabled.",
            invalid_keys=invalid_secure_booleans,
        )
    else:
        checks.passed(
            "production_security_settings",
            "HTTPS, proxy, logging, and rate-limit settings are enabled.",
        )

    app_version = effective_values.get("APP_VERSION", "").strip().lower()
    if not app_version or app_version in PLACEHOLDERS or app_version != target_commit:
        checks.warned(
            "application_version_binding",
            "Effective APP_VERSION is not the target commit; the unified launcher will override it.",
        )
    else:
        checks.passed(
            "application_version_binding",
            "APP_VERSION matches the target commit.",
        )

    database_path: Path | None = None
    database_engine = effective_values.get("DATABASE_ENGINE", "").strip().lower()
    raw_database_path = effective_values.get("DATABASE_PATH", "").strip()
    sqlite_values_valid = (
        database_engine == "sqlite"
        and raw_database_path
        and PureWindowsPath(raw_database_path).is_absolute()
        and not raw_database_path.startswith("\\\\")
        and parse_integer(effective_values.get("SQLITE_TIMEOUT"), 30) is not None
        and (parse_integer(effective_values.get("SQLITE_TIMEOUT"), 30) or 0) >= 30
        and effective_values.get("SQLITE_TRANSACTION_MODE", "IMMEDIATE").strip().upper()
        == "IMMEDIATE"
        and effective_values.get("SQLITE_JOURNAL_MODE", "WAL").strip().upper()
        == "WAL"
        and effective_values.get("SQLITE_SYNCHRONOUS", "FULL").strip().upper()
        == "FULL"
    )
    if sqlite_values_valid:
        database_path = Path(raw_database_path).resolve()
        checks.passed(
            "sqlite_configuration",
            "SQLite uses an explicit local path and the production durability settings.",
        )
    else:
        checks.failed(
            "sqlite_configuration",
            "SQLite configuration is missing, non-local, or below the durability contract.",
        )

    if database_path is None or not database_path.is_file():
        checks.failed("database_file", "The configured SQLite database file was not found.")
    else:
        database_stat = database_path.stat()
        sidecars = [
            str(database_path) + suffix
            for suffix in ("-wal", "-shm", "-journal")
            if Path(str(database_path) + suffix).exists()
        ]
        if database_path.is_symlink() or database_stat.st_nlink != 1 or sidecars:
            checks.failed(
                "database_file",
                "The database identity is ambiguous or SQLite sidecars are present.",
                sidecars=[Path(path).name for path in sidecars],
            )
        else:
            checks.passed(
                "database_file",
                "The configured database is a single-link file with no sidecars.",
                path=str(database_path),
                size=database_stat.st_size,
            )

    raw_media_root = effective_values.get("MEDIA_ROOT", "").strip()
    media_root: Path | None = None
    if (
        raw_media_root
        and PureWindowsPath(raw_media_root).is_absolute()
        and not raw_media_root.startswith("\\\\")
    ):
        media_root = Path(raw_media_root).resolve()
        if media_root.is_dir():
            checks.passed(
                "media_root",
                "The configured media directory exists.",
                path=str(media_root),
            )
        else:
            checks.warned(
                "media_root",
                "The configured media directory does not exist yet.",
                path=str(media_root),
            )
    else:
        checks.failed("media_root", "MEDIA_ROOT must be an explicit local Windows path.")

    current_python = Path(sys.executable).resolve()
    expected_python = (repository / "venv" / "Scripts" / "python.exe").resolve()
    python_version = tuple(sys.version_info[:3])
    if current_python != expected_python or not (
        PYTHON_MINIMUM <= python_version < PYTHON_MAXIMUM
    ):
        checks.failed(
            "python_runtime",
            "Readiness must run with the repository Python 3.11 virtual environment.",
            executable=str(current_python),
            version=".".join(str(part) for part in python_version),
        )
    else:
        checks.passed(
            "python_runtime",
            "The repository Python 3.11 virtual environment is active.",
            executable=str(current_python),
            version=".".join(str(part) for part in python_version),
        )

    requirements_path = repository / "requirements.txt"
    requirement_pins, unsupported_requirement_lines = parse_requirement_pins(
        requirements_path
    )
    if unsupported_requirement_lines or not requirement_pins:
        checks.failed(
            "python_requirement_contract",
            "Runtime requirements must contain only exact package pins.",
            unsupported_line_numbers=unsupported_requirement_lines,
        )
    else:
        checks.passed(
            "python_requirement_contract",
            "Runtime requirements use exact package pins.",
            package_count=len(requirement_pins),
        )
    mismatched_packages: list[str] = []
    for package_name, expected_version in requirement_pins.items():
        try:
            installed_version = importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            mismatched_packages.append(package_name)
            continue
        if installed_version != expected_version:
            mismatched_packages.append(package_name)
    if mismatched_packages:
        checks.failed(
            "python_dependencies",
            "The virtual environment does not match requirements.txt.",
            mismatched_packages=sorted(mismatched_packages),
        )
    else:
        pip_check = run_command(
            [str(current_python), "-m", "pip", "check"], cwd=repository
        )
        if pip_check.returncode == 0:
            checks.passed(
                "python_dependencies",
                "Pinned Python packages are installed and dependency metadata is consistent.",
            )
        else:
            checks.failed(
                "python_dependencies",
                "Python dependency metadata is inconsistent.",
            )

    package_json_path = repository / "frontend" / "package.json"
    package_lock_path = repository / "frontend" / "package-lock.json"
    try:
        package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
        package_lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
        lock_root = package_lock["packages"][""]
        lock_matches = (
            package_lock.get("lockfileVersion") == 3
            and lock_root.get("dependencies", {}) == package_json.get("dependencies", {})
            and lock_root.get("devDependencies", {})
            == package_json.get("devDependencies", {})
            and lock_root.get("engines", {}) == package_json.get("engines", {})
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        package_json = {}
        lock_matches = False
    if lock_matches:
        checks.passed(
            "frontend_lock",
            "package-lock.json v3 matches the frontend package contract.",
        )
    else:
        checks.failed(
            "frontend_lock",
            "package-lock.json does not match package.json.",
        )

    node_executable = shutil.which("node.exe") or shutil.which("node")
    npm_executable = shutil.which("npm.cmd") or shutil.which("npm")
    node_version_text = ""
    npm_version_text = ""
    node_range = parse_node_range(str(package_json.get("engines", {}).get("node", "")))
    if node_executable and node_range:
        node_version_result = run_command([node_executable, "--version"], cwd=repository)
        node_version_text = node_version_result.stdout.strip()
        node_version = parse_semver(node_version_text)
        minimum_node, maximum_major = node_range
        node_valid = (
            node_version_result.returncode == 0
            and node_version is not None
            and node_version >= minimum_node
            and node_version[0] < maximum_major
        )
    else:
        node_valid = False
    if node_valid:
        checks.passed(
            "node_runtime",
            "Node satisfies the frontend engine contract.",
            executable=node_executable,
            version=node_version_text.lstrip("v"),
        )
    else:
        checks.failed(
            "node_runtime",
            "Node is missing or outside the frontend engine contract.",
        )

    if npm_executable:
        npm_version_result = run_command([npm_executable, "--version"], cwd=repository)
        npm_version_text = npm_version_result.stdout.strip()
        npm_inventory = run_command(
            [
                npm_executable,
                "--prefix",
                str(repository / "frontend"),
                "ls",
                "--depth=0",
                "--json",
            ],
            cwd=repository,
            timeout=120,
        )
        npm_valid = npm_version_result.returncode == 0 and npm_inventory.returncode == 0
    else:
        npm_valid = False
    if npm_valid:
        checks.passed(
            "frontend_dependencies",
            "Installed frontend packages satisfy the lockfile root contract.",
            npm_version=npm_version_text,
        )
    else:
        checks.failed(
            "frontend_dependencies",
            "Frontend packages are missing or inconsistent; run npm ci before release.",
        )

    source_manifest = repository / "static" / "app" / "manifest.json"
    collected_manifest = repository / "staticfiles" / "app" / "manifest.json"
    source_manifest_valid, source_missing_assets = inspect_manifest(
        source_manifest, source_manifest.parent
    ) if source_manifest.is_file() else (False, [])
    source_inputs = [
        path
        for path in (repository / "frontend" / "src").rglob("*")
        if path.is_file()
    ]
    source_inputs.extend(
        path
        for path in (
            package_json_path,
            package_lock_path,
            repository / "frontend" / "vite.config.ts",
        )
        if path.is_file()
    )
    build_is_current = (
        source_manifest_valid
        and source_inputs
        and max(path.stat().st_mtime_ns for path in source_inputs)
        <= source_manifest.stat().st_mtime_ns
    )
    if build_is_current:
        checks.passed(
            "frontend_build",
            "The Vite manifest and referenced assets are current.",
        )
    else:
        checks.failed(
            "frontend_build",
            "Frontend build artifacts are missing, stale, or incomplete.",
            missing_asset_count=len(source_missing_assets),
        )

    collected_manifest_valid, collected_missing_assets = inspect_manifest(
        collected_manifest, collected_manifest.parent
    ) if collected_manifest.is_file() else (False, [])
    manifests_match = False
    if source_manifest.is_file() and collected_manifest.is_file():
        manifests_match = (
            hashlib.sha256(source_manifest.read_bytes()).digest()
            == hashlib.sha256(collected_manifest.read_bytes()).digest()
        )
    if collected_manifest_valid and manifests_match:
        checks.passed(
            "collected_static",
            "collectstatic contains the current frontend manifest and assets.",
        )
    else:
        checks.failed(
            "collected_static",
            "Collected static files are stale or incomplete; run collectstatic.",
            missing_asset_count=len(collected_missing_assets),
        )

    child_environment = os.environ.copy()
    child_environment["FFXIVSHARE_ENV_FILE"] = str(environment_file)
    child_environment["APP_VERSION"] = target_commit
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    child_environment["PYTHONUTF8"] = "1"
    django_check = run_command(
        [str(current_python), "-B", "manage.py", "check", "--deploy"],
        cwd=repository,
        environment=child_environment,
        timeout=120,
    )
    if django_check.returncode == 0:
        checks.passed(
            "django_deploy_check",
            "Django deployment checks completed successfully.",
        )
    else:
        checks.failed(
            "django_deploy_check",
            "Django deployment checks failed; output was intentionally not copied into the report.",
        )

    migration_drift = run_command(
        [
            str(current_python),
            "-B",
            "manage.py",
            "makemigrations",
            "--check",
            "--dry-run",
        ],
        cwd=repository,
        environment=child_environment,
        timeout=120,
    )
    if migration_drift.returncode == 0:
        checks.passed(
            "migration_drift",
            "Django models and committed migrations agree.",
        )
    else:
        checks.failed(
            "migration_drift",
            "Django model changes are missing committed migrations.",
        )

    schema_status = "unavailable"
    safe_to_start = False
    schema_check = run_command(
        [str(current_python), "-B", "manage.py", "check_deployment_schema"],
        cwd=repository,
        environment=child_environment,
        timeout=120,
    )
    schema_report: dict[str, Any] | None = None
    for output_line in reversed(
        (schema_check.stdout + "\n" + schema_check.stderr).splitlines()
    ):
        line = output_line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                schema_report = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if schema_report:
        schema_status = str(schema_report.get("status", "unavailable"))
        safe_to_start = bool(schema_report.get("safe_to_start", False))
    if schema_status == "current" and safe_to_start:
        checks.passed(
            "database_schema",
            "The database schema is current and safe to start.",
        )
    elif schema_status == "upgrade_required":
        checks.warned(
            "database_schema",
            "Database migrations are pending; the unified launcher must perform the verified upgrade.",
            pending_migration_count=len(schema_report.get("pending_migrations", [])),
        )
    else:
        checks.failed(
            "database_schema",
            "Database migration history could not be verified safely.",
        )

    if inspect_port_8000():
        checks.failed(
            "waitress_listener",
            "Port 8000 is listening; stop Waitress before the final readiness check.",
        )
    else:
        checks.passed(
            "waitress_listener",
            "Port 8000 is available for the maintenance workflow.",
        )

    branch_result = git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    upstream_result = git(
        repository,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    report = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cutover_authorized": False,
        "ready_for_maintenance": not checks.blocker_ids,
        "safe_to_start": safe_to_start,
        "database_upgrade_required": schema_status == "upgrade_required",
        "repository": {
            "root": str(repository),
            "head": head,
            "target_commit": target_commit,
            "branch": branch_result.stdout.strip() if branch_result.returncode == 0 else None,
            "upstream": upstream_result.stdout.strip()
            if upstream_result.returncode == 0
            else None,
            "dirty_paths": dirty_paths,
        },
        "environment": {
            "path": str(environment_file),
            "configured_keys": configured_keys,
            "process_override_keys": process_override_keys,
            "values_recorded": False,
        },
        "runtime": {
            "python_executable": str(current_python),
            "python_version": ".".join(str(part) for part in python_version),
            "node_executable": node_executable,
            "node_version": node_version_text.lstrip("v") or None,
            "npm_executable": npm_executable,
            "npm_version": npm_version_text or None,
        },
        "database": {
            "path": str(database_path) if database_path else None,
            "schema_status": schema_status,
        },
        "media_root": str(media_root) if media_root else None,
        "checks": checks.items,
        "blockers": checks.blocker_ids,
        "warnings": checks.warning_ids,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with output_path.open("x", encoding="utf-8", newline="\n") as output_file:
        output_file.write(serialized)
    report_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
    summary = {
        "status": "ready" if report["ready_for_maintenance"] else "not_ready",
        "report": str(output_path),
        "sha256": report_sha256,
        "blockers": checks.blocker_ids,
        "warnings": checks.warning_ids,
        "cutover_authorized": False,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if report["ready_for_maintenance"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
