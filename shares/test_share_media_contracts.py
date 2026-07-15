from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase


class ShareMediaFrontendContractTests(SimpleTestCase):
    @staticmethod
    def read_frontend(path):
        return (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / path
        ).read_text(encoding='utf-8')

    @staticmethod
    def read_template(path):
        return (
            Path(settings.BASE_DIR) / 'templates' / path
        ).read_text(encoding='utf-8')

    def test_clipboard_failure_returns_manual_result_without_echoing_text(self):
        clipboard_source = self.read_frontend('core/clipboard.ts')

        self.assertIn("status: 'manual-required'", clipboard_source)
        self.assertIn("status: 'copied'", clipboard_source)
        self.assertIn('const clipboardWriteTimeoutMs = 1500', clipboard_source)
        self.assertIn('Promise.race', clipboard_source)
        self.assertIn('textArea.readOnly = true', clipboard_source)
        self.assertNotIn('showMessage', clipboard_source)
        self.assertNotIn('请手动复制：${text}', clipboard_source)

    def test_manual_copy_modal_is_global_labelled_and_readonly(self):
        base_source = self.read_template('base.html')
        markup = render_to_string('shares/includes/manual_copy_modal.html')
        manual_copy_source = self.read_frontend('features/manual-copy.ts')

        self.assertIn(
            "{% include 'shares/includes/manual_copy_modal.html' %}",
            base_source,
        )
        self.assertIn('aria-labelledby="manual-copy-title"', markup)
        self.assertIn('aria-describedby="manual-copy-description"', markup)
        self.assertIn('data-manual-copy-text', markup)
        self.assertIn('readonly', markup)
        self.assertIn("elements.textArea.value = ''", manual_copy_source)
        self.assertIn('elements.textArea.value = text', manual_copy_source)
        self.assertNotIn('showMessage(text', manual_copy_source)

    def test_share_cards_use_reversed_copy_counter_urls(self):
        card_source = self.read_template('shares/includes/share_card.html')
        action_source = self.read_frontend('features/share-actions.ts')
        copy_source = self.read_frontend('features/share-copy.ts')

        record_contract = (
            'data-record-copy-url="{% url \'record_copy\' share.share_id %}"'
        )
        self.assertEqual(card_source.count(record_contract), 2)
        self.assertIn('button.dataset.recordCopyUrl', action_source)
        self.assertNotIn('`/share/${encodeURIComponent', action_source)
        self.assertIn('export async function recordShareCopy', copy_source)
        self.assertIn("result.status === 'manual-required'", copy_source)

    def test_share_image_guards_warning_state_and_fits_header_text(self):
        image_source = self.read_frontend('features/share-image.ts')

        self.assertIn("root.dataset.contentRevealed === 'true'", image_source)
        self.assertIn("root.addEventListener('share:content-revealed'", image_source)
        self.assertIn("modal.addEventListener('hidden.bs.modal'", image_source)
        self.assertIn("generateButton.focus({ preventScroll: true })", image_source)
        self.assertIn('if (!isContentRevealed(elements.root))', image_source)
        self.assertIn('export function fitCanvasText', image_source)
        self.assertIn('const headerHeight = 72', image_source)
        self.assertGreaterEqual(image_source.count('fitCanvasText(context,'), 3)
        self.assertIn("setAttribute('aria-busy', 'true')", self.read_frontend(
            'features/share-copy.ts',
        ))
