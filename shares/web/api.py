from functools import wraps

from django.http import JsonResponse
from django.utils.cache import add_never_cache_headers, patch_vary_headers
from django.views.decorators.csrf import csrf_exempt

from shares.models import Collection, CollectionItem, Share
from shares.policies import (
    can_view_collection,
    can_view_share,
    share_api_denial_status,
)


def read_only_json_api(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.method not in {'GET', 'HEAD'}:
            response = JsonResponse({'error': 'Method not allowed'}, status=405)
            response.headers['Allow'] = 'GET, HEAD'
        else:
            response = view_func(request, *args, **kwargs)
        patch_vary_headers(response, ('Cookie',))
        add_never_cache_headers(response)
        return response

    return csrf_exempt(wrapper)


@read_only_json_api
def get_share_code(request, share_id):
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return JsonResponse({'error': 'Share not found'}, status=404)
    if not can_view_share(request.user, share):
        status = share_api_denial_status(share)
        error = 'Permission denied' if status == 403 else 'Share not found'
        return JsonResponse({'error': error}, status=status)
    return JsonResponse([{
        'title': share.title,
        'code': share.strategy_code,
    }], safe=False)


@read_only_json_api
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
