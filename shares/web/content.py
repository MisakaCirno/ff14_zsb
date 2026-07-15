from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from shares.forms import CreateShareForm, EditShareForm
from shares.models import Collection, Share
from shares.policies import can_view_share, is_moderator, viewable_share_queryset
from shares.presentation import build_share_detail_view_model
from shares.rate_limits import consume_rate_limit, request_identity
from shares.selectors import (
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


_MY_CONTENT_TABS = {'my_shares', 'collections', 'likes', 'favorites'}


def share_detail(request, share_id):
    try:
        share = share_detail_queryset(request.user).get(share_id=share_id)
    except Share.DoesNotExist:
        return render(request, '404.html', status=404)
    if not can_view_share(request.user, share):
        messages.error(request, '该分享不存在或您没有权限访问')
        return redirect('index')
    detail = build_share_detail_view_model(share, request.user)
    related_collections = related_collection_summaries(share, request.user)
    share_logs = (
        share.logs.select_related('user').order_by('-created_at')
        if detail.actions.can_view_logs
        else None
    )
    user_collections = []
    if detail.actions.can_add_to_collection:
        user_collections = Collection.objects.filter(author=request.user).order_by('-updated_at')
    canonical_share_path = share.get_absolute_url()
    return render(request, 'shares/detail.html', {
        'share': share,
        'detail': detail,
        'canonical_share_path': canonical_share_path,
        'canonical_share_url': request.build_absolute_uri(canonical_share_path),
        'related_collections': related_collections,
        'user_collections': user_collections,
        'share_logs': share_logs,
        'is_liked': detail.is_liked,
        'is_favorited': detail.is_favorited,
    })


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
    share = get_object_or_404(Share, share_id=share_id, author=request.user)
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
    share = get_object_or_404(Share, share_id=share_id)
    if request.user != share.author and not is_moderator(request.user):
        messages.error(request, '您没有权限删除此分享')
        return redirect('share_detail', share_id=share_id)
    if request.method == 'POST':
        is_author = request.user == share.author
        share.delete()
        messages.success(request, '分享已删除')
        return redirect('my_shares' if is_author else 'index')
    return render(request, 'shares/delete.html', {'share': share})


@login_required
def my_shares(request):
    tab = request.GET.get('tab', 'my_shares')
    if tab not in _MY_CONTENT_TABS:
        tab = 'my_shares'

    context = {'current_tab': tab}
    page_number = request.GET.get('page')
    if tab == 'collections':
        queryset = Collection.objects.filter(author=request.user).annotate(
            item_count=Count('collectionitem'),
        ).order_by('-updated_at', '-pk')
        context['collections'] = Paginator(queryset, 12).get_page(page_number)
        return render(request, 'shares/my_shares.html', context)

    if tab == 'likes':
        queryset = viewable_share_queryset(
            request.user,
            request.user.liked_shares.all(),
        )
        ordering = ('-created_at', '-pk')
    elif tab == 'favorites':
        queryset = viewable_share_queryset(
            request.user,
            request.user.favorited_shares.all(),
        )
        ordering = ('-created_at', '-pk')
    else:
        queryset = Share.objects.filter(author=request.user)
        ordering = (
            ('created_at', 'pk')
            if request.GET.get('order') == 'desc'
            else ('-created_at', '-pk')
        )
    queryset = annotate_share_cards(queryset, request.user).order_by(*ordering)
    context['shares'] = Paginator(queryset, 12).get_page(page_number)
    return render(request, 'shares/my_shares.html', context)
