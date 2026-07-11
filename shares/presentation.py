from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, resolve_url
from django.template.loader import render_to_string
from django.utils.cache import add_never_cache_headers

from .models import UserProfile


HOME_FEED_MODES = {
    UserProfile.HomeFeedMode.PAGINATED,
    UserProfile.HomeFeedMode.INFINITE,
}


def is_htmx_request(request):
    return request.headers.get('HX-Request', '').lower() == 'true'


def get_home_feed_mode(request):
    """Read the requested or persisted home-feed mode without mutating state."""
    requested_mode = request.GET.get('feed')
    if requested_mode in HOME_FEED_MODES:
        return requested_mode
    if request.user.is_authenticated:
        try:
            return request.user.profile.home_feed_mode
        except UserProfile.DoesNotExist:
            return UserProfile.HomeFeedMode.INFINITE
    return request.session.get(
        'home_feed_mode',
        UserProfile.HomeFeedMode.INFINITE,
    )


def build_query_string(request, **updates):
    params = request.GET.copy()
    for key, value in updates.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


def build_share_cards_return_url(request):
    params = request.GET.copy()
    params.pop('partial', None)
    params.pop('page', None)
    params.pop('continuation', None)
    query = params.urlencode()
    return f'{request.path}?{query}' if query else request.path


def build_share_cards_next_query(request, shares):
    if not shares.has_next():
        return ''
    return build_query_string(
        request,
        page=shares.next_page_number(),
        partial=None,
        continuation='1',
        feed=UserProfile.HomeFeedMode.INFINITE,
    )


def redirect_response(request, to, *args, **kwargs):
    if not is_htmx_request(request):
        return redirect(to, *args, **kwargs)
    target = resolve_url(to, *args, **kwargs)
    response = HttpResponse(status=204)
    response.headers['HX-Redirect'] = target
    add_never_cache_headers(response)
    return response


def render_share_cards_response(request, shares):
    context = {
        'shares': shares,
        'share_cards_return_url': build_share_cards_return_url(request),
        'share_cards_next_query': build_share_cards_next_query(request, shares),
    }
    is_continuation = (
        is_htmx_request(request)
        and request.GET.get('continuation') == '1'
        and request.GET.get('feed') == UserProfile.HomeFeedMode.INFINITE
    )
    html = render_to_string(
        (
            'shares/includes/share_cards_page.html'
            if is_continuation else 'shares/includes/share_cards.html'
        ),
        context,
        request=request,
    )
    if is_htmx_request(request):
        response = HttpResponse(html)
    else:
        response = JsonResponse({
            'html': html,
            'has_next': shares.has_next(),
            'next_page': shares.next_page_number() if shares.has_next() else None,
        })
    add_never_cache_headers(response)
    return response
