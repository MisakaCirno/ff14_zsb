from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from .models import Collection, Share


class AccountContentPermissionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='content-owner',
            password='password123',
        )
        self.outsider = User.objects.create_user(
            username='content-outsider',
            password='password123',
        )
        self.staff = User.objects.create_user(
            username='content-staff',
            password='password123',
            is_staff=True,
        )
        self.owned_share = Share.objects.create(
            title='owner managed share',
            strategy_code='[stgy:owner-managed]',
            author=self.owner,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.external_share = Share.objects.create(
            title='external interacted share',
            strategy_code='[stgy:external-interacted]',
            author=self.outsider,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.collection = Collection.objects.create(
            title='owner managed collection',
            description='original collection description',
            author=self.owner,
            is_public=True,
        )

    def test_my_content_renders_management_links_for_owned_content(self):
        self.client.force_login(self.owner)

        shares_response = self.client.get(reverse('my_shares'))

        self.assertEqual(shares_response.status_code, 200)
        self.assertContains(shares_response, self.owned_share.title)
        self.assertContains(
            shares_response,
            f'href="{reverse("edit_share", args=[self.owned_share.share_id])}"',
        )
        self.assertContains(
            shares_response,
            f'href="{reverse("delete_share", args=[self.owned_share.share_id])}"',
        )

        collections_response = self.client.get(
            reverse('my_shares'),
            {'tab': 'collections'},
        )

        self.assertEqual(collections_response.status_code, 200)
        self.assertContains(collections_response, self.collection.title)
        self.assertContains(
            collections_response,
            f'href="{reverse("edit_collection", args=[self.collection.pk])}"',
        )
        self.assertContains(
            collections_response,
            f'href="{reverse("delete_collection", args=[self.collection.pk])}"',
        )

    def test_interaction_tabs_do_not_render_management_controls_for_other_users_content(self):
        self.owner.liked_shares.add(self.external_share)
        self.owner.favorited_shares.add(self.external_share)
        self.client.force_login(self.owner)
        edit_url = reverse('edit_share', args=[self.external_share.share_id])
        delete_url = reverse('delete_share', args=[self.external_share.share_id])

        for tab in ('likes', 'favorites'):
            with self.subTest(tab=tab):
                response = self.client.get(reverse('my_shares'), {'tab': tab})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, self.external_share.title)
                self.assertContains(response, 'data-share-card-variant="browse"')
                self.assertNotContains(response, 'data-managed-share')
                self.assertNotContains(response, 'management-card__actions')
                self.assertNotContains(response, f'href="{edit_url}"')
                self.assertNotContains(response, f'href="{delete_url}"')

    def test_non_owner_and_staff_cannot_edit_collection(self):
        original_state = (
            self.collection.title,
            self.collection.description,
            self.collection.is_public,
        )
        payload = {
            'title': 'unauthorized replacement',
            'description': 'unauthorized description',
            'is_public': '',
        }
        url = reverse('edit_collection', args=[self.collection.pk])

        for actor in (self.outsider, self.staff):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)

                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, payload).status_code, 404)

                self.collection.refresh_from_db()
                self.assertEqual(
                    (
                        self.collection.title,
                        self.collection.description,
                        self.collection.is_public,
                    ),
                    original_state,
                )

    def test_non_owner_and_staff_cannot_delete_collection(self):
        url = reverse('delete_collection', args=[self.collection.pk])

        for actor in (self.outsider, self.staff):
            with self.subTest(actor=actor.username):
                self.client.force_login(actor)

                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url).status_code, 404)
                self.assertTrue(
                    Collection.objects.filter(pk=self.collection.pk).exists(),
                )

    def test_owner_can_get_and_post_collection_edit(self):
        self.client.force_login(self.owner)
        url = reverse('edit_collection', args=[self.collection.pk])

        get_response = self.client.get(url)

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.context['collection'], self.collection)

        post_response = self.client.post(url, {
            'title': 'updated collection title',
            'description': 'updated collection description',
        })

        self.assertRedirects(
            post_response,
            reverse('collection_detail', args=[self.collection.pk]),
        )
        self.collection.refresh_from_db()
        self.assertEqual(self.collection.title, 'updated collection title')
        self.assertEqual(
            self.collection.description,
            'updated collection description',
        )
        self.assertFalse(self.collection.is_public)

    def test_collection_delete_requires_csrf_and_owner_can_submit_valid_post(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        url = reverse('delete_collection', args=[self.collection.pk])

        denied_response = csrf_client.post(url)

        self.assertEqual(denied_response.status_code, 403)
        self.assertTrue(Collection.objects.filter(pk=self.collection.pk).exists())

        get_response = csrf_client.get(url)

        self.assertEqual(get_response.status_code, 200)
        self.assertTrue(Collection.objects.filter(pk=self.collection.pk).exists())
        csrf_token = get_response.cookies['csrftoken'].value

        allowed_response = csrf_client.post(
            url,
            {'csrfmiddlewaretoken': csrf_token},
        )

        self.assertRedirects(allowed_response, reverse('my_shares'))
        self.assertTrue(Collection.objects.filter(pk=self.collection.pk).exists())
        self.collection.refresh_from_db()
        self.assertIsNotNone(self.collection.deleted_at)
