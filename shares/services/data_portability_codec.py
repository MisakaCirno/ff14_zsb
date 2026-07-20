"""Deterministic serializers for portable site-data records."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import json
import math
from typing import Any
from uuid import UUID

from django.core import serializers
from django.utils.encoding import is_protected_type
from django.utils.functional import Promise

from .data_portability_schema import (
    DATASET_VERSION,
    ENTITY_FIELDS_BY_VERSION,
    ENTITY_SPECS_BY_VERSION,
    DataPortabilityError,
    EntitySpec,
    V3_ENTITY_FIELDS,
    V3_ENTITY_SPECS,
    V3_MODEL_SCHEMA_SIGNATURE,
    V3_NATURAL_KEY_PROTOCOL,
)


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


def _current_model_schema_signature(
    dataset_version: int = DATASET_VERSION,
) -> str:
    payload = []
    specs = ENTITY_SPECS_BY_VERSION[dataset_version]
    fields_by_entity = ENTITY_FIELDS_BY_VERSION[dataset_version]
    for spec in specs:
        model = spec.model
        frozen_fields = fields_by_entity[spec.name]
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


def _current_v3_model_schema_signature() -> str:
    # v3 is a historical wire contract. The current models may gain choices
    # or fields in later dataset versions without rewriting that fingerprint.
    return V3_MODEL_SCHEMA_SIGNATURE


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
