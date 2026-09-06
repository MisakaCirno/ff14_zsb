from django.utils.cache import add_never_cache_headers


class PreviewPageCacheMiddleware:
    """Do not let dynamic HTML outlive the metadata used to build preview URLs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        cache_directives = {
            directive.strip().lower()
            for directive in response.get('Cache-Control', '').split(',')
        }
        if (
            response.get('Content-Type', '').split(';', 1)[0] == 'text/html'
            and 'no-store' not in cache_directives
        ):
            add_never_cache_headers(response)
        return response
