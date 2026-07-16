from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Report, Share, ShareLog


class ModerationQueuePerformanceContractTests(TestCase):
    REVIEW_RESPONSE_BUDGET = 300_000
    REPORT_RESPONSE_BUDGET = 350_000
    QUERY_BUDGET = 10

    def setUp(self):
        self.moderator = User.objects.create_user(
            username='moderation-scale-staff',
            password='password123',
            is_staff=True,
        )
        self.author = User.objects.create_user(
            username='moderation-scale-author',
            password='password123',
        )
        self.client.force_login(self.moderator)

    def _create_shares(self, *, prefix, count, status):
        return Share.objects.bulk_create([
            Share(
                share_id=f'{prefix}-{index:03d}',
                title=f'{prefix} 性能分享 {index:03d}',
                strategy_code=f'[stgy:{prefix}-{index:03d}]',
                description='队列描述' + ('描' * 700),
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=status,
            )
            for index in range(count)
        ])

    def _create_logs(self, shares, *, marker):
        ShareLog.objects.bulk_create([
            ShareLog(
                share=share,
                user=self.moderator,
                action=ShareLog.ActionType.OTHER,
                details=(f'{marker}日志前缀' + ('长' * 600) + f'{marker}-TAIL'),
            )
            for share in shares
            for _ in range(100)
        ])

    def _get_with_query_count(self, url):
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url)
            response_bytes = len(response.content)
        return response, len(captured), response_bytes

    def test_review_queue_has_constant_queries_and_bounded_related_data(self):
        first_share = self._create_shares(
            prefix='review-scale',
            count=1,
            status=Share.Status.PENDING,
        )
        self._create_logs(first_share, marker='REVIEW')
        url = reverse('admin_review_list')
        _, single_queries, _ = self._get_with_query_count(url)

        remaining_shares = self._create_shares(
            prefix='review-full',
            count=19,
            status=Share.Status.PENDING,
        )
        self._create_logs(remaining_shares, marker='REVIEW')
        response, full_queries, response_bytes = self._get_with_query_count(url)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(full_queries, single_queries + 1)
        self.assertLessEqual(full_queries, self.QUERY_BUDGET)
        self.assertLess(response_bytes, self.REVIEW_RESPONSE_BUDGET)
        self.assertEqual(len(response.context['review_items']), 20)
        self.assertContains(response, 'id="reviewResolutionModal"', count=1)
        self.assertContains(response, 'name="reason"', count=1)
        for item in response.context['review_items']:
            share = item['share']
            self.assertEqual(len(share.share_logs), 5)
            self.assertTrue(share.share_logs_truncated)
            self.assertIn('details', share.share_logs[0].get_deferred_fields())
        self.assertNotContains(response, 'REVIEW-TAIL')

    def test_report_queue_has_constant_queries_and_bounded_related_data(self):
        reporters = User.objects.bulk_create([
            User(username=f'moderation-scale-reporter-{index:02d}')
            for index in range(10)
        ])

        def create_report_data(shares):
            reports = []
            for share in shares:
                reports.extend(
                    Report(
                        share=share,
                        reporter=None,
                        reason='举报预览' + ('举' * 600) + 'REPORT-TAIL',
                    )
                    for _ in range(90)
                )
                reports.extend(
                    Report(
                        share=share,
                        reporter=reporter,
                        reason='实名举报预览' + ('报' * 600) + 'REPORT-TAIL',
                    )
                    for reporter in reporters
                )
            Report.objects.bulk_create(reports)
            self._create_logs(shares, marker='REPORT-LOG')

        first_share = self._create_shares(
            prefix='report-scale',
            count=1,
            status=Share.Status.APPROVED,
        )
        create_report_data(first_share)
        url = reverse('admin_report_list')
        _, single_queries, _ = self._get_with_query_count(url)

        remaining_shares = self._create_shares(
            prefix='report-full',
            count=9,
            status=Share.Status.APPROVED,
        )
        create_report_data(remaining_shares)
        response, full_queries, response_bytes = self._get_with_query_count(url)

        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(full_queries, single_queries + 1)
        self.assertLessEqual(full_queries, self.QUERY_BUDGET)
        self.assertLess(response_bytes, self.REPORT_RESPONSE_BUDGET)
        self.assertEqual(len(response.context['shares']), 10)
        for share in response.context['shares']:
            self.assertEqual(share.pending_count, 100)
            self.assertEqual(len(share.pending_reports), 5)
            self.assertTrue(share.pending_reports_truncated)
            self.assertEqual(len(share.share_logs), 5)
            self.assertTrue(share.share_logs_truncated)
            self.assertIn('reason', share.pending_reports[0].get_deferred_fields())
            self.assertIn('details', share.share_logs[0].get_deferred_fields())
        self.assertNotContains(response, 'REPORT-TAIL')
        self.assertNotContains(response, 'REPORT-LOG-TAIL')
