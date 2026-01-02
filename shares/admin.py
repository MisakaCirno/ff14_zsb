from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from .models import Share, UserProfile, Announcement


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ['title', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['title', 'content']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Share)
class ShareAdmin(admin.ModelAdmin):
    list_display = ['title', 'share_id', 'get_author_display', 'visibility', 'views', 'created_at']
    list_filter = ['visibility', 'created_at', 'author']
    search_fields = ['title', 'share_id', 'description', 'author__username', 'author__profile__nickname']
    readonly_fields = ['share_id', 'created_at', 'updated_at', 'views']
    date_hierarchy = 'created_at'
    list_per_page = 20
    
    fieldsets = (
        ('基本信息', {
            'fields': ('title', 'author', 'share_id', 'visibility')
        }),
        ('内容', {
            'fields': ('strategy_code', 'description')
        }),
        ('统计信息', {
            'fields': ('views', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_author_display(self, obj):
        """显示作者昵称或用户名"""
        if obj.author:
            return obj.author.profile.get_display_name()
        return "匿名用户"
    get_author_display.short_description = '作者'
    get_author_display.admin_order_field = 'author__username'
    
    actions = ['make_public', 'make_private']
    
    def make_public(self, request, queryset):
        """批量设为公开"""
        updated = queryset.update(is_public=True)
        self.message_user(request, f'已将 {updated} 个分享设为公开')
    make_public.short_description = '设为公开'
    
    def make_private(self, request, queryset):
        """批量设为私有"""
        updated = queryset.update(is_public=False)
        self.message_user(request, f'已将 {updated} 个分享设为私有')
    make_private.short_description = '设为私有'


class UserProfileInline(admin.StackedInline):
    """用户资料内联编辑"""
    model = UserProfile
    can_delete = False
    verbose_name = '用户资料'
    verbose_name_plural = '用户资料'
    fields = ['nickname', 'bio']


class UserAdmin(BaseUserAdmin):
    """扩展用户管理"""
    inlines = [UserProfileInline]
    list_display = ['username', 'get_nickname', 'email', 'is_staff', 'is_active', 'date_joined']
    list_filter = ['is_staff', 'is_active', 'date_joined']
    search_fields = ['username', 'email', 'profile__nickname']
    
    def get_nickname(self, obj):
        """显示昵称"""
        return obj.profile.nickname if hasattr(obj, 'profile') and obj.profile.nickname else '-'
    get_nickname.short_description = '昵称'


# 重新注册 User 模型
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'nickname', 'get_share_count', 'created_at', 'updated_at']
    search_fields = ['user__username', 'nickname', 'bio']
    list_filter = ['created_at']
    readonly_fields = ['created_at', 'updated_at']
    
    fieldsets = (
        ('用户信息', {
            'fields': ('user',)
        }),
        ('个人资料', {
            'fields': ('nickname', 'bio')
        }),
        ('时间信息', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_share_count(self, obj):
        """显示分享数量"""
        return obj.user.shares.count()
    get_share_count.short_description = '分享数量'
