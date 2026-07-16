from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from django.apps import apps
from django.core import serializers
from django.db import connection, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Max
from django.utils.encoding import is_protected_type
from django.utils.functional import Promise
from django.utils import timezone


DATASET_FORMAT = 'ffxivshare-jsonl'
DATASET_VERSION = 3
SUPPORTED_DATASET_VERSIONS = frozenset({1, 2, DATASET_VERSION})
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
V3_CODEC = 'canonical-jsonl-utc-microseconds'
V3_SESSION_PROJECTION_POLICY = 'force_logout_at_cutover'
V3_MODEL_SCHEMA_SIGNATURE = (
    '9b91a3b943d2986115508db51c216d94040053ec2c8e19b900acd2e0ddfdd685'
)
V3_NATURAL_KEY_PROTOCOL = MappingProxyType({
    'auth.group': ('name',),
    'auth.permission': ('codename', 'content_type.app_label', 'content_type.model'),
    'auth.user': ('username',),
    'contenttypes.contenttype': ('app_label', 'model'),
})
SQLITE_INTERNAL_TABLES = frozenset({
    'sqlite_sequence',
    'sqlite_stat1',
    'sqlite_stat4',
})
POSTGRES_IMPORT_LOCK_KEYS = (0x46584653, 0x524D3139)
_V3_DATETIME_PATTERN = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
)


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
V3_ENTITY_SPECS = (
    *V2_ENTITY_SPECS,
    EntitySpec(
        'admin_log_entries',
        'admin.LogEntry',
        'admin_log_entries.jsonl',
    ),
)
ENTITY_SPECS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_SPECS,
    2: V2_ENTITY_SPECS,
    3: V3_ENTITY_SPECS,
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
V3_ENTITY_FIELDS = MappingProxyType({
    **V2_ENTITY_FIELDS,
    'admin_log_entries': frozenset({
        'action_time',
        'user',
        'content_type',
        'object_id',
        'object_repr',
        'action_flag',
        'change_message',
    }),
})
ENTITY_FIELDS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_FIELDS,
    2: V2_ENTITY_FIELDS,
    3: V3_ENTITY_FIELDS,
})

for frozen_version, frozen_specs in ENTITY_SPECS_BY_VERSION.items():
    frozen_entity_names = frozenset(spec.name for spec in frozen_specs)
    if frozenset(ENTITY_FIELDS_BY_VERSION[frozen_version]) != frozen_entity_names:
        raise RuntimeError(
            f'The frozen v{frozen_version} schema must cover every portable entity.'
        )


def _entity_specs_for_version(dataset_version: int) -> tuple[EntitySpec, ...]:
    try:
        return ENTITY_SPECS_BY_VERSION[dataset_version]
    except KeyError as exc:
        raise DataPortabilityError(
            f'No entity projection for dataset version {dataset_version}.'
        ) from exc


def _entity_by_name_for_version(dataset_version: int) -> dict[str, EntitySpec]:
    return {
        spec.name: spec
        for spec in _entity_specs_for_version(dataset_version)
    }


def _schema_fingerprint(dataset_version: int) -> str:
    specs = _entity_specs_for_version(dataset_version)
    payload = {
        'format': DATASET_FORMAT,
        'format_version': dataset_version,
        'codec': V3_CODEC if dataset_version == 3 else 'django-jsonl-legacy',
        'entities': [
            {
                'name': spec.name,
                'model': spec.model_label.lower(),
                'file': spec.filename,
                'fields': sorted(ENTITY_FIELDS_BY_VERSION[dataset_version][spec.name]),
            }
            for spec in specs
        ],
    }
    if dataset_version == 3:
        payload['semantic_contract'] = {
            'model_schema_signature': V3_MODEL_SCHEMA_SIGNATURE,
            'datetime': 'utc-with-exactly-six-microseconds',
            'foreign_keys': 'natural-key-when-available-otherwise-primary-key',
            'json': 'utf8-sorted-keys-no-nonfinite-numbers',
            'many_to_many': 'auto-through-natural-keys-canonical-sort',
            'natural_keys': {
                model_label: list(fields)
                for model_label, fields in V3_NATURAL_KEY_PROTOCOL.items()
            },
            'sessions': {
                'policy': V3_SESSION_PROJECTION_POLICY,
                'serialized': False,
                'source_evidence': [
                    'row_count',
                    'unexpired_count',
                    'latest_expiry',
                ],
                'target_required_row_count': 0,
            },
        }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return sha256(encoded).hexdigest()


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


def _format_v3_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _canonical_v3_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_v3_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise DataPortabilityError('v3 cannot encode timezone-aware time values.')
        return value.strftime('%H:%M:%S.%f')
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (Decimal, UUID, Promise)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise DataPortabilityError('v3 cannot encode non-finite floating-point values.')
    if isinstance(value, dict):
        return {
            str(key): _canonical_v3_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_v3_value(item) for item in value]
    return value


def _v3_schema_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_v3_schema_value(item) for item in value]
    raise RuntimeError(
        f'Unsupported value in the frozen v3 model schema: {value!r}'
    )


def _v3_callable_name(value: Any) -> str | None:
    if value is None:
        return None
    module = getattr(value, '__module__', value.__class__.__module__)
    name = getattr(
        value,
        '__qualname__',
        getattr(value, '__name__', value.__class__.__qualname__),
    )
    return f'{module}.{name}'


def _v3_model_field_semantics(model_field) -> dict[str, Any]:
    relation_kind = None
    if model_field.many_to_many:
        relation_kind = 'many_to_many'
    elif model_field.one_to_one:
        relation_kind = 'one_to_one'
    elif model_field.many_to_one:
        relation_kind = 'many_to_one'

    relation = None
    if relation_kind is not None:
        related_model = model_field.remote_field.model
        relation = {
            'kind': relation_kind,
            'target_model': related_model._meta.label_lower,
            'target_field': (
                related_model._meta.pk.name
                if model_field.many_to_many
                else model_field.target_field.name
            ),
            'db_constraint': bool(getattr(model_field, 'db_constraint', True)),
            'on_delete': _v3_callable_name(
                getattr(model_field.remote_field, 'on_delete', None)
            ),
            'reference_encoding': (
                f'natural:{related_model._meta.label_lower}'
                if related_model._meta.label_lower in V3_NATURAL_KEY_PROTOCOL
                else 'primary_key'
            ),
        }
        if model_field.many_to_many:
            through = model_field.remote_field.through
            relation.update({
                'through_auto_created': bool(through._meta.auto_created),
                'through_table': through._meta.db_table,
                'through_source_field': model_field.m2m_field_name(),
                'through_target_field': model_field.m2m_reverse_field_name(),
            })

    choices = sorted(
        (_v3_schema_value(choice[0]) for choice in model_field.flatchoices),
        key=_v3_sort_key,
    )
    return {
        'name': model_field.name,
        'column': model_field.column,
        'internal_type': model_field.get_internal_type(),
        'primary_key': bool(model_field.primary_key),
        'null': bool(model_field.null),
        'blank': bool(model_field.blank),
        'unique': bool(model_field.unique),
        'db_index': bool(model_field.db_index),
        'db_collation': getattr(model_field, 'db_collation', None),
        'serialize': bool(model_field.serialize),
        'max_length': model_field.max_length,
        'max_digits': getattr(model_field, 'max_digits', None),
        'decimal_places': getattr(model_field, 'decimal_places', None),
        'choices': choices,
        'json_encoder': _v3_callable_name(getattr(model_field, 'encoder', None)),
        'json_decoder': _v3_callable_name(getattr(model_field, 'decoder', None)),
        'relation': relation,
    }


def _current_v3_model_schema_signature() -> str:
    payload = []
    for spec in V3_ENTITY_SPECS:
        model = spec.model
        frozen_fields = V3_ENTITY_FIELDS[spec.name]
        payload.append({
            'entity': spec.name,
            'model': model._meta.label_lower,
            'table': model._meta.db_table,
            'primary_key': _v3_model_field_semantics(model._meta.pk),
            'fields': [
                _v3_model_field_semantics(model._meta.get_field(field_name))
                for field_name in sorted(frozen_fields)
            ],
        })
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    return sha256(encoded).hexdigest()


def _v3_sort_key(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    )


def _v3_related_reference(related_model, related) -> Any:
    model_label = related_model._meta.label_lower
    if model_label == 'auth.user':
        value = [related.username]
    elif model_label == 'auth.group':
        value = [related.name]
    elif model_label == 'contenttypes.contenttype':
        value = [related.app_label, related.model]
    elif model_label == 'auth.permission':
        value = [
            related.codename,
            related.content_type.app_label,
            related.content_type.model,
        ]
    else:
        value = related.pk
    return _canonical_v3_value(value)


