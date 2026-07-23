from dataclasses import dataclass

from django.core.paginator import Paginator
from django.db.models import (
    BooleanField,
    Count,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, Substr

from .models import Collection, CollectionItem, Report, Share, SiteMessage
from .policies import viewable_collection_queryset, viewable_share_queryset


_RELATED_COLLECTION_PREVIEW_SIZE = 5
_RELATED_COLLECTIONS_PER_PAGE = 6


@dataclass(frozen=True, slots=True)
class RelatedCollectionSummary:
    collection: Collection
    visible_items: tuple[CollectionItem, ...]
    visible_item_count: int


def _collection_item_count_subquery(visible_share_ids=None):
    counts = CollectionItem.objects.filter(collection_id=OuterRef('pk'))
    if visible_share_ids is not None:
        counts = counts.filter(share_id__in=visible_share_ids)
    counts = counts.order_by().values('collection_id').annotate(
        total=Count('pk'),
    ).values('total')[:1]
    return Coalesce(
        Subquery(counts, output_field=IntegerField()),
        Value(0),
        output_field=IntegerField(),
    )


def annotate_collection_cards(queryset):
    """Add item totals without joining every collection item into the page."""
    return queryset.annotate(item_count=_collection_item_count_subquery())


def _related_collection_page(queryset, page_number, selected_collection_id):
    paginator = Paginator(queryset, _RELATED_COLLECTIONS_PER_PAGE)
    if page_number not in (None, ''):
        return paginator.get_page(page_number)
    try:
        selected_pk = int(selected_collection_id)
    except (TypeError, ValueError):
        return paginator.get_page(None)
    if selected_pk <= 0:
        return paginator.get_page(None)

    target = queryset.filter(pk=selected_pk).values('pk', 'updated_at').first()
    if target is None:
        return paginator.get_page(None)
    items_before_target = queryset.filter(
        Q(updated_at__gt=target['updated_at'])
        | Q(updated_at=target['updated_at'], pk__gt=target['pk'])
    ).count()
    return paginator.get_page((items_before_target // paginator.per_page) + 1)


def related_collection_summaries(
    share,
    user,
    *,
    page_number=None,
    selected_collection_id=None,
):
    """Build one bounded page of permission-filtered collection previews."""
    visible_share_ids = viewable_share_queryset(user).order_by().values('pk')
    queryset = viewable_collection_queryset(
        user,
        Collection.objects.select_related(
            'author',
            'author__profile',
        ).filter(collectionitem__share=share),
    ).annotate(
        visible_item_count=_collection_item_count_subquery(visible_share_ids),
    ).order_by('-updated_at', '-pk')
    page = _related_collection_page(
        queryset,
        page_number,
        selected_collection_id,
    )
    collections = list(page.object_list)
    if not collections:
        page.object_list = ()
        return page

    visible_items_by_collection = {
        collection.pk: []
        for collection in collections
    }
    preview_filter = Q()
    for collection_id in visible_items_by_collection:
        preview_item_ids = CollectionItem.objects.filter(
            collection_id=collection_id,
            share_id__in=visible_share_ids,
        ).order_by(
            'order',
            'added_at',
            'pk',
        ).values('pk')[:_RELATED_COLLECTION_PREVIEW_SIZE]
        preview_filter |= Q(
            collection_id=collection_id,
            pk__in=Subquery(preview_item_ids),
        )

    preceding_visible_items = CollectionItem.objects.filter(
        collection_id=OuterRef('collection_id'),
        share_id__in=visible_share_ids,
        order__lt=OuterRef('order'),
    ).order_by().values('collection_id').annotate(
        total=Count('pk'),
    ).values('total')[:1]
    collection_items = CollectionItem.objects.filter(
        preview_filter | Q(
            collection_id__in=visible_items_by_collection,
            share_id=share.pk,
        ),
        share_id__in=visible_share_ids,
    ).annotate(
        visible_position=(
            Coalesce(
                Subquery(preceding_visible_items, output_field=IntegerField()),
                Value(0),
                output_field=IntegerField(),
            )
            + Value(1)
        ),
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
    page.object_list = tuple(summaries)
    return page


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
        likes_count=_interaction_count_subquery(Share.likes.through),
        favorites_count=_interaction_count_subquery(Share.favorites.through),
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
    queryset = Share.objects.filter(deleted_at__isnull=True).select_related(
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


def site_message_list_queryset(user, mailbox='inbox'):
    """Return one stable, bounded-preview mailbox for the current user."""
    queryset = SiteMessage.objects.filter(recipient=user)
    if mailbox == 'archived':
        queryset = queryset.filter(archived_at__isnull=False)
    else:
        queryset = queryset.filter(archived_at__isnull=True)
        if mailbox == 'unread':
            queryset = queryset.filter(read_at__isnull=True)
    return queryset.annotate(
        content_preview=Substr('content', 1, 240),
    ).defer(
        'content',
        'metadata',
    ).order_by('-created_at', '-pk')


def admin_task_counts():
    share_counts = Share.objects.filter(
        deleted_at__isnull=True,
    ).aggregate(
        pending_reviews_count=Count(
            'pk',
            filter=Q(status=Share.Status.PENDING),
        ),
        restricted_shares_count=Count(
            'pk',
            filter=(
                ~Q(restriction_state=Share.RestrictionState.CLEAR)
                & ~Q(status=Share.Status.PENDING)
            ),
        ),
    )
    return {
        **share_counts,
        'pending_reports_count': Report.objects.filter(
            status=Report.Status.PENDING,
            share__deleted_at__isnull=True,
        ).values('share_id').distinct().count(),
    }
