import logging

from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.admin.utils import unquote
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Subquery
from django.utils import timezone

from .admin_forms import AnnouncementAdminForm, ShareAdminForm, UserProfileAdminForm
from .models import Share, ShareLog, UserProfile, Announcement, Report, SiteMessage


logger = logging.getLogger(__name__)

ADMIN_ACTION_BATCH_SIZE = 100


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

    @staticmethod
    def _visibility_change_details(visibility):
        visibility_label = {
            Share.Visibility.PUBLIC: '公开',
            Share.Visibility.PRIVATE: '私有',
        }[visibility]
        return f'Django Admin 批量操作：可见性改为“{visibility_label}”。'

    @staticmethod
    def _empty_visibility_counts():
        return {
            'updated': 0,
            'already_target': 0,
            'restricted': 0,
            'not_approved': 0,
            'selection_changed': 0,
        }

    @staticmethod
    def _selected_pk_subquery(queryset):
        # Keep the complete ChangeList filtering contract while dropping display
        # ordering. In particular, select_across passes the filtered queryset
        # rather than an explicit (and potentially unbounded) primary-key list.
        return queryset.order_by().values('pk')

    def _visibility_action_batches(self, queryset):
        """Yield stable, ascending PK batches from the live admin selection."""
        database = queryset.db
        selected_pks = self._selected_pk_subquery(queryset)
        high_water = (
            Share.objects.using(database)
            .filter(pk__in=Subquery(selected_pks))
            .order_by('-pk')
            .values_list('pk', flat=True)
            .first()
        )
        if high_water is None:
            return

        last_pk = None
        while True:
            candidates = (
                Share.objects.using(database)
                .filter(
                    pk__in=Subquery(self._selected_pk_subquery(queryset)),
                    pk__lte=high_water,
                )
            )
            if last_pk is not None:
                candidates = candidates.filter(pk__gt=last_pk)
            candidate_pks = tuple(
                candidates.order_by('pk').values_list('pk', flat=True)
                [:ADMIN_ACTION_BATCH_SIZE]
            )
            if not candidate_pks:
                return
            yield candidate_pks
            last_pk = candidate_pks[-1]

    def _apply_visibility_batch(
        self,
        request,
        queryset,
        candidate_pks,
        *,
        visibility,
    ):
        """Apply one already-bounded batch inside the caller's transaction."""
        database = queryset.db
        counts = self._empty_visibility_counts()
        locked_shares = list(
            Share.objects.using(database)
            .select_for_update()
            .only(
                'pk',
                'share_id',
                'title',
                'visibility',
                'status',
                'restriction_state',
                'updated_at',
            )
            .filter(pk__in=candidate_pks)
            .order_by('pk')
        )

        # The filter may have changed while waiting for the row locks. Re-run
        # the original admin queryset only after the candidate rows are locked.
        currently_selected_ids = set(
            Share.objects.using(database)
            .filter(pk__in=candidate_pks)
            .filter(pk__in=Subquery(self._selected_pk_subquery(queryset)))
            .values_list('pk', flat=True)
        )
        counts['selection_changed'] = (
            len(candidate_pks) - len(currently_selected_ids)
        )

        changed_shares = []
        for share in locked_shares:
            if share.pk not in currently_selected_ids:
                continue
            if visibility == Share.Visibility.PUBLIC:
                if share.restriction_state != Share.RestrictionState.CLEAR:
                    counts['restricted'] += 1
                    continue
                if share.status != Share.Status.APPROVED:
                    counts['not_approved'] += 1
                    continue
            if share.visibility == visibility:
                counts['already_target'] += 1
                continue
            changed_shares.append(share)

        if not changed_shares:
            return counts

        changed_at = timezone.now()
        changed_ids = [share.pk for share in changed_shares]
        Share.objects.using(database).filter(pk__in=changed_ids).update(
            visibility=visibility,
            updated_at=changed_at,
        )
        for share in changed_shares:
            share.visibility = visibility
            share.updated_at = changed_at

        details = self._visibility_change_details(visibility)
        ShareLog.objects.using(database).bulk_create(
            [
                ShareLog(
                    user_id=request.user.pk,
                    share_id=share.pk,
                    action=ShareLog.ActionType.EDIT,
                    details=details,
                )
                for share in changed_shares
            ],
            batch_size=ADMIN_ACTION_BATCH_SIZE,
        )
        LogEntry.objects.db_manager(database).log_actions(
            request.user.pk,
            changed_shares,
            CHANGE,
            details,
        )
        counts['updated'] = len(changed_shares)
        return counts

    def _run_visibility_action(self, request, queryset, *, visibility):
        totals = self._empty_visibility_counts()
        database = queryset.db
        batches = iter(self._visibility_action_batches(queryset))
        batch_number = 1
        while True:
            try:
                candidate_pks = next(batches)
            except StopIteration:
                return totals, None
            except Exception:
                logger.exception(
                    'Django Admin visibility batch selection failed',
                    extra={
                        'event': 'admin.visibility_batch_selection_failed',
                        'batch_number': batch_number,
                        'batch_size': 0,
                        'target_visibility': visibility,
                    },
                )
                return totals, batch_number
            try:
                with transaction.atomic(using=database):
                    batch_counts = self._apply_visibility_batch(
                        request,
                        queryset,
                        candidate_pks,
                        visibility=visibility,
                    )
            except Exception:
                logger.exception(
                    'Django Admin visibility batch failed',
                    extra={
                        'event': 'admin.visibility_batch_failed',
                        'batch_number': batch_number,
                        'batch_size': len(candidate_pks),
                        'target_visibility': visibility,
                    },
                )
                return totals, batch_number
            for name, value in batch_counts.items():
                totals[name] += value
            batch_number += 1

    @admin.action(
        permissions=['change'],
        description='设为公开（仅限审核通过且无限制）',
    )
    def make_public(self, request, queryset):
        """Publish eligible shares without changing moderation evidence."""
        totals, failed_batch = self._run_visibility_action(
            request,
            queryset,
            visibility=Share.Visibility.PUBLIC,
        )

        summary = (
            f'批量公开完成：已更新 {totals["updated"]} 个；'
            f'已是公开 {totals["already_target"]} 个；'
            f'因仍有内容限制而跳过 {totals["restricted"]} 个；'
            f'因尚未审核通过而跳过 {totals["not_approved"]} 个。'
        )
        if totals['selection_changed']:
            summary += (
                f' 处理期间有 {totals["selection_changed"]} 个项目已不再符合'
                '原筛选条件。'
            )
        if totals['restricted'] or totals['not_approved']:
            summary += ' 请先在审核中心完成审核或解除内容限制。'
        if failed_batch is not None:
            summary += (
                f' 第 {failed_batch} 批处理失败且已回滚；此前成功批次已保留，'
                '剩余项目尚未处理，可安全重试。'
            )
        self.message_user(
            request,
            summary,
            level=(
                messages.ERROR
                if failed_batch is not None
                else (
                    messages.WARNING
                    if (
                        totals['restricted']
                        or totals['not_approved']
                        or totals['selection_changed']
                    )
                    else messages.SUCCESS
                )
            ),
        )

    @admin.action(permissions=['change'], description='设为私有')
    def make_private(self, request, queryset):
        """Tighten visibility without changing moderation evidence."""
        totals, failed_batch = self._run_visibility_action(
            request,
            queryset,
            visibility=Share.Visibility.PRIVATE,
        )

        summary = (
            f'批量设为私有完成：已更新 {totals["updated"]} 个；'
            f'已是私有 {totals["already_target"]} 个。'
        )
        if totals['selection_changed']:
            summary += (
                f' 处理期间有 {totals["selection_changed"]} 个项目已不再符合'
                '原筛选条件。'
            )
        if failed_batch is not None:
            summary += (
                f' 第 {failed_batch} 批处理失败且已回滚；此前成功批次已保留，'
                '剩余项目尚未处理，可安全重试。'
            )
        self.message_user(
            request,
            summary,
            level=(
                messages.ERROR
                if failed_batch is not None
                else (
                    messages.WARNING
                    if totals['selection_changed']
                    else messages.SUCCESS
                )
            ),
        )


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
