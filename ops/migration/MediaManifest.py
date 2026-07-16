from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import unicodedata
from uuid import uuid4


MANIFEST_FORMAT = 'ffxivshare-media-manifest'
MANIFEST_VERSION = 2
HASH_ALGORITHM = 'sha256'
SNAPSHOT_ID_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')


class MediaManifestError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    )


def _reject_unsafe_path_shape(path: Path, *, label: str) -> None:
    if '..' in path.parts:
        raise MediaManifestError(f'{label} must not contain parent traversal.')
    if path == Path(path.anchor):
        raise MediaManifestError(f'{label} must not be a filesystem root.')
    if os.name == 'nt':
        if path.drive.startswith('\\\\'):
            raise MediaManifestError(f'{label} must be on a local drive, not UNC.')
        _drive, tail = os.path.splitdrive(str(path))
        if ':' in tail:
            raise MediaManifestError(f'{label} must not use an alternate data stream.')


def _reject_reparse_components(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise MediaManifestError(f'{label} cannot be inspected: {exc}') from exc
        if _is_reparse_point(current):
            raise MediaManifestError(
                f'{label} must not traverse a symlink or reparse point.'
            )


def _resolve_input_directory(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise MediaManifestError('Media root must be an absolute path.')
    _reject_unsafe_path_shape(path, label='Media root')
    _reject_reparse_components(path, label='Media root')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MediaManifestError(f'Media root cannot be resolved: {exc}') from exc
    if not resolved.is_dir():
        raise MediaManifestError('Media root must be an existing directory.')
    if _is_reparse_point(resolved):
        raise MediaManifestError('Media root must not be a symlink or reparse point.')
    return resolved


def _resolve_output_file(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise MediaManifestError('Output path must be absolute.')
    _reject_unsafe_path_shape(path, label='Output path')
    _reject_reparse_components(path.parent, label='Output parent')
    return path.resolve(strict=False)


def _resolve_input_file(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise MediaManifestError('Manifest input path must be absolute.')
    _reject_unsafe_path_shape(path, label='Manifest input path')
    _reject_reparse_components(path, label='Manifest input path')
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MediaManifestError(f'Manifest input cannot be resolved: {exc}') from exc
    if not resolved.is_file():
        raise MediaManifestError('Manifest input must be a regular file.')
    return resolved


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _canonical_path_key(value: str) -> str:
    decomposed = unicodedata.normalize('NFD', value)
    return unicodedata.normalize('NFC', decomposed.casefold())


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _file_sha256(path: Path) -> tuple[int, str]:
    digest = sha256()
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    try:
        path_after = path.stat()
    except OSError as exc:
        raise MediaManifestError(f'Media file disappeared while hashing: {path.name}') from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or _stat_identity(after) != _stat_identity(path_after)
    ):
        raise MediaManifestError(f'Media file changed while hashing: {path.name}')
    return after.st_size, digest.hexdigest()


def _iter_regular_files(root: Path):
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (
                    _canonical_path_key(entry.name),
                    entry.name,
                ),
                reverse=True,
            )
        except OSError as exc:
            raise MediaManifestError(f'Cannot enumerate media directory: {exc}') from exc
        for entry in entries:
            path = Path(entry.path)
            if _is_reparse_point(path):
                raise MediaManifestError(
                    f'Media tree contains a symlink or reparse point: {entry.name}'
                )
            if entry.is_dir(follow_symlinks=False):
                pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                yield path
            else:
                raise MediaManifestError(
                    f'Media tree contains a non-regular entry: {entry.name}'
                )


def _tree_inventory(
    root: Path,
) -> dict[str, tuple[Path, tuple[int, int, int, int, int]]]:
    inventory: dict[str, tuple[Path, tuple[int, int, int, int, int]]] = {}
    canonical_paths: dict[str, str] = {}
    for path in _iter_regular_files(root):
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, root):
            raise MediaManifestError(f'Media path escapes the root: {path.name}')
        relative = path.relative_to(root).as_posix()
        normalized = unicodedata.normalize('NFC', relative)
        if not normalized or normalized.startswith('/') or '..' in Path(normalized).parts:
            raise MediaManifestError(f'Invalid relative media path: {relative!r}')
        collision_key = _canonical_path_key(normalized)
        previous = canonical_paths.get(collision_key)
        if previous is not None:
            raise MediaManifestError(
                'Media paths collide after NFC and case-insensitive normalization: '
                f'{previous!r} and {normalized!r}'
            )
        canonical_paths[collision_key] = normalized
        inventory[normalized] = (path, _stat_identity(path.stat()))
    return inventory


def build_manifest(root: Path, *, snapshot_id: str) -> dict[str, object]:
    if SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
        raise MediaManifestError(
            'Snapshot ID must use 1-128 ASCII letters, digits, dots, dashes, or '
            'underscores.'
        )
    files: list[dict[str, object]] = []
    initial_inventory = _tree_inventory(root)
    total_size = 0
    for normalized in sorted(
        initial_inventory,
        key=lambda value: (_canonical_path_key(value), value),
    ):
        path, expected_identity = initial_inventory[normalized]
        size, digest = _file_sha256(path)
        if _stat_identity(path.stat()) != expected_identity:
            raise MediaManifestError(f'Media file changed during inventory: {normalized}')
        total_size += size
        files.append({
            'path': normalized,
            'size': size,
            'sha256': digest,
        })
    final_inventory = _tree_inventory(root)
    initial_identities = {
        relative: identity
        for relative, (_path, identity) in initial_inventory.items()
    }
    final_identities = {
        relative: identity
        for relative, (_path, identity) in final_inventory.items()
    }
    if initial_identities != final_identities:
        raise MediaManifestError('Media tree changed while the manifest was built.')
    return {
        'format': MANIFEST_FORMAT,
        'format_version': MANIFEST_VERSION,
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'hash_algorithm': HASH_ALGORITHM,
        'path_normalization': 'unicode_nfc_canonical_caseless_unique',
        'source_snapshot': {
            'id': snapshot_id,
            'offline_confirmed': True,
        },
        'file_count': len(files),
        'total_size': total_size,
        'files': files,
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise MediaManifestError(f'Output already exists: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_reparse_components(path.parent, label='Output parent')
    temporary = path.with_name(f'.{path.name}.tmp-{uuid4().hex}')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise MediaManifestError(f'Output appeared while publishing: {path}') from exc
        temporary.unlink()
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaManifestError(f'Invalid media manifest {path.name}: {exc}') from exc
    if not isinstance(payload, dict):
        raise MediaManifestError(f'Media manifest must be an object: {path.name}')
    if (
        payload.get('format') != MANIFEST_FORMAT
        or payload.get('format_version') != MANIFEST_VERSION
        or payload.get('hash_algorithm') != HASH_ALGORITHM
        or payload.get('path_normalization')
        != 'unicode_nfc_canonical_caseless_unique'
    ):
        raise MediaManifestError(f'Unsupported media manifest: {path.name}')
    rows = payload.get('files')
    if not isinstance(rows, list):
        raise MediaManifestError(f'Media manifest files must be a list: {path.name}')
    source_snapshot = payload.get('source_snapshot')
    if (
        not isinstance(source_snapshot, dict)
        or source_snapshot.get('offline_confirmed') is not True
        or not isinstance(source_snapshot.get('id'), str)
        or SNAPSHOT_ID_PATTERN.fullmatch(source_snapshot['id']) is None
    ):
        raise MediaManifestError(
            f'Media manifest lacks an offline snapshot attestation: {path.name}'
        )
    file_count = payload.get('file_count')
    declared_total_size = payload.get('total_size')
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count < 0
        or not isinstance(declared_total_size, int)
        or isinstance(declared_total_size, bool)
        or declared_total_size < 0
    ):
        raise MediaManifestError(f'Invalid media manifest totals: {path.name}')
    seen: set[str] = set()
    ordered_paths: list[str] = []
    total_size = 0
    for row in rows:
        if not isinstance(row, dict):
            raise MediaManifestError(f'Invalid media row in {path.name}')
        relative = row.get('path')
        size = row.get('size')
        digest = row.get('sha256')
        if (
            not isinstance(relative, str)
            or relative != unicodedata.normalize('NFC', relative)
            or '\\' in relative
            or PurePosixPath(relative).is_absolute()
            or PurePosixPath(relative).as_posix() != relative
            or any(part in {'', '.', '..'} for part in PurePosixPath(relative).parts)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in '0123456789abcdef' for character in digest)
        ):
            raise MediaManifestError(f'Invalid media row in {path.name}')
        key = _canonical_path_key(relative)
        if key in seen:
            raise MediaManifestError(f'Duplicate media path in {path.name}: {relative}')
        seen.add(key)
        ordered_paths.append(relative)
        total_size += size
    expected_order = sorted(
        ordered_paths,
        key=lambda value: (_canonical_path_key(value), value),
    )
    if ordered_paths != expected_order:
        raise MediaManifestError(f'Media manifest rows are not canonical: {path.name}')
    if file_count != len(rows) or declared_total_size != total_size:
        raise MediaManifestError(f'Media manifest totals do not match: {path.name}')
    return payload


def compare_manifests(source: dict[str, object], target: dict[str, object]) -> dict[str, object]:
    source_files = {str(row['path']): row for row in source['files']}
    target_files = {str(row['path']): row for row in target['files']}
    source_paths = set(source_files)
    target_paths = set(target_files)
    missing = sorted(
        source_paths - target_paths,
        key=lambda value: (_canonical_path_key(value), value),
    )
    unexpected = sorted(
        target_paths - source_paths,
        key=lambda value: (_canonical_path_key(value), value),
    )
    changed = sorted(
        (
            path
            for path in source_paths & target_paths
            if (
                source_files[path]['size'] != target_files[path]['size']
                or source_files[path]['sha256'] != target_files[path]['sha256']
            )
        ),
        key=lambda value: (_canonical_path_key(value), value),
    )
    return {
        'format': 'ffxivshare-media-comparison',
        'format_version': 1,
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'matched': not missing and not unexpected and not changed,
        'source_file_count': source['file_count'],
        'source_total_size': source['total_size'],
        'target_file_count': target['file_count'],
        'target_total_size': target['total_size'],
        'missing_paths': missing,
        'unexpected_paths': unexpected,
        'changed_paths': changed,
    }


def _build_command(args: argparse.Namespace) -> int:
    if not args.confirm_offline_snapshot:
        raise MediaManifestError(
            'Refusing to scan an unconfirmed live media tree. Freeze writes and '
            'create an offline snapshot first, then pass '
            '--confirm-offline-snapshot.'
        )
    root = _resolve_input_directory(args.root)
    output = _resolve_output_file(args.output)
    if _is_within(output, root):
        raise MediaManifestError('Manifest output must be outside the media root.')
    _write_json_atomic(
        output,
        build_manifest(root, snapshot_id=args.snapshot_id),
    )
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    source_path = _resolve_input_file(args.source)
    target_path = _resolve_input_file(args.target)
    output = _resolve_output_file(args.output)
    if output in {source_path, target_path}:
        raise MediaManifestError('Comparison output must differ from its inputs.')
    result = compare_manifests(
        _load_manifest(source_path),
        _load_manifest(target_path),
    )
    _write_json_atomic(output, result)
    return 0 if result['matched'] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Build or compare immutable FFXIVShare media manifests.'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)
    build = subparsers.add_parser('build')
    build.add_argument('--root', required=True)
    build.add_argument('--output', required=True)
    build.add_argument('--snapshot-id', required=True)
    build.add_argument('--confirm-offline-snapshot', action='store_true')
    build.set_defaults(handler=_build_command)
    compare = subparsers.add_parser('compare')
    compare.add_argument('--source', required=True)
    compare.add_argument('--target', required=True)
    compare.add_argument('--output', required=True)
    compare.set_defaults(handler=_compare_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return args.handler(args)
    except (MediaManifestError, OSError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
