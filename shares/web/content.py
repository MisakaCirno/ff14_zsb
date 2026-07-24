from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models.functions import Substr
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.vary import vary_on_headers

from shares.content_preferences import (
    apply_hidden_content_preferences,
    resolve_content_display_preferences,
)
from shares.forms import CreateShareForm, EditShareForm
from shares.models import Collection, Share
from shares.policies import can_view_share, is_moderator, viewable_share_queryset
from shares.presentation import (
    build_my_reaction_return_url,
    build_share_detail_view_model,
    is_htmx_request,
)
from shares.rate_limits import consume_rate_limit, request_identity
from shares.selectors import (
    annotate_collection_cards,
    annotate_share_cards,
    related_collection_summaries,
    share_detail_queryset,
)
from shares.services.shares import (
    CollectionUnavailableError,
    ShareEditConflictError,
    create_share_from_form,
    update_share_from_form,
)
from shares.services.deletion import (
    ContentDeletionPermissionError,
    move_share_to_trash,
    restore_share_from_trash,
)


_MY_CONTENT_TABS = {'my_shares', 'collections', 'likes', 'favorites', 'trash'}
_DETAIL_LOG_PREVIEW_SIZE = 25


@vary_on_headers('HX-Request', 'Cookie')
def share_detail(request, share_id):
    try:
        share = share_detail_queryset(request.user).get(share_id=share_id)
    except Share.DoesNotExist:
        return render(request, '404.html', status=404)
    if not can_view_share(request.user, share):
        messages.error(request, '该分享不存在或您没有权限访问')
        return redirect('index')
    preferences = resolve_content_display_preferences(request)
    detail = build_share_detail_view_model(
        share,
        request.user,
        show_spoiler=preferences.spoiler == 'show',
        show_nsfw=preferences.nsfw == 'show',
    )
    canonical_share_path = share.get_absolute_url()
    context = {
        'share': share,
        'detail': detail,
        'canonical_share_path': canonical_share_path,
        'canonical_share_url': request.build_absolute_uri(canonical_share_path),
        'is_liked': detail.is_liked,
        'is_favorited': detail.is_favorited,
        **preferences.as_context(),
    }
    if (
        is_htmx_request(request)
        and request.GET.get('presentation') == 'overlay'
    ):
        return render(request, 'shares/includes/share_detail_overlay.html', context)

    related_collections = related_collection_summaries(
        share,
        request.user,
        page_number=request.GET.get('page'),
        selected_collection_id=request.GET.get('collection_id'),
    )
    share_logs = None
    share_logs_truncated = False
    if detail.actions.can_view_logs:
        log_preview = list(
            share.logs.select_related('user').annotate(
                details_preview=Substr('details', 1, 500),
            ).defer('details').order_by('-created_at', '-pk')
            [:_DETAIL_LOG_PREVIEW_SIZE + 1]
        )
        share_logs_truncated = len(log_preview) > _DETAIL_LOG_PREVIEW_SIZE
        share_logs = tuple(log_preview[:_DETAIL_LOG_PREVIEW_SIZE])
    has_user_collections = False
    if detail.actions.can_add_to_collection:
        has_user_collections = Collection.objects.filter(
            author=request.user,
            deleted_at__isnull=True,
        ).exists()
    context.update({
        'related_collections': related_collections,
        'has_user_collections': has_user_collections,
        'share_logs': share_logs,
        'share_logs_truncated': share_logs_truncated,
    })
    return render(request, 'shares/detail.html', context)


def create_share(request):
    response_status = 200
    if request.method == 'POST':
        form = CreateShareForm(request.POST, user=request.user)
        rule_name = 'authenticated_create_user' if request.user.is_authenticated else 'anonymous_create_ip'
        rate_limit = consume_rate_limit(rule_name, request_identity(request))
        if not rate_limit.allowed:
            messages.error(request, '创建请求过于频繁，请稍后再试。')
            response_status = 429
        elif form.is_valid():
            try:
                result = create_share_from_form(form=form, actor=request.user)
            except CollectionUnavailableError:
                form.add_error('collection_id', '所选合集已不存在，请重新选择。')
            else:
                if result.requires_review:
                    messages.info(request, '您的分享已提交，审核通过后将显示在公开列表中。在此期间，您可以通过链接分享给他人。')
                else:
                    messages.success(request, '分享创建成功！')
                return redirect('share_detail', share_id=result.share.share_id)
    else:
        form = CreateShareForm(user=request.user)
    return render(request, 'shares/create.html', {
        'form': form,
    }, status=response_status)


