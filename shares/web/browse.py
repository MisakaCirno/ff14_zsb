from urllib.parse import urlsplit

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST, require_safe
from django.views.decorators.vary import vary_on_headers

from shares.content_preferences import (
    apply_hidden_content_preferences,
    resolve_content_display_preferences,
)
from shares.models import Announcement, Collection, Share, UserProfile
from shares.policies import can_view_share, is_moderator, public_share_queryset
from shares.presentation import (
    HOME_FEED_MODES,
    build_query_string,
    build_share_cards_next_query,
    build_share_cards_return_url,
    build_user_presentation,
    get_home_feed_mode,
    is_htmx_request,
    redirect_response,
    render_share_cards_response,
)
from shares.selectors import annotate_collection_cards, annotate_share_cards
from shares.validation import SEARCH_QUERY_MAX_LENGTH


_BROWSE_CATEGORIES = {
    Share.Category.ENTERTAINMENT,
    Share.Category.COMBAT,
}
_PUBLIC_PROFILE_TABS = {'shares', 'collections'}
_BROWSE_ORDERINGS = {
    'latest': ('-created_at', '-pk'),
    'likes': ('-likes_count', '-created_at', '-pk'),
    'views': ('-views', '-created_at', '-pk'),
    'favorites': ('-favorites_count', '-created_at', '-pk'),
    'copies': ('-copies', '-created_at', '-pk'),
}


def _is_browse_explorer_request(request):
    return (
        is_htmx_request(request)
        and request.headers.get('HX-Target') == 'browse-explorer'
    )


def _prepare_browse_shares(request, queryset):
    category = request.GET.get('category')
    if category not in _BROWSE_CATEGORIES:
        category = None
    if category:
        queryset = queryset.filter(category=category)

    preferences = resolve_content_display_preferences(request)
    queryset = apply_hidden_content_preferences(queryset, preferences)

    sort_by = request.GET.get('sort', 'latest')
    if sort_by not in _BROWSE_ORDERINGS:
        sort_by = 'latest'
    queryset = annotate_share_cards(queryset, request.user).order_by(
        *_BROWSE_ORDERINGS[sort_by]
    )
    return queryset, {
        'current_category': category,
        'sort_by': sort_by,
        **preferences.as_context(),
    }


@require_POST
def set_home_feed_mode(request):
    requested_mode = request.POST.get('feed')
    if requested_mode not in HOME_FEED_MODES:
        messages.error(request, '无效的浏览模式。')
        return redirect('index')
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if profile.home_feed_mode != requested_mode:
            profile.home_feed_mode = requested_mode
            profile.save(update_fields=['home_feed_mode', 'updated_at'])
    else:
        request.session['home_feed_mode'] = requested_mode
    next_url = request.POST.get('next')
    if (
        next_url
        and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        and urlsplit(next_url).path.startswith('/')
    ):
        return redirect(next_url)
    return redirect('index')


@vary_on_headers('HX-Request', 'HX-Target', 'Cookie')
@require_safe
def index(request):
    queryset, browse_options = _prepare_browse_shares(
        request,
        public_share_queryset(),
    )
    shares = Paginator(queryset, 12).get_page(request.GET.get('page'))
    is_browse_explorer_request = _is_browse_explorer_request(request)
    if (
        not is_browse_explorer_request
        and (
            is_htmx_request(request)
            or request.GET.get('partial') == 'shares'
        )
    ):
        return render_share_cards_response(request, shares, browse_options)
    feed_mode = get_home_feed_mode(request)
    response = render(request, 'shares/index.html', {
        'shares': shares,
        **browse_options,
        'latest_announcement': Announcement.objects.filter(
            is_active=True,
        ).order_by('-created_at').first(),
        'feed_mode': feed_mode,
        'paginated_query': build_query_string(
            request,
            feed=UserProfile.HomeFeedMode.PAGINATED,
            page=None,
            partial=None,
        ),
        'infinite_query': build_query_string(
            request,
            feed=UserProfile.HomeFeedMode.INFINITE,
            page=None,
            partial=None,
        ),
        'share_cards_return_url': build_share_cards_return_url(
            request,
            page_number=shares.number,
        ),
        'share_cards_next_query': build_share_cards_next_query(request, shares),
    })
    if is_browse_explorer_request:
        add_never_cache_headers(response)
    return response


