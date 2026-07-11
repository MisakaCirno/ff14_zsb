from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from shares.forms import AdminReviewRejectForm, ReportForm, ReportResolutionForm
from shares.models import Report, Share, ShareLog, SiteMessage
from shares.policies import can_view_share, is_moderator
from shares.rate_limits import consume_rate_limit, request_identity
from shares.selectors import admin_task_counts
from shares.services.audit import log_share_action
from shares.services.messages import send_site_message


def _admin_context(**context):
    context.update(admin_task_counts())
    return context


@user_passes_test(is_moderator)
def admin_review_list(request):
    pending = Share.objects.filter(status=Share.Status.PENDING).prefetch_related(
        Prefetch(
            'logs',
            queryset=ShareLog.objects.select_related('user').order_by('-created_at'),
            to_attr='share_logs',
        )
    ).order_by('-created_at')
    shares = Paginator(pending, 20).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_review_list.html', _admin_context(
        shares=shares,
        reject_form=AdminReviewRejectForm(),
    ))


@user_passes_test(is_moderator)
@require_POST
def admin_approve_share(request, share_id):
    with transaction.atomic():
        share = get_object_or_404(Share.objects.select_for_update(), share_id=share_id)
        if share.status != Share.Status.PENDING:
            messages.warning(request, f'分享 "{share.title}" 已处理，无需重复审核')
            return redirect('admin_review_list')
        share.status = Share.Status.APPROVED
        share.review_feedback = ''
        share.reviewed_at = timezone.now()
        share.reviewed_by = request.user
        share.save(update_fields=[
            'status', 'review_feedback', 'reviewed_at', 'reviewed_by', 'updated_at',
        ])
        log_share_action(
            request.user, share, ShareLog.ActionType.REVIEW_APPROVE, '管理通过审核',
        )
    messages.success(request, f'分享 "{share.title}" 已通过审核')
    return redirect('admin_review_list')


@user_passes_test(is_moderator)
@require_POST
def admin_reject_share(request, share_id):
    form = AdminReviewRejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, '拒绝原因不能为空')
        return redirect('admin_review_list')
    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        share = get_object_or_404(Share.objects.select_for_update(), share_id=share_id)
        if share.status != Share.Status.PENDING:
            messages.warning(request, f'分享 "{share.title}" 已处理，无需重复审核')
            return redirect('admin_review_list')
        share.status = Share.Status.REJECTED
        share.visibility = Share.Visibility.PRIVATE
        share.review_feedback = reason
        share.reviewed_at = timezone.now()
        share.reviewed_by = request.user
        share.save(update_fields=[
            'status', 'visibility', 'review_feedback', 'reviewed_at',
            'reviewed_by', 'updated_at',
        ])
        log_share_action(
            request.user,
            share,
            ShareLog.ActionType.REVIEW_REJECT,
            f'管理员拒绝审核并设为私有。原因：{reason}',
        )
        if share.author:
            send_site_message(
                recipient=share.author,
                sender=request.user,
                message_type=SiteMessage.MessageType.SHARE_REJECTED,
                title=f'分享「{share.title}」审核未通过',
                content=f'你的分享「{share.title}」审核未通过。\n\n原因：{reason}\n\n你可以修改后重新提交审核。',
                related_share=share,
                metadata={'action_url': share.get_absolute_url()},
            )
    messages.warning(request, f'分享 "{share.title}" 已被拒绝并设为私有')
    return redirect('admin_review_list')


@login_required
def report_share(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        raise Http404('Share not found')
    if request.method == 'POST':
        form = ReportForm(request.POST)
        rate_limit = consume_rate_limit('report_user', request_identity(request))
        if not rate_limit.allowed:
            messages.error(request, '举报请求过于频繁，请稍后再试。')
            return render(request, 'shares/report_share.html', {
                'form': form, 'share': share,
            }, status=429)
        if form.is_valid():
            if Report.objects.filter(
                share=share,
                reporter=request.user,
                status=Report.Status.PENDING,
            ).exists():
                messages.warning(request, '你已经提交过待处理的举报，请等待管理员处理。')
                return redirect('share_detail', share_id=share_id)
            report = form.save(commit=False)
            report.share = share
            report.reporter = request.user
            try:
                with transaction.atomic():
                    report.save()
            except IntegrityError:
                if Report.objects.filter(
                    share=share,
                    reporter=request.user,
                    status=Report.Status.PENDING,
                ).exists():
                    messages.warning(request, '你已经提交过待处理的举报，请等待管理员处理。')
                    return redirect('share_detail', share_id=share_id)
                raise
            messages.success(request, '举报已提交，管理员将尽快处理。')
            return redirect('share_detail', share_id=share_id)
    else:
        form = ReportForm()
    return render(request, 'shares/report_share.html', {'form': form, 'share': share})


@user_passes_test(is_moderator)
def admin_report_list(request):
    reported = Share.objects.annotate(
        pending_count=Count(
            'reports', filter=Q(reports__status=Report.Status.PENDING),
        )
    ).filter(pending_count__gt=0).prefetch_related(
        Prefetch(
            'reports',
            queryset=Report.objects.filter(
                status=Report.Status.PENDING,
            ).select_related('reporter'),
            to_attr='pending_reports',
        ),
        Prefetch(
            'logs',
            queryset=ShareLog.objects.select_related('user').order_by('-created_at'),
            to_attr='share_logs',
        ),
    ).order_by('-pending_count', '-updated_at')
    shares = Paginator(reported, 10).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_report_list.html', _admin_context(
        shares=shares,
        resolution_form=ReportResolutionForm(),
    ))


