from .policies import is_moderator
from .selectors import admin_task_counts, unread_site_message_count

def admin_counts(request):
    """
    上下文处理器：提供站内信未读数和管理员待办事项计数
    """
    context = {}

    if request.user.is_authenticated:
        unread_messages_count = unread_site_message_count(request.user)
        context.update({
            'global_unread_site_messages_count': unread_messages_count,
            'has_user_notifications': unread_messages_count > 0,
        })

    if is_moderator(request.user):
        counts = admin_task_counts()
        pending_reviews_count = counts['pending_reviews_count']
        restricted_shares_count = counts['restricted_shares_count']
        pending_reports_count = counts['pending_reports_count']
        context.update({
            'pending_reviews_count': pending_reviews_count,
            'restricted_shares_count': restricted_shares_count,
            'pending_reports_count': pending_reports_count,
            'global_pending_reviews_count': pending_reviews_count,
            'global_restricted_shares_count': restricted_shares_count,
            'global_pending_reports_count': pending_reports_count,
            'has_admin_actions': pending_reviews_count + pending_reports_count > 0
        })

    return context
