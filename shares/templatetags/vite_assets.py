import json
from functools import lru_cache
from pathlib import Path, PurePosixPath

from django import template
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join


register = template.Library()


@lru_cache(maxsize=8)
def _read_manifest(path_value, modified_ns):
    del modified_ns
    path = Path(path_value)
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise ImproperlyConfigured(f'Vite manifest cannot be read: {path}') from exc
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(
            f'Vite manifest is not valid JSON: {path}',
        ) from exc
    if not isinstance(manifest, dict):
        raise ImproperlyConfigured(f'Vite manifest must contain an object: {path}')
    return manifest


def _load_manifest():
    path = Path(settings.VITE_MANIFEST_PATH)
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise ImproperlyConfigured(
            f'Vite manifest is missing: {path}. Run npm --prefix frontend run build.',
        ) from exc
    return _read_manifest(str(path), modified_ns)


def _asset_url(asset_path):
    if not isinstance(asset_path, str) or not asset_path:
        raise ImproperlyConfigured('Vite manifest contains an invalid asset path.')
    path = PurePosixPath(asset_path)
    if path.is_absolute() or '..' in path.parts:
        raise ImproperlyConfigured(
            f'Vite manifest asset must stay inside static/app: {asset_path}',
        )
    return static(f'app/{path.as_posix()}')


@register.simple_tag
def vite_assets(entrypoint=None):
    entrypoint = entrypoint or settings.VITE_ENTRYPOINT
    manifest = _load_manifest()
    entry = manifest.get(entrypoint)
    if not isinstance(entry, dict):
        raise ImproperlyConfigured(
            f'Vite entrypoint {entrypoint!r} is missing from the manifest.',
        )

    script_url = _asset_url(entry.get('file'))
    css_files = entry.get('css', [])
    if not isinstance(css_files, list):
        raise ImproperlyConfigured(
            f'Vite entrypoint {entrypoint!r} has an invalid CSS list.',
        )
    css_tags = format_html_join(
        '\n',
        '<link rel="stylesheet" href="{}">',
        ((_asset_url(css_file),) for css_file in css_files),
    )
    script_tag = format_html('<script type="module" src="{}"></script>', script_url)
    return format_html('{}\n{}', css_tags, script_tag)
