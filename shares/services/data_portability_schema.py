"""Frozen wire schemas for portable site-data datasets.

This module is intentionally limited to versioned schema metadata and its
deterministic fingerprint.  Import/export execution remains in
``data_portability`` so schema review cannot accidentally acquire database
side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType

from django.apps import apps


DATASET_FORMAT = 'ffxivshare-jsonl'
DATASET_VERSION = 5
SUPPORTED_DATASET_VERSIONS = frozenset({1, 2, 3, 4, DATASET_VERSION})
MANIFEST_FILENAME = 'manifest.json'
VALIDATION_REPORT_FILENAME = 'validation-report.json'
IMPORT_REPORT_FILENAME = 'import-report.json'

V3_CODEC = 'canonical-jsonl-utc-microseconds'
V3_SESSION_PROJECTION_POLICY = 'force_logout_at_cutover'
V3_MODEL_SCHEMA_SIGNATURE = (
    '9b91a3b943d2986115508db51c216d94040053ec2c8e19b900acd2e0ddfdd685'
)
V4_MODEL_SCHEMA_SIGNATURE = (
    'bdd2b55012b63037477304e3de7a2168ddb741b6faf12c8dc83d22a24368de85'
)
V5_MODEL_SCHEMA_SIGNATURE = (
    'dd7d306a6fc075fae33f17cec09b30c8cf15406dfc5440fc79d83e0cddbd1900'
)
V3_NATURAL_KEY_PROTOCOL = MappingProxyType({
    'auth.group': ('name',),
    'auth.permission': ('codename', 'content_type.app_label', 'content_type.model'),
    'auth.user': ('username',),
    'contenttypes.contenttype': ('app_label', 'model'),
})
_V3_DATETIME_PATTERN = re.compile(
    r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
)


class DataPortabilityError(RuntimeError):
    pass


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
V4_ENTITY_SPECS = V3_ENTITY_SPECS
V5_ENTITY_SPECS = V4_ENTITY_SPECS
ENTITY_SPECS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_SPECS,
    2: V2_ENTITY_SPECS,
    3: V3_ENTITY_SPECS,
    4: V4_ENTITY_SPECS,
    5: V5_ENTITY_SPECS,
})
ENTITY_SPECS = ENTITY_SPECS_BY_VERSION[DATASET_VERSION]
ENTITY_BY_NAME = {spec.name: spec for spec in ENTITY_SPECS}

# Dataset schemas are public migration contracts, not projections of whichever
# Django models happen to be installed when an import runs. Keep every v1
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

# v2 added persistent moderation restrictions to Share. It remains a frozen
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
V4_ENTITY_FIELDS = MappingProxyType({
    **V3_ENTITY_FIELDS,
    'shares': frozenset({
        *V3_ENTITY_FIELDS['shares'],
        'deleted_at',
        'deleted_by',
        'deletion_origin',
        'deletion_reason',
    }),
    'collections': frozenset({
        *V3_ENTITY_FIELDS['collections'],
        'deleted_at',
        'deleted_by',
        'deletion_reason',
    }),
})
V5_ENTITY_FIELDS = V4_ENTITY_FIELDS
ENTITY_FIELDS_BY_VERSION = MappingProxyType({
    1: V1_ENTITY_FIELDS,
    2: V2_ENTITY_FIELDS,
    3: V3_ENTITY_FIELDS,
    4: V4_ENTITY_FIELDS,
    5: V5_ENTITY_FIELDS,
})
ENTITY_FIELDS = ENTITY_FIELDS_BY_VERSION[DATASET_VERSION]
MODEL_SCHEMA_SIGNATURE_BY_VERSION = MappingProxyType({
    3: V3_MODEL_SCHEMA_SIGNATURE,
    4: V4_MODEL_SCHEMA_SIGNATURE,
    5: V5_MODEL_SCHEMA_SIGNATURE,
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
        'codec': V3_CODEC if dataset_version >= 3 else 'django-jsonl-legacy',
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
    if dataset_version >= 3:
        payload['semantic_contract'] = {
            'model_schema_signature': MODEL_SCHEMA_SIGNATURE_BY_VERSION[
                dataset_version
            ],
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
