from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Collection, CollectionItem, Report, Share


class ModelConstraintTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author')
        self.reporter = User.objects.create_user(username='reporter')
        self.admin = User.objects.create_user(username='admin', is_staff=True)
        self.share = Share.objects.create(
            title='约束测试',
            strategy_code='[stgy:constraints]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def assert_update_rejected(self, **updates):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Share.objects.filter(pk=self.share.pk).update(**updates)

    def test_share_choices_and_counters_are_constrained(self):
        for updates in (
            {'category': 'invalid'},
            {'visibility': 'invalid'},
            {'status': 'invalid'},
            {'views': -1},
            {'copies': -1},
        ):
            with self.subTest(updates=updates):
                self.assert_update_rejected(**updates)

    def test_share_review_state_is_constrained(self):
        self.assert_update_rejected(
            status=Share.Status.PENDING,
            reviewed_at=timezone.now(),
            reviewed_by=self.admin,
        )
        self.assert_update_rejected(
            reviewed_at=None,
            reviewed_by=self.admin,
        )

    def test_share_restriction_state_is_constrained(self):
        self.assert_update_rejected(restriction_state='invalid')
        self.assert_update_rejected(
            restriction_state=Share.RestrictionState.REPORT_TAKEDOWN,
            restriction_reason='',
            restricted_at=None,
        )
        self.assert_update_rejected(
            restriction_state=Share.RestrictionState.CLEAR,
            restriction_reason='无限制状态不能保留原因',
        )
        self.assert_update_rejected(status=Share.Status.REJECTED)

    def test_only_one_pending_report_per_reporter_and_share(self):
        first = Report.objects.create(
            share=self.share,
            reporter=self.reporter,
            reason='第一次举报',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Report.objects.create(
                    share=self.share,
                    reporter=self.reporter,
                    reason='重复举报',
                )

        first.status = Report.Status.DISMISSED
        first.resolved_at = timezone.now()
        first.resolved_by = self.admin
        first.resolution_reason = '未发现问题'
        first.save(update_fields=[
            'status',
            'resolved_at',
            'resolved_by',
            'resolution_reason',
        ])
        Report.objects.create(
            share=self.share,
            reporter=self.reporter,
            reason='处理后再次举报',
        )

        self.assertEqual(
            Report.objects.filter(share=self.share, reporter=self.reporter).count(),
            2,
        )

    @override_settings(RATE_LIMIT_ENABLED=False)
    def test_duplicate_report_submission_is_handled_without_server_error(self):
        self.client.force_login(self.reporter)
        url = reverse('report_share', args=[self.share.share_id])

        first = self.client.post(url, {'reason': '第一次举报'})
        second = self.client.post(url, {'reason': '重复举报'})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(
            Report.objects.filter(share=self.share, reporter=self.reporter).count(),
            1,
        )

    def test_report_resolution_state_is_constrained(self):
        report = Report.objects.create(
            share=self.share,
            reporter=self.reporter,
            reason='举报内容',
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Report.objects.filter(pk=report.pk).update(
                    status=Report.Status.RESOLVED,
                    resolved_at=None,
                )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Report.objects.filter(pk=report.pk).update(
                    resolution_reason='待处理状态不应有处理说明',
                )

    def test_collection_order_slots_are_unique(self):
        collection = Collection.objects.create(title='合集', author=self.author)
        second_share = Share.objects.create(
            title='第二个分享',
            strategy_code='[stgy:constraints-second]',
            author=self.author,
        )
        CollectionItem.objects.create(
            collection=collection,
            share=self.share,
            order=1,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CollectionItem.objects.create(
                    collection=collection,
                    share=second_share,
                    order=1,
                )