def _v3_field_value(obj, model_field):
    if model_field.is_relation and (
        model_field.many_to_one or model_field.one_to_one
    ):
        related_pk = getattr(obj, model_field.attname)
        if related_pk is None:
            return None
        related_model = model_field.remote_field.model
        related = getattr(obj, model_field.name)
        return _v3_related_reference(related_model, related)

    value = model_field.value_from_object(obj)
    if not is_protected_type(value) and not isinstance(
        value,
        (dict, list, tuple, Decimal, UUID, Promise),
    ):
        value = model_field.value_to_string(obj)
    return _canonical_v3_value(value)


def _v3_record(spec: EntitySpec, obj, fields: set[str]) -> dict[str, Any]:
    model = spec.model
    field_map = {
        model_field.name: model_field
        for model_field in model._meta.local_fields
        if not model_field.primary_key
    }
    m2m_map = {
        model_field.name: model_field
        for model_field in model._meta.local_many_to_many
        if model_field.remote_field.through._meta.auto_created
    }
    serialized_fields: dict[str, Any] = {}
    for field_name in sorted(fields):
        if field_name in field_map:
            serialized_fields[field_name] = _v3_field_value(
                obj,
                field_map[field_name],
            )
            continue
        try:
            model_field = m2m_map[field_name]
        except KeyError as exc:
            raise DataPortabilityError(
                f'Frozen v3 field {spec.name}.{field_name} no longer exists.'
            ) from exc
        related_model = model_field.remote_field.model
        values = []
        for related in getattr(obj, field_name).all().iterator(chunk_size=1000):
            values.append(_v3_related_reference(related_model, related))
        serialized_fields[field_name] = sorted(values, key=_v3_sort_key)

    return {
        'model': spec.model_label.lower(),
        'pk': _canonical_v3_value(obj.pk),
        'fields': serialized_fields,
    }


def _write_v3_record(stream, record: dict[str, Any]) -> None:
    stream.write(json.dumps(
        record,
        ensure_ascii=False,
        allow_nan=False,
        separators=(',', ':'),
        sort_keys=True,
    ))
    stream.write('\n')


def _serialize_v3_queryset(queryset, stream, *, spec: EntitySpec) -> None:
    fields = _expected_serialized_fields(spec, dataset_version=3)
    for obj in queryset.iterator(chunk_size=1000):
        _write_v3_record(stream, _v3_record(spec, obj, fields))


def _serialize_entity(
    spec: EntitySpec,
    destination: Path,
    *,
    dataset_version: int = DATASET_VERSION,
) -> dict[str, Any]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    with destination.open('w', encoding='utf-8', newline='\n') as stream:
        if dataset_version == 3:
            _serialize_v3_queryset(queryset, stream, spec=spec)
        else:
            _serialize_queryset(
                queryset,
                stream,
                fields=_expected_serialized_fields(
                    spec,
                    dataset_version=dataset_version,
                ),
            )
    return {
        'model': spec.model_label.lower(),
        'file': spec.filename,
        'count': count,
        'sha256': _sha256_file(destination),
    }


def _session_projection() -> dict[str, Any]:
    Session = apps.get_model('sessions.Session')
    now = timezone.now()
    latest_expiry = Session._default_manager.aggregate(
        latest=Max('expire_date'),
    )['latest']
    return {
        'table': Session._meta.db_table,
        'policy': V3_SESSION_PROJECTION_POLICY,
        'source_row_count': Session._default_manager.count(),
        'source_unexpired_count': Session._default_manager.filter(
            expire_date__gt=now,
        ).count(),
        'source_latest_expiry': (
            _format_v3_datetime(latest_expiry)
            if latest_expiry is not None
            else None
        ),
        'target_required_row_count': 0,
    }


def _table_row_count(table_name: str) -> int:
    quoted = connection.ops.quote_name(table_name)
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT COUNT(*) FROM {quoted}')
        return int(cursor.fetchone()[0])


def _discovered_database_objects() -> dict[str, str]:
    with connection.cursor() as cursor:
        table_info = connection.introspection.get_table_list(cursor)
    return {
        item.name: item.type
        for item in table_info
    }


def _internal_database_tables(
    discovered: set[str],
    *,
    vendor: str,
) -> set[str]:
    if vendor == 'sqlite':
        return discovered & SQLITE_INTERNAL_TABLES
    return set()


def _v3_table_inventory() -> dict[str, Any]:
    categories = _v3_static_table_categories()
    Session = apps.get_model('sessions.Session')
    excluded = {Session._meta.db_table}
    discovered_objects = _discovered_database_objects()
    discovered = {
        name
        for name, object_type in discovered_objects.items()
        if object_type in {'t', 'p'}
    }
    unsupported_objects = {
        name: object_type
        for name, object_type in discovered_objects.items()
        if object_type not in {'t', 'p'}
    }
    internal = _internal_database_tables(
        discovered,
        vendor=connection.vendor,
    )
    classified = (
        set(categories['direct'])
        | set(categories['embedded'])
        | set(categories['regenerated'])
        | excluded
        | internal
    )
    required = (
        set(categories['direct'])
        | set(categories['embedded'])
        | set(categories['regenerated'])
        | excluded
    )
    unknown = sorted(discovered - classified)
    return {
        'categories': categories,
        'discovered': discovered,
        'excluded': excluded,
        'internal': internal,
        'missing_required': sorted(required - discovered),
        'unsupported_objects': dict(sorted(unsupported_objects.items())),
        'unknown_counts': {
            table_name: _table_row_count(table_name)
            for table_name in unknown
        },
    }


def _build_table_projection() -> dict[str, Any]:
    Session = apps.get_model('sessions.Session')
    inventory = _v3_table_inventory()
    categories = inventory['categories']
    unknown_counts = inventory['unknown_counts']
    unknown_nonempty = {
        table_name: count
        for table_name, count in unknown_counts.items()
        if count
    }
    if unknown_nonempty:
        details = ', '.join(
            f'{table_name}={count}'
            for table_name, count in sorted(unknown_nonempty.items())
        )
        raise DataPortabilityError(
            'Unknown non-empty database tables are outside the frozen v3 '
            f'projection: {details}'
        )
    if inventory['unsupported_objects']:
        details = ', '.join(
            f'{name}({object_type})'
            for name, object_type in inventory['unsupported_objects'].items()
        )
        raise DataPortabilityError(
            'Unclassified database objects are outside the frozen v3 projection: '
            + details
        )

    session = _session_projection()
    return {
        'direct': categories['direct'],
        'embedded': categories['embedded'],
        'regenerated': categories['regenerated'],
        'excluded': {
            Session._meta.db_table: session,
        },
        'internal': sorted(inventory['internal']),
        'unknown_empty': sorted(unknown_counts),
        'unknown_nonempty': {},
        'unsupported_objects': {},
    }


def _permission_key(permission) -> tuple[str, str, str]:
    content_type = permission.content_type
    return (
        permission.codename,
        content_type.app_label,
        content_type.model,
    )


def _build_referenced_dependency_projection() -> dict[str, list[list[str]]]:
    Group = apps.get_model('auth.Group')
    User = apps.get_model('auth.User')
    LogEntry = apps.get_model('admin.LogEntry')
    permission_keys: set[tuple[str, str, str]] = set()

    for group in Group._default_manager.prefetch_related('permissions').iterator(
        chunk_size=1000,
    ):
        for permission in group.permissions.all():
            permission_keys.add(_permission_key(permission))
    for user in User._default_manager.prefetch_related('user_permissions').iterator(
        chunk_size=1000,
    ):
        for permission in user.user_permissions.all():
            permission_keys.add(_permission_key(permission))

    content_type_keys = {
        (app_label, model_name)
        for _, app_label, model_name in permission_keys
    }
    content_type_keys.update(
        LogEntry._default_manager.exclude(content_type=None).values_list(
            'content_type__app_label',
            'content_type__model',
        )
    )
    return {
        'permissions': [list(key) for key in sorted(permission_keys)],
        'content_types': [list(key) for key in sorted(content_type_keys)],
    }


