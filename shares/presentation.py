from django.http import JsonResponse
from django.template.loader import render_to_string

from .models import UserProfile


HOME_FEED_MODES = {
    UserProfile.HomeFeedMode.PAGINATED,
    UserProfile.HomeFeedMode.INFINITE,
}


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


def render_share_cards_response(request, shares):
    html = render_to_string(
        'shares/includes/share_cards.html',
        {'shares': shares},
        request=request,
    )
    return JsonResponse({
        'html': html,
        'has_next': shares.has_next(),
        'next_page': shares.next_page_number() if shares.has_next() else None,
    })
