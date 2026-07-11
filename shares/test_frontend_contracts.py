from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Share


class FrontendShellContractTests(TestCase):
    def setUp(self):
        author = User.objects.create_user(username='author', password='password123')
        self.share = Share.objects.create(
            title='前端契约测试',
            strategy_code='[stgy:a&"b]',
            author=author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def test_shell_loads_vite_without_legacy_vue_or_inline_base_logic(self):
        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertRegex(content, r'<meta name="csrf-token" content="[^"]+">')
        self.assertIn('csrftoken', response.cookies)
        self.assertNotIn('vue.global.js', content)
        self.assertNotIn('function updateHistoryDropdown', content)

    def test_share_copy_button_uses_escaped_data_contract(self):
        response = self.client.get(reverse('index'))
        content = response.content.decode()

        self.assertIn('data-copy-strategy', content)
        self.assertIn('data-copy-code="[stgy:a&amp;&quot;b]"', content)
        self.assertIn(f'data-share-id="{self.share.share_id}"', content)
        self.assertNotIn('copyStrategyCode(', content)


class FrontendTemplateSourceTests(SimpleTestCase):
    def read_template(self, relative_path):
        return (Path(settings.BASE_DIR) / 'templates' / relative_path).read_text(
            encoding='utf-8',
        )

    def test_shared_copy_buttons_do_not_use_inline_handlers(self):
        template_paths = (
            'shares/includes/share_cards.html',
            'shares/my_shares.html',
            'shares/user_public_profile.html',
        )

        for template_path in template_paths:
            with self.subTest(template=template_path):
                source = self.read_template(template_path)
                self.assertIn('data-copy-strategy', source)
                self.assertNotIn('copyStrategyCode(', source)

    def test_base_template_has_no_classic_business_script(self):
        source = self.read_template('base.html')

        self.assertNotIn('vue.global.js', source)
        self.assertNotIn('function showMessage', source)
        self.assertNotIn('function updateHistoryDropdown', source)
        self.assertNotIn('onclick="copyQQGroup', source)
        self.assertNotIn('onclick="clearHistory', source)
