from django.db.models import Count, OuterRef, Subquery
from django.utils import timezone

from shares.models import Report, Share, ShareLog


def _latest_value(queryset, field_name):
    return Subquery(queryset.values(field_name)[:1])


def _log_key(row, prefix):
    created_at = row[f'latest_{prefix}_at']
    if created_at is None:
        return None
    return created_at, row[f'latest_{prefix}_pk']


def _is_after(candidate, baseline):
    return candidate is not None and (baseline is None or candidate > baseline)


def _append_check(target, *, check, share_ids):
    ordered_ids = sorted(set(share_ids))
    if ordered_ids:
        target.append({
            'check': check,
            'count': len(ordered_ids),
            'share_ids': ordered_ids,
        })


def build_share_restriction_preflight():
    """Inspect migrated moderation evidence without changing application data."""
    latest_resolved = Report.objects.filter(
        share_id=OuterRef('pk'),
        status=Report.Status.RESOLVED,
    ).order_by('-resolved_at', '-pk')
    latest_approve = ShareLog.objects.filter(
        share_id=OuterRef('pk'),
        action=ShareLog.ActionType.REVIEW_APPROVE,
    ).order_by('-created_at', '-pk')
    latest_reject = ShareLog.objects.filter(
        share_id=OuterRef('pk'),
        action=ShareLog.ActionType.REVIEW_REJECT,
    ).order_by('-created_at', '-pk')
    latest_release = ShareLog.objects.filter(
        share_id=OuterRef('pk'),
        action=ShareLog.ActionType.RESTRICTION_RELEASE,
    ).order_by('-created_at', '-pk')
    latest_confirm = ShareLog.objects.filter(
        share_id=OuterRef('pk'),
        action=ShareLog.ActionType.RESTRICTION_CONFIRM,
    ).order_by('-created_at', '-pk')

    evidence = Share.objects.annotate(
        latest_resolved_at=_latest_value(latest_resolved, 'resolved_at'),
        latest_resolved_pk=_latest_value(latest_resolved, 'pk'),
        latest_approve_at=_latest_value(latest_approve, 'created_at'),
        latest_approve_pk=_latest_value(latest_approve, 'pk'),
        latest_reject_at=_latest_value(latest_reject, 'created_at'),
        latest_reject_pk=_latest_value(latest_reject, 'pk'),
        latest_release_at=_latest_value(latest_release, 'created_at'),
        latest_release_pk=_latest_value(latest_release, 'pk'),
        latest_confirm_at=_latest_value(latest_confirm, 'created_at'),
        latest_confirm_pk=_latest_value(latest_confirm, 'pk'),
    )

    resolved_report_without_takedown = []
    active_reject_without_restriction = []
    active_reject_wrong_restriction = []
    rejected_without_restriction = []
    restriction_without_active_evidence = []
    private_clear_without_classification = []
    legacy_private_on_non_private = []
    invalid_restriction_metadata = []
    ambiguous_report_approvals = []
    legacy_private_reviews = []

    evidence_rows = evidence.values(
        'share_id',
        'status',
        'visibility',
        'restriction_state',
        'restriction_reason',
        'restricted_at',
        'restricted_by_id',
        'latest_resolved_at',
        'latest_resolved_pk',
        'latest_approve_at',
        'latest_approve_pk',
        'latest_reject_at',
        'latest_reject_pk',
        'latest_release_at',
        'latest_release_pk',
        'latest_confirm_at',
        'latest_confirm_pk',
    ).iterator(chunk_size=1000)

    for row in evidence_rows:
        share_id = row['share_id']
        state = row['restriction_state']
        approve_key = _log_key(row, 'approve')
        reject_key = _log_key(row, 'reject')
        release_key = _log_key(row, 'release')
        confirm_key = _log_key(row, 'confirm')

        keep_times = [
            value for value in (
                row['latest_resolved_at'],
                row['latest_confirm_at'],
            ) if value is not None
        ]
        latest_keep_at = max(keep_times) if keep_times else None
        report_evidence_active = (
            latest_keep_at is not None
            and not (
                row['latest_release_at'] is not None
                and row['latest_release_at'] > latest_keep_at
            )
        )
        reject_evidence_active = (
            reject_key is not None
            and not _is_after(approve_key, reject_key)
            and not _is_after(release_key, reject_key)
        )

        if report_evidence_active:
            expected_state = Share.RestrictionState.REPORT_TAKEDOWN
        elif reject_evidence_active or row['status'] == Share.Status.REJECTED:
            expected_state = Share.RestrictionState.REVIEW_REJECTED
        else:
            expected_state = Share.RestrictionState.CLEAR

        if expected_state == Share.RestrictionState.REPORT_TAKEDOWN:
            if state != expected_state:
                resolved_report_without_takedown.append(share_id)
            approve_after_keep = (
                row['latest_approve_at'] is not None
                and row['latest_approve_at'] > latest_keep_at
            )
            confirmation_after_approval = _is_after(confirm_key, approve_key)
            if (
                state == Share.RestrictionState.REPORT_TAKEDOWN
                and approve_after_keep
                and not confirmation_after_approval
            ):
                ambiguous_report_approvals.append(share_id)
        elif expected_state == Share.RestrictionState.REVIEW_REJECTED:
            if state == Share.RestrictionState.CLEAR:
                active_reject_without_restriction.append(share_id)
                if row['status'] == Share.Status.REJECTED:
                    rejected_without_restriction.append(share_id)
            elif state != Share.RestrictionState.REVIEW_REJECTED:
                active_reject_wrong_restriction.append(share_id)
        elif state in {
            Share.RestrictionState.REPORT_TAKEDOWN,
            Share.RestrictionState.REVIEW_REJECTED,
        }:
            restriction_without_active_evidence.append(share_id)

        if state == Share.RestrictionState.LEGACY_PRIVATE:
            if row['visibility'] != Share.Visibility.PRIVATE:
                legacy_private_on_non_private.append(share_id)
            elif expected_state == Share.RestrictionState.CLEAR:
                legacy_private_reviews.append(share_id)
        elif (
            state == Share.RestrictionState.CLEAR
            and row['visibility'] == Share.Visibility.PRIVATE
            and release_key is None
        ):
            private_clear_without_classification.append(share_id)

        reason = row['restriction_reason'] or ''
        clear_metadata_valid = (
            not reason
            and row['restricted_at'] is None
            and row['restricted_by_id'] is None
        )
        active_metadata_valid = bool(reason.strip()) and row['restricted_at'] is not None
        if (
            state == Share.RestrictionState.CLEAR
            and not clear_metadata_valid
        ) or (
            state != Share.RestrictionState.CLEAR
            and not active_metadata_valid
        ):
            invalid_restriction_metadata.append(share_id)

    known_share_statuses = {value for value, _ in Share.Status.choices}
    known_visibilities = {value for value, _ in Share.Visibility.choices}
    known_restrictions = {value for value, _ in Share.RestrictionState.choices}
    known_report_statuses = {value for value, _ in Report.Status.choices}
    known_log_actions = {value for value, _ in ShareLog.ActionType.choices}
    invalid_enum_counts = {
        'share_status': Share.objects.exclude(status__in=known_share_statuses).count(),
        'share_visibility': Share.objects.exclude(
            visibility__in=known_visibilities,
        ).count(),
        'share_restriction_state': Share.objects.exclude(
            restriction_state__in=known_restrictions,
        ).count(),
        'report_status': Report.objects.exclude(status__in=known_report_statuses).count(),
        'share_log_action': ShareLog.objects.exclude(action__in=known_log_actions).count(),
    }

    blocking_errors = []
    _append_check(
        blocking_errors,
        check='resolved_report_without_takedown',
        share_ids=resolved_report_without_takedown,
    )
    _append_check(
        blocking_errors,
        check='active_reject_log_without_restriction',
        share_ids=active_reject_without_restriction,
    )
    _append_check(
        blocking_errors,
        check='active_reject_evidence_wrong_restriction',
        share_ids=active_reject_wrong_restriction,
    )
    _append_check(
        blocking_errors,
        check='rejected_share_without_restriction',
        share_ids=rejected_without_restriction,
    )
    _append_check(
        blocking_errors,
        check='restriction_without_active_evidence',
        share_ids=restriction_without_active_evidence,
    )
    _append_check(
        blocking_errors,
        check='private_clear_without_classification',
        share_ids=private_clear_without_classification,
    )
    _append_check(
        blocking_errors,
        check='legacy_private_on_non_private',
        share_ids=legacy_private_on_non_private,
    )
    _append_check(
        blocking_errors,
        check='invalid_restriction_metadata',
        share_ids=invalid_restriction_metadata,
    )
    for key, count in invalid_enum_counts.items():
        if count:
            blocking_errors.append({
                'check': f'invalid_{key}',
                'count': count,
            })

    manual_categories = []
    _append_check(
        manual_categories,
        check='report_approval_after_takedown',
        share_ids=ambiguous_report_approvals,
    )
    _append_check(
        manual_categories,
        check='legacy_private_requires_classification',
        share_ids=legacy_private_reviews,
    )
    manual_share_ids = sorted({
        share_id
        for category in manual_categories
        for share_id in category['share_ids']
    })

    status_visibility = list(
        Share.objects.values('status', 'visibility')
        .annotate(count=Count('pk'))
        .order_by('status', 'visibility')
    )
    restriction_states = {
        row['restriction_state']: row['count']
        for row in Share.objects.values('restriction_state')
        .annotate(count=Count('pk'))
        .order_by('restriction_state')
    }
    return {
        'generated_at': timezone.now().isoformat(),
        'valid': not blocking_errors,
        'ready_for_cutover': not blocking_errors and not manual_share_ids,
        'counts': {
            'shares': Share.objects.count(),
            'reports': Report.objects.count(),
            'resolved_report_shares': evidence.filter(
                latest_resolved_at__isnull=False,
            ).count(),
            'private_clear_shares': Share.objects.filter(
                visibility=Share.Visibility.PRIVATE,
                restriction_state=Share.RestrictionState.CLEAR,
            ).count(),
            'legacy_private_reviews': len(legacy_private_reviews),
            'active_restrictions_missing_actor': Share.objects.exclude(
                restriction_state=Share.RestrictionState.CLEAR,
            ).filter(restricted_by__isnull=True).count(),
        },
        'status_visibility': status_visibility,
        'restriction_states': restriction_states,
        'blocking_errors': blocking_errors,
        'manual_review': {
            'count': len(manual_share_ids),
            'share_ids': manual_share_ids,
            'categories': manual_categories,
            'reason': (
                '需要管理员明确确认维持或解除限制，并留下对应审计日志。'
            ),
        },
    }
