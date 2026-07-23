from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Coalesce, Length, Substr
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from shares.forms import (
    AdminReviewRejectForm,
    ModeratorTakedownForm,
    ReportForm,
    ReportResolutionForm,
    RestrictionConfirmationForm,
    RestrictionReleaseForm,
)
from shares.models import Report, Share, ShareLog
from shares.policies import can_view_share, is_moderator
from shares.rate_limits import consume_rate_limit, request_identity
from shares.services.moderation import (
    approve_share,
    confirm_share_restriction,
    reject_share,
    release_share_restriction,
    resolve_report,
    resolve_share_reports,
    takedown_share,
)


_QUEUE_LOG_PREVIEW_SIZE = 5
_QUEUE_REPORT_PREVIEW_SIZE = 5
_QUEUE_TEXT_PREVIEW_LENGTH = 500
_QUEUE_LOG_TEXT_PREVIEW_LENGTH = 300
_QUEUE_REPORT_TEXT_PREVIEW_LENGTH = 160
_AUDIT_TEXT_PREVIEW_LENGTH = 2000


def _admin_context(**context):
    return context


def _log_preview_queryset(*, text_length=_QUEUE_TEXT_PREVIEW_LENGTH):
    return ShareLog.objects.select_related('user').annotate(
        details_preview=Substr('details', 1, text_length),
        details_length=Length('details'),
    ).defer('details').order_by('-created_at', '-pk')


def _queue_log_prefetch():
    return Prefetch(
        'logs',
        queryset=_log_preview_queryset(
            text_length=_QUEUE_LOG_TEXT_PREVIEW_LENGTH,
        )[:_QUEUE_LOG_PREVIEW_SIZE + 1],
        to_attr='share_logs',
    )


def _queue_share_queryset(queryset=None, *, include_strategy_code=False):
    queryset = queryset if queryset is not None else Share.objects.all()
    queryset = queryset.filter(deleted_at__isnull=True)
    deferred_fields = [
        'description',
        'review_feedback',
        'restriction_reason',
    ]
    if not include_strategy_code:
        deferred_fields.append('strategy_code')
    return queryset.select_related(
        'author',
        'author__profile',
        'restricted_by',
    ).annotate(
        description_preview=Substr(
            'description',
            1,
            _QUEUE_TEXT_PREVIEW_LENGTH,
        ),
        restriction_reason_preview=Substr(
            'restriction_reason',
            1,
            _QUEUE_TEXT_PREVIEW_LENGTH,
        ),
        review_feedback_preview=Substr(
            'review_feedback',
            1,
            _QUEUE_TEXT_PREVIEW_LENGTH,
        ),
    ).defer(*deferred_fields).prefetch_related(_queue_log_prefetch())


def _normalize_log_preview(share):
    logs = list(getattr(share, 'share_logs', ()))
    share.share_logs_truncated = len(logs) > _QUEUE_LOG_PREVIEW_SIZE
    share.share_logs = tuple(logs[:_QUEUE_LOG_PREVIEW_SIZE])


def _staff_reason_form(
    form_class,
    *,
    auto_id,
    help_id,
    error_id,
    data=None,
    initial=None,
):
    form = form_class(data=data, auto_id=auto_id, initial=initial)
    reason_attrs = form.fields['reason'].widget.attrs
    reason_attrs['aria-describedby'] = help_id
    if form.is_bound and form.errors.get('reason'):
        reason_attrs['aria-invalid'] = 'true'
        reason_attrs['aria-describedby'] = f'{help_id} {error_id}'
    return form


def _review_queryset():
    return _queue_share_queryset(
        Share.objects.filter(
            status=Share.Status.PENDING,
            deleted_at__isnull=True,
        ),
        include_strategy_code=True,
    ).order_by('-created_at', '-pk')


def _restriction_queryset():
    return _queue_share_queryset(
        Share.objects.filter(
            ~Q(restriction_state=Share.RestrictionState.CLEAR),
            ~Q(status=Share.Status.PENDING),
            deleted_at__isnull=True,
        ),
        include_strategy_code=True,
    ).order_by('-created_at', '-pk')


