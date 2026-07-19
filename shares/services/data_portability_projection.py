"""Database projections captured in v3 portable dataset manifests."""

from __future__ import annotations

from typing import Any

from django.apps import apps
from django.db import connection, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Max
from django.utils import timezone

from .data_portability_codec import _format_v3_datetime
from .data_portability_schema import (
    DataPortabilityError,
    EntitySpec,
    V3_ENTITY_FIELDS,
    V3_ENTITY_SPECS,
    V3_SESSION_PROJECTION_POLICY,
)


SQLITE_INTERNAL_TABLES = frozenset({
    'sqlite_sequence',
    'sqlite_stat1',
    'sqlite_stat4',
})


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
