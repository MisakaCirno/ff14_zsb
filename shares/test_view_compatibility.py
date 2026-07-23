from django.test import SimpleTestCase

from . import views


class LegacyViewFacadeTests(SimpleTestCase):
    def test_historical_view_imports_remain_callable(self):
        expected_names = {
            'about',
            'add_share_to_collection',
            'admin_approve_share',
            'admin_reject_share',
            'admin_takedown_share',
            'admin_report_list',
            'admin_report_logs',
            'admin_resolve_report',
            'admin_resolve_share_reports',
            'admin_restriction_list',
            'admin_review_list',
            'admin_review_logs',
            'announcement_list',
            'collection_detail',
            'create_collection',
            'create_share',
            'delete_collection',
            'delete_share',
            'edit_collection',
            'edit_share',
            'get_admin_counts',
            'get_collection_codes',
            'get_share_code',
            'index',
            'mark_all_site_messages_read',
            'my_shares',
            'open_site_message',
            'page_not_found',
            'password_change',
            'profile_edit',
            'record_copy',
            'record_view',
            'register',
            'remove_share_from_collection',
            'report_share',
            'search',
            'set_home_feed_mode',
            'share_detail',
            'site_message_detail',
            'site_message_list',
            'toggle_announcement_visibility',
            'toggle_favorite',
            'toggle_like',
            'user_login',
            'user_logout',
            'user_public_profile',
        }

        for name in expected_names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(views, name)))
