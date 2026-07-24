from dataclasses import dataclass

from django.db.models import QuerySet


CONTENT_DISPLAY_MODES = frozenset({'hide', 'mask', 'show'})
DEFAULT_CONTENT_DISPLAY_MODE = 'mask'
CONTENT_DISPLAY_SESSION_KEYS = {
    'spoiler': 'browse_spoiler_preference',
    'nsfw': 'browse_nsfw_preference',
}
LEGACY_CONTENT_DISPLAY_PARAMETERS = {
    'spoiler': 'hide_spoiler',
    'nsfw': 'hide_nsfw',
}


@dataclass(frozen=True, slots=True)
class ContentDisplayPreferences:
    spoiler: str
    nsfw: str

    def as_context(self):
        return {
            'spoiler_preference': self.spoiler,
            'nsfw_preference': self.nsfw,
            # Keep the old context flags while external links migrate to the
            # explicit three-state query parameters.
            'hide_spoiler': self.spoiler == 'hide',
            'hide_nsfw': self.nsfw == 'hide',
        }


def _resolve_display_mode(request, parameter):
    mode = request.GET.get(parameter)
    session_key = CONTENT_DISPLAY_SESSION_KEYS[parameter]
    if mode in CONTENT_DISPLAY_MODES:
        if request.session.get(session_key) != mode:
            request.session[session_key] = mode
        return mode

    legacy_parameter = LEGACY_CONTENT_DISPLAY_PARAMETERS[parameter]
    if request.GET.get(legacy_parameter) == 'on':
        if request.session.get(session_key) != 'hide':
            request.session[session_key] = 'hide'
        return 'hide'

    saved_mode = request.session.get(session_key)
    if saved_mode in CONTENT_DISPLAY_MODES:
        return saved_mode
    return DEFAULT_CONTENT_DISPLAY_MODE


def resolve_content_display_preferences(request):
    """Resolve and persist the viewer's site-wide content display policy."""
    return ContentDisplayPreferences(
        spoiler=_resolve_display_mode(request, 'spoiler'),
        nsfw=_resolve_display_mode(request, 'nsfw'),
    )


def apply_hidden_content_preferences(
    queryset: QuerySet,
    preferences: ContentDisplayPreferences,
    *,
    field_prefix='',
):
    """Remove content marked hidden while leaving mask/show to presentation."""
    if field_prefix and not field_prefix.endswith('__'):
        field_prefix = f'{field_prefix}__'
    if preferences.spoiler == 'hide':
        queryset = queryset.filter(**{f'{field_prefix}is_spoiler': False})
    if preferences.nsfw == 'hide':
        queryset = queryset.filter(**{f'{field_prefix}is_nsfw': False})
    return queryset
