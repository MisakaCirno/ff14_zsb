from dataclasses import dataclass

from django.db.models import (
    BooleanField,
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
    Window,
)
from django.db.models.functions import Coalesce, RowNumber

from .models import Collection, CollectionItem, Report, Share, SiteMessage
from .policies import can_view_collection, viewable_share_queryset


_RELATED_COLLECTION_PREVIEW_SIZE = 5


@dataclass(frozen=True, slots=True)
class RelatedCollectionSummary:
    collection: Collection
    visible_items: tuple[CollectionItem, ...]
    visible_item_count: int


def related_collection_summaries(share, user):
    """Build permission-filtered collection previews for a share detail page."""
    visible_share_ids = viewable_share_queryset(user).order_by().values('pk')
    contains_share = CollectionItem.objects.filter(
        collection_id=OuterRef('pk'),
        share=share,
    )
    collections = [
        collection
        for collection in Collection.objects.select_related(
            'author',
            'author__profile',
        ).annotate(
            contains_share=Exists(contains_share),
            visible_item_count=Count(
                'collectionitem',
                filter=Q(collectionitem__share_id__in=visible_share_ids),
            ),
        ).filter(contains_share=True).order_by('-updated_at')
        if can_view_collection(user, collection)
    ]
    if not collections:
        return []

    visible_items_by_collection = {
        collection.pk: []
        for collection in collections
    }
    collection_items = CollectionItem.objects.filter(
        collection_id__in=visible_items_by_collection,
        share_id__in=visible_share_ids,
    ).annotate(
        visible_position=Window(
            expression=RowNumber(),
            partition_by=[F('collection_id')],
            order_by=(
                F('order').asc(),
                F('added_at').asc(),
                F('pk').asc(),
            ),
        ),
    ).filter(
        Q(visible_position__lte=_RELATED_COLLECTION_PREVIEW_SIZE)
        | Q(share_id=share.pk),
    ).select_related(
        'share',
        'share__author',
        'share__author__profile',
    ).order_by('collection_id', 'order', 'added_at', 'pk')
    for item in collection_items:
        visible_items_by_collection[item.collection_id].append(item)

    summaries = []
    for collection in collections:
        visible_items = visible_items_by_collection[collection.pk]
        preview_items = visible_items[:_RELATED_COLLECTION_PREVIEW_SIZE]
        if not any(item.share_id == share.pk for item in preview_items):
            current_item = next(
                (item for item in visible_items if item.share_id == share.pk),
                None,
            )
            if current_item is not None:
                preview_items = [
                    *preview_items[:_RELATED_COLLECTION_PREVIEW_SIZE - 1],
                    current_item,
                ]
        summaries.append(RelatedCollectionSummary(
            collection=collection,
            visible_items=tuple(preview_items),
            visible_item_count=collection.visible_item_count,
        ))
    return summaries


def _annotate_current_user_interactions(queryset, user):
    if user.is_authenticated:
        return queryset.annotate(
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
    return queryset.annotate(
        is_liked=Value(False, output_field=BooleanField()),
        is_favorited=Value(False, output_field=BooleanField()),
    )


def annotate_share_cards(queryset, user):
    """Add card statistics and current-user interaction state."""
    queryset = queryset.select_related('author', 'author__profile').annotate(
        likes_count=Count('likes', distinct=True),
        favorites_count=Count('favorites', distinct=True),
    )
    return _annotate_current_user_interactions(queryset, user)


def _interaction_count_subquery(through_model):
    counts = through_model.objects.filter(
        share_id=OuterRef('pk'),
    ).order_by().values('share_id').annotate(
        total=Count('pk'),
    ).values('total')[:1]
    return Coalesce(
        Subquery(counts, output_field=IntegerField()),
        Value(0),
        output_field=IntegerField(),
    )


def share_detail_queryset(user):
    """Return shares with all single-record detail presentation fields loaded."""
    queryset = Share.objects.select_related(
        'author',
        'author__profile',
    ).annotate(
        likes_count=_interaction_count_subquery(Share.likes.through),
        favorites_count=_interaction_count_subquery(Share.favorites.through),
    )
    return _annotate_current_user_interactions(queryset, user)


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
            Q(status=Share.Status.PENDING)
            | ~Q(restriction_state=Share.RestrictionState.CLEAR)
        ).count(),
        'pending_reports_count': Share.objects.annotate(
            pending_count=Count(
                'reports',
                filter=Q(reports__status=Report.Status.PENDING),
            )
        ).filter(pending_count__gt=0).count(),
    }
