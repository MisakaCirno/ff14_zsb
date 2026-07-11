from functools import wraps
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme

from shares.presentation import is_htmx_request


def _get_login_return_url(request):
    current_url = request.headers.get('HX-Current-URL')
    if not current_url or not url_has_allowed_host_and_scheme(
        current_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return request.get_full_path()

    parsed = urlsplit(current_url)
    return_url = parsed.path or '/'
    if parsed.query:
        return_url = f'{return_url}?{parsed.query}'
    return return_url


def login_required_or_hx_redirect(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)

        is_htmx = is_htmx_request(request)
        return_url = _get_login_return_url(request) if is_htmx else request.get_full_path()
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
