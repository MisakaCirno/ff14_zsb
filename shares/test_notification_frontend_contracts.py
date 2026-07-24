from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class NotificationFrontendContractTests(SimpleTestCase):
    def read_frontend(self, relative_path):
        return (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / relative_path
        ).read_text(encoding='utf-8')

    def read_template(self, relative_path):
        return (
            Path(settings.BASE_DIR) / 'templates' / relative_path
        ).read_text(encoding='utf-8')

    def test_dynamic_notifications_connect_to_the_shared_live_region(self):
        template = self.read_template('shares/includes/flash_messages.html')
        module_source = self.read_frontend('core/notify.ts')

        self.assertIn('id="message-container"', template)
        self.assertIn('aria-live="polite"', template)
        self.assertIn('data-notification', template)
        self.assertIn('aria-label="关闭通知"', template)
        self.assertIn("getElementById('message-container')", module_source)

    def test_announcement_uses_an_accessible_native_dialog(self):
        template = self.read_template('shares/index.html')
        module_source = self.read_frontend('features/announcement.ts')
        styles = self.read_frontend('styles/browse-page.css')

        self.assertIn('data-dismiss-announcement', template)
        self.assertIn('<dialog', template)
        self.assertIn('aria-labelledby="browse-announcement-title"', template)
        self.assertIn("dialog.addEventListener('cancel'", module_source)
        self.assertIn('.browse-announcement-dialog::backdrop', styles)