def _build_dependency_projection() -> dict[str, Any]:
    ContentType = apps.get_model('contenttypes.ContentType')
    Permission = apps.get_model('auth.Permission')
    content_types = sorted(ContentType._default_manager.values_list(
        'app_label',
        'model',
    ))
    permissions = sorted(Permission._default_manager.values_list(
        'codename',
        'content_type__app_label',
        'content_type__model',
        'name',
    ))
    return {
        'content_types': [list(key) for key in content_types],
        'permissions': [
            {
                'natural_key': list(row[:3]),
                'name': row[3],
            }
            for row in permissions
        ],
        'references': _build_referenced_dependency_projection(),
    }


def _build_migration_projection() -> dict[str, Any]:
    loader = MigrationLoader(connection, ignore_no_migrations=True)
    applied = []
    for migration in MigrationRecorder.Migration.objects.order_by('app', 'name'):
        applied.append({
            'app': migration.app,
            'name': migration.name,
            'applied_at': (
                _format_v3_datetime(migration.applied)
                if migration.applied is not None
                else None
            ),
        })
    return {
        'table': MigrationRecorder.Migration._meta.db_table,
        'applied': applied,
        'leaf_nodes': [
            list(node)
            for node in sorted(loader.graph.leaf_nodes())
        ],
    }


def _quote_qualified_identifier(identifier: str) -> str:
    return '.'.join(
        connection.ops.quote_name(part.strip('"'))
        for part in identifier.split('.')
    )


def _postgres_sequence_name(spec: EntitySpec) -> str:
    model = spec.model
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT pg_get_serial_sequence(%s, %s)',
            [model._meta.db_table, model._meta.pk.column],
        )
        sequence_name = cursor.fetchone()[0]
    if not sequence_name:
        raise DataPortabilityError(
            f'PostgreSQL sequence not found for {spec.name}.'
        )
    return sequence_name


def _effective_sequence_next_floor(
    spec: EntitySpec,
    *,
    postgres_sequence_name: str | None = None,
) -> int:
    model = spec.model
    pk_field = model._meta.pk
    max_live_pk = model._default_manager.aggregate(
        max_pk=Max(pk_field.name),
    )['max_pk'] or 0
    if connection.vendor == 'sqlite':
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT seq FROM sqlite_sequence WHERE name = %s',
                [model._meta.db_table],
            )
            row = cursor.fetchone()
        stored_floor = int(row[0]) + 1 if row is not None else 1
        return max(int(max_live_pk) + 1, stored_floor)
    if connection.vendor == 'postgresql':
        sequence_name = postgres_sequence_name or _postgres_sequence_name(spec)
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT last_value, is_called FROM '
                f'{_quote_qualified_identifier(sequence_name)}'
            )
            last_value, is_called = cursor.fetchone()
        return int(last_value) + (1 if is_called else 0)
    raise DataPortabilityError(
        f'Dataset sequence floors are not implemented for {connection.vendor}.'
    )


def _sequence_next_floor(spec: EntitySpec) -> dict[str, Any] | None:
    model = spec.model
    pk_field = model._meta.pk
    if not isinstance(pk_field, models.AutoField):
        return None
    max_live_pk = model._default_manager.aggregate(
        max_pk=Max(pk_field.name),
    )['max_pk'] or 0
    next_floor = max(
        int(max_live_pk) + 1,
        _effective_sequence_next_floor(spec),
    )

    return {
        'table': model._meta.db_table,
        'pk_field': pk_field.column,
        'max_live_pk': int(max_live_pk),
        'next_value_floor': next_floor,
    }


def _build_sequence_projection() -> dict[str, dict[str, Any]]:
    projection = {}
    for spec in V3_ENTITY_SPECS:
        sequence = _sequence_next_floor(spec)
        if sequence is not None:
            projection[spec.name] = sequence
    return projection


def export_dataset(output_directory: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    current_schema_signature = _current_v3_model_schema_signature()
    if current_schema_signature != V3_MODEL_SCHEMA_SIGNATURE:
        raise DataPortabilityError(
            'The runtime model semantics no longer match the frozen v3 schema; '
            'define a new dataset version before exporting.'
        )
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
            table_projection = _build_table_projection()
            dependency_projection = _build_dependency_projection()
            migration_projection = _build_migration_projection()
            sequence_projection = _build_sequence_projection()
            for spec in V3_ENTITY_SPECS:
                entities[spec.name] = _serialize_entity(
                    spec,
                    staging / spec.filename,
                    dataset_version=DATASET_VERSION,
                )

        manifest = {
            'format': DATASET_FORMAT,
            'format_version': DATASET_VERSION,
            'codec': V3_CODEC,
            'schema_fingerprint': _schema_fingerprint(DATASET_VERSION),
            'model_schema_signature': V3_MODEL_SCHEMA_SIGNATURE,
            'application_version': os.environ.get('APP_VERSION', 'unknown'),
            'exported_at': _format_v3_datetime(timezone.now()),
            'source_database': connection.vendor,
            'entities': entities,
            'dependencies': dependency_projection,
            'migration_projection': migration_projection,
            'identity': {
                'sequences': sequence_projection,
            },
            'table_projection': table_projection,
            'session_projection': table_projection['excluded'][
                apps.get_model('sessions.Session')._meta.db_table
            ],
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
        if (
            dataset_version == 3
            and isinstance(model_field, models.DateTimeField)
            and (
                not isinstance(value, str)
                or _V3_DATETIME_PATTERN.fullmatch(value) is None
            )
        ):
            errors.append(
                f'{model_field.name} must be UTC with exactly six microseconds'
            )
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
    for model_field in spec.model._meta.local_many_to_many:
        if model_field.name not in fields:
            continue
        if not isinstance(fields[model_field.name], list):
            errors.append(f'{model_field.name} must be a list')
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


def _natural_permission(value: Any) -> tuple[str, str, str] | None:
    if (
        isinstance(value, list)
        and len(value) == 3
        and all(isinstance(item, str) for item in value)
    ):
        return tuple(value)
    return None


def _natural_content_type(value: Any) -> tuple[str, str] | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, str) for item in value)
    ):
        return tuple(value)
    return None


def _available_dependency_catalog() -> tuple[
    dict[tuple[str, str, str], str],
    set[tuple[str, str]],
]:
    Permission = apps.get_model('auth.Permission')
    ContentType = apps.get_model('contenttypes.ContentType')
    permissions = {
        (codename, app_label, model): name
        for codename, app_label, model, name
        in Permission._default_manager.values_list(
            'codename',
            'content_type__app_label',
            'content_type__model',
            'name',
        )
    }
    content_types = set(ContentType._default_manager.values_list(
        'app_label',
        'model',
    ))
    return permissions, content_types


def _available_dependency_keys() -> tuple[
    set[tuple[str, str, str]],
    set[tuple[str, str]],
]:
    permissions, content_types = _available_dependency_catalog()
    return set(permissions), content_types


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
        share_key = report.fields.get('share')
        if _is_hashable(share_key):
            reports_by_share.setdefault(share_key, []).append(report)

    rejects_by_share: dict[Any, list[ParsedRecord]] = {}
    approvals_by_share: dict[Any, list[ParsedRecord]] = {}
    for log in records['share_logs']:
        target = None
        if log.fields.get('action') == 'reject':
            target = rejects_by_share
        elif log.fields.get('action') == 'approve':
            target = approvals_by_share
        if target is not None:
            share_key = log.fields.get('share')
            if _is_hashable(share_key):
                target.setdefault(share_key, []).append(log)

    projections: dict[Any, RestrictionProjection] = {}
    for share in records['shares']:
        fields = share.fields
        share_pk = share.pk
        if not _is_hashable(share_pk):
            continue
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
    try:
        previous = seen.get(key)
    except TypeError:
        _add_error(errors_by_record, record, f'{label} must be hashable')
        return
    if previous is None:
        seen[key] = record
        return
    _add_error(errors_by_record, previous, f'duplicate {label}: {key!r}')
    _add_error(errors_by_record, record, f'duplicate {label}: {key!r}')


def _is_hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _key_exists(keys: set[Any], value: Any) -> bool:
    return _is_hashable(value) and value in keys