def _review_form_ids(action, share_id):
    prefix = {
        'reject': 'review-reject',
        'confirm': 'review-confirm',
        'release': 'review-release',
    }[action]
    return {
        'auto_id': f'{prefix}-{share_id}-%s',
        'help_id': f'{prefix}-help-{share_id}',
        'error_id': f'{prefix}-errors-{share_id}',
    }


def _review_item(
    share,
    *,
    return_page,
    invalid_action=None,
    target_outside_queue=False,
):
    return {
        'share': share,
        'confirmation_version': share.updated_at.isoformat(),
        'invalid_action': invalid_action,
        'return_page': return_page,
        'target_outside_queue': target_outside_queue,
    }


def _review_action_url(share, action):
    view_name = {
        'reject': 'admin_reject_share',
        'confirm': 'admin_confirm_share_restriction',
        'release': 'admin_release_share_restriction',
    }[action]
    return reverse(view_name, args=[share.share_id])


def _moderation_queue_name(share):
    return (
        'admin_review_list'
        if share.status == Share.Status.PENDING
        else 'admin_restriction_list'
    )


def _review_resolution_error(share, action, *, target_outside_queue):
    presentation = {
        'reject': {
            'title': '拒绝审核',
            'context_label': '',
            'context': '',
            'submit': '确认拒绝并通知用户',
            'tone': 'danger',
        },
        'confirm': {
            'title': '确认继续维持内容限制',
            'context_label': '当前原因：',
            'context': share.restriction_reason_preview,
            'submit': '确认维持并通知作者',
            'tone': 'danger',
        },
        'release': {
            'title': '解除内容限制',
            'context_label': '当前原因：',
            'context': share.restriction_reason_preview,
            'submit': '确认解除并通知作者',
            'tone': 'success',
        },
    }[action]
    return {
        'action_url': _review_action_url(share, action),
        'title': presentation['title'],
        'subject': f'分享：{share.title}',
        'context_label': presentation['context_label'],
        'context': presentation['context'],
        'submit': presentation['submit'],
        'tone': presentation['tone'],
        'target_stale': target_outside_queue,
    }


