from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import shutil
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from django.apps import apps
from django.core import serializers
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone


DATASET_FORMAT = 'ffxivshare-jsonl'
DATASET_VERSION = 2
SUPPORTED_DATASET_VERSIONS = frozenset({1, DATASET_VERSION})
MANIFEST_FILENAME = 'manifest.json'
VALIDATION_REPORT_FILENAME = 'validation-report.json'
IMPORT_REPORT_FILENAME = 'import-report.json'

V1_SHARE_LOG_ACTIONS = frozenset({
    'create',
    'edit',
    'approve',
    'reject',
    'add_collection',
    'remove_collection',
    'report_handle',
    'delete',
    'other',
})
HISTORICAL_REPORT_REASON_FALLBACK = '历史举报下架记录未保存处理说明'
HISTORICAL_REVIEW_REASON_FALLBACK = '历史审核拒绝记录未保存原因'
HISTORICAL_PRIVATE_REASON_FALLBACK = '历史私密状态来源待人工确认'


@dataclass(frozen=True)
class EntitySpec:
    name: str
    model_label: str
    filename: str

    @property
    def model(self):
        return apps.get_model(self.model_label)


V1_ENTITY_SPECS = (
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
V2_ENTITY_SPECS = V1_ENTITY_SPECS
ENTITY_SPECS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_SPECS,
    2: V2_ENTITY_SPECS,
})
ENTITY_SPECS = ENTITY_SPECS_BY_VERSION[DATASET_VERSION]
ENTITY_BY_NAME = {spec.name: spec for spec in ENTITY_SPECS}

# Dataset schemas are public migration contracts, not projections of whichever
# Django models happen to be installed when an import runs.  Keep every v1
# entity explicit so later model fields cannot silently become required by the
# historical validator or enter the v1 database digest.
V1_ENTITY_FIELDS = MappingProxyType({
    'groups': frozenset({
        'name',
        'permissions',
    }),
    'users': frozenset({
        'password',
        'last_login',
        'is_superuser',
        'username',
        'first_name',
        'last_name',
        'email',
        'is_staff',
        'is_active',
        'date_joined',
        'groups',
        'user_permissions',
    }),
    'user_profiles': frozenset({
        'user',
        'nickname',
        'bio',
        'home_feed_mode',
        'created_at',
        'updated_at',
    }),
    'shares': frozenset({
        'share_id',
        'title',
        'strategy_code',
        'description',
        'author',
        'created_at',
        'updated_at',
        'category',
        'visibility',
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by',
        'is_spoiler',
        'is_nsfw',
        'is_original',
        'views',
        'copies',
        'likes',
        'favorites',
    }),
    'collections': frozenset({
        'title',
        'description',
        'author',
        'created_at',
        'updated_at',
        'is_public',
    }),
    'collection_items': frozenset({
        'collection',
        'share',
        'order',
        'added_at',
    }),
    'reports': frozenset({
        'share',
        'reporter',
        'reason',
        'created_at',
        'status',
        'resolved_at',
        'resolved_by',
        'resolution_reason',
    }),
    'share_logs': frozenset({
        'share',
        'user',
        'action',
        'details',
        'created_at',
    }),
    'announcements': frozenset({
        'title',
        'content',
        'is_active',
        'created_at',
        'updated_at',
    }),
    'site_messages': frozenset({
        'recipient',
        'sender',
        'message_type',
        'title',
        'content',
        'related_share',
        'related_report',
        'metadata',
        'created_at',
        'read_at',
        'archived_at',
    }),
})

# v2 added persistent moderation restrictions to Share.  It remains a frozen
# historical wire contract: future Django model fields must require a dataset
# version bump instead of silently changing v2 validation or digests.
V2_ENTITY_FIELDS = MappingProxyType({
    **V1_ENTITY_FIELDS,
    'shares': frozenset({
        *V1_ENTITY_FIELDS['shares'],
        'restriction_state',
        'restriction_reason',
        'restricted_at',
        'restricted_by',
    }),
})
ENTITY_FIELDS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_FIELDS,
    2: V2_ENTITY_FIELDS,
})

