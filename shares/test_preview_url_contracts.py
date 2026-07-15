from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .templatetags.share_urls import board_preview_url


class BoardPreviewUrlContractTests(SimpleTestCase):
    def test_strategy_code_is_encoded_as_one_path_segment(self):
        strategy_code = '[stgy:a/b?c#d%e&f"g]'
        expected_url = '/n/board/%5Bstgy%3Aa%2Fb%3Fc%23d%25e%26f%22g%5D'

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