def _page_containing_review(queryset, page_number, target_share=None):
    paginator = Paginator(queryset, 20)
    page = paginator.get_page(page_number)
    if target_share is None or any(item.pk == target_share.pk for item in page):
        return page

    target_is_queued = queryset.filter(pk=target_share.pk).exists()
    if not target_is_queued:
        return page

    items_before_target = queryset.filter(
        Q(created_at__gt=target_share.created_at)
        | Q(created_at=target_share.created_at, pk__gt=target_share.pk)
    ).count()
    return paginator.get_page((items_before_target // paginator.per_page) + 1)


def _moderation_queue_context(
    *,
    queryset,
    active_tab,
    page_title,
    header_title,
    header_summary,
    count_label,
    empty_title,
    empty_message,
    pagination_aria_label,
    page_number=None,
    pagination_base_url=None,
    invalid_share=None,
    invalid_action=None,
    invalid_form=None,
):
    shares = _page_containing_review(
        queryset,
        page_number,
        invalid_share,
    )
    for share in shares:
        _normalize_log_preview(share)
    review_items = [
        _review_item(
            share,
            return_page=shares.number,
            invalid_action=(
                invalid_action
                if invalid_share is not None and share.pk == invalid_share.pk
                else None
            ),
        )
        for share in shares
    ]
    target_is_visible = any(
        item['share'].pk == invalid_share.pk
        for item in review_items
    ) if invalid_share is not None else True
    if not target_is_visible:
        _normalize_log_preview(invalid_share)
        review_items.insert(0, _review_item(
            invalid_share,
            return_page=shares.number,
            invalid_action=invalid_action,
            target_outside_queue=True,
        ))
    if invalid_form is None:
        resolution_form = _staff_reason_form(
            ReportResolutionForm,
            auto_id='review-resolution-%s',
            help_id='review-resolution-help',
            error_id='review-resolution-errors',
        )
        resolution_error = None
        resolution_error_id = 'review-resolution-errors'
        resolution_help_id = 'review-resolution-help'
        resolution_version = ''
    else:
        form_ids = _review_form_ids(invalid_action, invalid_share.share_id)
        resolution_form = invalid_form
        resolution_error = _review_resolution_error(
            invalid_share,
            invalid_action,
            target_outside_queue=not target_is_visible,
        )
        resolution_error_id = form_ids['error_id']
        resolution_help_id = form_ids['help_id']
        resolution_version = (
            invalid_form['version'].value()
            if invalid_action == 'confirm'
            else ''
        )
    return _admin_context(
        shares=shares,
        review_items=tuple(review_items),
        review_resolution_form=resolution_form,
        review_resolution_error=resolution_error,
        review_resolution_error_id=resolution_error_id,
        review_resolution_help_id=resolution_help_id,
        review_resolution_version=resolution_version,
        moderation_active_tab=active_tab,
        page_title=page_title,
        header_title=header_title,
        header_summary=header_summary,
        count_label=count_label,
        empty_title=empty_title,
        empty_message=empty_message,
        pagination_aria_label=pagination_aria_label,
        pagination_base_url=pagination_base_url,
    )


def _review_queue_context(
    *,
    page_number=None,
    pagination_base_url=None,
    invalid_share=None,
    invalid_action=None,
    invalid_form=None,
):
    return _moderation_queue_context(
        queryset=_review_queryset(),
        active_tab='admin_review_list',
        page_title='审核列表',
        header_title='待审核内容',
        header_summary=(
            '这里只显示等待管理员审核的分享。被下架的内容在作者修改后会进入这里，'
            '审核通过即解除限制，审核未通过则返回下架内容。'
        ),
        count_label='个待审核分享',
        empty_title='没有待审核内容',
        empty_message='当前没有等待管理员处理的分享。',
        pagination_aria_label='审核队列分页',
        page_number=page_number,
        pagination_base_url=(
            pagination_base_url or reverse('admin_review_list')
        ),
        invalid_share=invalid_share,
        invalid_action=invalid_action,
        invalid_form=invalid_form,
    )


def _restriction_queue_context(
    *,
    page_number=None,
    pagination_base_url=None,
    invalid_share=None,
    invalid_action=None,
    invalid_form=None,
):
    return _moderation_queue_context(
        queryset=_restriction_queryset(),
        active_tab='admin_restriction_list',
        page_title='下架内容',
        header_title='下架内容',
        header_summary=(
            '集中查看当前无法公开访问的内容。作者可以根据原因修改并重新提交审核；'
            '复审期间内容会转入审核列表。'
        ),
        count_label='个下架分享',
        empty_title='没有下架内容',
        empty_message='当前没有处于内容限制状态的分享。',
        pagination_aria_label='下架内容分页',
        page_number=page_number,
        pagination_base_url=(
            pagination_base_url or reverse('admin_restriction_list')
        ),
        invalid_share=invalid_share,
        invalid_action=invalid_action,
        invalid_form=invalid_form,
    )


def _render_review_form_error(request, *, share_id, action, form):
    share = get_object_or_404(
        _queue_share_queryset(include_strategy_code=True),
        share_id=share_id,
    )
    if action == 'confirm' and form.errors.get('version'):
        # Keep the moderator's explanation, but replace an unusable concurrency
        # token with the current server value so the corrected form can retry.
        form.data = form.data.copy()
        form.data['version'] = share.updated_at.isoformat()
    if action == 'reject':
        context_factory = _review_queue_context
        queue_url = reverse('admin_review_list')
    else:
        context_factory = _restriction_queue_context
        queue_url = reverse('admin_restriction_list')
    return render(
        request,
        'shares/admin_review_list.html',
        context_factory(
            page_number=request.POST.get('return_page'),
            pagination_base_url=queue_url,
            invalid_share=share,
            invalid_action=action,
            invalid_form=form,
        ),
        status=400,
    )


@user_passes_test(is_moderator)
def admin_review_list(request):
    return render(
        request,
        'shares/admin_review_list.html',
        _review_queue_context(page_number=request.GET.get('page')),
    )


@user_passes_test(is_moderator)
def admin_restriction_list(request):
    return render(
        request,
        'shares/admin_review_list.html',
        _restriction_queue_context(page_number=request.GET.get('page')),
    )


@user_passes_test(is_moderator)
def admin_takedown_share(request, share_id):
    share = get_object_or_404(
        Share.objects.select_related('author'),
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if request.method == 'POST':
        form = ModeratorTakedownForm(request.POST)
        if form.is_valid():
            result = takedown_share(
                share_id=share_id,
                moderator=request.user,
                reason=form.cleaned_data['reason'].strip(),
            )
            if result.outcome == 'already_restricted':
                messages.info(request, f'分享 "{result.share.title}" 已处于限制状态')
            elif result.outcome == 'requires_review':
                messages.warning(request, '待审核或已拒绝的分享应通过审核流程处理')
            else:
                messages.success(request, f'分享 "{result.share.title}" 已下架，作者已收到说明')
            return redirect('share_detail', share_id=share_id)
        response_status = 400
    else:
        if share.is_restricted:
            messages.info(request, f'分享 "{share.title}" 已处于限制状态')
            return redirect('share_detail', share_id=share_id)
        if share.status != Share.Status.APPROVED:
            messages.warning(request, '待审核或已拒绝的分享应通过审核流程处理')
            return redirect('admin_review_list')
        form = ModeratorTakedownForm()
        response_status = 200
    return render(
        request,
        'shares/admin_takedown_share.html',
        _admin_context(share=share, form=form),
        status=response_status,
    )


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
    form = _staff_reason_form(
        AdminReviewRejectForm,
        data=request.POST,
        **_review_form_ids('reject', share_id),
    )
    if not form.is_valid():
        return _render_review_form_error(
            request,
            share_id=share_id,
            action='reject',
            form=form,
        )
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
    form = _staff_reason_form(
        RestrictionReleaseForm,
        data=request.POST,
        **_review_form_ids('release', share_id),
    )
    if not form.is_valid():
        return _render_review_form_error(
            request,
            share_id=share_id,
            action='release',
            form=form,
        )
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
    return redirect(_moderation_queue_name(result.share))


@user_passes_test(is_moderator)
@require_POST
def admin_confirm_share_restriction(request, share_id):
    form = _staff_reason_form(
        RestrictionConfirmationForm,
        data=request.POST,
        **_review_form_ids('confirm', share_id),
    )
    if not form.is_valid():
        return _render_review_form_error(
            request,
            share_id=share_id,
            action='confirm',
            form=form,
        )
    try:
        result = confirm_share_restriction(
            share_id=share_id,
            moderator=request.user,
            reason=form.cleaned_data['reason'].strip(),
            expected_version=form.cleaned_data['version'],
        )
    except Share.DoesNotExist as exc:
        raise Http404('Share not found') from exc
    if result.outcome == 'stale':
        messages.info(request, '分享限制已发生变化，请刷新后重新确认')
    elif result.outcome == 'already_clear':
        messages.info(request, f'分享 "{result.share.title}" 当前没有活动限制')
    elif result.outcome == 'requires_review':
        messages.warning(request, '审核拒绝限制必须通过重新审核流程处理')
    else:
        messages.success(request, f'已确认继续限制分享 "{result.share.title}"')
    return redirect(_moderation_queue_name(result.share))


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


def _pending_report_count():
    counts = Report.objects.filter(
        share_id=OuterRef('pk'),
        status=Report.Status.PENDING,
    ).order_by().values('share_id').annotate(
        total=Count('pk'),
    ).values('total')[:1]
    return Coalesce(
        Subquery(counts, output_field=IntegerField()),
        Value(0),
        output_field=IntegerField(),
    )


def _report_preview_queryset(*, text_length=_QUEUE_TEXT_PREVIEW_LENGTH):
    return Report.objects.select_related('reporter').annotate(
        reason_preview=Substr('reason', 1, text_length),
        reason_length=Length('reason'),
    ).defer('reason').order_by('-created_at', '-pk')


def _pending_report_queryset(*, text_length=_QUEUE_TEXT_PREVIEW_LENGTH):
    return _report_preview_queryset(text_length=text_length).filter(
        status=Report.Status.PENDING,
    )


def _report_queryset():
    return _queue_share_queryset(
        Share.objects.filter(deleted_at__isnull=True).annotate(
            pending_count=_pending_report_count(),
        ).filter(pending_count__gt=0)
    ).prefetch_related(
        Prefetch(
            'reports',
            queryset=_pending_report_queryset(
                text_length=_QUEUE_REPORT_TEXT_PREVIEW_LENGTH,
            )[:_QUEUE_REPORT_PREVIEW_SIZE],
            to_attr='pending_reports',
        ),
    ).order_by('-pending_count', '-updated_at', '-pk')


def _normalize_report_preview(share):
    _normalize_log_preview(share)
    reports = tuple(getattr(share, 'pending_reports', ()))
    share.pending_reports = reports
    share.pending_reports_truncated = share.pending_count > len(reports)


def _page_containing_report(queryset, page_number, target_share=None):
    paginator = Paginator(queryset, 10)
    page = paginator.get_page(page_number)
    if target_share is None or any(item.pk == target_share.pk for item in page):
        return page

    target = queryset.filter(pk=target_share.pk).first()
    if target is None:
        return page

    items_before_target = queryset.filter(
        Q(pending_count__gt=target.pending_count)
        | Q(
            pending_count=target.pending_count,
            updated_at__gt=target.updated_at,
        )
        | Q(
            pending_count=target.pending_count,
            updated_at=target.updated_at,
            pk__gt=target.pk,
        )
    ).count()
    return paginator.get_page((items_before_target // paginator.per_page) + 1)


def _report_queue_context(
    *,
    page_number=None,
    pagination_base_url=None,
    target_share=None,
    resolution_form=None,
    resolution_error=None,
):
    shares = _page_containing_report(
        _report_queryset(),
        page_number,
        target_share,
    )
    for share in shares:
        _normalize_report_preview(share)
    if resolution_form is None:
        resolution_form = _staff_reason_form(
            ReportResolutionForm,
            auto_id='report-resolution-%s',
            help_id='report-resolution-help',
            error_id='report-resolution-errors',
        )
    return _admin_context(
        shares=shares,
        resolution_form=resolution_form,
        resolution_error=resolution_error,
        moderation_active_tab='admin_report_list',
        pagination_base_url=pagination_base_url,
    )


def _report_resolution_error(*, action, action_url, share, report=None):
    resolve = action == 'resolve'
    if report is not None:
        reporter = report.reporter.username if report.reporter else '已删除账户'
        return {
            'action_url': action_url,
            'share_id': share.share_id,
            'title': '认可单条举报' if resolve else '驳回单条举报',
            'subject': f'举报人：{reporter}',
            'context_label': '举报内容：',
            'context': report.reason_preview,
            'submit': '确认认可并通知用户' if resolve else '确认驳回并通知举报人',
            'tone': 'danger' if resolve else 'secondary',
            'target_stale': report.status != Report.Status.PENDING,
        }

    pending_count = Report.objects.filter(
        share=share,
        status=Report.Status.PENDING,
    ).count()
    if resolve:
        return {
            'action_url': action_url,
            'share_id': share.share_id,
            'title': '认可举报并更新限制' if share.is_restricted else '认可全部举报',
            'subject': f'分享：{share.title}',
            'context_label': (
                f'当前{share.get_restriction_state_display()}：'
                if share.is_restricted
                else ''
            ),
            'context': (
                share.restriction_reason_preview
                if share.is_restricted
                else ''
            ),
            'submit': '确认认可并通知用户',
            'tone': 'danger',
            'target_stale': pending_count == 0,
        }
    return {
        'action_url': action_url,
        'share_id': share.share_id,
        'title': '驳回全部举报',
        'subject': f'分享：{share.title}',
        'context_label': '待处理举报：',
        'context': f'共 {pending_count} 条，处理结果会通知所有仍存在的举报人。',
        'submit': '确认全部驳回并通知用户',
        'tone': 'secondary',
        'target_stale': pending_count == 0,
    }


def _render_report_form_error(
    request,
    *,
    form,
    target_share,
    resolution_error,
):
    return render(
        request,
        'shares/admin_report_list.html',
        _report_queue_context(
            page_number=request.POST.get('return_page'),
            pagination_base_url=reverse('admin_report_list'),
            target_share=target_share,
            resolution_form=form,
            resolution_error=resolution_error,
        ),
        status=400,
    )


@user_passes_test(is_moderator)
def admin_report_list(request):
    return render(
        request,
        'shares/admin_report_list.html',
        _report_queue_context(page_number=request.GET.get('page')),
    )


@user_passes_test(is_moderator)
def admin_report_share(request, share_id):
    share = get_object_or_404(
        _queue_share_queryset(
            Share.objects.annotate(pending_count=_pending_report_count())
        ),
        share_id=share_id,
    )
    _normalize_log_preview(share)
    reports = Paginator(
        _pending_report_queryset(
            text_length=_AUDIT_TEXT_PREVIEW_LENGTH,
        ).filter(share=share),
        20,
    ).get_page(request.GET.get('page'))
    resolution_form = _staff_reason_form(
        ReportResolutionForm,
        auto_id='report-resolution-%s',
        help_id='report-resolution-help',
        error_id='report-resolution-errors',
    )
    return render(request, 'shares/admin_report_share.html', _admin_context(
        share=share,
        reports=reports,
        resolution_form=resolution_form,
        resolution_error=None,
        moderation_active_tab='admin_report_list',
    ))


@user_passes_test(is_moderator)
@require_POST
def admin_resolve_report(request, report_id, action):
    if action not in {'resolve', 'dismiss'}:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')
    form = _staff_reason_form(
        ReportResolutionForm,
        data=request.POST,
        auto_id='report-resolution-%s',
        help_id='report-resolution-help',
        error_id='report-resolution-errors',
    )
    if not form.is_valid():
        report = get_object_or_404(
            _report_preview_queryset(
                text_length=_AUDIT_TEXT_PREVIEW_LENGTH,
            ).select_related('share', 'reporter'),
            pk=report_id,
        )
        return _render_report_form_error(
            request,
            form=form,
            target_share=report.share,
            resolution_error=_report_resolution_error(
                action=action,
                action_url=reverse(
                    'admin_resolve_report',
                    args=[report_id, action],
                ),
                share=report.share,
                report=report,
            ),
        )
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
    form = _staff_reason_form(
        ReportResolutionForm,
        data=request.POST,
        auto_id='report-resolution-%s',
        help_id='report-resolution-help',
        error_id='report-resolution-errors',
    )
    if not form.is_valid():
        share = get_object_or_404(
            _queue_share_queryset(),
            share_id=share_id,
        )
        return _render_report_form_error(
            request,
            form=form,
            target_share=share,
            resolution_error=_report_resolution_error(
                action=action,
                action_url=reverse(
                    'admin_resolve_share_reports',
                    args=[share_id, action],
                ),
                share=share,
            ),
        )
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
        _log_preview_queryset(
            text_length=_AUDIT_TEXT_PREVIEW_LENGTH,
        ).filter(action__in=[
            ShareLog.ActionType.REVIEW_APPROVE,
            ShareLog.ActionType.REVIEW_REJECT,
            ShareLog.ActionType.MODERATOR_TAKEDOWN,
            ShareLog.ActionType.RESTRICTION_CONFIRM,
            ShareLog.ActionType.RESTRICTION_RELEASE,
        ]).select_related('user', 'share'),
        20,
    ).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_review_logs.html', _admin_context(logs=logs))


@user_passes_test(is_moderator)
def admin_report_logs(request):
    logs = Paginator(
        _log_preview_queryset(
            text_length=_AUDIT_TEXT_PREVIEW_LENGTH,
        ).filter(
            action=ShareLog.ActionType.REPORT_HANDLE,
        ).select_related('user', 'share'),
        20,
    ).get_page(request.GET.get('page'))
    return render(request, 'shares/admin_report_logs.html', _admin_context(logs=logs))