@vary_on_headers('HX-Request', 'HX-Target', 'Cookie')
@require_safe
def search(request):
    query = request.GET.get('q', '').strip()
    if not query:
        return redirect_response(request, 'index')
    if len(query) > SEARCH_QUERY_MAX_LENGTH:
        messages.error(request, f'搜索内容不能超过 {SEARCH_QUERY_MAX_LENGTH} 个字符。')
        return redirect_response(request, 'index')
    try:
        share = Share.objects.get(share_id=query)
        if can_view_share(request.user, share):
            return redirect_response(request, 'share_detail', share_id=share.share_id)
    except Share.DoesNotExist:
        pass
    queryset = public_share_queryset().filter(
        Q(title__icontains=query)
        | Q(description__icontains=query)
        | Q(author__profile__nickname__icontains=query)
        | Q(author__username__icontains=query)
    ).distinct()
    queryset, browse_options = _prepare_browse_shares(request, queryset)
    shares = Paginator(queryset, 12).get_page(request.GET.get('page'))
    is_browse_explorer_request = _is_browse_explorer_request(request)
    if (
        not is_browse_explorer_request
        and (
            is_htmx_request(request)
            or request.GET.get('partial') == 'shares'
        )
    ):
        return render_share_cards_response(request, shares, browse_options)
    response = render(request, 'shares/index.html', {
        'shares': shares,
        'search_query': query,
        **browse_options,
        'feed_mode': get_home_feed_mode(request),
        'paginated_query': build_query_string(
            request,
            feed=UserProfile.HomeFeedMode.PAGINATED,
            page=None,
            partial=None,
        ),
        'infinite_query': build_query_string(
            request,
            feed=UserProfile.HomeFeedMode.INFINITE,
            page=None,
            partial=None,
        ),
        'share_cards_return_url': build_share_cards_return_url(
            request,
            page_number=shares.number,
        ),
        'share_cards_next_query': build_share_cards_next_query(request, shares),
    })
    if is_browse_explorer_request:
        add_never_cache_headers(response)
    return response


def user_public_profile(request, username):
    author = get_object_or_404(
        User.objects.select_related('profile'),
        username=username,
    )
    current_tab = request.GET.get('tab', 'shares')
    if current_tab not in _PUBLIC_PROFILE_TABS:
        current_tab = 'shares'
    preferences = resolve_content_display_preferences(request)
    context = {
        'author': author,
        'current_tab': current_tab,
        'author_presentation': build_user_presentation(author),
        **preferences.as_context(),
    }
    public_shares = public_share_queryset(
        Share.objects.filter(author=author),
    ).order_by('-created_at', '-pk')
    if current_tab == 'collections':
        context['public_share_count'] = public_shares.count()
        queryset = annotate_collection_cards(
            Collection.objects.filter(
                author=author,
                is_public=True,
                deleted_at__isnull=True,
            ),
        ).order_by('-updated_at', '-pk')
        context['collections'] = Paginator(queryset, 12).get_page(
            request.GET.get('page')
        )
    else:
        visible_shares = apply_hidden_content_preferences(
            public_shares,
            preferences,
        )
        visible_shares = annotate_share_cards(
            visible_shares,
            request.user,
        ).order_by('-created_at', '-pk')
        context['shares'] = Paginator(visible_shares, 12).get_page(
            request.GET.get('page')
        )
        if preferences.spoiler == 'hide' or preferences.nsfw == 'hide':
            context['public_share_count'] = public_shares.count()
        else:
            context['public_share_count'] = context['shares'].paginator.count
        context['share_cards_return_url'] = build_share_cards_return_url(
            request,
            page_number=context['shares'].number,
        )
    return render(request, 'shares/user_public_profile.html', context)


def announcement_list(request):
    queryset = Announcement.objects.all()
    if not is_moderator(request.user):
        queryset = queryset.filter(is_active=True)
    announcements = Paginator(queryset.order_by('-created_at'), 10).get_page(
        request.GET.get('page')
    )
    return render(request, 'shares/announcement_list.html', {'announcements': announcements})


@user_passes_test(is_moderator)
@require_POST
def toggle_announcement_visibility(request, announcement_id):
    requested_state = request.POST.get('is_active')
    if requested_state not in {'0', '1'}:
        messages.error(request, '无效的站点动态状态')
        return redirect('announcement_list')
    with transaction.atomic():
        announcement = get_object_or_404(
            Announcement.objects.select_for_update(),
            id=announcement_id,
        )
        is_active = requested_state == '1'
        if announcement.is_active != is_active:
            announcement.is_active = is_active
            announcement.save(update_fields=['is_active', 'updated_at'])
    status = '激活' if announcement.is_active else '隐藏'
    messages.success(request, f'站点动态 "{announcement.title}" 已{status}')
    return redirect('announcement_list')


def about(request):
    return render(request, 'about.html')


def page_not_found(request, exception):
    return render(request, '404.html', status=404)
