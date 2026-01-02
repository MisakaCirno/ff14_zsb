from django.urls import path, re_path
from . import views

urlpatterns = [
    # 主页
    path('', views.index, name='index'),
    
    # 分享相关
    re_path(r'^s/(?P<share_id>[^/]+)(?:/.*)?$', views.share_detail, name='share_detail'),
    path('create/', views.create_share, name='create_share'),
    path('share/<str:share_id>/edit/', views.edit_share, name='edit_share'),
    path('share/<str:share_id>/delete/', views.delete_share, name='delete_share'),
    path('my-shares/', views.my_shares, name='my_shares'),
    path('search/', views.search, name='search'),
    
    # 用户认证
    path('register/', views.register, name='register'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    
    # 用户资料
    path('profile/edit/', views.profile_edit, name='profile_edit'),
    path('profile/password/', views.password_change, name='password_change'),
    
    # 其他
    path('about/', views.about, name='about'),
    path('announcements/', views.announcement_list, name='announcement_list'),
    path('announcements/<int:announcement_id>/toggle/', views.toggle_announcement_visibility, name='toggle_announcement_visibility'),
    
    # 管理员审核 (使用 staff 前缀避免与 Django Admin 冲突)
    path('staff/reviews/', views.admin_review_list, name='admin_review_list'),
    path('staff/reviews/<str:share_id>/approve/', views.admin_approve_share, name='admin_approve_share'),
    path('staff/reviews/<str:share_id>/reject/', views.admin_reject_share, name='admin_reject_share'),
    
    # 举报处理
    path('share/<str:share_id>/report/', views.report_share, name='report_share'),
    path('staff/reports/', views.admin_report_list, name='admin_report_list'),
    path('staff/reports/<int:report_id>/<str:action>/', views.admin_resolve_report, name='admin_resolve_report'),
    path('staff/reports/share/<str:share_id>/<str:action>/', views.admin_resolve_share_reports, name='admin_resolve_share_reports'),
]
