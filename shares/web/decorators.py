from functools import wraps
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme

from shares.presentation import is_htmx_request


def get_safe_local_return_url(request, candidate):
    """Return a normalized site-local URL, or ``None`` when it is unsafe."""
    if (
        not isinstance(candidate, str)
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
    ):
        return None

    candidate = candidate.strip()
    if not candidate or '\\' in candidate:
        return None

    try:
        is_allowed = url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if not is_allowed:
        return None
    if parsed.scheme or parsed.netloc:
        # Do not accept protocol-relative URLs: an explicit scheme is required
        # before an absolute same-origin URL is normalized to a local path.
        if not parsed.scheme or not parsed.netloc:
            return None
    elif not candidate.startswith('/') or candidate.startswith('//'):
        return None

    path = parsed.path or '/'
    if not path.startswith('/') or path.startswith('//'):
        return None
    return urlunsplit(('', '', path, parsed.query, parsed.fragment))


def _get_share_detail_return_url(view_kwargs):
    share_id = view_kwargs.get('share_id')
    if share_id is None:
        return None
    return reverse('share_detail', kwargs={'share_id': share_id})


def _get_login_return_url(request, view_kwargs):
    for candidate in (
        request.headers.get('HX-Current-URL'),
        request.POST.get(REDIRECT_FIELD_NAME),
        request.headers.get('Referer'),
    ):
        return_url = get_safe_local_return_url(request, candidate)
        if return_url:
            return return_url
    return _get_share_detail_return_url(view_kwargs) or '/'


def _get_form_login_return_url(request, view_kwargs):
    if REDIRECT_FIELD_NAME not in request.POST:
        return request.get_full_path()

    for candidate in (
        request.POST.get(REDIRECT_FIELD_NAME),
        request.headers.get('Referer'),
    ):
        return_url = get_safe_local_return_url(request, candidate)
        if return_url:
            return return_url

    return _get_share_detail_return_url(view_kwargs) or '/'


def login_required_or_hx_redirect(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)

        is_htmx = is_htmx_request(request)
        return_url = (
            _get_login_return_url(request, kwargs)
            if is_htmx
            else _get_form_login_return_url(request, kwargs)
        )
        redirect_response = redirect_to_login(
            return_url,
            resolve_url(settings.LOGIN_URL),
            REDIRECT_FIELD_NAME,
        )
        if not is_htmx:
            return redirect_response

        response = HttpResponse(status=204)
        response.headers['HX-Redirect'] = redirect_response.headers['Location']
        add_never_cache_headers(response)
        return response

    return wrapper
