from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from django.apps import apps
from django.core import serializers
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone


DATASET_FORMAT = 'ffxivshare-jsonl'
DATASET_VERSION = 1
MANIFEST_FILENAME = 'manifest.json'
VALIDATION_REPORT_FILENAME = 'validation-report.json'
IMPORT_REPORT_FILENAME = 'import-report.json'


@dataclass(frozen=True)
class EntitySpec:
    name: str
    model_label: str
    filename: str

    @property
    def model(self):
        return apps.get_model(self.model_label)


ENTITY_SPECS = (
    EntitySpec('groups', 'auth.Group', 'groups.jsonl'),
    EntitySpec('users', 'auth.User', 'users.jsonl'),
    EntitySpec('user_profiles', 'shares.UserProfile', 'user_profiles.jsonl'),
    EntitySpec('shares', 'shares.Share', 'shares.jsonl'),
    EntitySpec('collections', 'shares.Collection', 'collections.jsonl'),
    EntitySpec('collection_items', 'shares.CollectionItem', 'collection_items.jsonl'),
    EntitySpec('reports', 'shares.Report', 'reports.jsonl'),
    EntitySpec('share_logs', 'shares.ShareLog', 'share_logs.jsonl'),
    EntitySpec('announcements', 'shares.Announcement', 'announcements.jsonl'),
    EntitySpec('site_messages', 'shares.SiteMessage', 'site_messages.jsonl'),
)
ENTITY_BY_NAME = {spec.name: spec for spec in ENTITY_SPECS}


class DataPortabilityError(RuntimeError):
    pass


@dataclass
class ParsedRecord:
    entity: str
    line: int
    pk: Any
    fields: dict[str, Any]


@dataclass
class ValidationReport:
    dataset: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    quarantined_records: list[dict[str, Any]] = field(default_factory=list)
    entity_counts: dict[str, int] = field(default_factory=dict)
    manifest: dict[str, Any] | None = None

    @property
    def valid(self) -> bool:
        return not self.errors and not self.quarantined_records

    def as_dict(self) -> dict[str, Any]:
        return {
            'format': DATASET_FORMAT,
            'format_version': DATASET_VERSION,
            'generated_at': timezone.now().isoformat(),
            'dataset': self.dataset,
            'valid': self.valid,
            'entity_counts': self.entity_counts,
            'errors': self.errors,
            'warnings': self.warnings,
            'quarantined_records': self.quarantined_records,
        }


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp-{uuid4().hex}')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _serialize_queryset(queryset, stream) -> None:
    serializers.serialize(
        'jsonl',
        queryset.iterator(chunk_size=1000),
        stream=stream,
        use_natural_foreign_keys=True,
    )


def _serialize_entity(spec: EntitySpec, destination: Path) -> dict[str, Any]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    with destination.open('w', encoding='utf-8', newline='\n') as stream:
        _serialize_queryset(queryset, stream)
    return {
        'model': spec.model_label.lower(),
        'file': spec.filename,
        'count': count,
        'sha256': _sha256_file(destination),
    }