def _validate_cross_entity(
    records: dict[str, list[ParsedRecord]],
    errors_by_record: dict[tuple[str, int], list[str]],
    *,
    dataset_version: int,
) -> dict[str, list[list[str]]]:
    user_names: dict[str, ParsedRecord] = {}
    group_names: dict[str, ParsedRecord] = {}
    share_pks = {record.pk for record in records['shares'] if _is_hashable(record.pk)}
    report_pks = {record.pk for record in records['reports'] if _is_hashable(record.pk)}
    collection_pks = {
        record.pk
        for record in records['collections']
        if _is_hashable(record.pk)
    }
    referenced_permissions: set[tuple[str, str, str]] = set()
    referenced_content_types: set[tuple[str, str]] = set()
    if dataset_version == 3:
        available_permissions, available_content_types = _available_dependency_keys()
    else:
        available_permissions, available_content_types = set(), set()

    for record in records['groups']:
        name = record.fields.get('name')
        if isinstance(name, str):
            _check_duplicate(group_names, name, record, 'group name', errors_by_record)
        if dataset_version == 3:
            for reference in _list_or_empty(record.fields.get('permissions')):
                permission = _natural_permission(reference)
                if permission is None or permission not in available_permissions:
                    _add_error(
                        errors_by_record,
                        record,
                        f'unknown permission reference: {reference!r}',
                    )
                    continue
                referenced_permissions.add(permission)
                referenced_content_types.add((permission[1], permission[2]))

    for record in records['users']:
        username = record.fields.get('username')
        if isinstance(username, str):
            _check_duplicate(user_names, username, record, 'username', errors_by_record)
        for group_ref in _list_or_empty(record.fields.get('groups')):
            group_name = _natural_user(group_ref)
            if group_name is None or group_name not in group_names:
                _add_error(errors_by_record, record, f'unknown group reference: {group_ref!r}')
        if dataset_version == 3:
            for reference in _list_or_empty(record.fields.get('user_permissions')):
                permission = _natural_permission(reference)
                if permission is None or permission not in available_permissions:
                    _add_error(
                        errors_by_record,
                        record,
                        f'unknown permission reference: {reference!r}',
                    )
                    continue
                referenced_permissions.add(permission)
                referenced_content_types.add((permission[1], permission[2]))

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
            for reference in _list_or_empty(fields.get(field_name)):
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
        if not _key_exists(collection_pks, collection_id):
            _add_error(errors_by_record, record, 'collection item references an unknown collection')
        if not _key_exists(share_pks, share_id):
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
        if not _key_exists(share_pks, share_id):
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
        if not _key_exists(share_pks, record.fields.get('share')):
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
        if related_share is not None and not _key_exists(share_pks, related_share):
            _add_error(errors_by_record, record, 'site message references an unknown share')
        related_report = fields.get('related_report')
        if related_report is not None and not _key_exists(report_pks, related_report):
            _add_error(errors_by_record, record, 'site message references an unknown report')

    if dataset_version == 3:
        for record in records['admin_log_entries']:
            fields = record.fields
            username = _natural_user(fields.get('user'))
            if username is None or username not in user_names:
                _add_error(
                    errors_by_record,
                    record,
                    'admin log references an unknown user',
                )
            content_type_reference = fields.get('content_type')
            if content_type_reference is not None:
                content_type = _natural_content_type(content_type_reference)
                if (
                    content_type is None
                    or content_type not in available_content_types
                ):
                    _add_error(
                        errors_by_record,
                        record,
                        'admin log content type cannot be mapped on this target',
                    )
                else:
                    referenced_content_types.add(content_type)
            if fields.get('action_flag') not in {1, 2, 3}:
                _add_error(
                    errors_by_record,
                    record,
                    'admin log action_flag is invalid',
                )

    return {
        'permissions': [list(key) for key in sorted(referenced_permissions)],
        'content_types': [list(key) for key in sorted(referenced_content_types)],
    }


def _v3_static_table_categories() -> dict[str, list[str]]:
    direct = sorted(spec.model._meta.db_table for spec in V3_ENTITY_SPECS)
    embedded = set()
    for spec in V3_ENTITY_SPECS:
        for model_field in spec.model._meta.local_many_to_many:
            if (
                model_field.name in V3_ENTITY_FIELDS[spec.name]
                and model_field.remote_field.through._meta.auto_created
            ):
                embedded.add(model_field.remote_field.through._meta.db_table)
    return {
        'direct': direct,
        'embedded': sorted(embedded),
        'regenerated': sorted({
            apps.get_model('contenttypes.ContentType')._meta.db_table,
            apps.get_model('auth.Permission')._meta.db_table,
            MigrationRecorder.Migration._meta.db_table,
        }),
        'internal': ['sqlite_sequence'] if connection.vendor == 'sqlite' else [],
    }


