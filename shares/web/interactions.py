from django.db.models import F
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.vary import vary_on_headers

from shares.models import Share
from shares.policies import can_view_share
from shares.presentation import is_htmx_request
from shares.rate_limits import consume_rate_limit, get_client_ip
from shares.web.decorators import login_required_or_hx_redirect


def _record_counter(request, share, *, cookie_name, rule_name, field_name):
    recorded = request.COOKIES.get(cookie_name, '')
    recorded_ids = recorded.split(',') if recorded else []
    if share.share_id not in recorded_ids:
        limit = consume_rate_limit(rule_name, f'ip:{get_client_ip(request)}')
        if limit.allowed:
            Share.objects.filter(pk=share.pk).update(**{field_name: F(field_name) + 1})
            share.refresh_from_db()
            recorded_ids.append(share.share_id)
            recorded_ids = recorded_ids[-100:]
    return recorded_ids


@require_POST
def record_view(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    viewed_ids = _record_counter(
        request,
        share,
        cookie_name='viewed_shares',
        rule_name='view_counter_ip',
        field_name='views',
    )
    response = JsonResponse({'status': 'success', 'views_count': share.views})
    response.set_cookie(
        'viewed_shares',
        ','.join(viewed_ids),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite='Lax',
    )
    return response


@require_POST
def record_copy(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    copied_ids = _record_counter(
        request,
        share,
        cookie_name='copied_shares',
        rule_name='copy_counter_ip',
        field_name='copies',
    )
    response = JsonResponse({'status': 'success', 'copies_count': share.copies})
    response.set_cookie(
        'copied_shares',
        ','.join(copied_ids),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite='Lax',
    )
    return response


@vary_on_headers('HX-Request', 'Cookie')
@never_cache
@login_required_or_hx_redirect
@require_POST
def toggle_like(request, share_id):
    if is_htmx_request(request) and request.GET.get('fragment') != 'card':
        return HttpResponseBadRequest('Unsupported interaction fragment.')
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    if share.likes.filter(id=request.user.id).exists():
        share.likes.remove(request.user)
        is_liked = False
    else:
        share.likes.add(request.user)
        is_liked = True
    likes_count = share.likes.count()
    if is_htmx_request(request):
        return render(request, 'shares/includes/like_button.html', {
            'share': share,
            'is_liked': is_liked,
            'likes_count': likes_count,
        })
    return JsonResponse({
        'status': 'success',
        'is_liked': is_liked,
        'likes_count': likes_count,
    })


@vary_on_headers('HX-Request', 'Cookie')
@never_cache
@login_required_or_hx_redirect
@require_POST
def toggle_favorite(request, share_id):
    if is_htmx_request(request) and request.GET.get('fragment') != 'card':
        return HttpResponseBadRequest('Unsupported interaction fragment.')
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    if share.favorites.filter(id=request.user.id).exists():
        share.favorites.remove(request.user)
        is_favorited = False
    else:
        share.favorites.add(request.user)
        is_favorited = True
    favorites_count = share.favorites.count()
    if is_htmx_request(request):
        return render(request, 'shares/includes/favorite_button.html', {
            'share': share,
            'is_favorited': is_favorited,
            'favorites_count': favorites_count,
        })
    return JsonResponse({
        'status': 'success',
        'is_favorited': is_favorited,
        'favorites_count': favorites_count,
    })
