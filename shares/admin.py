from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction

from .admin_forms import AnnouncementAdminForm, ShareAdminForm, UserProfileAdminForm
from .models import Share, UserProfile, Announcement, Report, SiteMessage


def _lock_admin_object(model, object_id):
    try:
        return (
            model._default_manager.select_for_update()
            .filter(pk=unquote(object_id))
            .first()
        )
    except (TypeError, ValueError, ValidationError):
        return None


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    form = AnnouncementAdminForm
    list_display = ['title', 'is_active', 'created_at', 'updated_at']
    list_filter = ['is_active', 'created_at']
    search_fields = ['title', 'content']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(Share)
class ShareAdmin(admin.ModelAdmin):
    form = ShareAdminForm
    list_display = ['title', 'share_id', 'get_author_display', 'visibility', 'status', 'restriction_state', 'views', 'copies', 'created_at']
    list_filter = ['visibility', 'status', 'restriction_state', 'created_at', 'author']
    search_fields = ['title', 'share_id', 'description', 'author__username', 'author__profile__nickname']
    readonly_fields = [
        'share_id',
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by',
        'restriction_state',
        'restriction_reason',
        'restricted_at',
        'restricted_by',
        'created_at',
        'updated_at',
        'views',
        'copies',
    ]
    date_hierarchy = 'created_at'
    list_per_page = 20
    
    fieldsets = (
        ('基本信息', {
            'fields': ('title', 'author', 'share_id', 'visibility', 'status')
        }),
        ('审核信息', {
            'fields': ('review_feedback', 'reviewed_at', 'reviewed_by')
        }),
        ('活动限制（请通过审核中心操作）', {
            'fields': (
                'restriction_state',
                'restriction_reason',
                'restricted_at',
                'restricted_by',
            )
        }),
        ('内容', {
            'fields': ('strategy_code', 'description')
        }),
        ('统计信息', {
            'fields': ('views', 'copies', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_author_display(self, obj):
        """显示作者昵称或用户名"""
        if obj.author:
            profile = getattr(obj.author, 'profile', None)
            return profile.get_display_name() if profile else obj.author.username
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
    form = UserProfileAdminForm
    can_delete = False
    extra = 0
    max_num = 1
    verbose_name = '用户资料'
    verbose_name_plural = '用户资料'
    fields = ['nickname', 'bio', 'version']

    def has_add_permission(self, request, obj=None):
        return False


class UserAdmin(BaseUserAdmin):
    """扩展用户管理"""
    inlines = [UserProfileInline]
    list_display = ['username', 'get_nickname', 'email', 'is_staff', 'is_active', 'date_joined']
    list_filter = ['is_staff', 'is_active', 'date_joined']
    search_fields = ['username', 'email', 'profile__nickname']

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST' and object_id is not None:
            with transaction.atomic():
                user = _lock_admin_object(User, object_id)
                if user is not None:
                    UserProfile.objects.select_for_update().filter(
                        user_id=user.pk,
                    ).first()
                return super().changeform_view(
                    request,
                    object_id,
                    form_url,
                    extra_context,
                )
        return super().changeform_view(
            request,
            object_id,
            form_url,
            extra_context,
        )
    
    def get_nickname(self, obj):
        """显示昵称"""
        return obj.profile.nickname if hasattr(obj, 'profile') and obj.profile.nickname else '-'
    get_nickname.short_description = '昵称'


# 重新注册 User 模型
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    form = UserProfileAdminForm
    list_display = ['user', 'nickname', 'get_share_count', 'created_at', 'updated_at']
    search_fields = ['user__username', 'nickname', 'bio']
    list_filter = ['created_at']
    readonly_fields = ['user', 'created_at', 'updated_at']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        if request.method == 'POST' and object_id is not None:
            with transaction.atomic():
                _lock_admin_object(UserProfile, object_id)
                return super().changeform_view(
                    request,
                    object_id,
                    form_url,
                    extra_context,
                )
        return super().changeform_view(
            request,
            object_id,
            form_url,
            extra_context,
        )
    
    fieldsets = (
        ('用户信息', {
            'fields': ('user',)
        }),
        ('个人资料', {
            'fields': ('nickname', 'bio', 'version')
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


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ['share', 'reporter', 'status', 'created_at', 'resolved_at', 'resolved_by']
    list_filter = ['status', 'created_at', 'resolved_at']
    search_fields = ['share__title', 'share__share_id', 'reporter__username', 'reason', 'resolution_reason']
    readonly_fields = [
        'share',
        'reporter',
        'reason',
        'status',
        'created_at',
        'resolved_at',
        'resolved_by',
        'resolution_reason',
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SiteMessage)
class SiteMessageAdmin(admin.ModelAdmin):
    list_display = ['title', 'recipient', 'message_type', 'created_at', 'read_at']
    list_filter = ['message_type', 'created_at', 'read_at']
    search_fields = ['title', 'content', 'recipient__username', 'sender__username']
    readonly_fields = [
        'recipient',
        'sender',
        'message_type',
        'title',
        'content',
        'related_share',
        'related_report',
        'metadata',
        'created_at',
        'read_at',
        'archived_at',
    ]
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