def export_dataset(output_directory: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    output = Path(output_directory).expanduser().resolve()
    if output.exists() and not overwrite:
        raise DataPortabilityError(
            f'Output directory already exists: {output}. Use --overwrite explicitly.'
        )
    if output.exists() and not output.is_dir():
        raise DataPortabilityError(f'Output path is not a directory: {output}')

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f'.{output.name}.tmp-{uuid4().hex}'
    staging.mkdir()
    try:
        entities: dict[str, Any] = {}
        with transaction.atomic():
            for spec in ENTITY_SPECS:
                entities[spec.name] = _serialize_entity(spec, staging / spec.filename)

        manifest = {
            'format': DATASET_FORMAT,
            'format_version': DATASET_VERSION,
            'application_version': os.environ.get('APP_VERSION', 'unknown'),
            'exported_at': timezone.now().isoformat(),
            'source_database': connection.vendor,
            'entities': entities,
        }
        _write_json_atomic(staging / MANIFEST_FILENAME, manifest)
        report = validate_dataset(staging)
        _write_json_atomic(staging / VALIDATION_REPORT_FILENAME, report.as_dict())
        if not report.valid:
            details = '; '.join(report.errors[:3])
            if report.quarantined_records:
                first = report.quarantined_records[0]
                details = (
                    f'{details}; quarantined records: '
                    f'{len(report.quarantined_records)}; first: '
                    f'{first.get("entity")} line {first.get("line")} '
                    f'{first.get("errors")}'
                ).strip('; ')
            raise DataPortabilityError(
                'The freshly exported dataset failed validation; output was not '
                f'published. {details}'
            )

        if output.exists():
            shutil.rmtree(output)
        staging.replace(output)
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _expected_serialized_fields(spec: EntitySpec) -> set[str]:
    fields = {
        field.name
        for field in spec.model._meta.local_fields
        if field.serialize and not field.primary_key
    }
    fields.update(
        field.name
        for field in spec.model._meta.local_many_to_many
        if field.serialize and field.remote_field.through._meta.auto_created
    )
    return fields


def _record_schema_errors(spec: EntitySpec, fields: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = _expected_serialized_fields(spec)
    missing = sorted(expected - fields.keys())
    extra = sorted(fields.keys() - expected)
    if missing:
        errors.append(f'missing fields: {", ".join(missing)}')
    if extra:
        errors.append(f'unknown fields: {", ".join(extra)}')

    for model_field in spec.model._meta.local_fields:
        if model_field.primary_key or model_field.name not in fields:
            continue
        value = fields[model_field.name]
        if model_field.is_relation:
            if value is None and not model_field.null:
                errors.append(f'{model_field.name} cannot be null')
            continue
        if value is None:
            if not model_field.null:
                errors.append(f'{model_field.name} cannot be null')
            continue
        try:
            converted = model_field.to_python(value)
        except Exception as exc:
            errors.append(f'{model_field.name} has invalid value: {exc}')
            continue
        if model_field.max_length and isinstance(converted, str):
            if len(converted) > model_field.max_length:
                errors.append(
                    f'{model_field.name} exceeds max length {model_field.max_length}'
                )
        if model_field.choices:
            allowed = {choice[0] for choice in model_field.flatchoices}
            if converted not in allowed:
                errors.append(f'{model_field.name} is not an allowed choice')
    return errors


def _natural_user(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return None


def _add_error(
    errors_by_record: dict[tuple[str, int], list[str]],
    record: ParsedRecord,
    message: str,
) -> None:
    errors_by_record.setdefault((record.entity, record.line), []).append(message)


def _check_duplicate(
    seen: dict[Any, ParsedRecord],
    key: Any,
    record: ParsedRecord,
    label: str,
    errors_by_record: dict[tuple[str, int], list[str]],
) -> None:
    previous = seen.get(key)
    if previous is None:
        seen[key] = record
        return
    _add_error(errors_by_record, previous, f'duplicate {label}: {key!r}')
    _add_error(errors_by_record, record, f'duplicate {label}: {key!r}')


def _validate_cross_entity(
    records: dict[str, list[ParsedRecord]],
    errors_by_record: dict[tuple[str, int], list[str]],
) -> None:
    user_names: dict[str, ParsedRecord] = {}
    group_names: dict[str, ParsedRecord] = {}
    share_pks = {record.pk for record in records['shares']}
    report_pks = {record.pk for record in records['reports']}
    collection_pks = {record.pk for record in records['collections']}

    for record in records['groups']:
        name = record.fields.get('name')
        if isinstance(name, str):
            _check_duplicate(group_names, name, record, 'group name', errors_by_record)

    for record in records['users']:
        username = record.fields.get('username')
        if isinstance(username, str):
            _check_duplicate(user_names, username, record, 'username', errors_by_record)
        for group_ref in record.fields.get('groups', []):
            group_name = _natural_user(group_ref)
            if group_name is None or group_name not in group_names:
                _add_error(errors_by_record, record, f'unknown group reference: {group_ref!r}')

    profile_users: dict[str, ParsedRecord] = {}
    for record in records['user_profiles']:
        username = _natural_user(record.fields.get('user'))
        if username is None or username not in user_names:
            _add_error(errors_by_record, record, 'profile references an unknown user')
        else:
            _check_duplicate(profile_users, username, record, 'user profile', errors_by_record)
        if record.fields.get('home_feed_mode') not in {'paginated', 'infinite'}:
            _add_error(errors_by_record, record, 'invalid home feed mode')

    share_ids: dict[str, ParsedRecord] = {}
    for record in records['shares']:
        fields = record.fields
        share_id = fields.get('share_id')
        if isinstance(share_id, str):
            _check_duplicate(share_ids, share_id, record, 'share_id', errors_by_record)
        for field_name in ('author', 'reviewed_by'):
            reference = fields.get(field_name)
            username = _natural_user(reference)
            if reference is not None and (username is None or username not in user_names):
                _add_error(errors_by_record, record, f'{field_name} references an unknown user')
        for field_name in ('likes', 'favorites'):
            for reference in fields.get(field_name, []):
                username = _natural_user(reference)
                if username is None or username not in user_names:
                    _add_error(errors_by_record, record, f'{field_name} contains an unknown user')
        for counter in ('views', 'copies'):
            value = fields.get(counter)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _add_error(errors_by_record, record, f'{counter} must be nonnegative')
        if fields.get('status') == 'pending':
            if fields.get('reviewed_at') is not None or fields.get('reviewed_by') is not None:
                _add_error(errors_by_record, record, 'pending share contains review metadata')
        if fields.get('reviewed_by') is not None and fields.get('reviewed_at') is None:
            _add_error(errors_by_record, record, 'reviewer is set without review time')

    for record in records['collections']:
        username = _natural_user(record.fields.get('author'))
        if username is None or username not in user_names:
            _add_error(errors_by_record, record, 'collection references an unknown author')

    collection_pairs: dict[tuple[Any, Any], ParsedRecord] = {}
    collection_orders: dict[tuple[Any, Any], ParsedRecord] = {}
    for record in records['collection_items']:
        collection_id = record.fields.get('collection')
        share_id = record.fields.get('share')
        order = record.fields.get('order')
        if collection_id not in collection_pks:
            _add_error(errors_by_record, record, 'collection item references an unknown collection')
        if share_id not in share_pks:
            _add_error(errors_by_record, record, 'collection item references an unknown share')
        _check_duplicate(
            collection_pairs,
            (collection_id, share_id),
            record,
            'collection/share pair',
            errors_by_record,
        )
        _check_duplicate(
            collection_orders,
            (collection_id, order),
            record,
            'collection order slot',
            errors_by_record,
        )

    pending_reports: dict[tuple[Any, Any], ParsedRecord] = {}
    for record in records['reports']:
        fields = record.fields
        share_id = fields.get('share')
        reporter = _natural_user(fields.get('reporter'))
        if share_id not in share_pks:
            _add_error(errors_by_record, record, 'report references an unknown share')
        if reporter is None or reporter not in user_names:
            _add_error(errors_by_record, record, 'report references an unknown reporter')
        resolver = fields.get('resolved_by')
        resolver_name = _natural_user(resolver)
        if resolver is not None and (resolver_name is None or resolver_name not in user_names):
            _add_error(errors_by_record, record, 'report references an unknown resolver')
        if fields.get('status') == 'pending':
            if (
                fields.get('resolved_at') is not None
                or fields.get('resolved_by') is not None
                or fields.get('resolution_reason') != ''
            ):
                _add_error(errors_by_record, record, 'pending report contains resolution data')
            _check_duplicate(
                pending_reports,
                (share_id, reporter),
                record,
                'pending report',
                errors_by_record,
            )
        elif fields.get('resolved_at') is None:
            _add_error(errors_by_record, record, 'finished report has no resolution time')

    for record in records['share_logs']:
        if record.fields.get('share') not in share_pks:
            _add_error(errors_by_record, record, 'share log references an unknown share')
        username = _natural_user(record.fields.get('user'))
        if username is None or username not in user_names:
            _add_error(errors_by_record, record, 'share log references an unknown user')

    for record in records['site_messages']:
        fields = record.fields
        for field_name in ('recipient', 'sender'):
            reference = fields.get(field_name)
            username = _natural_user(reference)
            if reference is not None and (username is None or username not in user_names):
                _add_error(errors_by_record, record, f'{field_name} references an unknown user')
        if fields.get('recipient') is None:
            _add_error(errors_by_record, record, 'site message has no recipient')
        related_share = fields.get('related_share')
        if related_share is not None and related_share not in share_pks:
            _add_error(errors_by_record, record, 'site message references an unknown share')
        related_report = fields.get('related_report')
        if related_report is not None and related_report not in report_pks:
            _add_error(errors_by_record, record, 'site message references an unknown report')


def validate_dataset(dataset_directory: str | Path) -> ValidationReport:
    root = Path(dataset_directory).expanduser().resolve()
    report = ValidationReport(dataset=str(root))
    manifest_path = root / MANIFEST_FILENAME
    if not root.is_dir():
        report.errors.append(f'Dataset directory does not exist: {root}')
        return report
    if not manifest_path.is_file():
        report.errors.append(f'Missing {MANIFEST_FILENAME}')
        return report

    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        report.errors.append(f'Invalid manifest: {exc}')
        return report
    report.manifest = manifest
    if manifest.get('format') != DATASET_FORMAT:
        report.errors.append(f'Unsupported dataset format: {manifest.get("format")!r}')
    if manifest.get('format_version') != DATASET_VERSION:
        report.errors.append(
            f'Unsupported dataset version: {manifest.get("format_version")!r}'
        )

    manifest_entities = manifest.get('entities')
    if not isinstance(manifest_entities, dict):
        report.errors.append('Manifest entities must be an object')
        return report
    unexpected_entities = sorted(set(manifest_entities) - set(ENTITY_BY_NAME))
    if unexpected_entities:
        report.errors.append(
            f'Unexpected manifest entities: {", ".join(unexpected_entities)}'
        )

    records: dict[str, list[ParsedRecord]] = {spec.name: [] for spec in ENTITY_SPECS}
    errors_by_record: dict[tuple[str, int], list[str]] = {}
    for spec in ENTITY_SPECS:
        metadata = manifest_entities.get(spec.name)
        if not isinstance(metadata, dict):
            report.errors.append(f'Missing manifest entry for {spec.name}')
            continue
        if metadata.get('model') != spec.model_label.lower():
            report.errors.append(f'Unexpected model metadata for {spec.name}')
        if metadata.get('file') != spec.filename:
            report.errors.append(f'Unexpected filename for {spec.name}')
            continue
        data_path = root / spec.filename
        if not data_path.is_file():
            report.errors.append(f'Missing data file: {spec.filename}')
            continue
        actual_digest = _sha256_file(data_path)
        if actual_digest != metadata.get('sha256'):
            report.errors.append(f'Checksum mismatch: {spec.filename}')

        seen_pks: dict[Any, ParsedRecord] = {}
        with data_path.open('r', encoding='utf-8') as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    report.errors.append(f'{spec.filename}:{line_number}: blank line')
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    report.quarantined_records.append({
                        'entity': spec.name,
                        'line': line_number,
                        'pk': None,
                        'errors': [f'invalid JSON: {exc}'],
                    })
                    continue
                if not isinstance(payload, dict):
                    report.quarantined_records.append({
                        'entity': spec.name,
                        'line': line_number,
                        'pk': None,
                        'errors': ['record must be an object'],
                    })
                    continue
                record = ParsedRecord(
                    entity=spec.name,
                    line=line_number,
                    pk=payload.get('pk'),
                    fields=payload.get('fields') if isinstance(payload.get('fields'), dict) else {},
                )
                records[spec.name].append(record)
                record_errors: list[str] = []
                if payload.get('model') != spec.model_label.lower():
                    record_errors.append(f'unexpected model: {payload.get("model")!r}')
                if record.pk is None:
                    record_errors.append('missing primary key')
                if not isinstance(payload.get('fields'), dict):
                    record_errors.append('fields must be an object')
                else:
                    record_errors.extend(_record_schema_errors(spec, record.fields))
                if record_errors:
                    errors_by_record[(spec.name, line_number)] = record_errors
                if record.pk is not None:
                    _check_duplicate(
                        seen_pks,
                        record.pk,
                        record,
                        'primary key',
                        errors_by_record,
                    )

        report.entity_counts[spec.name] = len(records[spec.name])
        expected_count = metadata.get('count')
        if expected_count != len(records[spec.name]):
            report.errors.append(
                f'Count mismatch for {spec.name}: expected {expected_count!r}, '
                f'found {len(records[spec.name])}'
            )

    _validate_cross_entity(records, errors_by_record)
    existing_quarantine = {
        (item['entity'], item['line'])
        for item in report.quarantined_records
    }
    for spec in ENTITY_SPECS:
        for record in records[spec.name]:
            key = (record.entity, record.line)
            messages = sorted(set(errors_by_record.get(key, [])))
            if messages and key not in existing_quarantine:
                report.quarantined_records.append({
                    'entity': record.entity,
                    'line': record.line,
                    'pk': record.pk,
                    'errors': messages,
                })
    return report


def write_validation_report(report: ValidationReport, path: str | Path) -> None:
    _write_json_atomic(Path(path).expanduser().resolve(), report.as_dict())


def _database_entity_digest(spec: EntitySpec) -> tuple[int, str]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    stream = StringIO(newline='\n')
    _serialize_queryset(queryset, stream)
    return count, sha256(stream.getvalue().encode('utf-8')).hexdigest()


def database_matches_manifest(manifest: dict[str, Any]) -> bool:
    metadata_by_entity = manifest.get('entities', {})
    for spec in ENTITY_SPECS:
        metadata = metadata_by_entity.get(spec.name, {})
        count, digest = _database_entity_digest(spec)
        if count != metadata.get('count') or digest != metadata.get('sha256'):
            return False
    return True


def _database_has_portable_data() -> bool:
    return any(spec.model._default_manager.exists() for spec in ENTITY_SPECS)


def _reset_imported_sequences() -> None:
    sql_statements = connection.ops.sequence_reset_sql(
        no_style(),
        [spec.model for spec in ENTITY_SPECS],
    )
    if not sql_statements:
        return
    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def import_dataset(
    dataset_directory: str | Path,
    *,
    report_path: str | Path | None = None,
) -> str:
    root = Path(dataset_directory).expanduser().resolve()
    validation = validate_dataset(root)
    validation_path = (
        Path(report_path).expanduser().resolve()
        if report_path
        else root / IMPORT_REPORT_FILENAME
    )
    if not validation.valid:
        write_validation_report(validation, validation_path)
        raise DataPortabilityError(
            f'Dataset validation failed; see quarantine report: {validation_path}'
        )
    assert validation.manifest is not None

    if _database_has_portable_data():
        if database_matches_manifest(validation.manifest):
            _write_json_atomic(validation_path, {
                **validation.as_dict(),
                'status': 'already_imported',
            })
            return 'already_imported'
        validation.errors.append(
            'Target database is not empty and does not exactly match this dataset.'
        )
        write_validation_report(validation, validation_path)
        raise DataPortabilityError(
            'Refusing to import into a non-empty target database with different data.'
        )

    import_quarantine: list[dict[str, Any]] = []
    try:
        with transaction.atomic():
            for spec in ENTITY_SPECS:
                data_path = root / spec.filename
                with data_path.open('r', encoding='utf-8') as stream:
                    for line_number, line in enumerate(stream, start=1):
                        try:
                            with transaction.atomic():
                                objects = list(serializers.deserialize('jsonl', line))
                                if len(objects) != 1:
                                    raise ValueError('expected exactly one serialized object')
                                objects[0].save(save_m2m=True)
                        except Exception as exc:
                            try:
                                pk = json.loads(line).get('pk')
                            except Exception:
                                pk = None
                            import_quarantine.append({
                                'entity': spec.name,
                                'line': line_number,
                                'pk': pk,
                                'errors': [f'{type(exc).__name__}: {exc}'],
                            })
            if import_quarantine:
                raise DataPortabilityError('One or more records could not be imported.')
            _reset_imported_sequences()
            if not database_matches_manifest(validation.manifest):
                validation.errors.append(
                    'Post-import counts or SHA-256 digests do not match the manifest.'
                )
                raise DataPortabilityError('Post-import verification failed.')
    except Exception as exc:
        validation.quarantined_records.extend(import_quarantine)
        if not validation.errors:
            validation.errors.append(str(exc))
        write_validation_report(validation, validation_path)
        if isinstance(exc, DataPortabilityError):
            raise
        raise DataPortabilityError(f'Import failed and was rolled back: {exc}') from exc

    _write_json_atomic(validation_path, {
        **validation.as_dict(),
        'status': 'imported',
    })
    return 'imported'
