from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Max
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from shares.forms import CollectionForm
from shares.models import Collection, CollectionItem, Share, ShareLog
from shares.policies import (
    can_view_collection,
    is_owner,
    viewable_share_queryset,
)
from shares.services.audit import log_share_action


@login_required
def create_collection(request):
    if request.method == 'POST':
        form = CollectionForm(request.POST)
        if form.is_valid():
            collection = form.save(commit=False)
            collection.author = request.user
            collection.save()
            messages.success(request, '合集创建成功！')
            next_url = request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect('my_shares')
    else:
        form = CollectionForm()
    return render(request, 'shares/create_collection.html', {'form': form})


@login_required
def edit_collection(request, collection_id):
    collection = get_object_or_404(Collection, id=collection_id, author=request.user)
    if request.method == 'POST':
        form = CollectionForm(request.POST, instance=collection)
        if form.is_valid():
            form.save()
            messages.success(request, '合集更新成功！')
            return redirect('collection_detail', collection_id=collection.id)
    else:
        form = CollectionForm(instance=collection)
    return render(request, 'shares/edit_collection.html', {
        'form': form,
        'collection': collection,
    })


@login_required
def delete_collection(request, collection_id):
    collection = get_object_or_404(Collection, id=collection_id, author=request.user)
    if request.method == 'POST':
        collection.delete()
        messages.success(request, '合集已删除')
        return redirect('my_shares')
    return render(request, 'shares/delete_collection.html', {'collection': collection})


def collection_detail(request, collection_id):
    collection = get_object_or_404(
        Collection.objects.select_related('author', 'author__profile'),
        id=collection_id,
    )
    if not can_view_collection(request.user, collection):
        messages.error(request, '该合集不存在或您没有权限访问')
        return redirect('index')
    visible_share_ids = viewable_share_queryset(request.user).order_by().values('pk')
    collection_items = CollectionItem.objects.filter(
        collection=collection,
        share_id__in=visible_share_ids,
    ).select_related(
        'share',
        'share__author',
        'share__author__profile',
    ).order_by('order', 'added_at', 'pk')
    items = Paginator(collection_items, 12).get_page(request.GET.get('page'))
    return render(request, 'shares/collection_detail.html', {
        'collection': collection,
        'items': items,
        'can_manage_collection': is_owner(request.user, collection),
    })


@login_required
@require_POST
def add_share_to_collection(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if share.author != request.user:
        messages.error(request, '只能将自己的分享添加到合集')
        return redirect('share_detail', share_id=share_id)
    with transaction.atomic():
        collection = get_object_or_404(
            Collection.objects.select_for_update(),
            id=request.POST.get('collection_id'),
            author=request.user,
        )
        if CollectionItem.objects.filter(collection=collection, share=share).exists():
            messages.warning(request, '该分享已在合集中')
        else:
            max_order = CollectionItem.objects.filter(
                collection=collection,
            ).aggregate(Max('order'))['order__max']
            CollectionItem.objects.create(
                collection=collection,
                share=share,
                order=(max_order or 0) + 1,
            )
            log_share_action(
                request.user,
                share,
                ShareLog.ActionType.ADD_TO_COLLECTION,
                f'加入合集: {collection.title}',
            )
    return redirect('share_detail', share_id=share_id)


@login_required
@require_POST
def remove_share_from_collection(request, collection_id, share_id):
    with transaction.atomic():
        collection = get_object_or_404(
            Collection.objects.select_for_update(),
            id=collection_id,
            author=request.user,
        )
        share = get_object_or_404(Share, share_id=share_id)
        item = get_object_or_404(
            CollectionItem.objects.select_for_update(),
            collection=collection,
            share=share,
        )
        item.delete()
        log_share_action(
            request.user,
            share,
            ShareLog.ActionType.REMOVE_FROM_COLLECTION,
            f'移出合集: {collection.title}',
        )
    messages.success(request, '分享已从合集中移除')
    return redirect('collection_detail', collection_id=collection.id)