def _validate_v3_manifest_shape(
    manifest: dict[str, Any],
    report: ValidationReport,
) -> None:
    if manifest.get('codec') != V3_CODEC:
        report.errors.append(f'Unexpected v3 codec: {manifest.get("codec")!r}')
    if manifest.get('schema_fingerprint') != _schema_fingerprint(3):
        report.errors.append('v3 schema fingerprint does not match this importer')
    if manifest.get('model_schema_signature') != V3_MODEL_SCHEMA_SIGNATURE:
        report.errors.append('v3 model schema signature does not match this importer')
    if (
        DATASET_VERSION == 3
        and _current_v3_model_schema_signature() != V3_MODEL_SCHEMA_SIGNATURE
    ):
        report.errors.append(
            'The current v3 exporter runtime no longer implements its frozen '
            'schema contract'
        )

    projection = manifest.get('table_projection')
    if not isinstance(projection, dict):
        report.errors.append('v3 table_projection must be an object')
        return
    expected = _v3_static_table_categories()
    for category in ('direct', 'embedded', 'regenerated'):
        if projection.get(category) != expected[category]:
            report.errors.append(f'Unexpected v3 {category} table projection')
    internal = projection.get('internal')
    if not isinstance(internal, list):
        report.errors.append('v3 internal table projection must be a list')
    if projection.get('unknown_nonempty') != {}:
        report.errors.append('v3 source contains unknown non-empty tables')
    if projection.get('unsupported_objects') != {}:
        report.errors.append('v3 source contains unclassified database objects')

    Session = apps.get_model('sessions.Session')
    session_table = Session._meta.db_table
    session = manifest.get('session_projection')
    excluded = projection.get('excluded')
    if not isinstance(session, dict):
        report.errors.append('v3 session_projection must be an object')
        return
    if not isinstance(excluded, dict) or excluded.get(session_table) != session:
        report.errors.append('v3 excluded table projection must contain django_session')
    if session.get('table') != session_table:
        report.errors.append('v3 session projection names an unexpected table')
    if session.get('policy') != V3_SESSION_PROJECTION_POLICY:
        report.errors.append('v3 session projection must force logout at cutover')
    if session.get('target_required_row_count') != 0:
        report.errors.append('v3 target session row count must be zero')
    for field_name in ('source_row_count', 'source_unexpired_count'):
        value = session.get(field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            report.errors.append(f'v3 session {field_name} must be nonnegative')
    source_row_count = session.get('source_row_count')
    source_unexpired_count = session.get('source_unexpired_count')
    if (
        isinstance(source_row_count, int)
        and not isinstance(source_row_count, bool)
        and isinstance(source_unexpired_count, int)
        and not isinstance(source_unexpired_count, bool)
        and source_unexpired_count > source_row_count
    ):
        report.errors.append(
            'v3 unexpired session count cannot exceed the total session count'
        )
    latest_expiry = session.get('source_latest_expiry')
    if (
        latest_expiry is not None
        and (
            not isinstance(latest_expiry, str)
            or _V3_DATETIME_PATTERN.fullmatch(latest_expiry) is None
        )
    ):
        report.errors.append('v3 session latest expiry must use exact UTC microseconds')
    if 'session_data' in session:
        report.errors.append('v3 session projection must not contain session_data')


def _validate_v3_dependencies(
    manifest: dict[str, Any],
    derived_references: dict[str, list[list[str]]],
    report: ValidationReport,
) -> None:
    dependencies = manifest.get('dependencies')
    if not isinstance(dependencies, dict):
        report.errors.append('v3 dependencies must be an object')
        return

    raw_content_types = dependencies.get('content_types')
    content_types: set[tuple[str, str]] = set()
    if not isinstance(raw_content_types, list):
        report.errors.append('v3 dependency content_types must be a list')
    else:
        invalid_content_type = False
        for raw_key in raw_content_types:
            key = _natural_content_type(raw_key)
            if key is None:
                invalid_content_type = True
            else:
                content_types.add(key)
        canonical_content_types = [list(key) for key in sorted(content_types)]
        if invalid_content_type or raw_content_types != canonical_content_types:
            report.errors.append(
                'v3 dependency content_types must be unique canonical natural keys'
            )

    raw_permissions = dependencies.get('permissions')
    permissions: dict[tuple[str, str, str], str] = {}
    if not isinstance(raw_permissions, list):
        report.errors.append('v3 dependency permissions must be a list')
    else:
        invalid_permission = False
        for raw_permission in raw_permissions:
            if not isinstance(raw_permission, dict):
                invalid_permission = True
                continue
            natural_key = _natural_permission(raw_permission.get('natural_key'))
            name = raw_permission.get('name')
            if (
                natural_key is None
                or not isinstance(name, str)
                or natural_key in permissions
            ):
                invalid_permission = True
                continue
            permissions[natural_key] = name
        canonical_permissions = [
            {
                'natural_key': list(key),
                'name': permissions[key],
            }
            for key in sorted(permissions)
        ]
        if invalid_permission or raw_permissions != canonical_permissions:
            report.errors.append(
                'v3 dependency permissions must be unique canonical natural keys with names'
            )

    references = dependencies.get('references')
    if references != derived_references:
        report.errors.append(
            'v3 dependency references do not match serialized records'
        )

    for reference in derived_references['content_types']:
        key = _natural_content_type(reference)
        if key is not None and key not in content_types:
            report.errors.append(
                f'v3 referenced ContentType is absent from source catalog: {key!r}'
            )
    for reference in derived_references['permissions']:
        key = _natural_permission(reference)
        if key is not None and key not in permissions:
            report.errors.append(
                f'v3 referenced Permission is absent from source catalog: {key!r}'
            )
    for key in permissions:
        if (key[1], key[2]) not in content_types:
            report.errors.append(
                f'v3 Permission references an absent source ContentType: {key!r}'
            )

    target_permissions, target_content_types = _available_dependency_catalog()
    missing_content_types = sorted(content_types - target_content_types)
    if missing_content_types:
        report.errors.append(
            'Target cannot map source ContentTypes: '
            + ', '.join(f'{app_label}.{model}' for app_label, model in missing_content_types)
        )
    missing_permissions = sorted(set(permissions) - set(target_permissions))
    if missing_permissions:
        report.errors.append(
            'Target cannot map source Permissions: '
            + ', '.join('/'.join(key) for key in missing_permissions)
        )
    renamed_permissions = sorted(
        key
        for key, source_name in permissions.items()
        if key in target_permissions and target_permissions[key] != source_name
    )
    if renamed_permissions:
        report.errors.append(
            'Target Permission names differ from source: '
            + ', '.join('/'.join(key) for key in renamed_permissions)
        )


def _validate_v3_migration_projection(
    manifest: dict[str, Any],
    report: ValidationReport,
) -> None:
    projection = manifest.get('migration_projection')
    if not isinstance(projection, dict):
        report.errors.append('v3 migration_projection must be an object')
        return
    if projection.get('table') != MigrationRecorder.Migration._meta.db_table:
        report.errors.append('v3 migration projection names an unexpected table')

    raw_applied = projection.get('applied')
    applied_nodes: set[tuple[str, str]] = set()
    canonical_applied: list[dict[str, str]] = []
    invalid_applied = False
    if not isinstance(raw_applied, list):
        report.errors.append('v3 applied migration projection must be a list')
    else:
        for item in raw_applied:
            if not isinstance(item, dict):
                invalid_applied = True
                continue
            app_label = item.get('app')
            name = item.get('name')
            applied_at = item.get('applied_at')
            node = (app_label, name)
            if (
                not isinstance(app_label, str)
                or not isinstance(name, str)
                or not isinstance(applied_at, str)
                or _V3_DATETIME_PATTERN.fullmatch(applied_at) is None
                or node in applied_nodes
            ):
                invalid_applied = True
                continue
            applied_nodes.add(node)
            canonical_applied.append({
                'app': app_label,
                'name': name,
                'applied_at': applied_at,
            })
        canonical_applied.sort(key=lambda item: (item['app'], item['name']))
        if invalid_applied or raw_applied != canonical_applied:
            report.errors.append(
                'v3 applied migrations must be unique, sorted, and use exact UTC times'
            )

    raw_leaf_nodes = projection.get('leaf_nodes')
    leaf_nodes: list[tuple[str, str]] = []
    if isinstance(raw_leaf_nodes, list):
        for raw_node in raw_leaf_nodes:
            node = _natural_content_type(raw_node)
            if node is None:
                leaf_nodes = []
                break
            leaf_nodes.append(node)
    canonical_leaf_nodes = [list(node) for node in sorted(set(leaf_nodes))]
    if (
        not isinstance(raw_leaf_nodes, list)
        or raw_leaf_nodes != canonical_leaf_nodes
    ):
        report.errors.append('v3 migration leaf nodes must be unique canonical pairs')

    loader = MigrationLoader(connection, ignore_no_migrations=True)
    known_nodes = set(loader.graph.nodes)
    unknown_source_migrations = sorted(applied_nodes - known_nodes)
    if unknown_source_migrations:
        report.errors.append(
            'v3 source migrations are unknown to this importer: '
            + ', '.join('/'.join(node) for node in unknown_source_migrations)
        )
    unknown_source_leaves = sorted(set(leaf_nodes) - known_nodes)
    if unknown_source_leaves:
        report.errors.append(
            'v3 source migration leaves are unknown to this importer: '
            + ', '.join('/'.join(node) for node in unknown_source_leaves)
        )
    if not leaf_nodes:
        report.errors.append('v3 source migration leaves must not be empty')
    missing_source_leaves = sorted(set(leaf_nodes) - applied_nodes)
    if missing_source_leaves:
        report.errors.append(
            'v3 source migration leaves are not recorded as applied: '
            + ', '.join('/'.join(node) for node in missing_source_leaves)
        )
    missing_target_leaves = sorted(
        set(leaf_nodes) - set(loader.applied_migrations)
    )
    if missing_target_leaves:
        report.errors.append(
            'Target has not applied source migration leaves: '
            + ', '.join('/'.join(node) for node in missing_target_leaves)
        )


def _validate_v3_sequences(
    manifest: dict[str, Any],
    records: dict[str, list[ParsedRecord]],
    report: ValidationReport,
) -> None:
    identity = manifest.get('identity')
    sequences = identity.get('sequences') if isinstance(identity, dict) else None
    if not isinstance(sequences, dict):
        report.errors.append('v3 identity.sequences must be an object')
        return
    expected_specs = {
        spec.name: spec
        for spec in V3_ENTITY_SPECS
        if isinstance(spec.model._meta.pk, models.AutoField)
    }
    if set(sequences) != set(expected_specs):
        report.errors.append('v3 sequence projection does not cover every auto-PK entity')
        return
    for entity_name, spec in expected_specs.items():
        metadata = sequences.get(entity_name)
        if not isinstance(metadata, dict):
            report.errors.append(f'Invalid v3 sequence metadata for {entity_name}')
            continue
        pks = [record.pk for record in records[entity_name]]
        if any(not isinstance(pk, int) or isinstance(pk, bool) for pk in pks):
            report.errors.append(f'v3 {entity_name} primary keys must be integers')
            continue
        max_live_pk = max(pks, default=0)
        next_floor = metadata.get('next_value_floor')
        if (
            metadata.get('table') != spec.model._meta.db_table
            or metadata.get('pk_field') != spec.model._meta.pk.column
            or metadata.get('max_live_pk') != max_live_pk
            or not isinstance(next_floor, int)
            or isinstance(next_floor, bool)
            or next_floor < max_live_pk + 1
        ):
            report.errors.append(f'Invalid v3 sequence metadata for {entity_name}')


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
    specs = _entity_specs_for_version(schema_version)
    entity_by_name = _entity_by_name_for_version(schema_version)
    expected_jsonl_files = {spec.filename for spec in specs}
    unexpected_jsonl_files = sorted(
        path.name
        for path in root.glob('*.jsonl')
        if path.is_file() and path.name not in expected_jsonl_files
    )
    if unexpected_jsonl_files:
        report.errors.append(
            'Unexpected JSONL files: ' + ', '.join(unexpected_jsonl_files)
        )
    if schema_version == 3:
        _validate_v3_manifest_shape(manifest, report)

    manifest_entities = manifest.get('entities')
    if not isinstance(manifest_entities, dict):
        report.errors.append('Manifest entities must be an object')
        return report
    unexpected_entities = sorted(set(manifest_entities) - set(entity_by_name))
    if unexpected_entities:
        report.errors.append(
            f'Unexpected manifest entities: {", ".join(unexpected_entities)}'
        )

    records: dict[str, list[ParsedRecord]] = {spec.name: [] for spec in specs}
    errors_by_record: dict[tuple[str, int], list[str]] = {}
    for spec in specs:
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
        with data_path.open('r', encoding='utf-8', newline='') as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    report.errors.append(f'{spec.filename}:{line_number}: blank line')
                    continue
                try:
                    payload = json.loads(
                        line,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError(f'non-finite JSON constant: {value}')
                        ),
                    )
                except (json.JSONDecodeError, ValueError) as exc:
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
                if schema_version == 3:
                    try:
                        canonical_line = json.dumps(
                            payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(',', ':'),
                            sort_keys=True,
                        ) + '\n'
                    except (TypeError, ValueError) as exc:
                        record_errors.append(f'record is not canonical v3 JSON: {exc}')
                    else:
                        if line != canonical_line:
                            record_errors.append('record is not canonical v3 JSONL')
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
                if record.pk is not None and not _is_hashable(record.pk):
                    record_errors.append('primary key must be hashable')
                if record_errors:
                    errors_by_record[(spec.name, line_number)] = record_errors
                if record.pk is not None and _is_hashable(record.pk):
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
    dependencies = _validate_cross_entity(
        records,
        errors_by_record,
        dataset_version=schema_version,
    )
    if schema_version == 3:
        _validate_v3_dependencies(manifest, dependencies, report)
        _validate_v3_migration_projection(manifest, report)
        _validate_v3_sequences(manifest, records, report)
    existing_quarantine = {
        (item['entity'], item['line'])
        for item in report.quarantined_records
    }
    for spec in specs:
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


