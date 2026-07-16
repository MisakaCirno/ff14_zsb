import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import AdminReviewRejectForm
from .models import Share, ShareLog


class ReviewFrontendSourceContractTests(SimpleTestCase):
    def read_project_file(self, relative_path):
        return (Path(settings.BASE_DIR) / relative_path).read_text(encoding='utf-8')

    def read_template(self, relative_path):
        return self.read_project_file(Path('templates') / relative_path)

    def read_frontend(self, relative_path):
        return self.read_project_file(Path('frontend/src') / relative_path)

    def test_review_pages_use_shared_moderation_structure(self):
        queue_source = self.read_template('shares/admin_review_list.html')
        log_source = self.read_template('shares/admin_review_logs.html')

        for source in (queue_source, log_source):
            with self.subTest(template=source[:40]):
                self.assertIn('shares/includes/moderation_page_header.html', source)
                self.assertIn('shares/includes/admin_tabs.html', source)
                self.assertNotIn('style=', source)

        self.assertIn('shares/includes/moderation_review_card.html', queue_source)
        self.assertIn('shares/includes/pagination.html', queue_source)
        self.assertNotIn('pagination-lg', queue_source)
        self.assertNotIn('shares.paginator.page_range', queue_source)
        self.assertIn('shares/includes/moderation_audit_log.html', log_source)
        self.assertIn("log_kind='review'", log_source)
        self.assertNotIn('<table', log_source)

    def test_review_cards_use_one_shared_server_form_and_modal(self):
        queue_source = self.read_template('shares/admin_review_list.html')
        card_source = self.read_template(
            'shares/includes/moderation_review_card.html'
        )
        trigger_source = self.read_template(
            'shares/includes/moderation_review_resolution_trigger.html'
        )
        modal_source = self.read_template(
            'shares/includes/moderation_review_resolution_modal.html'
        )

        self.assertIn('share-card card-hover moderation-review-card', card_source)
        self.assertIn(
            "share=share preview_variant='review'",
            card_source,
        )
        self.assertNotIn('share-preview__link', card_source)
        self.assertIn('moderation-action-grid', card_source)
        self.assertIn(
            "{% url 'admin_approve_share' share.share_id %}",
            card_source,
        )
        self.assertIn('moderation_review_resolution_trigger.html', card_source)
        self.assertIn('moderation_review_resolution_modal.html', queue_source)
        self.assertEqual(modal_source.count('id="reviewResolutionModal"'), 1)
        self.assertIn('data-resolution-version', modal_source)

        for action_name in (
            'admin_reject_share',
            'admin_confirm_share_restriction',
            'admin_release_share_restriction',
        ):
            with self.subTest(action=action_name):
                self.assertIn(action_name, trigger_source)

        self.assertIn('{{ resolution_form.reason }}', modal_source)
        self.assertNotIn('<textarea', modal_source)
        self.assertNotIn('style=', card_source + trigger_source + modal_source)

    def test_review_view_builds_one_shared_server_form(self):
        view_source = self.read_project_file('shares/web/moderation.py')

        self.assertIn('def _staff_reason_form(', view_source)
        self.assertNotIn('def _review_reason_form(', view_source)
        self.assertIn('def _review_queue_context(', view_source)
        self.assertIn('_review_item(', view_source)
        self.assertNotIn('def _new_review_form(', view_source)
        for prefix in ('review-reject', 'review-confirm', 'review-release'):
            with self.subTest(prefix=prefix):
                self.assertIn(f"'{prefix}'", view_source)
        self.assertIn("'auto_id': f'{prefix}-{share_id}-%s'", view_source)
        self.assertIn("'error_id': f'{prefix}-errors-{share_id}'", view_source)
        self.assertIn("'confirmation_version': share.updated_at.isoformat()", view_source)
        self.assertIn('review_resolution_form=resolution_form', view_source)
        self.assertIn("reason_attrs['aria-describedby'] = help_id", view_source)
        self.assertIn("reason_attrs['aria-invalid'] = 'true'", view_source)

    def test_shared_moderation_tabs_and_audit_list_are_accessible(self):
        tabs_source = self.read_template('shares/includes/admin_tabs.html')
        audit_source = self.read_template(
            'shares/includes/moderation_audit_log.html'
        )

        self.assertIn('class="moderation-tabs"', tabs_source)
        self.assertIn(
            'moderation_active_tab|default:request.resolver_match.url_name',
            tabs_source,
        )
        self.assertIn('aria-current="page"', tabs_source)
        self.assertIn('moderation-tabs__indicator', tabs_source)
        self.assertIn('有 {{ pending_reviews_count }} 个待审核或受限项目', tabs_source)
        self.assertNotIn('New alerts', tabs_source)
        self.assertNotIn('style=', tabs_source)

        self.assertIn('class="moderation-audit-list"', audit_source)
        self.assertIn("log_kind == 'review'", audit_source)
        self.assertIn("log_kind == 'report'", audit_source)
        self.assertIn('shares/includes/pagination.html', audit_source)
        self.assertIn('datetime="{{ log.created_at|date:\'c\' }}"', audit_source)
        self.assertNotIn('style=', audit_source)

    def test_moderation_css_bounds_long_content_and_mobile_actions(self):
        css_source = self.read_frontend('styles/moderation-page.css')
        main_source = self.read_frontend('styles/main.css')

        self.assertIn("@import './moderation-page.css';", main_source)

        for selector in (
            '.moderation-page',
            '.moderation-hero',
            '.moderation-tabs__list',
            '.moderation-review-grid',
            '.moderation-review-card__title',
            '.moderation-action-grid',
            '.moderation-audit-item',
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, css_source)

        self.assertIn('minmax(min(100%, 17.5rem), 1fr)', css_source)
        self.assertIn('overflow-wrap: anywhere;', css_source)
        self.assertIn('@media (max-width: 575.98px)', css_source)
        self.assertRegex(
            css_source,
            re.compile(
                r'\.moderation-action-grid,\s*'
                r'\.moderation-audit-item\s*\{\s*'
                r'grid-template-columns: minmax\(0, 1fr\);',
                re.DOTALL,
            ),
        )


class ReviewFrontendRenderingContractTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='review-ui-admin',
            password='password123',
            is_staff=True,
        )
        self.author = User.objects.create_user(
            username='review-ui-author-' + ('x' * 90),
            password='password123',
        )
        self.client.force_login(self.admin)

    def create_share(self, *, share_id, status=Share.Status.PENDING, **fields):
        return Share.objects.create(
            share_id=share_id,
            title=fields.pop('title', '需要审核的长标题' * 12),
            strategy_code='[stgy:test]',
            author=self.author,
            status=status,
            **fields,
        )

    def test_review_queue_renders_one_bounded_server_form(self):
        for index in range(21):
            self.create_share(share_id=f'2a3b4c{index:02d}')

        response = self.client.get(reverse('admin_review_list'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['review_items']), 20)
        form = response.context['review_resolution_form']
        field_html = str(form['reason'])
        self.assertIn('name="reason"', field_html)
        self.assertIn('maxlength="2000"', field_html)
        self.assertIn('minlength="2"', field_html)
        self.assertIn('aria-describedby=', field_html)
        self.assertContains(response, 'id="reviewResolutionModal"', count=1)
        self.assertContains(response, 'name="reason"', count=1)
        self.assertContains(response, 'data-resolution-trigger', count=20)

        self.assertContains(response, 'class="app-pagination')
        self.assertContains(response, 'aria-current="page"')
        self.assertNotContains(response, 'pagination-lg')

    def test_rendered_confirmation_version_can_be_submitted(self):
        share = self.create_share(
            share_id='3d4e5f6g',
            status=Share.Status.APPROVED,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='举报核查后限制访问',
            restricted_at=timezone.now(),
            restricted_by=self.admin,
        )
        response = self.client.get(reverse('admin_review_list'))
        item = next(
            item for item in response.context['review_items']
            if item['share'].pk == share.pk
        )
        version = item['confirmation_version']
        self.assertContains(
            response,
            f'data-resolution-version="{version}"',
        )

        confirmation = self.client.post(
            reverse('admin_confirm_share_restriction', args=[share.share_id]),
            {
                'version': version,
                'reason': '复核后确认继续维持限制',
            },
        )

        self.assertRedirects(confirmation, reverse('admin_review_list'))
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_CONFIRM,
        ).exists())

    def test_invalid_staff_reason_uses_the_bound_form_error(self):
        share = self.create_share(share_id='4e5f6g7h')
        invalid_form = AdminReviewRejectForm({'reason': '短'})
        self.assertFalse(invalid_form.is_valid())
        expected_error = str(invalid_form.errors['reason'][0])

        response = self.client.post(
            reverse('admin_reject_share', args=[share.share_id]),
            {'reason': '短'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, expected_error, status_code=400)
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.PENDING)
