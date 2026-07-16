import re

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from .forms import ReportResolutionForm
from .models import Report, Share, ShareLog


class ReportFrontendContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='author-' + ('a' * 143),
            password='password123',
        )
        self.reporter = User.objects.create_user(
            username='reporter',
            password='password123',
        )
        self.second_reporter = User.objects.create_user(
            username='reporter-two',
            password='password123',
        )
        self.moderator = User.objects.create_user(
            username='moderator-' + ('m' * 140),
            password='password123',
            is_staff=True,
        )
        self.long_title = '超长举报队列标题' + ('战' * 190)
        self.long_reason = '需要核查：' + ('连续内容' * 120)
        self.share = self.create_share(title=self.long_title, suffix='primary')
        self.report = Report.objects.create(
            share=self.share,
            reporter=self.reporter,
            reason=self.long_reason,
        )
        self.second_report = Report.objects.create(
            share=self.share,
            reporter=self.second_reporter,
            reason='第二条待处理举报',
        )

    def create_share(self, *, title, suffix):
        return Share.objects.create(
            title=title,
            strategy_code=f'[stgy:report-ui-{suffix}]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def read_source(self, relative_path):
        return (settings.BASE_DIR / relative_path).read_text(encoding='utf-8')

    def assert_css_rule_contains(self, source, selector, declarations):
        matches = re.findall(r'([^{}]+)\{([^{}]*)\}', source)
        bodies = [
            body
            for selectors, body in matches
            if selector in {item.strip() for item in selectors.split(',')}
        ]
        self.assertTrue(bodies, f'Missing CSS selector: {selector}')
        self.assertTrue(
            any(all(declaration in body for declaration in declarations) for body in bodies),
            f'{selector} does not contain {declarations}',
        )

    def test_report_templates_use_shared_moderation_components_and_one_resolution_form(self):
        submit_source = self.read_source('templates/shares/report_share.html')
        queue_source = self.read_source('templates/shares/admin_report_list.html')
        log_source = self.read_source('templates/shares/admin_report_logs.html')

        for source in (submit_source, queue_source, log_source):
            self.assertIn("shares/includes/moderation_page_header.html", source)
            self.assertIn("icon='bi bi-", source)
            self.assertIn('moderation-report-', source)
            self.assertNotIn('style="', source)

        self.assertIn('{{ form.reason }}', submit_source)
        self.assertIn("shares/includes/admin_tabs.html", queue_source)
        self.assertIn("shares/includes/pagination.html", queue_source)
        self.assertIn("shares/includes/empty_state.html", queue_source)
        self.assertIn('{{ resolution_form.reason }}', queue_source)
        self.assertEqual(queue_source.count('id="reportResolutionModal"'), 1)
        self.assertIn('data-resolution-modal-submit', queue_source)
        self.assertIn('{% if not resolution_error %} disabled{% endif %}', queue_source)
        self.assertNotIn('dismissReportModal', queue_source)
        self.assertNotIn('shares.paginator.page_range', queue_source)
        self.assertNotIn('href="?page=', queue_source)
        self.assertIn("shares/includes/moderation_audit_log.html", log_source)
        self.assertNotIn('<table', log_source)

    def test_report_css_bounds_long_content_and_mobile_actions_without_raw_colors(self):
        css_source = self.read_source('frontend/src/styles/moderation-page.css')
        main_source = self.read_source('frontend/src/styles/main.css')

        self.assertIn("@import './moderation-page.css';", main_source)
        for selector in (
            '.moderation-report-subject__meta',
            '.moderation-report-card__title',
            '.moderation-report-card__meta span',
            '.moderation-report-item__reporter',
            '.moderation-report-item__reason',
        ):
            self.assert_css_rule_contains(
                css_source,
                selector,
                ('min-width: 0;', 'overflow-wrap: anywhere;'),
            )
        self.assert_css_rule_contains(
            css_source,
            '.moderation-report-toolbar',
            ('flex-wrap: wrap;', 'min-width: 0;'),
        )
        self.assertIn('@media (max-width: 575.98px)', css_source)
        self.assert_css_rule_contains(
            css_source,
            '.moderation-report-toolbar',
            ('display: grid;', 'grid-template-columns: minmax(0, 1fr);'),
        )
        for selector in (
            '.moderation-report-links .btn',
            '.moderation-report-item__actions .btn',
            '.moderation-report-resolution__actions .btn',
        ):
            self.assert_css_rule_contains(css_source, selector, ('width: 100%;',))
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,8}\b', css_source))
        self.assertIsNone(re.search(r'\b(?:rgb|hsl)a?\(', css_source))

    @override_settings(RATE_LIMIT_ENABLED=False)
    def test_report_submission_uses_the_bound_server_form_and_renders_errors(self):
        self.client.force_login(self.reporter)
        url = reverse('report_share', args=[self.share.share_id])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="container moderation-page moderation-report-page"')
        self.assertContains(response, 'aria-describedby="report-reason-help"')
        self.assertNotContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'maxlength="2000"')
        self.assertContains(response, self.long_title)

        invalid_response = self.client.post(url, {'reason': ''})

        self.assertEqual(invalid_response.status_code, 200)
        self.assertTrue(invalid_response.context['form'].errors['reason'])
        self.assertContains(invalid_response, 'moderation-report-field__errors')
        self.assertContains(invalid_response, 'id="report-reason-errors"')
        self.assertContains(invalid_response, 'aria-invalid="true"')
        self.assertContains(
            invalid_response,
            'aria-describedby="report-reason-help report-reason-errors"',
        )

    def test_report_queue_renders_long_content_with_one_unique_server_form(self):
        self.client.force_login(self.moderator)

        response = self.client.get(reverse('admin_report_list'))

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context['resolution_form'], ReportResolutionForm)
        self.assertContains(response, self.long_title)
        self.assertContains(response, self.author.username)
        self.assertContains(response, self.long_reason)
        self.assertContains(response, 'id="reportResolutionModal"', count=1)
        self.assertContains(response, 'name="reason"', count=1)
        self.assertContains(response, 'id="report-resolution-reason"', count=1)
        self.assertContains(response, 'aria-describedby="report-resolution-help"')
        self.assertContains(response, 'minlength="2"')
        self.assertContains(response, 'maxlength="2000"')
        self.assertContains(response, 'data-resolution-modal-submit disabled')
        self.assertContains(response, 'data-resolution-trigger', count=4)
        self.assertNotContains(response, 'dismissReportModal')

    def test_invalid_resolution_uses_the_bound_form_error(self):
        self.client.force_login(self.moderator)
        invalid_form = ReportResolutionForm({'reason': '短'})
        self.assertFalse(invalid_form.is_valid())
        expected_error = str(invalid_form.errors['reason'][0])

        response = self.client.post(
            reverse('admin_resolve_report', args=[self.report.pk, 'dismiss']),
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
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, Report.Status.PENDING)

    def test_report_queue_uses_bounded_shared_pagination_and_empty_state(self):
        for index in range(10):
            share = self.create_share(title=f'举报分页 {index}', suffix=f'page-{index}')
            Report.objects.create(
                share=share,
                reporter=self.reporter,
                reason=f'分页举报 {index}',
            )
        self.client.force_login(self.moderator)

        first_page = self.client.get(reverse('admin_report_list'))
        second_page = self.client.get(reverse('admin_report_list'), {'page': 2})

        self.assertContains(first_page, 'aria-label="待处理举报分页"')
        self.assertContains(first_page, '?page=2')
        self.assertEqual(len(second_page.context['shares']), 1)

        Report.objects.all().delete()
        empty_response = self.client.get(reverse('admin_report_list'))
        self.assertContains(empty_response, 'class="card empty-state"')
        self.assertContains(empty_response, '暂无待处理的举报')

    def test_report_log_uses_responsive_audit_list_for_long_content_and_multiple_pages(self):
        details = '处理依据：' + ('不可拆分的连续审计说明' * 40)
        for _ in range(21):
            ShareLog.objects.create(
                share=self.share,
                user=self.moderator,
                action=ShareLog.ActionType.REPORT_HANDLE,
                details=details,
            )
        self.client.force_login(self.moderator)

        first_page = self.client.get(reverse('admin_report_logs'))
        second_page = self.client.get(reverse('admin_report_logs'), {'page': 2})

        self.assertContains(first_page, 'class="moderation-audit-list"')
        self.assertContains(first_page, self.long_title)
        self.assertContains(first_page, self.moderator.username)
        self.assertContains(first_page, details)
        self.assertContains(first_page, 'aria-label="举报日志分页"')
        self.assertEqual(len(second_page.context['logs']), 1)

        ShareLog.objects.all().delete()
        empty_response = self.client.get(reverse('admin_report_logs'))
        self.assertContains(empty_response, '暂无举报处理记录')
