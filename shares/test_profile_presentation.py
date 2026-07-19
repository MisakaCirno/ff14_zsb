import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Collection, CollectionItem, Share, UserProfile
from .presentation import build_user_presentation


User = get_user_model()


class UserPresentationTests(TestCase):
    def test_profile_nickname_and_bio_are_presented(self):
        user = User.objects.create_user(username='presentation-user')
        profile = user.profile
        profile.nickname = '展示昵称'
        profile.bio = '展示简介'
        profile.save(update_fields=['nickname', 'bio', 'updated_at'])

        presentation = build_user_presentation(user)

        self.assertEqual(presentation.display_name, '展示昵称')
        self.assertEqual(presentation.bio, '展示简介')
        self.assertFalse(presentation.is_anonymous)

    def test_missing_profile_falls_back_without_creating_a_row(self):
        user = User.objects.create_user(username='missing-presentation-user')
        UserProfile.objects.filter(user=user).delete()
        user = User.objects.get(pk=user.pk)

        presentation = build_user_presentation(user)

        self.assertEqual(presentation.display_name, user.username)
        self.assertEqual(presentation.bio, '')
        self.assertFalse(presentation.is_anonymous)
        self.assertFalse(UserProfile.objects.filter(user=user).exists())

    def test_none_is_presented_as_anonymous(self):
        presentation = build_user_presentation(None)

        self.assertEqual(presentation.display_name, '匿名用户')
        self.assertEqual(presentation.bio, '')
        self.assertTrue(presentation.is_anonymous)


class MissingUserProfilePageTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='missing-profile-author',
            password='CurrentPassword123!',
        )
        self.share = Share.objects.create(
            title='缺失资料展示测试',
            strategy_code='[stgy:missing-profile-presentation]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        self.collection = Collection.objects.create(
            title='缺失资料合集',
            author=self.author,
            is_public=True,
        )
        CollectionItem.objects.create(
            collection=self.collection,
            share=self.share,
            order=1,
        )
        UserProfile.objects.filter(user=self.author).delete()

    def assert_html_matches(self, response, pattern):
        self.assertRegex(
            response.content.decode(),
            re.compile(pattern, re.DOTALL),
        )

    def test_public_pages_show_username_and_do_not_repair_on_read(self):
        index_response = self.client.get(reverse('index'))
        profile_response = self.client.get(
            reverse('user_public_profile', args=[self.author.username]),
        )
        collection_response = self.client.get(
            reverse('collection_detail', args=[self.collection.pk]),
        )
        detail_response = self.client.get(
            reverse('share_detail', args=[self.share.share_id]),
        )

        for response in (
            index_response,
            profile_response,
            collection_response,
            detail_response,
        ):
            with self.subTest(path=response.wsgi_request.path):
                self.assertEqual(response.status_code, 200)

        escaped_username = re.escape(self.author.username)
        self.assert_html_matches(
            index_response,
            rf'class="browse-card__author"[^>]*>\s*{escaped_username}\s*</a>',
        )
        self.assert_html_matches(
            profile_response,
            rf'class="ui-page-title public-profile-hero__name">{escaped_username}</h1>',
        )
        self.assertContains(
            profile_response,
            f'<title>{self.author.username} 的个人主页 - 粘鼠板儿</title>',
            html=True,
        )
        self.assert_html_matches(
            collection_response,
            rf'collection-detail-hero__identity.*?创建者.*?>\s*{escaped_username}\s*</a>',
        )
        self.assertContains(
            detail_response,
            f'data-share-author="{self.author.username}"',
        )
        self.assertContains(
            detail_response,
            f'由 {self.author.username} 整理',
        )
        self.assertFalse(UserProfile.objects.filter(user=self.author).exists())

    def test_authenticated_navigation_and_profile_get_do_not_write_a_profile(self):
        self.client.force_login(self.author)

        index_response = self.client.get(reverse('index'))
        profile_edit_response = self.client.get(reverse('profile_edit'))

        self.assertEqual(index_response.status_code, 200)
        self.assertEqual(profile_edit_response.status_code, 200)
        escaped_username = re.escape(self.author.username)
        self.assert_html_matches(
            index_response,
            rf'class="app-navbar__user-name">\s*{escaped_username}\s*</span>',
        )
        self.assertFalse(UserProfile.objects.filter(user=self.author).exists())
