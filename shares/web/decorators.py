from functools import wraps

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponse
from django.shortcuts import resolve_url
from django.utils.cache import add_never_cache_headers

from shares.presentation import is_htmx_request


def login_required_or_hx_redirect(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)

        redirect_response = redirect_to_login(
            request.get_full_path(),
            resolve_url(settings.LOGIN_URL),
            REDIRECT_FIELD_NAME,
        )
        if not is_htmx_request(request):
            return redirect_response

        response = HttpResponse(status=204)
        response.headers['HX-Redirect'] = redirect_response.headers['Location']
        add_never_cache_headers(response)
        return response

    return wrapper
