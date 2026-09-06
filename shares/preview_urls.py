from urllib.parse import quote

from .render_version import get_board_render_version


def build_board_preview_url(strategy_code):
    """Encode one strategy code as a single renderer URL path segment."""
    encoded_code = quote(str(strategy_code or ''), safe='')
    url = f'/n/board/{encoded_code}'
    version = get_board_render_version()
    if version is not None:
        url += f'?rv={quote(version, safe="")}'
    return url
