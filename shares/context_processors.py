from .models import Share, Report
from django.db.models import Count, Q

def admin_counts(request):
    """
    上下文处理器：为每个请求提供管理员待办事项计数
    """
    if request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser):
        pending_reviews_count = Share.objects.filter(status=Share.Status.PENDING).count()
        
        pending_reports_count = Share.objects.annotate(
            pending_count=Count('reports', filter=Q(reports__status=Report.Status.PENDING))
        ).filter(pending_count__gt=0).count()
        
        return {
            'global_pending_reviews_count': pending_reviews_count,
            'global_pending_reports_count': pending_reports_count,
            'has_admin_actions': pending_reviews_count + pending_reports_count > 0
        }
    return {}
