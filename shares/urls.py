from django.urls import path, re_path
from . import views
from .web import accounts, messages as message_views

urlpatterns = [
    # 主页
    path('', views.index, name='index'),
    path('preferences/feed-mode/', views.set_home_feed_mode, name='set_home_feed_mode'),
    
    # 分享相关
    re_path(r'^s/(?P<share_id>[^/]+)(?:/.*)?$', views.share_detail, name='share_detail'),
    path('create/', views.create_share, name='create_share'),
    path('share/<str:share_id>/edit/', views.edit_share, name='edit_share'),
    path('share/<str:share_id>/delete/', views.delete_share, name='delete_share'),
    path('my-shares/', views.my_shares, name='my_shares'),
    path('search/', views.search, name='search'),
    
    # 用户认证
    path('register/', accounts.register, name='register'),
    path('login/', accounts.user_login, name='login'),
    path('logout/', accounts.user_logout, name='logout'),
    
    # 用户资料
    path('u/<str:username>/', views.user_public_profile, name='user_public_profile'),
    path('profile/edit/', accounts.profile_edit, name='profile_edit'),
    path('profile/password/', accounts.password_change, name='password_change'),
    path('messages/', message_views.site_message_list, name='site_message_list'),
    path('messages/mark-all-read/', message_views.mark_all_site_messages_read, name='mark_all_site_messages_read'),
    path('messages/<int:message_id>/open/', message_views.open_site_message, name='open_site_message'),
    path('messages/<int:message_id>/', message_views.site_message_detail, name='site_message_detail'),
    
    # 其他
    path('about/', views.about, name='about'),
    path('announcements/', views.announcement_list, name='announcement_list'),
    path('announcements/<int:announcement_id>/toggle/', views.toggle_announcement_visibility, name='toggle_announcement_visibility'),
    
    # 管理员审核 (使用 staff 前缀避免与 Django Admin 冲突)
    path('staff/reviews/', views.admin_review_list, name='admin_review_list'),
    path('staff/reviews/logs/', views.admin_review_logs, name='admin_review_logs'),
    path('staff/reviews/<str:share_id>/approve/', views.admin_approve_share, name='admin_approve_share'),
    path('staff/reviews/<str:share_id>/reject/', views.admin_reject_share, name='admin_reject_share'),
    
    # 举报处理
    path('share/<str:share_id>/report/', views.report_share, name='report_share'),
    path('staff/reports/', views.admin_report_list, name='admin_report_list'),
    path('staff/reports/logs/', views.admin_report_logs, name='admin_report_logs'),
    path('staff/reports/<int:report_id>/<str:action>/', views.admin_resolve_report, name='admin_resolve_report'),

    path('staff/reports/share/<str:share_id>/<str:action>/', views.admin_resolve_share_reports, name='admin_resolve_share_reports'),
    
    # 合集相关
    path('collections/create/', views.create_collection, name='create_collection'),
    path('collections/<int:collection_id>/', views.collection_detail, name='collection_detail'),
    path('collections/<int:collection_id>/edit/', views.edit_collection, name='edit_collection'),
    path('collections/<int:collection_id>/delete/', views.delete_collection, name='delete_collection'),
    path('share/<str:share_id>/add-to-collection/', views.add_share_to_collection, name='add_share_to_collection'),
    path('share/<str:share_id>/like/', views.toggle_like, name='toggle_like'),
    path('share/<str:share_id>/favorite/', views.toggle_favorite, name='toggle_favorite'),
    path('share/<str:share_id>/view/', views.record_view, name='record_view'),
    path('share/<str:share_id>/copy/', views.record_copy, name='record_copy'),
    path('collections/<int:collection_id>/remove-share/<str:share_id>/', views.remove_share_from_collection, name='remove_share_from_collection'),
    
    # API
    path('api/share/<str:share_id>/code/', views.get_share_code, name='get_share_code'),
    path('api/collection/<int:collection_id>/codes/', views.get_collection_codes, name='get_collection_codes'),
]
