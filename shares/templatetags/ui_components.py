from django import template

from shares.presentation import build_user_presentation


register = template.Library()


@register.filter
def user_display_name(user):
    return build_user_presentation(user).display_name


@register.simple_tag
def pagination_items(page_obj):
    """Return a bounded, template-friendly page range around the current page."""
    paginator = page_obj.paginator
    return tuple(
        {
            'is_current': page_number == page_obj.number,
            'is_ellipsis': page_number == paginator.ELLIPSIS,
            'number': None if page_number == paginator.ELLIPSIS else page_number,
        }
        for page_number in paginator.get_elided_page_range(
            page_obj.number,
            on_each_side=2,
            on_ends=1,
        )
    )
