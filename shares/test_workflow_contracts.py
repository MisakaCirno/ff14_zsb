from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Collection, CollectionItem, Report, Share, UserProfile


class ShareWriteWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.admin = User.objects.create_user(username='admin', password='password123', is_staff=True)

    def share_form_data(self, **overrides):
        data = {
            'title': '测试分享',
            'strategy_code': '[stgy:test-code]',
            'description': '',
            'category': Share.Category.ENTERTAINMENT,
            'visibility': Share.Visibility.PUBLIC,
        }
        data.update(overrides)
        return data

    def create_share(self, **overrides):
        data = {
            'title': '已有分享',
            'strategy_code': '[stgy:existing]',
            'author': self.author,
            'visibility': Share.Visibility.PUBLIC,
            'status': Share.Status.APPROVED,
        }
        data.update(overrides)
        return Share.objects.create(**data)

    def test_anonymous_create_forces_unlisted_approved_share(self):
        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertIsNone(share.author)
        self.assertEqual(share.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_regular_user_public_create_requires_review(self):
        self.client.force_login(self.author)

        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.author, self.author)
        self.assertEqual(share.visibility, Share.Visibility.PUBLIC)
        self.assertEqual(share.status, Share.Status.PENDING)

    def test_staff_public_create_is_approved_immediately(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse('create_share'), self.share_form_data())

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.author, self.admin)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_regular_user_unlisted_create_does_not_require_review(self):
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('create_share'),
            self.share_form_data(visibility=Share.Visibility.UNLISTED),
        )

        self.assertEqual(response.status_code, 302)
        share = Share.objects.get()
        self.assertEqual(share.visibility, Share.Visibility.UNLISTED)
        self.assertEqual(share.status, Share.Status.APPROVED)

    def test_editing_public_share_restarts_review_and_clears_old_feedback(self):
        share = self.create_share(
            review_feedback='旧反馈',
            reviewed_at=timezone.now(),
            reviewed_by=self.admin,
        )
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('edit_share', args=[share.share_id]),
            self.share_form_data(title='修改后的分享'),
        )

        self.assertEqual(response.status_code, 302)
        share.refresh_from_db()
        self.assertEqual(share.title, '修改后的分享')
        self.assertEqual(share.status, Share.Status.PENDING)
        self.assertEqual(share.review_feedback, '')
        self.assertIsNone(share.reviewed_at)
        self.assertIsNone(share.reviewed_by)

    def test_non_owner_cannot_delete_share(self):
        share = self.create_share()
        self.client.force_login(self.other_user)

        response = self.client.post(reverse('delete_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('share_detail', args=[share.share_id]))
        self.assertTrue(Share.objects.filter(pk=share.pk).exists())

    def test_author_can_delete_share(self):
        share = self.create_share()
        self.client.force_login(self.author)

        response = self.client.post(reverse('delete_share', args=[share.share_id]))

        self.assertRedirects(response, reverse('my_shares'))
        self.assertFalse(Share.objects.filter(pk=share.pk).exists())


class InteractionWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.user = User.objects.create_user(username='user', password='password123')
        self.share = Share.objects.create(
            title='互动测试',
            strategy_code='[stgy:interaction]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def test_like_endpoint_toggles_relation(self):
        self.client.force_login(self.user)

        add_response = self.client.post(reverse('toggle_like', args=[self.share.share_id]))
        remove_response = self.client.post(reverse('toggle_like', args=[self.share.share_id]))

        self.assertEqual(add_response.json(), {
            'status': 'success',
            'is_liked': True,
            'likes_count': 1,
        })
        self.assertEqual(remove_response.json(), {
            'status': 'success',
            'is_liked': False,
            'likes_count': 0,
        })
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())
        self.assertIn('HX-Request', add_response.headers['Vary'])
        self.assertIn('no-store', add_response.headers['Cache-Control'])

    def test_hx_like_endpoint_returns_reusable_card_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
        self.assertContains(response, 'btn-danger')
        self.assertContains(response, 'bi-heart-fill')
        self.assertContains(response, 'hx-post=')
        self.assertContains(response, '>1</span>')
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_hx_like_endpoint_returns_detail_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=detail',
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-danger')
        self.assertContains(response, 'bi-heart-fill')
        self.assertContains(response, 'fragment=detail')
        self.assertContains(response, 'me-2')
        self.assertContains(response, '>1</span>')
        self.assertNotContains(response, 'w-50')
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_favorite_endpoint_toggles_relation(self):
        self.client.force_login(self.user)

        add_response = self.client.post(reverse('toggle_favorite', args=[self.share.share_id]))
        remove_response = self.client.post(reverse('toggle_favorite', args=[self.share.share_id]))

        self.assertTrue(add_response.json()['is_favorited'])
        self.assertEqual(add_response.json()['favorites_count'], 1)
        self.assertFalse(remove_response.json()['is_favorited'])
        self.assertEqual(remove_response.json()['favorites_count'], 0)
        self.assertFalse(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_hx_favorite_endpoint_returns_reusable_card_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=card',
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-warning')
        self.assertContains(response, 'bi-star-fill')
        self.assertContains(response, 'hx-post=')
        self.assertContains(response, '>1</span>')
        self.assertTrue(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_hx_favorite_endpoint_returns_detail_button(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('toggle_favorite', args=[self.share.share_id]) + '?fragment=detail',
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'btn-warning')
        self.assertContains(response, 'bi-star-fill')
        self.assertContains(response, 'fragment=detail')
        self.assertContains(response, '>1</span>')
        self.assertNotContains(response, 'w-50')
        self.assertTrue(self.share.favorites.filter(pk=self.user.pk).exists())

    def test_hx_interaction_rejects_unknown_or_missing_fragment_without_mutating(self):
        self.client.force_login(self.user)

        for query in ('', '?fragment=unknown'):
            with self.subTest(query=query):
                response = self.client.post(
                    reverse('toggle_like', args=[self.share.share_id]) + query,
                    HTTP_HX_REQUEST='true',
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

    def test_expired_hx_interaction_redirects_the_full_page_to_login(self):
        detail_url = reverse('share_detail', args=[self.share.share_id])
        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card',
            HTTP_HX_REQUEST='true',
            HTTP_HX_CURRENT_URL=f'http://testserver{detail_url}',
        )

        self.assertEqual(response.status_code, 204)
        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [detail_url])
        self.assertNotIn('Location', response.headers)
        self.assertFalse(self.share.likes.exists())

    def test_expired_hx_interaction_rejects_external_current_url(self):
        action_url = reverse('toggle_like', args=[self.share.share_id]) + '?fragment=card'

        response = self.client.post(
            action_url,
            HTTP_HX_REQUEST='true',
            HTTP_HX_CURRENT_URL='https://example.invalid/phishing',
        )

        redirect_query = parse_qs(urlsplit(response.headers['HX-Redirect']).query)
        self.assertEqual(redirect_query['next'], [action_url])

    def test_hx_detail_interaction_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        action_url = reverse('toggle_like', args=[self.share.share_id]) + '?fragment=detail'

        denied = csrf_client.post(action_url, HTTP_HX_REQUEST='true')

        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.share.likes.filter(pk=self.user.pk).exists())

        page = csrf_client.get(reverse('share_detail', args=[self.share.share_id]))
        csrf_token = page.cookies['csrftoken'].value
        allowed = csrf_client.post(
            action_url,
            HTTP_HX_REQUEST='true',
            HTTP_X_CSRFTOKEN=csrf_token,
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(self.share.likes.filter(pk=self.user.pk).exists())

    def test_copy_counter_only_increments_once_per_client_cookie(self):
        first_response = self.client.post(reverse('record_copy', args=[self.share.share_id]))
        second_response = self.client.post(reverse('record_copy', args=[self.share.share_id]))

        self.share.refresh_from_db()
        self.assertEqual(first_response.json()['copies_count'], 1)
        self.assertEqual(second_response.json()['copies_count'], 1)
        self.assertEqual(self.share.copies, 1)

        another_client = Client()
        third_response = another_client.post(reverse('record_copy', args=[self.share.share_id]))
        self.share.refresh_from_db()
        self.assertEqual(third_response.json()['copies_count'], 2)
        self.assertEqual(self.share.copies, 2)

    def test_authenticated_user_can_report_share(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('report_share', args=[self.share.share_id]),
            {'reason': '需要管理员核查'},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        report = Report.objects.get()
        self.assertEqual(report.share, self.share)
        self.assertEqual(report.reporter, self.user)
        self.assertEqual(report.reason, '需要管理员核查')


class CollectionAndProfileWorkflowTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='password123')
        self.other_user = User.objects.create_user(username='other', password='password123')
        self.share = Share.objects.create(
            title='合集测试',
            strategy_code='[stgy:collection]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.collection = Collection.objects.create(title='我的合集', author=self.author)

    def test_user_creation_automatically_creates_profile(self):
        self.assertTrue(UserProfile.objects.filter(user=self.author).exists())
        self.assertEqual(self.author.profile.get_display_name(), 'author')

    def test_author_can_add_own_share_to_collection(self):
        self.client.force_login(self.author)

        response = self.client.post(
            reverse('add_share_to_collection', args=[self.share.share_id]),
            {'collection_id': self.collection.id},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        item = CollectionItem.objects.get()
        self.assertEqual(item.collection, self.collection)
        self.assertEqual(item.share, self.share)

    def test_other_user_cannot_add_someone_elses_share_to_collection(self):
        other_collection = Collection.objects.create(title='其他合集', author=self.other_user)
        self.client.force_login(self.other_user)

        response = self.client.post(
            reverse('add_share_to_collection', args=[self.share.share_id]),
            {'collection_id': other_collection.id},
        )

        self.assertRedirects(response, reverse('share_detail', args=[self.share.share_id]))
        self.assertFalse(CollectionItem.objects.exists())