class _DigestTextStream:
    def __init__(self):
        self.digest = sha256()

    def write(self, value: str) -> int:
        self.digest.update(value.encode('utf-8'))
        return len(value)


def _database_entity_digest(
    spec: EntitySpec,
    *,
    dataset_version: int,
) -> tuple[int, str]:
    queryset = spec.model._default_manager.order_by(spec.model._meta.pk.name)
    count = queryset.count()
    stream = _DigestTextStream()
    fields = _expected_serialized_fields(spec, dataset_version=dataset_version)
    if dataset_version == 3:
        _serialize_v3_queryset(queryset, stream, spec=spec)
    else:
        _serialize_queryset(queryset, stream, fields=fields)
    return count, stream.digest.hexdigest()


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


def _embedded_v3_rows_are_resolvable() -> bool:
    checked_tables: set[str] = set()
    quoted = connection.ops.quote_name
    for spec in V3_ENTITY_SPECS:
        for model_field in spec.model._meta.local_many_to_many:
            if (
                model_field.name not in V3_ENTITY_FIELDS[spec.name]
                or not model_field.remote_field.through._meta.auto_created
            ):
                continue
            through = model_field.remote_field.through
            table_name = through._meta.db_table
            if table_name in checked_tables:
                continue
            checked_tables.add(table_name)
            source_field = through._meta.get_field(model_field.m2m_field_name())
            target_field = through._meta.get_field(
                model_field.m2m_reverse_field_name()
            )
            source_model = source_field.remote_field.model
            target_model = target_field.remote_field.model
            sql = (
                'SELECT COUNT(*) FROM '
                f'{quoted(table_name)} bridge '
                f'INNER JOIN {quoted(source_model._meta.db_table)} source_row '
                f'ON bridge.{quoted(source_field.column)} = '
                f'source_row.{quoted(source_model._meta.pk.column)} '
                f'INNER JOIN {quoted(target_model._meta.db_table)} target_row '
                f'ON bridge.{quoted(target_field.column)} = '
                f'target_row.{quoted(target_model._meta.pk.column)}'
            )
            with connection.cursor() as cursor:
                cursor.execute(sql)
                resolvable_count = int(cursor.fetchone()[0])
            if resolvable_count != _table_row_count(table_name):
                return False
    return True


def _v3_target_structure_matches_manifest(manifest: dict[str, Any]) -> bool:
    report = ValidationReport(dataset='<target-database>', manifest=manifest)
    _validate_v3_manifest_shape(manifest, report)
    dependencies = manifest.get('dependencies')
    references = (
        dependencies.get('references')
        if isinstance(dependencies, dict)
        else None
    )
    if not isinstance(references, dict):
        return False
    _validate_v3_dependencies(manifest, references, report)
    _validate_v3_migration_projection(manifest, report)
    if report.errors:
        return False

    inventory = _v3_table_inventory()
    if inventory['missing_required'] or inventory['unsupported_objects']:
        return False
    if any(inventory['unknown_counts'].values()):
        return False
    return _embedded_v3_rows_are_resolvable()


def database_matches_manifest(
    manifest: dict[str, Any],
    *,
    require_sequence_floors: bool = True,
) -> bool:
    dataset_version = manifest.get('format_version')
    if (
        not isinstance(dataset_version, int)
        or isinstance(dataset_version, bool)
        or dataset_version not in SUPPORTED_DATASET_VERSIONS
    ):
        return False
    specs = _entity_specs_for_version(dataset_version)
    metadata_by_entity = manifest.get('entities', {})
    if not isinstance(metadata_by_entity, dict):
        return False
    if set(metadata_by_entity) != {spec.name for spec in specs}:
        return False
    Session = apps.get_model('sessions.Session')
    LogEntry = apps.get_model('admin.LogEntry')
    if Session._default_manager.exists():
        return False
    if dataset_version < 3 and LogEntry._default_manager.exists():
        return False
    inventory = _v3_table_inventory()
    if (
        inventory['missing_required']
        or inventory['unsupported_objects']
        or any(inventory['unknown_counts'].values())
    ):
        return False
    if not _embedded_v3_rows_are_resolvable():
        return False
    if dataset_version == 3:
        if manifest.get('schema_fingerprint') != _schema_fingerprint(3):
            return False
        if not _v3_target_structure_matches_manifest(manifest):
            return False
        identity = manifest.get('identity')
        sequences = identity.get('sequences') if isinstance(identity, dict) else None
        if not isinstance(sequences, dict):
            return False
    for spec in specs:
        metadata = metadata_by_entity.get(spec.name, {})
        count, digest = _database_entity_digest(
            spec,
            dataset_version=dataset_version,
        )
        if count != metadata.get('count') or digest != metadata.get('sha256'):
            return False
    if dataset_version == 1 and not _database_v1_restrictions_match():
        return False
    if require_sequence_floors:
        try:
            bindings = _preflight_sequence_bindings(specs)
            required_floors = _required_sequence_floors(manifest, specs)
            for spec in specs:
                required = required_floors.get(spec.name)
                if required is None:
                    continue
                actual = _effective_sequence_next_floor(
                    spec,
                    postgres_sequence_name=bindings[spec.name],
                )
                if actual < required:
                    return False
        except (DataPortabilityError, KeyError, TypeError):
            return False
    return True


def _database_has_portable_data() -> bool:
    inventory = _v3_table_inventory()
    if inventory['unsupported_objects']:
        return True
    categories = inventory['categories']
    portable_tables = (
        set(categories['direct'])
        | set(categories['embedded'])
        | inventory['excluded']
    )
    for table_name in portable_tables & inventory['discovered']:
        if _table_row_count(table_name):
            return True
    return any(inventory['unknown_counts'].values())


def _preflight_sequence_bindings(
    specs: tuple[EntitySpec, ...],
) -> dict[str, str | None]:
    if connection.vendor not in {'sqlite', 'postgresql'}:
        raise DataPortabilityError(
            f'Dataset sequence floors are not implemented for {connection.vendor}.'
        )
    bindings: dict[str, str | None] = {}
    for spec in specs:
        if not isinstance(spec.model._meta.pk, models.AutoField):
            continue
        sequence_name = (
            _postgres_sequence_name(spec)
            if connection.vendor == 'postgresql'
            else None
        )
        _effective_sequence_next_floor(
            spec,
            postgres_sequence_name=sequence_name,
        )
        bindings[spec.name] = sequence_name
    return bindings


def _required_sequence_floors(
    manifest: dict[str, Any],
    specs: tuple[EntitySpec, ...],
) -> dict[str, int]:
    source_version = manifest['format_version']
    manifest_sequences = (
        manifest['identity']['sequences']
        if source_version == 3
        else {}
    )
    floors: dict[str, int] = {}
    for spec in specs:
        pk_field = spec.model._meta.pk
        if not isinstance(pk_field, models.AutoField):
            continue
        max_live_pk = spec.model._default_manager.aggregate(
            max_pk=Max(pk_field.name),
        )['max_pk'] or 0
        required = int(max_live_pk) + 1
        if source_version == 3:
            required = max(
                required,
                manifest_sequences[spec.name]['next_value_floor'],
            )
        floors[spec.name] = required
    return floors


