from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from shares.forms import (
    AdminReviewRejectForm,
    ReportForm,
    ReportResolutionForm,
    RestrictionConfirmationForm,
    RestrictionReleaseForm,
)
from shares.models import Report, Share, ShareLog
from shares.policies import can_view_share, is_moderator
from shares.rate_limits import consume_rate_limit, request_identity
from shares.selectors import admin_task_counts
from shares.services.moderation import (
    approve_share,
    confirm_share_restriction,
    reject_share,
    release_share_restriction,
    resolve_report,
    resolve_share_reports,
)


def _admin_context(**context):
    context.update(admin_task_counts())
    return context


@user_passes_test(is_moderator)
def admin_review_list(request):
    pending = Share.objects.filter(
        Q(status=Share.Status.PENDING)
        | ~Q(restriction_state=Share.RestrictionState.CLEAR)
    ).select_related(
        'author',
        'author__profile',
        'restricted_by',
    ).prefetch_related(
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
        confirmation_form=RestrictionConfirmationForm(),
        release_form=RestrictionReleaseForm(),
    ))


@user_passes_test(is_moderator)
@require_POST
def admin_approve_share(request, share_id):
    try:
        result = approve_share(share_id=share_id, moderator=request.user)
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if not result.changed:
        messages.warning(request, f'分享 "{result.share.title}" 已处理，无需重复审核')
        return redirect('admin_review_list')
    if result.restriction_released:
        messages.success(request, f'分享 "{result.share.title}" 已通过审核并解除限制')
    else:
        messages.success(request, f'分享 "{result.share.title}" 已通过审核')
    return redirect('admin_review_list')


@user_passes_test(is_moderator)
@require_POST
def admin_reject_share(request, share_id):
    form = AdminReviewRejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, '拒绝原因不能为空')
        return redirect('admin_review_list')
    reason = form.cleaned_data['reason'].strip()
    try:
        result = reject_share(
            share_id=share_id,
            moderator=request.user,
            reason=reason,
        )
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if not result.changed:
        messages.warning(request, f'分享 "{result.share.title}" 已处理，无需重复审核')
        return redirect('admin_review_list')
    messages.warning(request, f'分享 "{result.share.title}" 已被拒绝并限制访问')
    return redirect('admin_review_list')


@user_passes_test(is_moderator)
@require_POST
def admin_release_share_restriction(request, share_id):
    form = RestrictionReleaseForm(request.POST)
    if not form.is_valid():
        messages.error(request, '解除说明不能为空')
        return redirect('admin_review_list')
    try:
        result = release_share_restriction(
            share_id=share_id,
            moderator=request.user,
            reason=form.cleaned_data['reason'].strip(),
        )
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if result.outcome == 'already_clear':
        messages.info(request, f'分享 "{result.share.title}" 当前没有活动限制')
    elif result.outcome == 'requires_review':
        messages.warning(request, '待审核或已拒绝的分享必须通过审核流程解除限制')
    else:
        messages.success(request, f'分享 "{result.share.title}" 的内容限制已解除')
    return redirect('admin_review_list')


@user_passes_test(is_moderator)
@require_POST
def admin_confirm_share_restriction(request, share_id):
    form = RestrictionConfirmationForm(request.POST)
    if not form.is_valid():
        messages.error(request, '确认说明不能为空')
        return redirect('admin_review_list')
    try:
        result = confirm_share_restriction(
            share_id=share_id,
            moderator=request.user,
            reason=form.cleaned_data['reason'].strip(),
        )
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if result.outcome == 'already_clear':
        messages.info(request, f'分享 "{result.share.title}" 当前没有活动限制')
    elif result.outcome == 'requires_review':
        messages.warning(request, '审核拒绝限制必须通过重新审核流程处理')
    else:
        messages.success(request, f'已确认继续限制分享 "{result.share.title}"')
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
    try:
        result = resolve_report(
            report_id=report_id,
            action=action,
            moderator=request.user,
            reason=reason,
        )
    except (Report.DoesNotExist, Share.DoesNotExist) as exc:
        raise Http404('Report not found') from exc
    if not result.changed:
        messages.warning(request, '该举报已处理，无需重复操作')
        return redirect('admin_report_list')
    if action == 'resolve':
        messages.success(request, f'举报已认可，分享 "{result.share.title}" 已被限制访问')
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
    try:
        result = resolve_share_reports(
            share_id=share_id,
            action=action,
            moderator=request.user,
            reason=reason,
        )
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if not result.changed:
        messages.warning(request, '该分享没有待处理的举报')
        return redirect('admin_report_list')
    if action == 'resolve':
        messages.success(request, f'已认可举报，分享 "{result.share.title}" 已被限制访问。')
    else:
        messages.info(request, '举报已全部驳回')
    return redirect('admin_report_list')


@user_passes_test(is_moderator)
def admin_review_logs(request):
    logs = Paginator(
        ShareLog.objects.filter(action__in=[
            ShareLog.ActionType.REVIEW_APPROVE,
            ShareLog.ActionType.REVIEW_REJECT,
            ShareLog.ActionType.RESTRICTION_CONFIRM,
            ShareLog.ActionType.RESTRICTION_RELEASE,
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
