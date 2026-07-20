from django.urls import path, re_path
from .web import (
    accounts,
    api,
    browse,
    collections,
    content,
    interactions,
    messages as message_views,
    moderation,
)

urlpatterns = [
    # 主页
    path('', browse.index, name='index'),
    path('preferences/feed-mode/', browse.set_home_feed_mode, name='set_home_feed_mode'),
    
    # 分享相关
    re_path(r'^s/(?P<share_id>[^/]+)(?:/.*)?$', content.share_detail, name='share_detail'),
    path('create/', content.create_share, name='create_share'),
    path('share/<str:share_id>/edit/', content.edit_share, name='edit_share'),
    path('share/<str:share_id>/delete/', content.delete_share, name='delete_share'),
    path('share/<str:share_id>/restore/', content.restore_share, name='restore_share'),
    path('my-shares/', content.my_shares, name='my_shares'),
    path('search/', browse.search, name='search'),
    
    # 用户认证
    path('register/', accounts.register, name='register'),
    path('login/', accounts.user_login, name='login'),
    path('logout/', accounts.user_logout, name='logout'),
    
    # 用户资料
    path('u/<str:username>/', browse.user_public_profile, name='user_public_profile'),
    path('profile/edit/', accounts.profile_edit, name='profile_edit'),
    path('profile/password/', accounts.password_change, name='password_change'),
    path('messages/', message_views.site_message_list, name='site_message_list'),
    path('messages/mark-all-read/', message_views.mark_all_site_messages_read, name='mark_all_site_messages_read'),
    path('messages/<int:message_id>/open/', message_views.open_site_message, name='open_site_message'),
    path('messages/<int:message_id>/archive/', message_views.set_site_message_archive_state, name='set_site_message_archive_state'),
    path('messages/<int:message_id>/', message_views.site_message_detail, name='site_message_detail'),
    
    # 其他
    path('about/', browse.about, name='about'),
    path('announcements/', browse.announcement_list, name='announcement_list'),
    path('announcements/<int:announcement_id>/toggle/', browse.toggle_announcement_visibility, name='toggle_announcement_visibility'),
    
    # 管理员审核 (使用 staff 前缀避免与 Django Admin 冲突)
    path('staff/reviews/', moderation.admin_review_list, name='admin_review_list'),
    path('staff/reviews/logs/', moderation.admin_review_logs, name='admin_review_logs'),
    path('staff/reviews/<str:share_id>/approve/', moderation.admin_approve_share, name='admin_approve_share'),
    path('staff/reviews/<str:share_id>/reject/', moderation.admin_reject_share, name='admin_reject_share'),
    path('staff/restrictions/<str:share_id>/confirm/', moderation.admin_confirm_share_restriction, name='admin_confirm_share_restriction'),
    path('staff/restrictions/<str:share_id>/release/', moderation.admin_release_share_restriction, name='admin_release_share_restriction'),
    
    # 举报处理
    path('share/<str:share_id>/report/', moderation.report_share, name='report_share'),
    path('staff/reports/', moderation.admin_report_list, name='admin_report_list'),
    path('staff/reports/logs/', moderation.admin_report_logs, name='admin_report_logs'),
    path('staff/reports/share/<str:share_id>/', moderation.admin_report_share, name='admin_report_share'),
    path('staff/reports/<int:report_id>/<str:action>/', moderation.admin_resolve_report, name='admin_resolve_report'),

    path('staff/reports/share/<str:share_id>/<str:action>/', moderation.admin_resolve_share_reports, name='admin_resolve_share_reports'),
    
    # 合集相关
    path('collections/create/', collections.create_collection, name='create_collection'),
    path('collections/<int:collection_id>/', collections.collection_detail, name='collection_detail'),
    path('collections/<int:collection_id>/edit/', collections.edit_collection, name='edit_collection'),
    path('collections/<int:collection_id>/delete/', collections.delete_collection, name='delete_collection'),
    path('collections/<int:collection_id>/restore/', collections.restore_collection, name='restore_collection'),
    path('share/<str:share_id>/collections/', collections.select_collection_for_share, name='select_collection_for_share'),
    path('share/<str:share_id>/add-to-collection/', collections.add_share_to_collection, name='add_share_to_collection'),
    path('share/<str:share_id>/like/', interactions.toggle_like, name='toggle_like'),
    path('share/<str:share_id>/favorite/', interactions.toggle_favorite, name='toggle_favorite'),
    path('share/<str:share_id>/view/', interactions.record_view, name='record_view'),
    path('share/<str:share_id>/copy/', interactions.record_copy, name='record_copy'),
    path('collections/<int:collection_id>/remove-share/<str:share_id>/', collections.remove_share_from_collection, name='remove_share_from_collection'),
    
    # API
    path('api/share/<str:share_id>/code/', api.get_share_code, name='get_share_code'),
    path('api/collection/<int:collection_id>/codes/', api.get_collection_codes, name='get_collection_codes'),
]
