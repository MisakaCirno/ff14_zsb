from dataclasses import dataclass
from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, resolve_url
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.cache import add_never_cache_headers

from .models import Share, UserProfile
from .policies import is_moderator, is_owner
from .preview_urls import build_board_preview_url


@dataclass(frozen=True, slots=True)
class UserPresentation:
    display_name: str
    bio: str
    is_anonymous: bool


def build_user_presentation(user):
    if user is None:
        return UserPresentation(
            display_name='匿名用户',
            bio='',
            is_anonymous=True,
        )

    username = user.get_username()
    try:
        profile = user.profile
    except UserProfile.DoesNotExist:
        return UserPresentation(
            display_name=username,
            bio='',
            is_anonymous=False,
        )
    return UserPresentation(
        display_name=profile.nickname or username,
        bio=profile.bio,
        is_anonymous=False,
    )


@dataclass(frozen=True, slots=True)
class ShareDetailAuthorViewModel:
    display_name: str
    username: str | None
    profile_url: str | None
    bio: str
    is_anonymous: bool


@dataclass(frozen=True, slots=True)
class ShareDetailBadgeViewModel:
    key: str
    label: str
    tone: str
    icon: str


@dataclass(frozen=True, slots=True)
class ShareDetailNoticeViewModel:
    key: str
    title: str
    message: str
    tone: str
    icon: str
    feedback: str = ''
    feedback_label: str = '审核反馈'


@dataclass(frozen=True, slots=True)
class ShareDetailContentWarningViewModel:
    key: str
    title: str
    message: str
    tone: str
    icon: str


@dataclass(frozen=True, slots=True)
class ShareDetailActionsViewModel:
    can_edit: bool
    can_delete: bool
    can_add_to_collection: bool
    can_report: bool
    can_view_logs: bool


@dataclass(frozen=True, slots=True)
class ShareDetailViewModel:
    author: ShareDetailAuthorViewModel
    badges: tuple[ShareDetailBadgeViewModel, ...]
    notice: ShareDetailNoticeViewModel | None
    content_warning: ShareDetailContentWarningViewModel | None
    actions: ShareDetailActionsViewModel
    likes_count: int
    favorites_count: int
    is_liked: bool
    is_favorited: bool
    preview_url: str


def _share_detail_author(share):
    if share.author_id is None:
        return ShareDetailAuthorViewModel(
            display_name='匿名用户',
            username=None,
            profile_url=None,
            bio='',
            is_anonymous=True,
        )

    author = share.author
    author_presentation = build_user_presentation(author)
    return ShareDetailAuthorViewModel(
        display_name=author_presentation.display_name,
        username=author.username,
        profile_url=reverse('user_public_profile', args=[author.username]),
        bio=author_presentation.bio,
        is_anonymous=False,
    )


def _share_detail_badges(share, *, can_see_rejected_state):
    category_badges = {
        Share.Category.COMBAT: ShareDetailBadgeViewModel(
            key='combat',
            label=Share.Category.COMBAT.label,
            tone='danger',
            icon='bi bi-shield-fill',
        ),
        Share.Category.ENTERTAINMENT: ShareDetailBadgeViewModel(
            key='entertainment',
            label=Share.Category.ENTERTAINMENT.label,
            tone='success',
            icon='bi bi-controller',
        ),
    }
    badges = [category_badges[share.category]]
    if share.is_original:
        badges.append(ShareDetailBadgeViewModel(
            key='original',
            label='原创',
            tone='primary',
            icon='bi bi-patch-check-fill',
        ))
    if share.visibility == Share.Visibility.UNLISTED:
        badges.append(ShareDetailBadgeViewModel(
            key='unlisted',
            label='不公开',
            tone='warning',
            icon='bi bi-link-45deg',
        ))
    elif share.visibility == Share.Visibility.PRIVATE:
        badges.append(ShareDetailBadgeViewModel(
            key='private',
            label='私有',
            tone='danger',
            icon='bi bi-lock-fill',
        ))
    if share.is_restricted and can_see_rejected_state:
        restriction_labels = {
            Share.RestrictionState.REPORT_TAKEDOWN: '举报下架',
            Share.RestrictionState.REVIEW_REJECTED: '审核限制',
            Share.RestrictionState.LEGACY_PRIVATE: '历史状态待确认',
        }
        badges.append(ShareDetailBadgeViewModel(
            key='restricted',
            label=restriction_labels[share.restriction_state],
            tone='danger',
            icon='bi bi-shield-lock-fill',
        ))
    if (
        share.status == Share.Status.PENDING
        and share.visibility == Share.Visibility.PUBLIC
    ):
        badges.append(ShareDetailBadgeViewModel(
            key='pending',
            label='待审核',
            tone='warning',
            icon='bi bi-hourglass-split',
        ))
    elif share.status == Share.Status.REJECTED and can_see_rejected_state:
        badges.append(ShareDetailBadgeViewModel(
            key='rejected',
            label='审核未通过',
            tone='danger',
            icon='bi bi-x-circle-fill',
        ))
    return tuple(badges)


