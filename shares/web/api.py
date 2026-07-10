from django.http import JsonResponse

from shares.models import Collection, CollectionItem, Share
from shares.policies import (
    can_view_collection,
    can_view_share,
    share_api_denial_status,
)


def get_share_code(request, share_id):
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return JsonResponse({'error': 'Share not found'}, status=404)
    if not can_view_share(request.user, share):
        status = share_api_denial_status(share)
        error = 'Permission denied' if status == 403 else 'Share not available'
        return JsonResponse({'error': error}, status=status)
    return JsonResponse([{
        'title': share.title,
        'code': share.strategy_code,
    }], safe=False)


def get_collection_codes(request, collection_id):
    try:
        collection = Collection.objects.get(id=collection_id)
    except Collection.DoesNotExist:
        return JsonResponse({'error': 'Collection not found'}, status=404)
    if not can_view_collection(request.user, collection):
        return JsonResponse({'error': 'Permission denied'}, status=403)
    payload = [
        {'title': item.share.title, 'code': item.share.strategy_code}
        for item in CollectionItem.objects.filter(collection=collection)
        .select_related('share')
        .order_by('order', 'added_at')
        if can_view_share(request.user, item.share)
    ]
    return JsonResponse(payload, safe=False)