def _notify_reporter(report, moderator, share, action, reason):
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
            if resolved else f'你对分享「{share.title}」的举报未被采纳。\n\n处理说明：{reason}'
        ),
        related_share=share,
        related_report=report,
        metadata={'action_url': share.get_absolute_url()},
    )


def _notify_author_takedown(share, moderator, reason, report=None):
    if not share.author:
        return
    send_site_message(
        recipient=share.author,
        sender=moderator,
        message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
        title=f'分享「{share.title}」已被设为私有',
        content=f'你的分享「{share.title}」因举报处理被设为私有。\n\n处理说明：{reason}',
        related_share=share,
        related_report=report,
        metadata={'action_url': share.get_absolute_url()},
    )


@user_passes_test(is_moderator)
@require_POST
def admin_resolve_report(request, report_id, action):
    if action not in {'resolve', 'dismiss'}:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')
    form = ReportResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, '处理说明不能为空')
        return redirect('admin_report_list')
    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        report = get_object_or_404(
            Report.objects.select_for_update().select_related('reporter'), id=report_id,
        )
        if report.status != Report.Status.PENDING:
            messages.warning(request, '该举报已处理，无需重复操作')
            return redirect('admin_report_list')
        share = Share.objects.select_for_update().get(pk=report.share_id)
        resolved_at = timezone.now()
        if action == 'resolve':
            report.status = Report.Status.RESOLVED
            share.visibility = Share.Visibility.PRIVATE
            share.save(update_fields=['visibility', 'updated_at'])
            details = f'认可举报 ID:{report_id}，设为私有。说明：{reason}'
        else:
            report.status = Report.Status.DISMISSED
            details = f'驳回举报 ID:{report_id}。说明：{reason}'
        log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, details)
        _notify_reporter(report, request.user, share, action, reason)
        if action == 'resolve':
            _notify_author_takedown(share, request.user, reason, report)
        report.resolved_at = resolved_at
        report.resolved_by = request.user
        report.resolution_reason = reason
        report.save(update_fields=['status', 'resolved_at', 'resolved_by', 'resolution_reason'])
    if action == 'resolve':
        messages.success(request, f'举报已认可，分享 "{share.title}" 已被设为私有')
    else:
        messages.info(request, '举报已驳回')
    return redirect('admin_report_list')


@user_passes_test(is_moderator)
@require_POST
def admin_resolve_share_reports(request, share_id, action):
    if action not in {'resolve', 'dismiss'}:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')
    form = ReportResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, '处理说明不能为空')
        return redirect('admin_report_list')
    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        share = get_object_or_404(Share.objects.select_for_update(), share_id=share_id)
        reports = list(
            Report.objects.select_for_update()
            .filter(share=share, status=Report.Status.PENDING)
            .select_related('reporter')
        )
        if not reports:
            messages.warning(request, '该分享没有待处理的举报')
            return redirect('admin_report_list')
        target_status = Report.Status.RESOLVED if action == 'resolve' else Report.Status.DISMISSED
        Report.objects.filter(id__in=[report.id for report in reports]).update(
            status=target_status,
            resolved_at=timezone.now(),
            resolved_by=request.user,
            resolution_reason=reason,
        )
        if action == 'resolve':
            share.visibility = Share.Visibility.PRIVATE
            share.save(update_fields=['visibility', 'updated_at'])
            details = f'批量认可所有举报，设为私有。说明：{reason}'
        else:
            details = f'批量驳回所有举报。说明：{reason}'
        log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, details)
        for report in reports:
            _notify_reporter(report, request.user, share, action, reason)
        if action == 'resolve':
            _notify_author_takedown(share, request.user, reason)
    if action == 'resolve':
        messages.success(request, f'已认可举报，分享 "{share.title}" 已设为私有，相关举报已标记为处理。')
    else:
        messages.info(request, '举报已全部驳回')
    return redirect('admin_report_list')


@user_passes_test(is_moderator)
def admin_review_logs(request):
    logs = Paginator(
        ShareLog.objects.filter(action__in=[
            ShareLog.ActionType.REVIEW_APPROVE,
            ShareLog.ActionType.REVIEW_REJECT,
        ]).select_related('user', 'share').order_by('-created_at'),
        20,
    ).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_review_logs.html', _admin_context(logs=logs))


@user_passes_test(is_moderator)
def admin_report_logs(request):
    logs = Paginator(
        ShareLog.objects.filter(
            action=ShareLog.ActionType.REPORT_HANDLE,
        ).select_related('user', 'share').order_by('-created_at'),
        20,
    ).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_report_logs.html', _admin_context(logs=logs))
