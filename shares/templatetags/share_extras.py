import base64
from django import template

register = template.Library()

@register.filter
def urlsafe_base64(value):
    """
    Encodes a string to URL-safe Base64.
    """
    if not value:
        return ''
    if isinstance(value, str):
        value = value.encode('utf-8')
    encoded = base64.urlsafe_b64encode(value).decode('utf-8')
    return encoded.rstrip('=')
