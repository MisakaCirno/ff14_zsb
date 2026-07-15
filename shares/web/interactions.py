import json

from django.db.models import F
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.vary import vary_on_headers

from shares.models import Share
from shares.policies import can_view_share
from shares.presentation import is_htmx_request
from shares.rate_limits import consume_rate_limit, get_client_ip
from shares.services.interactions import (
    ShareInteractionUnavailableError,
    set_favorite_state,
    set_like_state,
)
from shares.web.decorators import get_safe_local_return_url, login_required_or_hx_redirect


INTERACTION_FRAGMENTS = {'card', 'detail'}
INTERACTION_TARGET_STATES = {
    'active': True,
    'inactive': False,
}


def _add_removal_trigger(response, *, event_name, share_id):
    response.headers['HX-Trigger-After-Swap'] = json.dumps({
        event_name: {'shareId': share_id},
    }, separators=(',', ':'))
    return response


def _interaction_target_state(request):
    try:
        return INTERACTION_TARGET_STATES[request.POST['target_state']]
    except KeyError:
        return None


def _invalid_target_state_response(is_htmx):
    message = 'target_state must be active or inactive.'
    if is_htmx:
        return HttpResponseBadRequest(message)
    return JsonResponse({'status': 'error', 'message': message}, status=400)


def _hidden_share_response():
    return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)


def _interaction_success_response(request, result, payload):
    if 'next' in request.POST:
        return_url = (
            get_safe_local_return_url(request, request.POST.get('next'))
            or result.share.get_absolute_url()
        )
        return HttpResponseRedirect(return_url)
    return JsonResponse(payload)


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
    is_htmx = is_htmx_request(request)
    fragment = request.GET.get('fragment') if is_htmx else None
    if is_htmx and fragment not in INTERACTION_FRAGMENTS:
        return HttpResponseBadRequest('Unsupported interaction fragment.')
    target_active = _interaction_target_state(request)
    if target_active is None:
        return _invalid_target_state_response(is_htmx)
    try:
        result = set_like_state(
            share_id=share_id,
            user=request.user,
            target_active=target_active,
        )
    except ShareInteractionUnavailableError:
        return _hidden_share_response()
    if is_htmx:
        response = render(request, 'shares/includes/like_button.html', {
            'share': result.share,
            'is_liked': result.is_active,
            'likes_count': result.count,
            'interaction_fragment': fragment,
        })
        if not result.is_active:
            _add_removal_trigger(
                response,
                event_name='share-like-removed',
                share_id=result.share.share_id,
            )
        return response
    return _interaction_success_response(request, result, {
        'status': 'success',
        'is_liked': result.is_active,
        'likes_count': result.count,
    })


@vary_on_headers('HX-Request', 'Cookie')
@never_cache
@login_required_or_hx_redirect
@require_POST
def toggle_favorite(request, share_id):
    is_htmx = is_htmx_request(request)
    fragment = request.GET.get('fragment') if is_htmx else None
    if is_htmx and fragment not in INTERACTION_FRAGMENTS:
        return HttpResponseBadRequest('Unsupported interaction fragment.')
    target_active = _interaction_target_state(request)
    if target_active is None:
        return _invalid_target_state_response(is_htmx)
    try:
        result = set_favorite_state(
            share_id=share_id,
            user=request.user,
            target_active=target_active,
        )
    except ShareInteractionUnavailableError:
        return _hidden_share_response()
    if is_htmx:
        response = render(request, 'shares/includes/favorite_button.html', {
            'share': result.share,
            'is_favorited': result.is_active,
            'favorites_count': result.count,
            'interaction_fragment': fragment,
        })
        if not result.is_active:
            _add_removal_trigger(
                response,
                event_name='share-favorite-removed',
                share_id=result.share.share_id,
            )
        return response
    return _interaction_success_response(request, result, {
        'status': 'success',
        'is_favorited': result.is_active,
        'favorites_count': result.count,
    })
