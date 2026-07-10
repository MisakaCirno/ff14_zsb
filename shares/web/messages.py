from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from shares.models import SiteMessage


@login_required
def site_message_list(request):
    """站内信列表"""
    messages_list = SiteMessage.objects.filter(
        recipient=request.user,
        archived_at__isnull=True,
    ).select_related('sender', 'related_share', 'related_report')
    paginator = Paginator(messages_list, 20)
    site_messages = paginator.get_page(request.GET.get('page'))
    return render(request, 'shares/site_message_list.html', {
        'site_messages': site_messages,
    })


@login_required
def site_message_detail(request, message_id):
    """站内信详情"""
    site_message = get_object_or_404(
        SiteMessage.objects.select_related('sender', 'related_share', 'related_report'),
        id=message_id,
        recipient=request.user,
    )
    return render(request, 'shares/site_message_detail.html', {
        'site_message': site_message,
    })


@login_required
@require_POST
def open_site_message(request, message_id):
    site_message = get_object_or_404(
        SiteMessage,
        id=message_id,
        recipient=request.user,
    )
    if site_message.read_at is None:
        site_message.read_at = timezone.now()
        site_message.save(update_fields=['read_at'])
    return redirect('site_message_detail', message_id=site_message.id)


@login_required
@require_POST
def mark_all_site_messages_read(request):
    """标记全部站内信为已读"""
    updated = SiteMessage.objects.filter(
        recipient=request.user,
        read_at__isnull=True,
        archived_at__isnull=True,
    ).update(read_at=timezone.now())
    messages.success(request, f'已将 {updated} 条站内信标记为已读')
    return redirect('site_message_list')