def _raise_sequence_floor(
    spec: EntitySpec,
    next_value_floor: int,
    *,
    postgres_sequence_name: str | None = None,
) -> None:
    model = spec.model
    pk_field = model._meta.pk
    if not isinstance(pk_field, models.AutoField):
        return
    if connection.vendor == 'sqlite':
        required_seq = next_value_floor - 1
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT seq FROM sqlite_sequence WHERE name = %s',
                [model._meta.db_table],
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    'INSERT INTO sqlite_sequence(name, seq) VALUES (%s, %s)',
                    [model._meta.db_table, required_seq],
                )
            elif int(row[0]) < required_seq:
                cursor.execute(
                    'UPDATE sqlite_sequence SET seq = %s WHERE name = %s',
                    [required_seq, model._meta.db_table],
                )
        return
    if connection.vendor == 'postgresql':
        sequence_name = postgres_sequence_name or _postgres_sequence_name(spec)
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT last_value, is_called FROM '
                f'{_quote_qualified_identifier(sequence_name)}'
            )
            last_value, is_called = cursor.fetchone()
            current_floor = int(last_value) + (1 if is_called else 0)
            if current_floor < next_value_floor:
                cursor.execute(
                    'SELECT setval(%s::regclass, %s, false)',
                    [sequence_name, next_value_floor],
                )
        return
    raise DataPortabilityError(
        f'Dataset sequence floors are not implemented for {connection.vendor}.'
    )


def _finalize_sequence_floors(
    manifest: dict[str, Any],
    specs: tuple[EntitySpec, ...],
    bindings: dict[str, str | None],
) -> None:
    required_floors = _required_sequence_floors(manifest, specs)
    for spec in specs:
        required = required_floors.get(spec.name)
        if required is None:
            continue
        _raise_sequence_floor(
            spec,
            required,
            postgres_sequence_name=bindings[spec.name],
        )
        actual = _effective_sequence_next_floor(
            spec,
            postgres_sequence_name=bindings[spec.name],
        )
        if actual < required:
            raise DataPortabilityError(
                f'Sequence floor verification failed for {spec.name}: '
                f'expected at least {required}, found {actual}.'
            )


def _load_v1_restriction_projections(
    root: Path,
) -> dict[Any, RestrictionProjection]:
    records: dict[str, list[ParsedRecord]] = {
        'shares': [],
        'reports': [],
        'share_logs': [],
    }
    entity_by_name = _entity_by_name_for_version(1)
    for entity_name in records:
        spec = entity_by_name[entity_name]
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


def _resolve_import_report_path(
    root: Path,
    report_path: str | Path | None,
) -> Path:
    resolved = (
        Path(report_path).expanduser().resolve()
        if report_path is not None
        else root.with_name(f'{root.name}-{IMPORT_REPORT_FILENAME}')
    )
    try:
        resolved.relative_to(root)
    except ValueError:
        pass
    else:
        raise DataPortabilityError(
            'The import report must be stored outside the immutable dataset directory.'
        )
    if resolved.exists() and not resolved.is_file():
        raise DataPortabilityError(
            f'The import report path is not a regular file: {resolved}'
        )
    return resolved


def _import_report_payload(
    validation: ValidationReport,
    *,
    attempt_id: str,
    manifest_sha256: str | None,
    status: str,
    target_state: str,
    database_state: str,
    data_stage: str,
    sequence_stage: str,
    recoverable: bool,
    target_session_row_count: int | None,
    import_lock: str,
    message: str | None = None,
) -> dict[str, Any]:
    payload = {
        **validation.as_dict(),
        'operation': 'site_data_import',
        'attempt_id': attempt_id,
        'manifest_sha256': manifest_sha256,
        'status': status,
        'target_state': target_state,
        'database_state': database_state,
        'data_stage': data_stage,
        'sequence_stage': sequence_stage,
        'recoverable': recoverable,
        'cutover_authorized': False,
        'target_session_row_count': target_session_row_count,
        'exclusive_target_attested': True,
        'import_lock': import_lock,
    }
    if message:
        payload['message'] = message
    return payload


def _classify_import_target(manifest: dict[str, Any]) -> str:
    if database_matches_manifest(manifest):
        return 'complete'
    if database_matches_manifest(manifest, require_sequence_floors=False):
        return 'content_match'
    if _database_has_portable_data():
        return 'conflict'
    return 'empty'


def _reclassify_after_commit_exception(manifest: dict[str, Any]) -> str:
    try:
        if connection.connection is None or not connection.is_usable():
            return 'unknown'
        if database_matches_manifest(
            manifest,
            require_sequence_floors=False,
        ):
            return 'content_committed'
        if not _database_has_portable_data():
            return 'rolled_back'
        return 'conflict_or_partial'
    except Exception:
        return 'unknown'


def _ensure_durable_import_boundary() -> None:
    atomic_blocks = list(getattr(connection, 'atomic_blocks', []))
    testcase_only_boundary = bool(atomic_blocks) and all(
        getattr(block, '_from_testcase', False)
        for block in atomic_blocks
    )
    non_test_blocks = [
        block
        for block in atomic_blocks
        if not getattr(block, '_from_testcase', False)
    ]
    if non_test_blocks or (
        not connection.get_autocommit()
        and not testcase_only_boundary
    ):
        raise DataPortabilityError(
            'Site-data import cannot run inside an existing transaction; '
            'it requires its own durable commit boundary.'
        )


@contextmanager
def _exclusive_target_import_lock():
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT pg_try_advisory_lock(%s, %s)',
                list(POSTGRES_IMPORT_LOCK_KEYS),
            )
            acquired = bool(cursor.fetchone()[0])
        if not acquired:
            raise DataPortabilityError(
                'Another site-data import holds the PostgreSQL advisory lock.'
            )
        try:
            yield 'postgresql_session_advisory_lock'
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT pg_advisory_unlock(%s, %s)',
                        list(POSTGRES_IMPORT_LOCK_KEYS),
                    )
            except Exception:
                connection.close()
        return

    if connection.vendor == 'sqlite':
        atomic_blocks = list(getattr(connection, 'atomic_blocks', []))
        if atomic_blocks and all(
            getattr(block, '_from_testcase', False)
            for block in atomic_blocks
        ):
            yield 'sqlite_testcase_boundary'
            return

        previous_mode = None
        try:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA main.locking_mode')
                previous_mode = str(cursor.fetchone()[0]).lower()
                cursor.execute('PRAGMA main.locking_mode=EXCLUSIVE')
                selected_mode = str(cursor.fetchone()[0]).lower()
                if selected_mode != 'exclusive':
                    raise DataPortabilityError(
                        'SQLite refused exclusive locking mode for site-data import.'
                    )
                cursor.execute('BEGIN EXCLUSIVE')
                cursor.execute('COMMIT')
            yield 'sqlite_exclusive_locking_mode'
        except DataPortabilityError:
            raise
        except Exception as exc:
            raise DataPortabilityError(
                'Could not acquire the exclusive SQLite import lock. Stop the '
                'target service and retry.'
            ) from exc
        finally:
            try:
                if previous_mode == 'normal':
                    try:
                        with connection.cursor() as cursor:
                            cursor.execute('PRAGMA main.locking_mode=NORMAL')
                    except Exception:
                        pass
            finally:
                database_name = connection.settings_dict.get('NAME')
                if not connection.creation.is_in_memory_db(database_name):
                    # SQLite retains the EXCLUSIVE file lock after COMMIT in
                    # locking_mode=EXCLUSIVE, including after switching the
                    # mode back to NORMAL. Closing the file-backed connection
                    # is the only reliable release boundary for other workers.
                    connection.close()
        return

    raise DataPortabilityError(
        f'Exclusive site-data import locking is not implemented for '
        f'{connection.vendor}.'
    )


