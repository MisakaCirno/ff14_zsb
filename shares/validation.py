import re

from django.core.exceptions import ValidationError


STRATEGY_CODE_MAX_LENGTH = 4096
STRATEGY_CODE_INPUT_MAX_LENGTH = 8192
RICH_TEXT_MAX_LENGTH = 50_000
COLLECTION_DESCRIPTION_MAX_LENGTH = 5_000
PROFILE_BIO_MAX_LENGTH = 1_000
REPORT_REASON_MAX_LENGTH = 2_000
STAFF_REASON_MAX_LENGTH = 2_000
SEARCH_QUERY_MAX_LENGTH = 200

_STRATEGY_CODE_PATTERN = re.compile(r'\[stgy:[^\]\s]+\]')


def normalize_strategy_code(value):
    """Extract and normalize one game strategy code from pasted text."""
    candidate = str(value or '').strip().replace('【', '[').replace('】', ']')
    match = _STRATEGY_CODE_PATTERN.search(candidate)
    if not match:
        raise ValidationError('请输入完整的战术板代码，格式应为 [stgy:...]。')

    code = match.group(0)
    if len(code) > STRATEGY_CODE_MAX_LENGTH:
        raise ValidationError(f'战术板代码不能超过 {STRATEGY_CODE_MAX_LENGTH} 个字符。')
    if not code.isascii():
        raise ValidationError('战术板代码只能包含 ASCII 字符。')
    return code