@login_required
def edit_share(request, share_id):
    share = get_object_or_404(
        Share,
        share_id=share_id,
        author=request.user,
        deleted_at__isnull=True,
    )
    if request.method == 'POST':
        form = EditShareForm(request.POST, instance=share)
        if form.is_valid():
            try:
                result = update_share_from_form(form=form, actor=request.user)
            except ShareEditConflictError:
                form.add_error(None, '该分享已被其他操作更新。请刷新页面后重新编辑，当前提交未保存。')
                share.refresh_from_db()
            else:
                if not result.changed:
                    messages.info(request, '没有检测到需要保存的修改。')
                elif result.share.is_restricted:
                    messages.info(
                        request,
                        '修改已保存并重新进入审核；当前限制不会因编辑自动解除。'
                        '管理员处理后，分享仍会按照你选择的可见范围开放。',
                    )
                elif result.requires_review:
                    messages.info(request, '修改已保存，需要重新审核后才能在公开列表中显示。')
                else:
                    messages.success(request, '分享更新成功！')
                return redirect('share_detail', share_id=result.share.share_id)
    else:
        form = EditShareForm(instance=share)
    return render(request, 'shares/edit.html', {'form': form, 'share': share})


@login_required
def delete_share(request, share_id):
    share = get_object_or_404(
        Share,
        share_id=share_id,
        deleted_at__isnull=True,
    )
    if request.user != share.author and not is_moderator(request.user):
        messages.error(request, '您没有权限删除此分享')
        return redirect('share_detail', share_id=share_id)
    if request.method == 'POST':
        is_author = request.user == share.author
        try:
            move_share_to_trash(share_pk=share.pk, actor=request.user)
        except (Share.DoesNotExist, ContentDeletionPermissionError):
            return render(request, '404.html', status=404)
        messages.success(request, '分享已移入回收站，可随时恢复')
        return redirect('my_shares' if is_author else 'index')
    return render(request, 'shares/delete.html', {'share': share})


@login_required
def restore_share(request, share_id):
    if request.method != 'POST':
        return render(request, '404.html', status=404)
    share = get_object_or_404(
        Share,
        share_id=share_id,
        deleted_at__isnull=False,
    )
    try:
        result = restore_share_from_trash(
            share_pk=share.pk,
            actor=request.user,
        )
    except (Share.DoesNotExist, ContentDeletionPermissionError):
        return render(request, '404.html', status=404)
    if result.changed:
        messages.success(request, '分享已从回收站恢复')
    else:
        messages.info(request, '分享已经恢复，无需重复操作')
    return redirect(f'{reverse("my_shares")}?tab=trash')


@login_required
def my_shares(request):
    tab = request.GET.get('tab', 'my_shares')
    if tab not in _MY_CONTENT_TABS:
        tab = 'my_shares'

    preferences = resolve_content_display_preferences(request)
    context = {
        'current_tab': tab,
        **preferences.as_context(),
    }
    page_number = request.GET.get('page')
    if tab == 'trash':
        deleted_shares = annotate_share_cards(
            Share.objects.filter(
                author=request.user,
                deleted_at__isnull=False,
            ),
            request.user,
        ).order_by('-deleted_at', '-pk')
        deleted_collections = annotate_collection_cards(
            Collection.objects.filter(
                author=request.user,
                deleted_at__isnull=False,
            ),
        ).order_by('-deleted_at', '-pk')
        context['deleted_shares'] = Paginator(deleted_shares, 12).get_page(
            request.GET.get('share_page')
        )
        context['deleted_collections'] = Paginator(
            deleted_collections,
            12,
        ).get_page(request.GET.get('collection_page'))
        return render(request, 'shares/my_shares.html', context)
    if tab == 'collections':
        queryset = annotate_collection_cards(
            Collection.objects.filter(
                author=request.user,
                deleted_at__isnull=True,
            ),
        ).order_by('-updated_at', '-pk')
        context['collections'] = Paginator(queryset, 12).get_page(page_number)
        return render(request, 'shares/my_shares.html', context)

    if tab == 'likes':
        queryset = viewable_share_queryset(
            request.user,
            request.user.liked_shares.all(),
        )
        queryset = apply_hidden_content_preferences(queryset, preferences)
        ordering = ('-created_at', '-pk')
    elif tab == 'favorites':
        queryset = viewable_share_queryset(
            request.user,
            request.user.favorited_shares.all(),
        )
        queryset = apply_hidden_content_preferences(queryset, preferences)
        ordering = ('-created_at', '-pk')
    else:
        queryset = Share.objects.filter(
            author=request.user,
            deleted_at__isnull=True,
        )
        ordering = (
            ('created_at', 'pk')
            if request.GET.get('order') == 'desc'
            else ('-created_at', '-pk')
        )
    queryset = annotate_share_cards(queryset, request.user).order_by(*ordering)
    shares = Paginator(queryset, 12).get_page(page_number)
    context['shares'] = shares
    if tab in {'likes', 'favorites'}:
        context['share_interaction_return_url'] = build_my_reaction_return_url(
            tab=tab,
            page_number=shares.number,
        )
    return render(request, 'shares/my_shares.html', context)
