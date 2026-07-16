from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from .models import Collection, CollectionItem, Share, ShareLog
from .selectors import (
    annotate_collection_cards,
    annotate_share_cards,
    related_collection_summaries,
)


class DetailPerformanceContractTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='detail-performance-author',
            password='password123',
        )
        self.staff = User.objects.create_user(
            username='detail-performance-staff',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            title='详情性能基线',
            strategy_code='[stgy:detail-performance]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def create_collection(self, index):
        collection = Collection.objects.create(
            title=f'性能合集 {index:02d}',
            author=self.author,
            is_public=True,
        )
        CollectionItem.objects.create(
            collection=collection,
            share=self.share,
            order=1,
        )
        return collection

    def test_card_counts_use_independent_subqueries_without_join_multiplication(self):
        users = [
            User.objects.create_user(username=f'interaction-{index}')
            for index in range(12)
        ]
        self.share.likes.add(*users)
        self.share.favorites.add(*users[:7])

        queryset = annotate_share_cards(
            Share.objects.filter(pk=self.share.pk),
            AnonymousUser(),
        )
        sql = str(queryset.query)
        card = queryset.get()

        self.assertEqual(card.likes_count, 12)
        self.assertEqual(card.favorites_count, 7)
        self.assertNotIn('LEFT OUTER JOIN "shares_share_likes"', sql)
        self.assertNotIn('LEFT OUTER JOIN "shares_share_favorites"', sql)

    def test_collection_card_counts_use_a_correlated_subquery(self):
        collection = self.create_collection(0)
        for index in range(8):
            extra_share = Share.objects.create(
                title=f'合集计数分享 {index:02d}',
                strategy_code=f'[stgy:collection-count-{index}]',
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )
            CollectionItem.objects.create(
                collection=collection,
                share=extra_share,
                order=index + 2,
            )

        queryset = annotate_collection_cards(
            Collection.objects.filter(pk=collection.pk),
        )
        sql = str(queryset.query)
        result = queryset.get()

        self.assertEqual(result.item_count, 9)
        self.assertNotIn('LEFT OUTER JOIN "shares_collectionitem"', sql)

    def test_related_collection_previews_are_paginated_with_constant_queries(self):
        collections = [self.create_collection(index) for index in range(14)]

        with CaptureQueriesContext(connection) as captured:
            first_page = related_collection_summaries(
                self.share,
                AnonymousUser(),
            )
            first_snapshot = [
                summary.collection.pk
                for summary in first_page
            ]

        self.assertEqual(len(captured), 3)
        self.assertEqual(first_page.paginator.count, 14)
        self.assertEqual(len(first_snapshot), 6)

        second_page = related_collection_summaries(
            self.share,
            AnonymousUser(),
            page_number=2,
        )
        third_page = related_collection_summaries(
            self.share,
            AnonymousUser(),
            page_number=3,
        )
        self.assertEqual(len(second_page), 6)
        self.assertEqual(len(third_page), 2)

        selected_page = related_collection_summaries(
            self.share,
            AnonymousUser(),
            selected_collection_id=collections[0].pk,
        )
        self.assertEqual(selected_page.number, 3)
        self.assertIn(
            collections[0].pk,
            [summary.collection.pk for summary in selected_page],
        )
        explicit_page = related_collection_summaries(
            self.share,
            AnonymousUser(),
            page_number=1,
            selected_collection_id=collections[0].pk,
        )
        self.assertEqual(explicit_page.number, 1)

    def test_related_collection_previews_do_not_scan_all_items_with_a_window(self):
        collections = []
        filler_shares = [
            Share.objects.create(
                title=f'预览候选 {index:02d}',
                strategy_code=f'[stgy:preview-candidate-{index}]',
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )
            for index in range(12)
        ]
        for collection_index in range(6):
            collection = Collection.objects.create(
                title=f'大合集 {collection_index:02d}',
                author=self.author,
                is_public=True,
            )
            CollectionItem.objects.bulk_create([
                CollectionItem(
                    collection=collection,
                    share=filler_share,
                    order=index,
                )
                for index, filler_share in enumerate(filler_shares)
            ])
            CollectionItem.objects.create(
                collection=collection,
                share=self.share,
                order=99,
            )
            collections.append(collection)

        with CaptureQueriesContext(connection) as captured:
            page = related_collection_summaries(
                self.share,
                AnonymousUser(),
            )
            snapshot = [
                (
                    summary.collection.pk,
                    len(summary.visible_items),
                    summary.visible_items[-1].share_id,
                    summary.visible_items[-1].visible_position,
                )
                for summary in page
            ]

        sql = '\n'.join(query['sql'] for query in captured.captured_queries)
        self.assertEqual(len(captured), 3)
        self.assertEqual(len(snapshot), 6)
        self.assertNotIn('ROW_NUMBER', sql.upper())
        self.assertIn('INNER JOIN "shares_collectionitem"', captured[1]['sql'])
        for _, item_count, last_share_id, last_position in snapshot:
            self.assertEqual(item_count, 5)
            self.assertEqual(last_share_id, self.share.pk)
            self.assertEqual(last_position, 13)

    def test_detail_log_preview_is_bounded_and_reports_truncation(self):
        logs = [
            ShareLog.objects.create(
                share=self.share,
                user=self.staff,
                action=ShareLog.ActionType.OTHER,
                details=(
                    '最新日志可见前缀' + ('测' * 600) + '不应读取的日志尾部'
                    if index == 29
                    else f'日志 {index:02d}'
                ),
            )
            for index in range(30)
        ]
        self.client.force_login(self.staff)

        response = self.client.get(
            reverse('share_detail', args=[self.share.share_id]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['share_logs']), 25)
        self.assertTrue(response.context['share_logs_truncated'])
        self.assertEqual(response.context['share_logs'][0].pk, logs[-1].pk)
        self.assertIn(
            'details',
            response.context['share_logs'][0].get_deferred_fields(),
        )
        self.assertContains(response, '最新日志可见前缀')
        self.assertNotContains(response, '不应读取的日志尾部')
        self.assertContains(response, '这里只展示最近 25 条')
        self.assertNotContains(response, logs[0].details)

    def test_collection_picker_paginates_without_hiding_older_collections(self):
        collections = [self.create_collection(index) for index in range(25)]
        self.client.force_login(self.author)
        picker_url = reverse(
            'select_collection_for_share',
            args=[self.share.share_id],
        )

        first_page = self.client.get(picker_url)
        second_page = self.client.get(picker_url, {'page': 2})

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context['collections'].paginator.count, 25)
        self.assertEqual(len(first_page.context['collections']), 20)
        self.assertEqual(len(second_page.context['collections']), 5)
        self.assertNotContains(first_page, collections[0].title)
        self.assertContains(second_page, collections[0].title)

        CollectionItem.objects.filter(
            collection=collections[0],
            share=self.share,
        ).delete()
        add_response = self.client.post(
            reverse('add_share_to_collection', args=[self.share.share_id]),
            {'collection_id': collections[0].pk},
        )

        self.assertRedirects(
            add_response,
            reverse('share_detail', args=[self.share.share_id]),
        )
        self.assertTrue(CollectionItem.objects.filter(
            collection=collections[0],
            share=self.share,
        ).exists())
