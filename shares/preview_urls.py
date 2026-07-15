from urllib.parse import quote


def build_board_preview_url(strategy_code):
    """Encode one strategy code as a single renderer URL path segment."""
    return f'/n/board/{quote(str(strategy_code or ""), safe="")}'
