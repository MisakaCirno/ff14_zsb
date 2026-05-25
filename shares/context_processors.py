from .models import Share, Report, SiteMessage
from django.db.models import Count, Q

def admin_counts(request):
    """
    上下文处理器：提供站内信未读数和管理员待办事项计数
    """
    context = {}

    if request.user.is_authenticated:
        unread_messages_count = SiteMessage.objects.filter(
            recipient=request.user,
            read_at__isnull=True,
            archived_at__isnull=True,
        ).count()
        context.update({
            'global_unread_site_messages_count': unread_messages_count,
            'has_user_notifications': unread_messages_count > 0,
        })

    if request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser):
        pending_reviews_count = Share.objects.filter(status=Share.Status.PENDING).count()
        
        pending_reports_count = Share.objects.annotate(
            pending_count=Count('reports', filter=Q(reports__status=Report.Status.PENDING))
        ).filter(pending_count__gt=0).count()
        
        context.update({
            'global_pending_reviews_count': pending_reviews_count,
            'global_pending_reports_count': pending_reports_count,
            'has_admin_actions': pending_reviews_count + pending_reports_count > 0
        })

    return context