def _share_detail_notice(share, *, can_see_rejected_state):
    if share.is_restricted and can_see_rejected_state:
        report_takedown = (
            share.restriction_state == Share.RestrictionState.REPORT_TAKEDOWN
        )
        legacy_private = (
            share.restriction_state == Share.RestrictionState.LEGACY_PRIVATE
        )
        if report_takedown:
            title = '分享已被下架'
            message = (
                '此分享因举报处理被限制访问。编辑不会自动解除限制；'
                '管理员复核并解除后，分享仍会按照当前可见范围开放。'
            )
            tone = 'danger'
        elif legacy_private:
            title = '历史私密状态待确认'
            message = (
                '为避免旧版下架记录缺失导致内容被意外重新开放，此分享暂时保持限制。'
                '管理员完成来源确认后，分享仍会按照当前可见范围开放。'
            )
            tone = 'warning'
        else:
            title = '分享受到审核限制'
            message = (
                '你可以继续编辑此分享；修改后会重新进入审核。'
                '管理员通过后，分享仍会按照当前可见范围开放。'
            )
            tone = 'danger'
        return ShareDetailNoticeViewModel(
            key=share.restriction_state,
            title=title,
            message=message,
            tone=tone,
            icon='bi bi-shield-lock-fill',
            feedback=share.restriction_reason,
            feedback_label='限制原因',
        )
    if (
        share.status == Share.Status.PENDING
        and share.visibility == Share.Visibility.PUBLIC
    ):
        return ShareDetailNoticeViewModel(
            key='pending',
            title='待审核',
            message=(
                '此分享正在等待管理员审核。在审核通过前，它不会出现在公开列表中，'
                '但仍可通过链接访问。'
            ),
            tone='warning',
            icon='bi bi-exclamation-triangle-fill',
        )
    if share.status == Share.Status.REJECTED and can_see_rejected_state:
        return ShareDetailNoticeViewModel(
            key='rejected',
            title='审核未通过',
            message=(
                '此分享未通过审核，请根据反馈修改后重新提交。'
                if share.review_feedback else
                '此分享未通过审核，请修改内容后重新提交。'
            ),
            tone='danger',
            icon='bi bi-x-circle-fill',
            feedback=share.review_feedback,
        )
    return None


def _share_detail_content_warning(share):
    if share.is_nsfw and share.is_spoiler:
        return ShareDetailContentWarningViewModel(
            key='nsfw-spoiler',
            title='内容已隐藏',
            message='此分享可能包含令人不适和剧透内容。',
            tone='danger',
            icon='bi bi-exclamation-diamond-fill',
        )
    if share.is_nsfw:
        return ShareDetailContentWarningViewModel(
            key='nsfw',
            title='内容已隐藏',
            message='此分享可能包含令人不适的内容。',
            tone='danger',
            icon='bi bi-exclamation-diamond-fill',
        )
    if share.is_spoiler:
        return ShareDetailContentWarningViewModel(
            key='spoiler',
            title='内容已隐藏',
            message='此分享可能包含剧透内容。',
            tone='warning',
            icon='bi bi-eye-slash-fill',
        )
    return None


def build_share_detail_view_model(share, user):
    """Build the permission-aware, query-free presentation state for one share."""
    owner = is_owner(user, share)
    moderator = is_moderator(user)
    authenticated = bool(user and user.is_authenticated)
    can_see_rejected_state = owner or moderator
    return ShareDetailViewModel(
        author=_share_detail_author(share),
        badges=_share_detail_badges(
            share,
            can_see_rejected_state=can_see_rejected_state,
        ),
        notice=_share_detail_notice(
            share,
            can_see_rejected_state=can_see_rejected_state,
        ),
        content_warning=_share_detail_content_warning(share),
        actions=ShareDetailActionsViewModel(
            can_edit=owner,
            can_delete=owner or moderator,
            can_add_to_collection=owner,
            can_report=(
                authenticated
                and not owner
                and not share.is_restricted
            ),
            can_view_logs=moderator,
        ),
        likes_count=share.likes_count,
        favorites_count=share.favorites_count,
        is_liked=share.is_liked,
        is_favorited=share.is_favorited,
        preview_url=build_board_preview_url(share.strategy_code),
    )


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


def build_share_cards_return_url(request, *, page_number):
    params = request.GET.copy()
    params.pop('partial', None)
    params.pop('continuation', None)
    transport_request = (
        is_htmx_request(request)
        or request.GET.get('partial') == 'shares'
        or request.GET.get('continuation') == '1'
        or request.GET.get('feed') == UserProfile.HomeFeedMode.INFINITE
    )
    if transport_request or page_number <= 1:
        params.pop('page', None)
    else:
        params['page'] = str(page_number)
    query = params.urlencode()
    return f'{request.path}?{query}' if query else request.path


def build_my_reaction_return_url(*, tab, page_number):
    """Build a canonical return target for reaction cards in My Content."""
    if tab not in {'likes', 'favorites'}:
        raise ValueError('Unsupported reaction tab.')
    page_number = max(1, int(page_number))
    query = urlencode({'tab': tab, 'page': page_number})
    return f"{reverse('my_shares')}?{query}"


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
        'share_cards_return_url': build_share_cards_return_url(
            request,
            page_number=shares.number,
        ),
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