def _import_dataset_locked(
    dataset_directory: str | Path,
    *,
    report_path: str | Path | None = None,
    import_lock: str,
) -> str:
    root = Path(dataset_directory).expanduser().resolve()
    validation_path = _resolve_import_report_path(root, report_path)
    try:
        target_session_row_count = apps.get_model(
            'sessions.Session'
        )._default_manager.count()
    except Exception:
        target_session_row_count = None
    validation = validate_dataset(root)
    attempt_id = uuid4().hex
    manifest_path = root / MANIFEST_FILENAME
    manifest_sha256 = (
        _sha256_file(manifest_path)
        if manifest_path.is_file()
        else None
    )
    if not validation.valid:
        _write_json_atomic(validation_path, _import_report_payload(
            validation,
            attempt_id=attempt_id,
            manifest_sha256=manifest_sha256,
            status='invalid_dataset',
            target_state='not_inspected',
            database_state='unchanged',
            data_stage='not_started',
            sequence_stage='not_started',
            recoverable=True,
            target_session_row_count=target_session_row_count,
            import_lock=import_lock,
        ))
        raise DataPortabilityError(
            f'Dataset validation failed; see quarantine report: {validation_path}'
        )
    assert validation.manifest is not None
    source_version = validation.manifest['format_version']
    specs = _entity_specs_for_version(source_version)
    target_state = _classify_import_target(validation.manifest)
    if target_state == 'complete':
        try:
            _write_json_atomic(validation_path, _import_report_payload(
                validation,
                attempt_id=attempt_id,
                manifest_sha256=manifest_sha256,
                status='already_imported',
                target_state=target_state,
                database_state='complete',
                data_stage='verified',
                sequence_stage='verified',
                recoverable=False,
                target_session_row_count=0,
                import_lock=import_lock,
            ))
        except OSError as exc:
            raise DataPortabilityError(
                'The target database is complete, but final import evidence could '
                'not be written. Rerun with a writable external --report path.'
            ) from exc
        return 'already_imported'
    if target_state == 'conflict':
        validation.errors.append(
            'Target database is not empty and does not exactly match this dataset.'
        )
        _write_json_atomic(validation_path, _import_report_payload(
            validation,
            attempt_id=attempt_id,
            manifest_sha256=manifest_sha256,
            status='conflict',
            target_state=target_state,
            database_state='unchanged',
            data_stage='not_started',
            sequence_stage='not_started',
            recoverable=False,
            target_session_row_count=target_session_row_count,
            import_lock=import_lock,
        ))
        raise DataPortabilityError(
            'Refusing to import into a non-empty target database with different data.'
        )

    _write_json_atomic(validation_path, _import_report_payload(
        validation,
        attempt_id=attempt_id,
        manifest_sha256=manifest_sha256,
        status='started',
        target_state=target_state,
        database_state=(
            'content_committed'
            if target_state == 'content_match'
            else 'unchanged'
        ),
        data_stage=(
            'verified'
            if target_state == 'content_match'
            else 'not_started'
        ),
        sequence_stage='not_started',
        recoverable=True,
        target_session_row_count=target_session_row_count,
        import_lock=import_lock,
        message='Rerun the same import to recover from an interrupted attempt.',
    ))
    try:
        sequence_bindings = _preflight_sequence_bindings(specs)
    except Exception as exc:
        validation.errors.append(str(exc))
        _write_json_atomic(validation_path, _import_report_payload(
            validation,
            attempt_id=attempt_id,
            manifest_sha256=manifest_sha256,
            status='preflight_failed',
            target_state=target_state,
            database_state=(
                'content_committed'
                if target_state == 'content_match'
                else 'unchanged'
            ),
            data_stage=(
                'verified'
                if target_state == 'content_match'
                else 'not_started'
            ),
            sequence_stage='not_started',
            recoverable=True,
            target_session_row_count=target_session_row_count,
            import_lock=import_lock,
        ))
        if isinstance(exc, DataPortabilityError):
            raise
        raise DataPortabilityError(f'Import sequence preflight failed: {exc}') from exc

    import_quarantine: list[dict[str, Any]] = []
    v1_projections = (
        _load_v1_restriction_projections(root)
        if source_version == 1
        else {}
    )
    if target_state == 'empty':
        transaction_body_completed = False
        try:
            with transaction.atomic(durable=True):
                for spec in specs:
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
                                        raise ValueError(
                                            'expected exactly one serialized object'
                                        )
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
                    raise DataPortabilityError(
                        'One or more records could not be imported.'
                    )
                if not database_matches_manifest(
                    validation.manifest,
                    require_sequence_floors=False,
                ):
                    validation.errors.append(
                        'Post-import counts or SHA-256 digests do not match the manifest.'
                    )
                    raise DataPortabilityError('Post-import content verification failed.')
                transaction_body_completed = True
        except Exception as exc:
            validation.quarantined_records.extend(import_quarantine)
            if not validation.errors:
                validation.errors.append(str(exc))
            if transaction_body_completed:
                database_state = _reclassify_after_commit_exception(
                    validation.manifest
                )
                try:
                    _write_json_atomic(validation_path, _import_report_payload(
                        validation,
                        attempt_id=attempt_id,
                        manifest_sha256=manifest_sha256,
                        status='commit_unknown',
                        target_state=target_state,
                        database_state=database_state,
                        data_stage='commit_unknown',
                        sequence_stage='not_started',
                        recoverable=True,
                        target_session_row_count=target_session_row_count,
                        import_lock=import_lock,
                        message=(
                            'The commit result could not be proven. Do not clear the '
                            'target database; rerun the same import so it can classify '
                            'the durable state and recover safely.'
                        ),
                    ))
                except OSError as report_exc:
                    raise DataPortabilityError(
                        'The content commit result is unknown and its recovery report '
                        'could not be written. Do not clear the target database; rerun '
                        'the same import with a writable external --report path.'
                    ) from report_exc
                raise DataPortabilityError(
                    'The content commit result is unknown. Do not clear the target '
                    'database; rerun the same import to classify and recover it.'
                ) from exc
            try:
                _write_json_atomic(validation_path, _import_report_payload(
                    validation,
                    attempt_id=attempt_id,
                    manifest_sha256=manifest_sha256,
                    status='rolled_back',
                    target_state=target_state,
                    database_state='rolled_back',
                    data_stage='rolled_back',
                    sequence_stage='not_started',
                    recoverable=True,
                    target_session_row_count=target_session_row_count,
                    import_lock=import_lock,
                ))
            except OSError as report_exc:
                raise DataPortabilityError(
                    'The durable data transaction was rolled back, and the failure '
                    'report could not be written.'
                ) from report_exc
            if isinstance(exc, DataPortabilityError):
                raise
            raise DataPortabilityError(
                f'Import failed and the durable data transaction was rolled back: {exc}'
            ) from exc

    try:
        _finalize_sequence_floors(
            validation.manifest,
            specs,
            sequence_bindings,
        )
        if not database_matches_manifest(validation.manifest):
            raise DataPortabilityError(
                'Final database verification failed after sequence finalization.'
            )
    except Exception as exc:
        validation.errors.append(str(exc))
        try:
            _write_json_atomic(validation_path, _import_report_payload(
                validation,
                attempt_id=attempt_id,
                manifest_sha256=manifest_sha256,
                status='finalization_incomplete',
                target_state=target_state,
                database_state='content_committed',
                data_stage='verified',
                sequence_stage='incomplete',
                recoverable=True,
                target_session_row_count=0,
                import_lock=import_lock,
                message=(
                    'Business rows are committed. Rerun the same import to finish '
                    'the idempotent sequence phase; do not clear the target database.'
                ),
            ))
        except OSError as report_exc:
            raise DataPortabilityError(
                'Business rows are committed and sequence finalization is incomplete; '
                'the recovery report also could not be written. Rerun the same import '
                'with a writable external --report path.'
            ) from report_exc
        if isinstance(exc, DataPortabilityError):
            raise
        raise DataPortabilityError(
            'Business rows are committed, but sequence finalization failed. '
            'Rerun the same import to recover.'
        ) from exc

    success_status = (
        'recovered'
        if target_state == 'content_match'
        else 'imported'
    )
    try:
        _write_json_atomic(validation_path, _import_report_payload(
            validation,
            attempt_id=attempt_id,
            manifest_sha256=manifest_sha256,
            status=success_status,
            target_state=target_state,
            database_state='complete',
            data_stage='verified',
            sequence_stage='verified',
            recoverable=False,
            target_session_row_count=0,
            import_lock=import_lock,
        ))
    except OSError as exc:
        raise DataPortabilityError(
            'The target database is complete, but final import evidence could not '
            'be written. Rerun with a writable external --report path to recover '
            'the evidence without importing rows again.'
        ) from exc
    return success_status


def import_dataset(
    dataset_directory: str | Path,
    *,
    report_path: str | Path | None = None,
    confirm_exclusive_target: bool = False,
) -> str:
    if not confirm_exclusive_target:
        raise DataPortabilityError(
            'Refusing to import without --confirm-exclusive-target. Stop every '
            'target application writer and attest the exclusive maintenance window.'
        )
    _ensure_durable_import_boundary()
    with _exclusive_target_import_lock() as import_lock:
        return _import_dataset_locked(
            dataset_directory,
            report_path=report_path,
            import_lock=import_lock,
        )
