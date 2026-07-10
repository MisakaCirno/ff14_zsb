from django.db.models import Count, Exists, OuterRef, Q

from .models import Report, Share, SiteMessage


def annotate_share_cards(queryset, user):
    """Add card statistics and current-user interaction state."""
    queryset = queryset.select_related('author', 'author__profile').annotate(
        likes_count=Count('likes', distinct=True),
        favorites_count=Count('favorites', distinct=True),
    )
    if user.is_authenticated:
        queryset = queryset.annotate(
            is_liked=Exists(
                Share.likes.through.objects.filter(
                    share_id=OuterRef('pk'),
                    user_id=user.id,
                )
            ),
            is_favorited=Exists(
                Share.favorites.through.objects.filter(
                    share_id=OuterRef('pk'),
                    user_id=user.id,
                )
            ),
        )
    return queryset


def unread_site_message_count(user):
    if not user.is_authenticated:
        return 0
    return SiteMessage.objects.filter(
        recipient=user,
        read_at__isnull=True,
        archived_at__isnull=True,
    ).count()


def admin_task_counts():
    return {
        'pending_reviews_count': Share.objects.filter(
            status=Share.Status.PENDING,
        ).count(),
        'pending_reports_count': Share.objects.annotate(
            pending_count=Count(
                'reports',
                filter=Q(reports__status=Report.Status.PENDING),
            )
        ).filter(pending_count__gt=0).count(),
    }
