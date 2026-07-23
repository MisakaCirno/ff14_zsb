from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class InfiniteScrollFrontendContractTests(SimpleTestCase):
    def read_template(self, relative_path):
        return (
            Path(settings.BASE_DIR) / 'templates' / relative_path
        ).read_text(encoding='utf-8')

    def read_frontend(self, relative_path):
        return (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / relative_path
        ).read_text(encoding='utf-8')

    def test_infinite_scroll_exposes_region_sentinel_and_terminal_status(self):
        index_source = self.read_template('shares/index.html')
        page_source = self.read_template('shares/includes/share_cards_page.html')

        self.assertIn('id="share-grid"', index_source)
        self.assertIn('data-infinite-scroll-region', index_source)
        self.assertIn('data-infinite-scroll-sentinel', page_source)
        self.assertIn('hx-swap="outerHTML"', page_source)
        self.assertIn('hx-sync="this:drop"', page_source)
        self.assertNotIn('hx-push-url', page_source)
        self.assertNotIn('hx-select', page_source)
        self.assertIn(
            'data-infinite-scroll-end role="status" tabindex="-1"',
            page_source,
        )

    def test_keyboard_focus_module_keeps_non_keyboard_loads_separate(self):
        source = self.read_frontend('features/infinite-scroll.ts')

        self.assertIn("event.key !== 'Enter' && event.key !== ' '", source)
        self.assertIn('keyboardActivationCandidates', source)
        self.assertIn('existingCards', source)
        self.assertIn('focusTargetForLoadedContent', source)
        self.assertIn("'htmx:afterSettle'", source)
        self.assertIn("'htmx:responseError'", source)
        self.assertIn("'htmx:sendError'", source)
        self.assertIn("'htmx:timeout'", source)


class ShareDetailInteractionGroupContractTests(SimpleTestCase):
    def read_template(self, relative_path):
        return (
            Path(settings.BASE_DIR) / 'templates' / relative_path
        ).read_text(encoding='utf-8')

    def test_detail_reaction_and_copy_controls_are_named_groups(self):
        source = self.read_template('shares/detail.html')

        self.assertIn(
            'class="share-detail-reactions" role="group" '
            'aria-label="分享互动"',
            source,
        )
        self.assertIn('id="share-detail-code-label"', source)
        self.assertIn(
            'class="share-detail-field__control" role="group" '
            'aria-labelledby="share-detail-code-label"',
            source,
        )
        self.assertIn('id="share-detail-url-label"', source)
        self.assertIn(
            'class="share-detail-field__control '
            'share-detail-field__control--share" role="group" '
            'aria-labelledby="share-detail-url-label"',
            source,
        )
        self.assertIn(
            'class="share-detail-side-actions" role="group" '
            'aria-labelledby="share-detail-actions-title"',
            source,
        )

    def test_share_image_actions_are_a_named_group(self):
        source = self.read_template('shares/includes/share_detail_modals.html')

        self.assertIn(
            'class="modal-footer share-detail-image-modal__actions" '
            'role="group" aria-label="分享图片操作"',
            source,
        )
