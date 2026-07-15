from django import template

from ..preview_urls import build_board_preview_url


register = template.Library()


@register.filter
def board_preview_url(strategy_code):
    """Build a renderer URL with the strategy code as one encoded path segment."""
    return build_board_preview_url(strategy_code)
