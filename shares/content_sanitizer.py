"""Server-side sanitization for user-authored rich text."""

import re

import nh3


_BLOCK_CLASSES = {
    "ql-align-center",
    "ql-align-right",
    "ql-align-justify",
    "ql-direction-rtl",
    *(f"ql-indent-{level}" for level in range(1, 9)),
}

_CLEANER = nh3.Cleaner(
    tags={
        "a",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "span",
        "strong",
        "sub",
        "sup",
        "u",
        "ul",
    },
    clean_content_tags={
        "embed",
        "iframe",
        "math",
        "object",
        "script",
        "style",
        "svg",
        "template",
    },
    attributes={
        "a": {"href", "target", "title"},
        "span": {"style"},
    },
    allowed_classes={
        "blockquote": _BLOCK_CLASSES,
        "h1": _BLOCK_CLASSES,
        "h2": _BLOCK_CLASSES,
        "h3": _BLOCK_CLASSES,
        "h4": _BLOCK_CLASSES,
        "h5": _BLOCK_CLASSES,
        "h6": _BLOCK_CLASSES,
        "li": _BLOCK_CLASSES,
        "p": _BLOCK_CLASSES,
        "pre": {"ql-syntax"},
        "span": {
            "ql-font-monospace",
            "ql-font-serif",
            "ql-size-huge",
            "ql-size-large",
            "ql-size-small",
        },
    },
    filter_style_properties={"background-color", "color"},
    link_rel="noopener noreferrer nofollow",
    url_schemes={"http", "https", "mailto"},
)

_HEADING_TAG = re.compile(
    r"(?P<prefix></?)h(?P<level>[1-6])(?P<suffix>(?:\s[^<>]*)?>)",
    flags=re.IGNORECASE,
)


def sanitize_rich_text(value):
    """Return rich text limited to the formatting supported by the site."""
    if not value:
        return ""
    return _CLEANER.clean(str(value))


def sanitize_nested_rich_text(value, *, minimum_heading_level=4):
    """Sanitize rich text and rebase its headings below a host section title."""
    if not 1 <= minimum_heading_level <= 6:
        raise ValueError('minimum_heading_level must be between 1 and 6')

    cleaned = sanitize_rich_text(value)
    heading_levels = [
        int(match.group('level'))
        for match in _HEADING_TAG.finditer(cleaned)
    ]
    if not heading_levels:
        return cleaned

    offset = max(0, minimum_heading_level - min(heading_levels))
    if offset == 0:
        return cleaned

    def rebase(match):
        level = min(6, int(match.group('level')) + offset)
        return f'{match.group("prefix")}h{level}{match.group("suffix")}'

    return _HEADING_TAG.sub(rebase, cleaned)
