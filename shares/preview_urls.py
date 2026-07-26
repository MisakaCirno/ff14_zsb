from urllib.parse import quote

from django.conf import settings


def build_board_preview_url(strategy_code):
    """Encode one strategy code as a single renderer URL path segment."""
    encoded_code = quote(str(strategy_code or ''), safe='')
    encoded_version = quote(str(settings.BOARD_RENDER_CACHE_VERSION), safe='')
    return f'/n/board/{encoded_code}?rv={encoded_version}'
