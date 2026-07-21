from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from shares.models import Report, Share, ShareLog, SiteMessage
from shares.services.audit import log_share_action
from shares.services.messages import send_site_message


@dataclass(frozen=True, slots=True)
class ShareModerationResult:
    share: Share
    outcome: str
    restriction_released: bool = False

    @property
    def changed(self):
        return self.outcome == 'changed'


@dataclass(frozen=True, slots=True)
class ReportModerationResult:
    share: Share
    reports: tuple[Report, ...]
    outcome: str

    @property
    def changed(self):
        return self.outcome == 'changed'


def _required_reason(reason):
    cleaned = reason.strip() if isinstance(reason, str) else ''
    if not cleaned:
        raise ValueError('A non-blank moderation reason is required.')
    return cleaned


def _clear_restriction(share):
    previous = (
        share.restriction_state,
        share.restriction_reason,
    )
    share.restriction_state = Share.RestrictionState.CLEAR
    share.restriction_reason = ''
    share.restricted_at = None
    share.restricted_by = None
    return previous


def _apply_restriction(share, *, state, reason, moderator, restricted_at):
    share.restriction_state = state
    share.restriction_reason = reason
    share.restricted_at = restricted_at
    share.restricted_by = moderator


def _restriction_update_fields():
    return [
        'restriction_state',
        'restriction_reason',
        'restricted_at',
        'restricted_by',
    ]


def _log_restriction_release(*, moderator, share, previous, reason):
    previous_state, previous_reason = previous
    log_share_action(
        moderator,
        share,
        ShareLog.ActionType.RESTRICTION_RELEASE,
        (
            f'解除 {previous_state} 限制。解除说明：{reason}。'
            f'原限制原因：{previous_reason}'
        ),
    )


def _log_restriction_confirmation(*, moderator, share, previous, reason):
    previous_state, previous_reason = previous
    log_share_action(
        moderator,
        share,
        ShareLog.ActionType.RESTRICTION_CONFIRM,
        (
            f'确认维持 {share.restriction_state} 限制。复核说明：{reason}。'
            f'确认前状态：{previous_state}；原限制原因：{previous_reason}'
        ),
    )


def _notify_author_restriction_released(*, share, moderator, reason):
    if not share.author:
        return
    send_site_message(
        recipient=share.author,
        sender=moderator,
        message_type=SiteMessage.MessageType.SHARE_RESTORED,
        title=f'分享「{share.title}」的内容限制已解除',
        content=(
            f'你的分享「{share.title}」已通过管理员复核，内容限制已解除。'
            f'\n\n处理说明：{reason}'
        ),
        related_share=share,
        metadata={'action_url': share.get_absolute_url()},
    )


def _notify_author_moderator_takedown(*, share, moderator, reason):
    if not share.author:
        return
    send_site_message(
        recipient=share.author,
        sender=moderator,
        message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        title=f'分享「{share.title}」已由管理员下架',
        content=(
            f'你的分享「{share.title}」已由管理员主动下架，当前仅你和管理员可以访问。'
            f'\n\n管理员说明：{reason}'
            '\n\n你可以根据说明修改内容并重新提交审核；编辑本身不会立即解除限制。'
        ),
        related_share=share,
        metadata={'action_url': share.get_absolute_url()},
    )


@transaction.atomic
def takedown_share(*, share_id, moderator, reason):
    """Restrict an already-published share without rewriting its review result."""
    reason = _required_reason(reason)
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if share.is_restricted:
        return ShareModerationResult(share=share, outcome='already_restricted')
    if share.status != Share.Status.APPROVED:
        return ShareModerationResult(share=share, outcome='requires_review')

    _apply_restriction(
        share,
        state=Share.RestrictionState.MODERATOR_TAKEDOWN,
        reason=reason,
        moderator=moderator,
        restricted_at=timezone.now(),
    )
    share.save(update_fields=[*_restriction_update_fields(), 'updated_at'])
    log_share_action(
        moderator,
        share,
        ShareLog.ActionType.MODERATOR_TAKEDOWN,
        f'管理员主动下架分享。说明：{reason}',
    )
    _notify_author_moderator_takedown(
        share=share,
        moderator=moderator,
        reason=reason,
    )
    return ShareModerationResult(share=share, outcome='changed')


@transaction.atomic
def approve_share(*, share_id, moderator):
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if share.status != Share.Status.PENDING:
        return ShareModerationResult(share=share, outcome='already_processed')

    released = share.is_restricted
    previous_restriction = None
    share.status = Share.Status.APPROVED
    share.review_feedback = ''
    share.reviewed_at = timezone.now()
    share.reviewed_by = moderator
    update_fields = [
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by',
        'updated_at',
    ]
    if released:
        previous_restriction = _clear_restriction(share)
        update_fields.extend(_restriction_update_fields())
    share.save(update_fields=update_fields)

    log_share_action(
        moderator,
        share,
        ShareLog.ActionType.REVIEW_APPROVE,
        '管理员通过审核并解除活动限制' if released else '管理员通过审核',
    )
    if released:
        release_reason = '内容修改后复审通过'
        _log_restriction_release(
            moderator=moderator,
            share=share,
            previous=previous_restriction,
            reason=release_reason,
        )
        _notify_author_restriction_released(
            share=share,
            moderator=moderator,
            reason=release_reason,
        )
    return ShareModerationResult(
        share=share,
        outcome='changed',
        restriction_released=released,
    )


