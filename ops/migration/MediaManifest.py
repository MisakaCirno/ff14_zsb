from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
import unicodedata
from uuid import uuid4


MANIFEST_FORMAT = 'ffxivshare-media-manifest'
MANIFEST_VERSION = 1
HASH_ALGORITHM = 'sha256'


class MediaManifestError(RuntimeError):
    pass


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    return path.is_symlink() or bool(
        attributes & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    )


def _resolve_input_directory(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise MediaManifestError('Media root must be an absolute path.')
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
    return path.resolve(strict=False)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _file_sha256(path: Path) -> tuple[int, str]:
    before = path.stat()
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    after = path.stat()
    before_identity = (
        before.st_size,
        before.st_mtime_ns,
        getattr(before, 'st_ino', None),
    )
    after_identity = (
        after.st_size,
        after.st_mtime_ns,
        getattr(after, 'st_ino', None),
    )
    if before_identity != after_identity:
        raise MediaManifestError(f'Media file changed while hashing: {path.name}')
    return after.st_size, digest.hexdigest()


def _iter_regular_files(root: Path):
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(
                os.scandir(directory),
                key=lambda entry: (entry.name.casefold(), entry.name),
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


def build_manifest(root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    canonical_paths: dict[str, str] = {}
    total_size = 0
    for path in _iter_regular_files(root):
        resolved = path.resolve(strict=True)
        if not _is_within(resolved, root):
            raise MediaManifestError(f'Media path escapes the root: {path.name}')
        relative = path.relative_to(root).as_posix()
        normalized = unicodedata.normalize('NFC', relative)
        if not normalized or normalized.startswith('/') or '..' in Path(normalized).parts:
            raise MediaManifestError(f'Invalid relative media path: {relative!r}')
        collision_key = normalized.casefold()
        previous = canonical_paths.get(collision_key)
        if previous is not None:
            raise MediaManifestError(
                'Media paths collide after NFC and case-insensitive normalization: '
                f'{previous!r} and {normalized!r}'
            )
        canonical_paths[collision_key] = normalized
        size, digest = _file_sha256(path)
        total_size += size
        files.append({
            'path': normalized,
            'size': size,
            'sha256': digest,
        })
    files.sort(key=lambda item: (str(item['path']).casefold(), str(item['path'])))
    return {
        'format': MANIFEST_FORMAT,
        'format_version': MANIFEST_VERSION,
        'generated_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'hash_algorithm': HASH_ALGORITHM,
        'path_normalization': 'unicode_nfc_case_insensitive_unique',
        'file_count': len(files),
        'total_size': total_size,
        'files': files,
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise MediaManifestError(f'Output already exists: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
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
    ):
        raise MediaManifestError(f'Unsupported media manifest: {path.name}')
    rows = payload.get('files')
    if not isinstance(rows, list):
        raise MediaManifestError(f'Media manifest files must be a list: {path.name}')
    seen: set[str] = set()
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
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in '0123456789abcdef' for character in digest)
        ):
            raise MediaManifestError(f'Invalid media row in {path.name}')
        key = relative.casefold()
        if key in seen:
            raise MediaManifestError(f'Duplicate media path in {path.name}: {relative}')
        seen.add(key)
        total_size += size
    if payload.get('file_count') != len(rows) or payload.get('total_size') != total_size:
        raise MediaManifestError(f'Media manifest totals do not match: {path.name}')
    return payload


def compare_manifests(source: dict[str, object], target: dict[str, object]) -> dict[str, object]:
    source_files = {str(row['path']): row for row in source['files']}
    target_files = {str(row['path']): row for row in target['files']}
    source_paths = set(source_files)
    target_paths = set(target_files)
    missing = sorted(source_paths - target_paths, key=lambda value: value.casefold())
    unexpected = sorted(target_paths - source_paths, key=lambda value: value.casefold())
    changed = sorted(
        (
            path
            for path in source_paths & target_paths
            if (
                source_files[path]['size'] != target_files[path]['size']
                or source_files[path]['sha256'] != target_files[path]['sha256']
            )
        ),
        key=lambda value: value.casefold(),
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
    root = _resolve_input_directory(args.root)
    output = _resolve_output_file(args.output)
    if _is_within(output, root):
        raise MediaManifestError('Manifest output must be outside the media root.')
    _write_json_atomic(output, build_manifest(root))
    return 0


def _compare_command(args: argparse.Namespace) -> int:
    source_path = Path(args.source).expanduser().resolve(strict=True)
    target_path = Path(args.target).expanduser().resolve(strict=True)
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
