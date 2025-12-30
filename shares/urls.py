from django.urls import path
from . import views

urlpatterns = [
    # 主页
    path('', views.index, name='index'),
    
    # 分享相关
    path('share/<str:share_id>/', views.share_detail, name='share_detail'),
    path('create/', views.create_share, name='create_share'),
    path('share/<str:share_id>/edit/', views.edit_share, name='edit_share'),
    path('share/<str:share_id>/delete/', views.delete_share, name='delete_share'),
    path('my-shares/', views.my_shares, name='my_shares'),
    
    # 用户认证
    path('register/', views.register, name='register'),
    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    
    # 用户资料
    path('profile/edit/', views.profile_edit, name='profile_edit'),
    path('profile/password/', views.password_change, name='password_change'),
    
    # API
    path('api/qr/<str:share_id>/', views.generate_qr_code, name='generate_qr'),
]
