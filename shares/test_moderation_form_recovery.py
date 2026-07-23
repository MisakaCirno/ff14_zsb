import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Report, Share, ShareLog, SiteMessage


class ModerationFormRecoveryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='form-recovery-admin',
            password='password123',
            is_staff=True,
        )
        self.author = User.objects.create_user(
            username='form-recovery-author',
            password='password123',
        )
        self.reporter = User.objects.create_user(
            username='form-recovery-reporter',
            password='password123',
        )
        self.client.force_login(self.admin)

    def create_share(self, *, suffix, status=Share.Status.PENDING, **overrides):
        fields = {
            'title': f'表单恢复目标 {suffix}',
            'strategy_code': f'[stgy:form-recovery-{suffix}]',
            'author': self.author,
            'visibility': Share.Visibility.PUBLIC,
            'status': status,
        }
        fields.update(overrides)
        return Share.objects.create(**fields)

    def assert_no_moderation_writes(self, share):
        self.assertFalse(ShareLog.objects.filter(share=share).exists())
        self.assertFalse(SiteMessage.objects.filter(related_share=share).exists())

    def assert_active_moderation_tab(self, response, url):
        markup = response.content.decode()
        self.assertRegex(
            markup,
            re.compile(
                r'<a\s+'
                r'class="nav-link ui-segmented-nav__link moderation-tabs__link active"\s+'
                rf'href="{re.escape(url)}"\s+'
                r'aria-current="page">',
            ),
        )

    def test_invalid_review_reason_keeps_target_input_and_real_page_without_redirecting(self):
        target = self.create_share(suffix='target')
        for index in range(20):
            self.create_share(suffix=f'newer-{index}')

        response = self.client.post(
            reverse('admin_reject_share', args=[target.share_id]),
            {
                'reason': '短',
                'return_page': 'https://attacker.example/redirect',
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['shares'].number, 2)
        review_list_url = reverse('admin_review_list')
        self.assertEqual(
            response.context['pagination_base_url'],
            review_list_url,
        )
        self.assertContains(
            response,
            f'href="{review_list_url}?page=1"',
            status_code=400,
        )
        self.assert_active_moderation_tab(response, review_list_url)
        invalid_item = next(
            item for item in response.context['review_items']
            if item['share'].pk == target.pk
        )
        self.assertEqual(invalid_item['invalid_action'], 'reject')
        form = response.context['review_resolution_form']
        self.assertEqual(form['reason'].value(), '短')
        self.assertTrue(form.errors['reason'])
        self.assertContains(
            response,
            'id="reviewResolutionModal"',
            status_code=400,
        )
        self.assertContains(
            response,
            'name="csrfmiddlewaretoken"',
            status_code=400,
        )
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        self.assertContains(
            response,
            f'id="review-reject-errors-{target.share_id}"',
            status_code=400,
        )
        self.assertContains(response, 'aria-invalid="true"', status_code=400)
        target.refresh_from_db()
        self.assertEqual(target.status, Share.Status.PENDING)
        self.assert_no_moderation_writes(target)

    def test_invalid_confirmation_version_preserves_reason_and_supplies_retry_token(self):
        share = self.create_share(
            suffix='confirm-version',
            status=Share.Status.APPROVED,
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='等待管理员复核',
            restricted_at=timezone.now(),
            restricted_by=self.admin,
        )
        original_reason = share.restriction_reason
        url = reverse(
            'admin_confirm_share_restriction',
            args=[share.share_id],
        )

        response = self.client.post(url, {
            'reason': '保留这段人工复核说明',
            'version': 'not-a-version',
        })

        self.assertEqual(response.status_code, 400)
        form = response.context['review_resolution_form']
        self.assertEqual(form['reason'].value(), '保留这段人工复核说明')
        self.assertIn('version', form.errors)
        self.assertNotEqual(form['version'].value(), 'not-a-version')
        self.assertEqual(form['version'].value(), share.updated_at.isoformat())
        self.assertContains(response, '限制版本无效', status_code=400)
        self.assertContains(
            response,
            'id="reviewResolutionModal"',
            status_code=400,
        )
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        share.refresh_from_db()
        self.assertEqual(share.restriction_reason, original_reason)
        self.assert_no_moderation_writes(share)

        retry = self.client.post(url, {
            'reason': form['reason'].value(),
            'version': form['version'].value(),
        })

        self.assertRedirects(retry, reverse('admin_restriction_list'))
        share.refresh_from_db()
        self.assertEqual(share.restriction_reason, '保留这段人工复核说明')
        self.assertTrue(ShareLog.objects.filter(
            share=share,
            action=ShareLog.ActionType.RESTRICTION_CONFIRM,
        ).exists())

    def test_invalid_review_target_that_left_queue_is_rendered_as_recovery_item(self):
        share = self.create_share(
            suffix='left-queue',
            status=Share.Status.APPROVED,
        )

        response = self.client.post(
            reverse('admin_reject_share', args=[share.share_id]),
            {'reason': '短'},
        )

        self.assertEqual(response.status_code, 400)
        recovery_item = next(
            item for item in response.context['review_items']
            if item['share'].pk == share.pk
        )
        self.assertTrue(recovery_item['target_outside_queue'])
        self.assertEqual(
            response.context['review_resolution_form']['reason'].value(),
            '短',
        )
        self.assertContains(
            response,
            '目标状态已发生变化，本次提交尚未执行',
            status_code=400,
        )
        self.assertContains(
            response,
            'data-bs-target="#reviewResolutionModal"',
            status_code=400,
        )
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        share.refresh_from_db()
        self.assertEqual(share.status, Share.Status.APPROVED)
        self.assert_no_moderation_writes(share)

    def test_invalid_single_report_action_keeps_server_defined_target_context(self):
        share = self.create_share(
            suffix='single-report',
            status=Share.Status.APPROVED,
        )
        report = Report.objects.create(
            share=share,
            reporter=self.reporter,
            reason='举报人提交的核查内容',
        )
        for index in range(15):
            newer_share = self.create_share(
                suffix=f'newer-report-{index}',
                status=Share.Status.APPROVED,
            )
            Report.objects.create(
                share=newer_share,
                reporter=self.reporter,
                reason=f'用于推入第二页的举报 {index}',
            )
        action_url = reverse(
            'admin_resolve_report',
            args=[report.pk, 'dismiss'],
        )

        response = self.client.post(action_url, {
            'reason': '短',
            'return_page': 'https://attacker.example/redirect',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.context['shares'].number, 2)
        report_list_url = reverse('admin_report_list')
        self.assertEqual(
            response.context['pagination_base_url'],
            report_list_url,
        )
        self.assertContains(
            response,
            f'href="{report_list_url}?page=1"',
            status_code=400,
        )
        self.assert_active_moderation_tab(response, report_list_url)
        self.assertRegex(
            response.content.decode(),
            re.compile(
                r'class="accordion-collapse collapse show"\s+'
                rf'id="reportCollapse{re.escape(share.share_id)}"',
            ),
        )
        self.assertEqual(
            response.content.decode().count(
                'class="accordion-collapse collapse show"',
            ),
            1,
        )
        self.assertEqual(response.context['resolution_form']['reason'].value(), '短')
        self.assertEqual(
            response.context['resolution_error']['action_url'],
            action_url,
        )
        self.assertEqual(
            response.context['resolution_error']['subject'],
            f'举报人：{self.reporter.username}',
        )
        self.assertEqual(
            response.context['resolution_error']['context'],
            report.reason,
        )
        self.assertContains(
            response,
            f'action="{action_url}"',
            status_code=400,
        )
        self.assertContains(
            response,
            'id="report-resolution-errors"',
            status_code=400,
        )
        self.assertContains(
            response,
            'data-moderation-invalid-modal',
            count=1,
            status_code=400,
        )
        report.refresh_from_db()
        share.refresh_from_db()
        self.assertEqual(report.status, Report.Status.PENDING)
        self.assertEqual(share.restriction_state, Share.RestrictionState.CLEAR)
        self.assert_no_moderation_writes(share)

    def test_invalid_batch_report_action_and_stale_single_target_remain_recoverable(self):
        share = self.create_share(
            suffix='batch-report',
            status=Share.Status.APPROVED,
        )
        first = Report.objects.create(
            share=share,
            reporter=self.reporter,
            reason='第一条举报',
        )
        second_reporter = User.objects.create_user(username='second-form-reporter')
        second = Report.objects.create(
            share=share,
            reporter=second_reporter,
            reason='第二条举报',
        )
        batch_url = reverse(
            'admin_resolve_share_reports',
            args=[share.share_id, 'resolve'],
        )

        batch_response = self.client.post(batch_url, {'reason': '短'})

        self.assertEqual(batch_response.status_code, 400)
        self.assertEqual(
            batch_response.context['resolution_error']['action_url'],
            batch_url,
        )
        self.assertEqual(batch_response.context['resolution_error']['tone'], 'danger')
        self.assertEqual(
            batch_response.context['resolution_form']['reason'].value(),
            '短',
        )
        for report in (first, second):
            report.refresh_from_db()
            self.assertEqual(report.status, Report.Status.PENDING)
        self.assert_no_moderation_writes(share)

        first.status = Report.Status.DISMISSED
        first.resolution_reason = '由另一名管理员先处理'
        first.resolved_by = self.admin
        first.resolved_at = timezone.now()
        first.save(update_fields=[
            'status',
            'resolution_reason',
            'resolved_by',
            'resolved_at',
        ])
        second.status = Report.Status.DISMISSED
        second.resolution_reason = '同批次已由另一名管理员处理'
        second.resolved_by = self.admin
        second.resolved_at = timezone.now()
        second.save(update_fields=[
            'status',
            'resolution_reason',
            'resolved_by',
            'resolved_at',
        ])
        stale_url = reverse(
            'admin_resolve_report',
            args=[first.pk, 'dismiss'],
        )

        stale_response = self.client.post(stale_url, {'reason': '短'})

        self.assertEqual(stale_response.status_code, 400)
        self.assertEqual(stale_response.context['shares'].paginator.count, 0)
        self.assertTrue(stale_response.context['resolution_error']['target_stale'])
        self.assertEqual(
            stale_response.context['resolution_form']['reason'].value(),
            '短',
        )
        self.assertContains(
            stale_response,
            '目标状态已发生变化，本次提交尚未执行',
            status_code=400,
        )
        first.refresh_from_db()
        self.assertEqual(first.status, Report.Status.DISMISSED)
        self.assertEqual(first.resolution_reason, '由另一名管理员先处理')