@transaction.atomic
def reject_share(*, share_id, moderator, reason):
    reason = _required_reason(reason)
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if share.status != Share.Status.PENDING:
        return ShareModerationResult(share=share, outcome='already_processed')

    reviewed_at = timezone.now()
    share.status = Share.Status.REJECTED
    share.review_feedback = reason
    share.reviewed_at = reviewed_at
    share.reviewed_by = moderator
    update_fields = [
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by',
        'updated_at',
    ]
    if share.restriction_state not in {
        Share.RestrictionState.REPORT_TAKEDOWN,
        Share.RestrictionState.MODERATOR_TAKEDOWN,
    }:
        _apply_restriction(
            share,
            state=Share.RestrictionState.REVIEW_REJECTED,
            reason=reason,
            moderator=moderator,
            restricted_at=reviewed_at,
        )
        update_fields.extend(_restriction_update_fields())
    share.save(update_fields=update_fields)

    log_share_action(
        moderator,
        share,
        ShareLog.ActionType.REVIEW_REJECT,
        f'管理员拒绝审核并保留内容限制。原因：{reason}',
    )
    if share.author:
        send_site_message(
            recipient=share.author,
            sender=moderator,
            message_type=SiteMessage.MessageType.SHARE_REJECTED,
            title=f'分享「{share.title}」审核未通过',
            content=(
                f'你的分享「{share.title}」审核未通过。\n\n原因：{reason}'
                '\n\n你可以修改后重新提交审核；管理员通过前，其他用户无法访问。'
            ),
            related_share=share,
            metadata={'action_url': share.get_absolute_url()},
        )
    return ShareModerationResult(share=share, outcome='changed')


@transaction.atomic
def release_share_restriction(*, share_id, moderator, reason):
    reason = _required_reason(reason)
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if not share.is_restricted:
        return ShareModerationResult(share=share, outcome='already_clear')
    if share.restriction_state == Share.RestrictionState.LEGACY_PRIVATE:
        may_classify_legacy_private = (
            share.visibility == Share.Visibility.PRIVATE
            and share.status != Share.Status.REJECTED
        )
        if not may_classify_legacy_private:
            return ShareModerationResult(share=share, outcome='requires_review')
    elif share.status != Share.Status.APPROVED:
        return ShareModerationResult(share=share, outcome='requires_review')

    previous = _clear_restriction(share)
    share.save(update_fields=[*_restriction_update_fields(), 'updated_at'])
    _log_restriction_release(
        moderator=moderator,
        share=share,
        previous=previous,
        reason=reason,
    )
    _notify_author_restriction_released(
        share=share,
        moderator=moderator,
        reason=reason,
    )
    return ShareModerationResult(
        share=share,
        outcome='changed',
        restriction_released=True,
    )


@transaction.atomic
def confirm_share_restriction(
    *,
    share_id,
    moderator,
    reason,
    expected_version,
):
    reason = _required_reason(reason)
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if share.updated_at != expected_version:
        return ShareModerationResult(share=share, outcome='stale')
    if not share.is_restricted:
        return ShareModerationResult(share=share, outcome='already_clear')
    if share.restriction_state not in {
        Share.RestrictionState.REPORT_TAKEDOWN,
        Share.RestrictionState.LEGACY_PRIVATE,
    }:
        return ShareModerationResult(share=share, outcome='requires_review')

    previous = (
        share.restriction_state,
        share.restriction_reason,
    )
    _apply_restriction(
        share,
        state=Share.RestrictionState.REPORT_TAKEDOWN,
        reason=reason,
        moderator=moderator,
        restricted_at=timezone.now(),
    )
    share.save(update_fields=[*_restriction_update_fields(), 'updated_at'])
    _log_restriction_confirmation(
        moderator=moderator,
        share=share,
        previous=previous,
        reason=reason,
    )
    _notify_author_takedown(
        share=share,
        moderator=moderator,
        reason=reason,
    )
    return ShareModerationResult(share=share, outcome='changed')


def _notify_reporter(*, report, moderator, share, action, reason):
    if not report.reporter:
        return
    resolved = action == 'resolve'
    send_site_message(
        recipient=report.reporter,
        sender=moderator,
        message_type=(
            SiteMessage.MessageType.REPORT_RESOLVED
            if resolved else SiteMessage.MessageType.REPORT_DISMISSED
        ),
        title=(
            f'你对「{share.title}」的举报已处理'
            if resolved else f'你对「{share.title}」的举报未被采纳'
        ),
        content=(
            f'你对分享「{share.title}」的举报已处理，感谢反馈。\n\n处理说明：{reason}'
            if resolved else
            f'你对分享「{share.title}」的举报未被采纳。\n\n处理说明：{reason}'
        ),
        related_share=share,
        related_report=report,
        metadata={'action_url': share.get_absolute_url()},
    )


