from django import template
from django.utils.safestring import mark_safe

from shares.content_sanitizer import sanitize_nested_rich_text, sanitize_rich_text


register = template.Library()


@register.filter
def sanitize_html(value):
    """Sanitize stored rich text before intentionally rendering it as HTML."""
    return mark_safe(sanitize_rich_text(value))


@register.filter
def sanitize_nested_html(value):
    """Sanitize body content and keep its headings below the host section."""
    return mark_safe(sanitize_nested_rich_text(value))
