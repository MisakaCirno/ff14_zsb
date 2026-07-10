from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase

from .models import Collection, Share
from .policies import (
    can_view_collection,
    can_view_share,
    is_moderator,
    public_share_queryset,
    share_api_denial_status,
)


class SharePermissionPolicyTests(TestCase):
    def setUp(self):
        self.anonymous = AnonymousUser()
        self.author = User.objects.create_user(username='author', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.staff = User.objects.create_user(username='staff', password='password123', is_staff=True)

    def make_share(self, *, visibility, status):
        return Share.objects.create(
            title=f'{visibility}-{status}',
            strategy_code='[stgy:policy]',
            author=self.author,
            visibility=visibility,
            status=status,
        )

    def test_direct_link_share_access_matrix(self):
        expected_for_visitors = {
            (Share.Visibility.PUBLIC, Share.Status.APPROVED): True,
            (Share.Visibility.PUBLIC, Share.Status.PENDING): True,
            (Share.Visibility.PUBLIC, Share.Status.REJECTED): False,
            (Share.Visibility.UNLISTED, Share.Status.APPROVED): True,
            (Share.Visibility.UNLISTED, Share.Status.PENDING): True,
            (Share.Visibility.UNLISTED, Share.Status.REJECTED): False,
            (Share.Visibility.PRIVATE, Share.Status.APPROVED): False,
            (Share.Visibility.PRIVATE, Share.Status.PENDING): False,
            (Share.Visibility.PRIVATE, Share.Status.REJECTED): False,
        }

        for (visibility, status), visitor_can_view in expected_for_visitors.items():
            share = self.make_share(visibility=visibility, status=status)
            for user in (self.anonymous, self.other):
                with self.subTest(visibility=visibility, status=status, user=str(user)):
                    self.assertEqual(can_view_share(user, share), visitor_can_view)
            self.assertTrue(can_view_share(self.author, share))
            self.assertTrue(can_view_share(self.staff, share))

    def test_only_public_approved_shares_enter_public_querysets(self):
        public = self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.make_share(
            visibility=Share.Visibility.UNLISTED,
            status=Share.Status.APPROVED,
        )
        self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.PENDING,
        )

        self.assertEqual(list(public_share_queryset()), [public])

    def test_private_collection_is_visible_to_owner_and_moderator(self):
        private = Collection.objects.create(title='私有合集', author=self.author, is_public=False)
        public = Collection.objects.create(title='公开合集', author=self.author, is_public=True)

        self.assertFalse(can_view_collection(self.anonymous, private))
        self.assertFalse(can_view_collection(self.other, private))
        self.assertTrue(can_view_collection(self.author, private))
        self.assertTrue(can_view_collection(self.staff, private))
        self.assertTrue(can_view_collection(self.anonymous, public))

    def test_private_api_denial_remains_forbidden_while_moderated_content_is_hidden(self):
        private = self.make_share(
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.APPROVED,
        )
        rejected = self.make_share(
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.REJECTED,
        )

        self.assertEqual(share_api_denial_status(private), 403)
        self.assertEqual(share_api_denial_status(rejected), 404)
        self.assertTrue(is_moderator(self.staff))
        self.assertFalse(is_moderator(self.other))