def _notify_author_takedown(*, share, moderator, reason, report=None):
    if not share.author:
        return
    send_site_message(
        recipient=share.author,
        sender=moderator,
        message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        title=f'分享「{share.title}」已被下架',
        content=(
            f'你的分享「{share.title}」因举报处理被限制访问。'
            f'\n\n处理说明：{reason}\n\n编辑内容不会自动解除限制，请等待管理员复核。'
        ),
        related_share=share,
        related_report=report,
        metadata={'action_url': share.get_absolute_url()},
    )


def _finish_report(*, report, status, moderator, reason, resolved_at):
    report.status = status
    report.resolved_at = resolved_at
    report.resolved_by = moderator
    report.resolution_reason = reason
    report.save(update_fields=[
        'status',
        'resolved_at',
        'resolved_by',
        'resolution_reason',
    ])


@transaction.atomic
def resolve_report(*, report_id, action, moderator, reason):
    if action not in {'resolve', 'dismiss'}:
        raise ValueError(f'Unsupported report action: {action}')
    reason = _required_reason(reason)
    share_id = Report.objects.values_list('share_id', flat=True).get(pk=report_id)
    share = Share.objects.select_for_update().select_related('author').get(
        pk=share_id,
        deleted_at__isnull=True,
    )
    report = Report.objects.select_for_update().select_related('reporter').get(
        pk=report_id,
        share_id=share.pk,
    )
    if report.status != Report.Status.PENDING:
        return ReportModerationResult(
            share=share,
            reports=(report,),
            outcome='already_processed',
        )

    resolved_at = timezone.now()
    if action == 'resolve':
        _apply_restriction(
            share,
            state=Share.RestrictionState.REPORT_TAKEDOWN,
            reason=reason,
            moderator=moderator,
            restricted_at=resolved_at,
        )
        share.save(update_fields=[*_restriction_update_fields(), 'updated_at'])
        report_status = Report.Status.RESOLVED
        details = f'认可举报 ID:{report_id}，施加举报下架限制。说明：{reason}'
    else:
        report_status = Report.Status.DISMISSED
        details = f'驳回举报 ID:{report_id}。说明：{reason}'

    _finish_report(
        report=report,
        status=report_status,
        moderator=moderator,
        reason=reason,
        resolved_at=resolved_at,
    )
    log_share_action(moderator, share, ShareLog.ActionType.REPORT_HANDLE, details)
    _notify_reporter(
        report=report,
        moderator=moderator,
        share=share,
        action=action,
        reason=reason,
    )
    if action == 'resolve':
        _notify_author_takedown(
            share=share,
            moderator=moderator,
            reason=reason,
            report=report,
        )
    return ReportModerationResult(
        share=share,
        reports=(report,),
        outcome='changed',
    )


@transaction.atomic
def resolve_share_reports(*, share_id, action, moderator, reason):
    if action not in {'resolve', 'dismiss'}:
        raise ValueError(f'Unsupported report action: {action}')
    reason = _required_reason(reason)
    share = Share.objects.select_for_update().select_related('author').get(
        share_id=share_id,
        deleted_at__isnull=True,
    )
    reports = tuple(
        Report.objects.select_for_update()
        .filter(share=share, status=Report.Status.PENDING)
        .select_related('reporter')
        .order_by('pk')
    )
    if not reports:
        return ReportModerationResult(
            share=share,
            reports=(),
            outcome='already_processed',
        )

    resolved_at = timezone.now()
    report_status = (
        Report.Status.RESOLVED
        if action == 'resolve' else Report.Status.DISMISSED
    )
    Report.objects.filter(pk__in=[report.pk for report in reports]).update(
        status=report_status,
        resolved_at=resolved_at,
        resolved_by=moderator,
        resolution_reason=reason,
    )
    if action == 'resolve':
        _apply_restriction(
            share,
            state=Share.RestrictionState.REPORT_TAKEDOWN,
            reason=reason,
            moderator=moderator,
            restricted_at=resolved_at,
        )
        share.save(update_fields=[*_restriction_update_fields(), 'updated_at'])
        details = f'批量认可所有举报，施加举报下架限制。说明：{reason}'
    else:
        details = f'批量驳回所有举报。说明：{reason}'

    log_share_action(moderator, share, ShareLog.ActionType.REPORT_HANDLE, details)
    for report in reports:
        report.status = report_status
        report.resolved_at = resolved_at
        report.resolved_by = moderator
        report.resolution_reason = reason
        _notify_reporter(
            report=report,
            moderator=moderator,
            share=share,
            action=action,
            reason=reason,
        )
    if action == 'resolve':
        _notify_author_takedown(
            share=share,
            moderator=moderator,
            reason=reason,
        )
    return ReportModerationResult(
        share=share,
        reports=reports,
        outcome='changed',
    )
