"""URL configuration for ffxivshare project."""
import urllib.error
import urllib.request
from urllib.parse import quote

from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from django.urls import include, path, re_path
from django.views.decorators.http import require_safe

from . import health


class RendererResponseTooLarge(Exception):
    pass


def _renderer_response(upstream, *, status, include_body=True):
    content = b''
    if include_body:
        content = upstream.read(settings.RENDERER_PROXY_MAX_BYTES + 1)
        if len(content) > settings.RENDERER_PROXY_MAX_BYTES:
            raise RendererResponseTooLarge
    content_type = upstream.headers.get('Content-Type') or 'application/octet-stream'
    response = HttpResponse(content, status=status, content_type=content_type)
    response.headers['Cache-Control'] = 'no-store'
    return response


@require_safe
def proxy_view(request, path):
    """Forward development-only renderer requests without losing URL boundaries."""
    if path.startswith('board/'):
        strategy_code = path.removeprefix('board/')
        encoded_path = f'board/{quote(strategy_code, safe="")}'
    else:
        if any(segment in {'.', '..'} for segment in path.split('/')):
            return HttpResponse('Invalid renderer path.', status=400)
        encoded_path = quote(path, safe='/')
    target_url = f'https://ff14hub.com/n/{encoded_path}'
    if request.META.get('QUERY_STRING'):
        target_url += '?' + request.META.get('QUERY_STRING')

    try:
        req = urllib.request.Request(target_url, method=request.method)
        for header in ['HTTP_USER_AGENT', 'HTTP_ACCEPT']:
            if header in request.META:
                key = header[5:].replace('_', '-').title()
                req.add_header(key, request.META[header])

        with urllib.request.urlopen(
            req,
            timeout=settings.RENDERER_PROXY_TIMEOUT_SECONDS,
        ) as response:
            return _renderer_response(
                response,
                status=response.status,
                include_body=request.method != 'HEAD',
            )
    except urllib.error.HTTPError as error:
        try:
            return _renderer_response(
                error,
                status=error.code,
                include_body=request.method != 'HEAD',
            )
        except RendererResponseTooLarge:
            return HttpResponse('Renderer response exceeded the size limit.', status=502)
    except RendererResponseTooLarge:
        return HttpResponse('Renderer response exceeded the size limit.', status=502)
    except Exception:
        return HttpResponse('Renderer proxy request failed.', status=502)

# 自定义管理后台标题
admin.site.site_header = '粘鼠板儿管理后台'
admin.site.site_title = '管理后台'
admin.site.index_title = '欢迎使用管理后台'

urlpatterns = [
    path('health/live/', health.live, name='health_live'),
    path('health/ready/', health.ready, name='health_ready'),
    path('admin/', admin.site.urls),
    path('', include('shares.urls')), # Include shares app URLs
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    urlpatterns += [
        re_path(r'^n/(?P<path>.*)$', proxy_view),
    ]

handler404 = 'shares.web.browse.page_not_found'
