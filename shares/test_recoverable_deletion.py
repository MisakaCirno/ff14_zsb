from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import ShareAdmin, UserAdmin
from .models import Collection, CollectionItem, Report, Share, ShareLog


class RecoverableDeletionTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='trash-author',
            password='password123',
        )
        self.reader = User.objects.create_user(
            username='trash-reader',
            password='password123',
        )
        self.moderator = User.objects.create_user(
            username='trash-moderator',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            share_id='2t3r4a5s',
            title='必须可恢复的分享',
            strategy_code='[stgy:recoverable]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.share.likes.add(self.reader)
        self.share.favorites.add(self.reader)
        self.report = Report.objects.create(
            share=self.share,
            reporter=self.reader,
            reason='保留的举报',
        )
        self.initial_log = ShareLog.objects.create(
            share=self.share,
            user=self.author,
            action=ShareLog.ActionType.CREATE,
            details='保留的创建日志',
        )
        self.collection = Collection.objects.create(
            title='必须可恢复的合集',
            author=self.author,
        )
        self.item = CollectionItem.objects.create(
            collection=self.collection,
            share=self.share,
            order=1,
        )

    def test_owner_delete_and_restore_preserve_all_related_user_data(self):
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('delete_share', args=[self.share.share_id])
        )

        self.assertRedirects(response, reverse('my_shares'))
        self.share.refresh_from_db()
        self.assertIsNotNone(self.share.deleted_at)
        self.assertEqual(self.share.deleted_by, self.author)
        self.assertEqual(
            self.share.deletion_origin,
            Share.DeletionOrigin.OWNER,
        )
        self.assertEqual(self.share.likes.count(), 1)
        self.assertEqual(self.share.favorites.count(), 1)
        self.assertTrue(Report.objects.filter(pk=self.report.pk).exists())
        self.assertTrue(CollectionItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(ShareLog.objects.filter(pk=self.initial_log.pk).exists())
        self.assertTrue(ShareLog.objects.filter(
            share=self.share,
            action=ShareLog.ActionType.DELETE,
        ).exists())

        self.assertEqual(
            self.client.get(
                reverse('share_detail', args=[self.share.share_id])
            ).status_code,
            404,
        )
        self.assertNotContains(self.client.get(reverse('index')), self.share.title)
        trash = self.client.get(reverse('my_shares'), {'tab': 'trash'})
        self.assertContains(trash, self.share.title)
        self.assertContains(
            trash,
            reverse('restore_share', args=[self.share.share_id]),
        )

        restored = self.client.post(
            reverse('restore_share', args=[self.share.share_id])
        )

        self.assertRedirects(restored, f'{reverse("my_shares")}?tab=trash')
        self.share.refresh_from_db()
        self.assertIsNone(self.share.deleted_at)
        self.assertIsNone(self.share.deleted_by)
        self.assertEqual(self.share.deletion_origin, '')
        self.assertEqual(self.share.deletion_reason, '')
        self.assertEqual(self.share.likes.count(), 1)
        self.assertEqual(self.share.favorites.count(), 1)
        self.assertTrue(Report.objects.filter(pk=self.report.pk).exists())
        self.assertTrue(CollectionItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(ShareLog.objects.filter(
            share=self.share,
            action=ShareLog.ActionType.RESTORE,
        ).exists())

    def test_moderator_deleted_share_requires_moderator_restore(self):
        self.client.force_login(self.moderator)
        self.client.post(reverse('delete_share', args=[self.share.share_id]))
        self.share.refresh_from_db()
        self.assertEqual(
            self.share.deletion_origin,
            Share.DeletionOrigin.MODERATOR,
        )

        self.client.force_login(self.author)
        denied = self.client.post(
            reverse('restore_share', args=[self.share.share_id])
        )

        self.assertEqual(denied.status_code, 404)
        self.share.refresh_from_db()
        self.assertIsNotNone(self.share.deleted_at)
        trash = self.client.get(reverse('my_shares'), {'tab': 'trash'})
        self.assertContains(trash, '请联系管理员恢复')
        self.assertNotContains(
            trash,
            reverse('restore_share', args=[self.share.share_id]),
        )

        self.client.force_login(self.moderator)
        restored = self.client.post(
            reverse('restore_share', args=[self.share.share_id])
        )
        self.assertEqual(restored.status_code, 302)
        self.share.refresh_from_db()
        self.assertIsNone(self.share.deleted_at)

    def test_collection_delete_and_restore_preserve_membership(self):
        self.client.force_login(self.author)

        deleted = self.client.post(
            reverse('delete_collection', args=[self.collection.pk])
        )

        self.assertRedirects(deleted, reverse('my_shares'))
        self.collection.refresh_from_db()
        self.assertIsNotNone(self.collection.deleted_at)
        self.assertTrue(CollectionItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(
            self.client.get(
                reverse('collection_detail', args=[self.collection.pk])
            ).status_code,
            404,
        )
        trash = self.client.get(reverse('my_shares'), {'tab': 'trash'})
        self.assertContains(trash, self.collection.title)

        restored = self.client.post(
            reverse('restore_collection', args=[self.collection.pk])
        )

        self.assertEqual(restored.status_code, 302)
        self.collection.refresh_from_db()
        self.assertIsNone(self.collection.deleted_at)
        self.assertTrue(CollectionItem.objects.filter(pk=self.item.pk).exists())
        self.assertEqual(
            self.client.get(
                reverse('collection_detail', args=[self.collection.pk])
            ).status_code,
            200,
        )

    def test_deleted_content_is_hidden_from_json_apis(self):
        self.share.deleted_at = timezone.now()
        self.share.deleted_by = self.author
        self.share.deletion_origin = Share.DeletionOrigin.OWNER
        self.share.deletion_reason = '作者主动将分享移入回收站。'
        self.share.save()
        self.collection.deleted_at = timezone.now()
        self.collection.deleted_by = self.author
        self.collection.deletion_reason = '作者主动将合集移入回收站。'
        self.collection.save()

        share_response = self.client.get(
            reverse('get_share_code', args=[self.share.share_id])
        )
        collection_response = self.client.get(
            reverse('get_collection_codes', args=[self.collection.pk])
        )

        self.assertEqual(share_response.status_code, 404)
        self.assertEqual(collection_response.status_code, 404)

    def test_admin_disables_physical_user_and_share_deletion(self):
        request = RequestFactory().get('/admin/')
        request.user = self.moderator

        self.assertFalse(
            ShareAdmin(Share, AdminSite()).has_delete_permission(
                request,
                self.share,
            )
        )
        self.assertFalse(
            UserAdmin(User, AdminSite()).has_delete_permission(
                request,
                self.author,
            )
        )
