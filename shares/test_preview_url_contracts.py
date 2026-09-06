from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .templatetags.share_urls import board_preview_url


class BoardPreviewUrlContractTests(SimpleTestCase):
    @patch('shares.preview_urls.get_board_render_version', return_value='next/version +?&=#%中文')
    def test_render_cache_version_is_encoded_as_a_query_value(self, get_version):
        self.assertEqual(
            board_preview_url('[stgy:test]'),
            '/n/board/%5Bstgy%3Atest%5D?rv=next%2Fversion%20%2B%3F%26%3D%23%25%E4%B8%AD%E6%96%87',
        )

    @patch('shares.preview_urls.get_board_render_version', return_value='opaque-version')
    def test_strategy_code_is_encoded_as_one_path_segment(self, get_version):
        strategy_code = '[stgy:a/b?c#d%e&f"g]'
        expected_url = '/n/board/%5Bstgy%3Aa%2Fb%3Fc%23d%25e%26f%22g%5D?rv=opaque-version'

        self.assertEqual(board_preview_url(strategy_code), expected_url)

        share = SimpleNamespace(
            title='特殊字符预览',
            strategy_code=strategy_code,
            is_spoiler=False,
            is_nsfw=False,
            category='combat',
            is_original=False,
            views=0,
            copies=0,
            status='approved',
            visibility='public',
        )
        markup = render_to_string(
            'shares/includes/share_preview.html',
            {'share': share, 'preview_variant': 'standard'},
        )

        self.assertIn(f'src="{expected_url}"', markup)
        self.assertNotIn(f'/n/board/{strategy_code}', markup)

    @patch('shares.preview_urls.get_board_render_version', return_value=None)
    def test_unavailable_metadata_omits_query_and_preserves_path_encoding(self, get_version):
        self.assertEqual(board_preview_url('[stgy:/?+#%中]'),
                         '/n/board/%5Bstgy%3A%2F%3F%2B%23%25%E4%B8%AD%5D')
