from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from shares.models import SiteMessage
from shares.policies import can_view_share
from shares.selectors import site_message_list_queryset
from shares.services.messages import (
    mark_all_inbox_site_messages_read,
    mark_site_message_read,
    set_site_message_archive_state as set_archive_state,
)


MAILBOX_PRESENTATION = {
    'inbox': {
        'title': '收件箱',
        'description': '查看审核、举报和内容限制相关的站点通知。',
        'empty_title': '收件箱是空的',
        'empty_message': '新的审核与举报处理通知会显示在这里。',
    },
    'unread': {
        'title': '未读消息',
        'description': '集中处理尚未阅读的站点通知。',
        'empty_title': '没有未读消息',
        'empty_message': '当前收件箱中的通知都已读。',
    },
    'archived': {
        'title': '归档箱',
        'description': '查看已经归档的历史通知，并可随时恢复到收件箱。',
        'empty_title': '归档箱是空的',
        'empty_message': '归档后的站内信会保留在这里，不会丢失。',
    },
}


def _mailbox(value, *, default='inbox'):
    return value if value in MAILBOX_PRESENTATION else default


def _page_number(value):
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _mailbox_url(mailbox=None, page=None):
    query = {}
    if mailbox:
        query['mailbox'] = mailbox
    if page:
        query['page'] = page
    url = reverse('site_message_list')
    return f'{url}?{urlencode(query)}' if query else url


def _detail_url(message_id, mailbox=None, page=None):
    query = {}
    if mailbox:
        query['mailbox'] = mailbox
    if page:
        query['page'] = page
    url = reverse('site_message_detail', args=[message_id])
    return f'{url}?{urlencode(query)}' if query else url


def _message_or_404(*, recipient, message_id):
    try:
        return SiteMessage.objects.select_related('related_share').get(
            pk=message_id,
            recipient=recipient,
        )
    except SiteMessage.DoesNotExist as exc:
        raise Http404('Site message not found') from exc


@never_cache
@login_required
@require_safe
def site_message_list(request):
    """Render one current-user mailbox without mutating message state."""
    mailbox = _mailbox(request.GET.get('mailbox'))
    paginator = Paginator(
        site_message_list_queryset(request.user, mailbox),
        20,
    )
    site_messages = paginator.get_page(request.GET.get('page'))
    presentation = MAILBOX_PRESENTATION[mailbox]
    return render(request, 'shares/site_message_list.html', {
        'site_messages': site_messages,
        'mailbox': mailbox,
        'mailbox_title': presentation['title'],
        'mailbox_description': presentation['description'],
        'empty_title': presentation['empty_title'],
        'empty_message': presentation['empty_message'],
    })


@never_cache
@login_required
@require_safe
def site_message_detail(request, message_id):
    """Render an owned message; reading remains an explicit POST action."""
    site_message = _message_or_404(
        recipient=request.user,
        message_id=message_id,
    )
    default_mailbox = 'archived' if site_message.archived_at else 'inbox'
    mailbox = _mailbox(request.GET.get('mailbox'), default=default_mailbox)
    page = _page_number(request.GET.get('page'))
    related_share_url = None
    related_share_unavailable = False
    if site_message.related_share:
        if can_view_share(request.user, site_message.related_share):
            related_share_url = site_message.related_share.get_absolute_url()
        else:
            related_share_unavailable = True
    return render(request, 'shares/site_message_detail.html', {
        'site_message': site_message,
        'mailbox': mailbox,
        'page_number': page,
        'back_url': _mailbox_url(mailbox, page),
        'related_share_url': related_share_url,
        'related_share_unavailable': related_share_unavailable,
    })


@never_cache
@login_required
@require_POST
def open_site_message(request, message_id):
    submitted_mailbox = request.POST.get('mailbox')
    mailbox = _mailbox(submitted_mailbox) if submitted_mailbox else None
    page = _page_number(request.POST.get('page'))
    try:
        site_message, _ = mark_site_message_read(
            recipient=request.user,
            message_id=message_id,
        )
    except SiteMessage.DoesNotExist as exc:
        raise Http404('Site message not found') from exc
    return redirect(_detail_url(site_message.pk, mailbox, page))


@never_cache
@login_required
@require_POST
def mark_all_site_messages_read(request):
    """Mark the current user's unarchived unread messages read."""
    submitted_mailbox = request.POST.get('mailbox')
    mailbox = _mailbox(submitted_mailbox) if submitted_mailbox else None
    page = _page_number(request.POST.get('page'))
    updated = mark_all_inbox_site_messages_read(recipient=request.user)
    messages.success(request, f'已将 {updated} 条站内信标记为已读')
    return redirect(_mailbox_url(mailbox, page))


@never_cache
@login_required
@require_POST
def set_site_message_archive_state(request, message_id):
    target_state = request.POST.get('target_state')
    if target_state not in {'archived', 'inbox'}:
        return HttpResponseBadRequest('Unsupported archive target state.')
    submitted_mailbox = request.POST.get('mailbox')
    mailbox = _mailbox(submitted_mailbox) if submitted_mailbox else None
    page = _page_number(request.POST.get('page'))
    try:
        site_message, changed = set_archive_state(
            recipient=request.user,
            message_id=message_id,
            archived=target_state == 'archived',
        )
    except SiteMessage.DoesNotExist as exc:
        raise Http404('Site message not found') from exc

    if target_state == 'archived':
        notice = '站内信已归档' if changed else '站内信已经在归档箱中'
    else:
        notice = '站内信已恢复到收件箱' if changed else '站内信已经在收件箱中'
    messages.success(request, notice)
    return redirect(_mailbox_url(mailbox, page))
