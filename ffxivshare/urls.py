"""
URL configuration for ffxivshare project.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

# 自定义管理后台标题
admin.site.site_header = 'FFXIV 战术板分享平台管理后台'
admin.site.site_title = '管理后台'
admin.site.index_title = '欢迎使用管理后台'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('shares.urls')), # Include shares app URLs
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