for frozen_version, frozen_specs in ENTITY_SPECS_BY_VERSION.items():
    frozen_entity_names = frozenset(spec.name for spec in frozen_specs)
    if frozenset(ENTITY_FIELDS_BY_VERSION[frozen_version]) != frozen_entity_names:
        raise RuntimeError(
            f'The frozen v{frozen_version} schema must cover every portable entity.'
        )


class DataPortabilityError(RuntimeError):
    pass


@dataclass
class ParsedRecord:
    entity: str
    line: int
    pk: Any
    fields: dict[str, Any]


@dataclass(frozen=True)
class RestrictionProjection:
    state: str = 'clear'
    reason: str = ''
    restricted_at: Any = None
    restricted_by: Any = None

    def as_serialized_fields(self) -> dict[str, Any]:
        return {
            'restriction_state': self.state,
            'restriction_reason': self.reason,
            'restricted_at': self.restricted_at,
            'restricted_by': self.restricted_by,
        }


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
        source_version = (
            self.manifest.get('format_version')
            if isinstance(self.manifest, dict)
            else DATASET_VERSION
        )
        return {
            'format': DATASET_FORMAT,
            'format_version': source_version,
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


def _serialize_queryset(queryset, stream, *, fields: set[str] | None = None) -> None:
    options: dict[str, Any] = {
        'stream': stream,
        'use_natural_foreign_keys': True,
    }
    if fields is not None:
        options['fields'] = fields
    serializers.serialize(
        'jsonl',
        queryset.iterator(chunk_size=1000),
        **options,
    )


def _serialize_entity(spec: EntitySpec, destination: Path) -> dict[str, Any]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    with destination.open('w', encoding='utf-8', newline='\n') as stream:
        _serialize_queryset(
            queryset,
            stream,
            fields=_expected_serialized_fields(spec),
        )
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


def _current_serialized_fields(spec: EntitySpec) -> set[str]:
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


def _expected_serialized_fields(
    spec: EntitySpec,
    *,
    dataset_version: int = DATASET_VERSION,
) -> set[str]:
    try:
        return set(ENTITY_FIELDS_BY_VERSION[dataset_version][spec.name])
    except KeyError as exc:
        raise DataPortabilityError(
            f'No frozen schema for dataset v{dataset_version} entity {spec.name!r}.'
        ) from exc


def _record_schema_errors(
    spec: EntitySpec,
    fields: dict[str, Any],
    *,
    dataset_version: int,
) -> list[str]:
    errors: list[str] = []
    expected = _expected_serialized_fields(spec, dataset_version=dataset_version)
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
    if (
        dataset_version == 1
        and spec.name == 'share_logs'
        and fields.get('action') not in V1_SHARE_LOG_ACTIONS
    ):
        errors.append('action is not available in dataset version 1')
    return errors


def _natural_user(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _record_tiebreaker(record: ParsedRecord) -> tuple[int, Any]:
    if isinstance(record.pk, int) and not isinstance(record.pk, bool):
        return (0, record.pk)
    return (1, repr(record.pk))


def _latest_record(
    records: list[ParsedRecord],
    *,
    timestamp_field: str,
) -> ParsedRecord | None:
    if not records:
        return None
    minimum = datetime.min.replace(tzinfo=UTC)
    return max(
        records,
        key=lambda record: (
            _timestamp(record.fields.get(timestamp_field)) or minimum,
            _record_tiebreaker(record),
        ),
    )


def _record_event_key(
    record: ParsedRecord,
    *,
    timestamp_field: str,
) -> tuple[datetime, tuple[int, Any]]:
    return (
        _timestamp(record.fields.get(timestamp_field))
        or datetime.min.replace(tzinfo=UTC),
        _record_tiebreaker(record),
    )


def _clean_reason(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ''


def _derive_v1_restrictions(
    records: dict[str, list[ParsedRecord]],
) -> dict[Any, RestrictionProjection]:
    reports_by_share: dict[Any, list[ParsedRecord]] = {}
    for report in records['reports']:
        if report.fields.get('status') != 'resolved':
            continue
        reports_by_share.setdefault(report.fields.get('share'), []).append(report)

    rejects_by_share: dict[Any, list[ParsedRecord]] = {}
    approvals_by_share: dict[Any, list[ParsedRecord]] = {}
    for log in records['share_logs']:
        target = None
        if log.fields.get('action') == 'reject':
            target = rejects_by_share
        elif log.fields.get('action') == 'approve':
            target = approvals_by_share
        if target is not None:
            target.setdefault(log.fields.get('share'), []).append(log)

    projections: dict[Any, RestrictionProjection] = {}
    for share in records['shares']:
        fields = share.fields
        share_pk = share.pk
        latest_report = _latest_record(
            reports_by_share.get(share_pk, []),
            timestamp_field='resolved_at',
        )
        if latest_report is not None:
            report_fields = latest_report.fields
            projections[share_pk] = RestrictionProjection(
                state='report_takedown',
                reason=(
                    _clean_reason(report_fields.get('resolution_reason'))
                    or HISTORICAL_REPORT_REASON_FALLBACK
                ),
                restricted_at=(
                    report_fields.get('resolved_at')
                    or report_fields.get('created_at')
                    or fields.get('updated_at')
                ),
                restricted_by=report_fields.get('resolved_by'),
            )
            continue

        latest_reject = _latest_record(
            rejects_by_share.get(share_pk, []),
            timestamp_field='created_at',
        )
        latest_approval = _latest_record(
            approvals_by_share.get(share_pk, []),
            timestamp_field='created_at',
        )
        currently_rejected = fields.get('status') == 'rejected'
        reject_is_latest = (
            latest_reject is not None
            and (
                latest_approval is None
                or _record_event_key(
                    latest_reject,
                    timestamp_field='created_at',
                )
                > _record_event_key(
                    latest_approval,
                    timestamp_field='created_at',
                )
            )
        )
        if currently_rejected or reject_is_latest:
            reject_fields = latest_reject.fields if latest_reject is not None else {}
            projections[share_pk] = RestrictionProjection(
                state='review_rejected',
                reason=_clean_reason(fields.get('review_feedback'))
                or _clean_reason(reject_fields.get('details'))
                or HISTORICAL_REVIEW_REASON_FALLBACK,
                restricted_at=fields.get('reviewed_at')
                or reject_fields.get('created_at')
                or fields.get('updated_at'),
                restricted_by=fields.get('reviewed_by')
                or reject_fields.get('user'),
            )
            continue

        if fields.get('visibility') == 'private':
            projections[share_pk] = RestrictionProjection(
                state='legacy_private',
                reason=HISTORICAL_PRIVATE_REASON_FALLBACK,
                restricted_at=(
                    fields.get('updated_at')
                    or fields.get('created_at')
                ),
                restricted_by=None,
            )
            continue

        projections[share_pk] = RestrictionProjection()
    return projections


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
    *,
    dataset_version: int,
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

    for username, user_record in user_names.items():
        if username not in profile_users:
            _add_error(errors_by_record, user_record, 'user is missing a profile')

    share_ids: dict[str, ParsedRecord] = {}
    for record in records['shares']:
        fields = record.fields
        share_id = fields.get('share_id')
        if isinstance(share_id, str):
            _check_duplicate(share_ids, share_id, record, 'share_id', errors_by_record)
        for field_name in ('author', 'reviewed_by', 'restricted_by'):
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
        restriction_state = fields.get('restriction_state')
        if restriction_state == 'clear':
            if (
                fields.get('restriction_reason') != ''
                or fields.get('restricted_at') is not None
                or fields.get('restricted_by') is not None
            ):
                _add_error(errors_by_record, record, 'clear share contains restriction metadata')
        elif restriction_state in {
            'review_rejected',
            'report_takedown',
            'legacy_private',
        }:
            if not _clean_reason(fields.get('restriction_reason')):
                _add_error(errors_by_record, record, 'restricted share has no reason')
            if fields.get('restricted_at') is None:
                _add_error(errors_by_record, record, 'restricted share has no restriction time')
        if fields.get('status') == 'rejected' and restriction_state == 'clear':
            _add_error(errors_by_record, record, 'rejected share has no active restriction')

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
        reporter_reference = fields.get('reporter')
        reporter = _natural_user(reporter_reference)
        if share_id not in share_pks:
            _add_error(errors_by_record, record, 'report references an unknown share')
        if dataset_version == 1 and reporter_reference is None:
            _add_error(errors_by_record, record, 'version 1 report has no reporter')
        elif reporter_reference is not None and reporter not in user_names:
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
            if reporter_reference is not None:
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
        reference = record.fields.get('user')
        username = _natural_user(reference)
        if dataset_version == 1 and reference is None:
            _add_error(errors_by_record, record, 'version 1 share log has no user')
        elif reference is not None and (username is None or username not in user_names):
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
    if not isinstance(manifest, dict):
        report.errors.append('Manifest must be an object')
        return report
    report.manifest = manifest
    if manifest.get('format') != DATASET_FORMAT:
        report.errors.append(f'Unsupported dataset format: {manifest.get("format")!r}')
    source_version = manifest.get('format_version')
    if (
        not isinstance(source_version, int)
        or isinstance(source_version, bool)
        or source_version not in SUPPORTED_DATASET_VERSIONS
    ):
        report.errors.append(
            f'Unsupported dataset version: {source_version!r}'
        )
        schema_version = DATASET_VERSION
    else:
        schema_version = source_version
        if source_version == 1:
            report.warnings.append(
                'Dataset version 1 will be upgraded with persistent moderation '
                'restrictions during import.'
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
                    record_errors.extend(_record_schema_errors(
                        spec,
                        record.fields,
                        dataset_version=schema_version,
                    ))
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

    if schema_version == 1:
        projections = _derive_v1_restrictions(records)
        for record in records['shares']:
            projection = projections.get(record.pk)
            if projection is not None:
                record.fields.update(projection.as_serialized_fields())
    _validate_cross_entity(
        records,
        errors_by_record,
        dataset_version=schema_version,
    )
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


def _database_entity_digest(
    spec: EntitySpec,
    *,
    dataset_version: int,
) -> tuple[int, str]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    stream = StringIO(newline='\n')
    fields = _expected_serialized_fields(spec, dataset_version=dataset_version)
    _serialize_queryset(queryset, stream, fields=fields)
    return count, sha256(stream.getvalue().encode('utf-8')).hexdigest()


def _database_v1_restrictions_match() -> bool:
    records: dict[str, list[ParsedRecord]] = {
        'shares': [],
        'reports': [],
        'share_logs': [],
    }
    actual_shares: dict[Any, tuple[str, str, Any, str | None]] = {}
    Share = ENTITY_BY_NAME['shares'].model
    for line_number, row in enumerate(Share._default_manager.values(
        'pk',
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by__username',
        'visibility',
        'created_at',
        'updated_at',
        'restriction_state',
        'restriction_reason',
        'restricted_at',
        'restricted_by__username',
    ).iterator(chunk_size=1000), start=1):
        records['shares'].append(ParsedRecord(
            entity='shares',
            line=line_number,
            pk=row['pk'],
            fields={
                'status': row['status'],
                'review_feedback': row['review_feedback'],
                'reviewed_at': row['reviewed_at'],
                'reviewed_by': (
                    [row['reviewed_by__username']]
                    if row['reviewed_by__username'] is not None
                    else None
                ),
                'visibility': row['visibility'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
            },
        ))
        actual_shares[row['pk']] = (
            row['restriction_state'],
            row['restriction_reason'],
            row['restricted_at'],
            row['restricted_by__username'],
        )

    Report = ENTITY_BY_NAME['reports'].model
    for line_number, row in enumerate(Report._default_manager.filter(
        status='resolved'
    ).values(
        'pk',
        'share_id',
        'status',
        'created_at',
        'resolved_at',
        'resolved_by__username',
        'resolution_reason',
    ).iterator(chunk_size=1000), start=1):
        records['reports'].append(ParsedRecord(
            entity='reports',
            line=line_number,
            pk=row['pk'],
            fields={
                'share': row['share_id'],
                'status': row['status'],
                'created_at': row['created_at'],
                'resolved_at': row['resolved_at'],
                'resolved_by': (
                    [row['resolved_by__username']]
                    if row['resolved_by__username'] is not None
                    else None
                ),
                'resolution_reason': row['resolution_reason'],
            },
        ))

    ShareLog = ENTITY_BY_NAME['share_logs'].model
    for line_number, row in enumerate(ShareLog._default_manager.filter(
        action__in=['approve', 'reject']
    ).values(
        'pk',
        'share_id',
        'user__username',
        'action',
        'details',
        'created_at',
    ).iterator(chunk_size=1000), start=1):
        records['share_logs'].append(ParsedRecord(
            entity='share_logs',
            line=line_number,
            pk=row['pk'],
            fields={
                'share': row['share_id'],
                'user': (
                    [row['user__username']]
                    if row['user__username'] is not None
                    else None
                ),
                'action': row['action'],
                'details': row['details'],
                'created_at': row['created_at'],
            },
        ))

    expected = _derive_v1_restrictions(records)
    if set(actual_shares) != set(expected):
        return False
    for share_pk, projection in expected.items():
        state, reason, restricted_at, restricted_by = actual_shares[share_pk]
        expected_user = _natural_user(projection.restricted_by)
        if (
            state != projection.state
            or reason != projection.reason
            or _timestamp(restricted_at) != _timestamp(projection.restricted_at)
            or restricted_by != expected_user
        ):
            return False
    return True


def database_matches_manifest(manifest: dict[str, Any]) -> bool:
    dataset_version = manifest.get('format_version')
    if (
        not isinstance(dataset_version, int)
        or isinstance(dataset_version, bool)
        or dataset_version not in SUPPORTED_DATASET_VERSIONS
    ):
        return False
    metadata_by_entity = manifest.get('entities', {})
    for spec in ENTITY_SPECS:
        metadata = metadata_by_entity.get(spec.name, {})
        count, digest = _database_entity_digest(
            spec,
            dataset_version=dataset_version,
        )
        if count != metadata.get('count') or digest != metadata.get('sha256'):
            return False
    if dataset_version == 1 and not _database_v1_restrictions_match():
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


def _load_v1_restriction_projections(
    root: Path,
) -> dict[Any, RestrictionProjection]:
    records: dict[str, list[ParsedRecord]] = {
        'shares': [],
        'reports': [],
        'share_logs': [],
    }
    for entity_name in records:
        spec = ENTITY_BY_NAME[entity_name]
        with (root / spec.filename).open('r', encoding='utf-8') as stream:
            for line_number, line in enumerate(stream, start=1):
                payload = json.loads(line)
                records[entity_name].append(ParsedRecord(
                    entity=entity_name,
                    line=line_number,
                    pk=payload['pk'],
                    fields=payload['fields'],
                ))
    return _derive_v1_restrictions(records)


def _upgrade_v1_share_line(
    line: str,
    projections: dict[Any, RestrictionProjection],
) -> str:
    payload = json.loads(line)
    projection = projections[payload['pk']]
    payload['fields'].update(projection.as_serialized_fields())
    return json.dumps(payload, ensure_ascii=False) + '\n'


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
    source_version = validation.manifest['format_version']

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
    v1_projections = (
        _load_v1_restriction_projections(root)
        if source_version == 1
        else {}
    )
    try:
        with transaction.atomic():
            for spec in ENTITY_SPECS:
                data_path = root / spec.filename
                with data_path.open('r', encoding='utf-8') as stream:
                    for line_number, line in enumerate(stream, start=1):
                        try:
                            with transaction.atomic():
                                serialized_line = (
                                    _upgrade_v1_share_line(line, v1_projections)
                                    if source_version == 1 and spec.name == 'shares'
                                    else line
                                )
                                objects = list(serializers.deserialize(
                                    'jsonl',
                                    serialized_line,
                                ))
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
