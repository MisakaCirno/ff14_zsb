"""
URL configuration for ffxivshare project.
"""
from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
import urllib.request
import urllib.error

# 自定义管理后台标题
admin.site.site_header = '粘鼠板儿管理后台'
admin.site.site_title = '管理后台'
admin.site.index_title = '欢迎使用管理后台'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('shares.urls')), # Include shares app URLs
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    def proxy_view(request, path):
        target_url = f'https://ff14hub.com/n/{path}'
        if request.META.get('QUERY_STRING'):
            target_url += '?' + request.META.get('QUERY_STRING')
            
        try:
            req = urllib.request.Request(target_url)
            # 转发部分 Headers
            for header in ['HTTP_USER_AGENT', 'HTTP_ACCEPT']:
                if header in request.META:
                    key = header[5:].replace('_', '-').title()
                    req.add_header(key, request.META[header])
            
            with urllib.request.urlopen(req) as response:
                content = response.read()
                return HttpResponse(
                    content,
                    status=response.status,
                    content_type=response.headers.get('Content-Type')
                )
        except urllib.error.HTTPError as e:
            return HttpResponse(e.read(), status=e.code, content_type=e.headers.get('Content-Type'))
        except Exception as e:
            return HttpResponse(f"Proxy Error: {str(e)}", status=500)

    urlpatterns += [
        re_path(r'^n/(?P<path>.*)$', proxy_view),
    ]

handler404 = 'shares.web.browse.page_not_found'
