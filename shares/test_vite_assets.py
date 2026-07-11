import json
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ImproperlyConfigured
from django.template import Context, Template
from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .templatetags.vite_assets import _read_manifest


class ViteAssetsTagTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.manifest_path = Path(self.temp_dir.name) / 'manifest.json'
        _read_manifest.cache_clear()
        self.addCleanup(_read_manifest.cache_clear)

    def write_manifest(self, payload):
        self.manifest_path.write_text(
            json.dumps(payload),
            encoding='utf-8',
        )

    def render_assets(self, entrypoint='src/main.ts'):
        source = '{% load vite_assets %}{% vite_assets "' + entrypoint + '" %}'
        with override_settings(
            STATIC_URL='/assets/',
            VITE_MANIFEST_PATH=self.manifest_path,
            VITE_ENTRYPOINT='src/main.ts',
        ):
            return Template(source).render(Context())

    def test_renders_css_before_module_entry_with_posix_urls(self):
        self.write_manifest({
            'src/main.ts': {
                'file': 'assets/main-abc.js',
                'css': ['assets/main-def.css'],
            },
        })

        rendered = self.render_assets()

        self.assertIn('href="/assets/app/assets/main-def.css"', rendered)
        self.assertIn('type="module"', rendered)
        self.assertIn('src="/assets/app/assets/main-abc.js"', rendered)
        self.assertLess(rendered.index('<link'), rendered.index('<script'))
        self.assertNotIn('\\', rendered)

    def test_missing_manifest_fails_with_build_instruction(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured,
            'Run npm --prefix frontend run build',
        ):
            self.render_assets()

    def test_invalid_manifest_json_fails_explicitly(self):
        self.manifest_path.write_text('{invalid', encoding='utf-8')

        with self.assertRaisesMessage(ImproperlyConfigured, 'not valid JSON'):
            self.render_assets()

    def test_missing_entrypoint_fails_explicitly(self):
        self.write_manifest({})

        with self.assertRaisesMessage(ImproperlyConfigured, 'is missing'):
            self.render_assets()

    def test_manifest_asset_cannot_escape_static_app(self):
        self.write_manifest({
            'src/main.ts': {
                'file': '../outside.js',
            },
        })

        with self.assertRaisesMessage(ImproperlyConfigured, 'must stay inside'):
            self.render_assets()


class BuiltViteAssetsTests(SimpleTestCase):
    def test_configured_entrypoint_and_assets_exist(self):
        manifest_path = Path(settings.VITE_MANIFEST_PATH)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        entry = manifest[settings.VITE_ENTRYPOINT]

        asset_paths = [entry['file'], *entry.get('css', [])]
        for asset_path in asset_paths:
            with self.subTest(asset=asset_path):
                self.assertTrue((manifest_path.parent / asset_path).is_file())
