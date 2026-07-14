from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, Max, OuterRef
from django.shortcuts import get_object_or_404, redirect, render

from shares.forms import ShareForm
from shares.models import Collection, CollectionItem, Share, ShareLog
from shares.policies import can_view_share, is_moderator
from shares.rate_limits import consume_rate_limit, request_identity
from shares.selectors import related_collection_summaries
from shares.services.audit import log_share_action


def share_detail(request, share_id):
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return render(request, '404.html', status=404)
    if not can_view_share(request.user, share):
        messages.error(request, '该分享不存在或您没有权限访问')
        return redirect('index')
    related_collections = related_collection_summaries(share, request.user)
    share_logs = (
        share.logs.select_related('user').order_by('-created_at')
        if is_moderator(request.user)
        else None
    )
    user_collections = []
    if request.user.is_authenticated and share.author == request.user:
        user_collections = Collection.objects.filter(author=request.user).order_by('-updated_at')
    is_liked = request.user.is_authenticated and share.likes.filter(id=request.user.id).exists()
    is_favorited = request.user.is_authenticated and share.favorites.filter(id=request.user.id).exists()
    return render(request, 'shares/detail.html', {
        'share': share,
        'related_collections': related_collections,
        'user_collections': user_collections,
        'share_logs': share_logs,
        'is_liked': is_liked,
        'is_favorited': is_favorited,
    })


def create_share(request):
    response_status = 200
    if request.method == 'POST':
        form = ShareForm(request.POST)
        rule_name = 'authenticated_create_user' if request.user.is_authenticated else 'anonymous_create_ip'
        rate_limit = consume_rate_limit(rule_name, request_identity(request))
        if not rate_limit.allowed:
            messages.error(request, '创建请求过于频繁，请稍后再试。')
            response_status = 429
        elif form.is_valid():
            share = form.save(commit=False)
            if request.user.is_authenticated:
                share.author = request.user
                if share.visibility == Share.Visibility.PUBLIC and not is_moderator(request.user):
                    share.status = Share.Status.PENDING
                    messages.info(request, '您的分享已提交，审核通过后将显示在公开列表中。在此期间，您可以通过链接分享给他人。')
                else:
                    share.status = Share.Status.APPROVED
            else:
                share.author = None
                share.visibility = Share.Visibility.UNLISTED
                share.status = Share.Status.APPROVED
            with transaction.atomic():
                share.save()
                if request.user.is_authenticated:
                    log_share_action(request.user, share, ShareLog.ActionType.CREATE, '用户创建分享')
                collection_id = request.POST.get('collection_id')
                if collection_id and request.user.is_authenticated:
                    try:
                        collection = Collection.objects.select_for_update().get(
                            id=collection_id,
                            author=request.user,
                        )
                        max_order = CollectionItem.objects.filter(
                            collection=collection,
                        ).aggregate(Max('order'))['order__max']
                        CollectionItem.objects.create(
                            collection=collection,
                            share=share,
                            order=(max_order or 0) + 1,
                        )
                    except Collection.DoesNotExist:
                        pass
            if share.status == Share.Status.APPROVED:
                messages.success(request, '分享创建成功！')
            return redirect('share_detail', share_id=share.share_id)
    else:
        form = ShareForm()
    user_collections = (
        Collection.objects.filter(author=request.user).order_by('-updated_at')
        if request.user.is_authenticated
        else []
    )
    return render(request, 'shares/create.html', {
        'form': form,
        'user_collections': user_collections,
    }, status=response_status)


@login_required
def edit_share(request, share_id):
    share = get_object_or_404(Share, share_id=share_id, author=request.user)
    if request.method == 'POST':
        form = ShareForm(request.POST, instance=share)
        if form.is_valid():
            updated_share = form.save(commit=False)
            updated_share.status = (
                Share.Status.PENDING
                if updated_share.visibility == Share.Visibility.PUBLIC and not is_moderator(request.user)
                else Share.Status.APPROVED
            )
            if updated_share.status == Share.Status.PENDING:
                messages.info(request, '修改已保存，需要重新审核后才能在公开列表中显示。')
            updated_share.review_feedback = ''
            updated_share.reviewed_at = None
            updated_share.reviewed_by = None
            labels = (
                ('title', '标题'),
                ('strategy_code', '战术板代码'),
                ('description', '描述'),
                ('category', '分类'),
                ('visibility', '可见性'),
            )
            changes = [label for name, label in labels if name in form.changed_data]
            details = f"用户编辑内容: {', '.join(changes)}" if changes else '用户编辑分享'
            with transaction.atomic():
                updated_share.save()
                log_share_action(request.user, updated_share, ShareLog.ActionType.EDIT, details)
            if updated_share.status == Share.Status.APPROVED:
                messages.success(request, '分享更新成功！')
            return redirect('share_detail', share_id=share.share_id)
    else:
        form = ShareForm(instance=share)
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
    if tab == 'likes':
        queryset = request.user.liked_shares.all()
        ordering = '-created_at'
    elif tab == 'favorites':
        queryset = request.user.favorited_shares.all()
        ordering = '-created_at'
    else:
        queryset = Share.objects.filter(author=request.user)
        ordering = 'created_at' if request.GET.get('order') == 'desc' else '-created_at'
    queryset = queryset.annotate(
        likes_count=Count('likes', distinct=True),
        favorites_count=Count('favorites', distinct=True),
        is_liked=Exists(Share.likes.through.objects.filter(
            share_id=OuterRef('pk'), user_id=request.user.id,
        )),
        is_favorited=Exists(Share.favorites.through.objects.filter(
            share_id=OuterRef('pk'), user_id=request.user.id,
        )),
    )
    shares = Paginator(queryset.order_by(ordering), 12).get_page(request.GET.get('page'))
    collections = Collection.objects.filter(author=request.user).annotate(
        item_count=Count('collectionitem'),
    ).order_by('-updated_at')
    return render(request, 'shares/my_shares.html', {
        'shares': shares,
        'collections': collections,
        'current_tab': tab,
    })
