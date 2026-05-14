from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Share, UserProfile


class HomeFeedModeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='password123')
        for index in range(13):
            Share.objects.create(
                title=f'分享 {index}',
                strategy_code=f'[stgy:test-{index}]',
                author=self.user,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )

    def test_default_home_uses_paginated_mode(self):
        response = self.client.get(reverse('index'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['feed_mode'], UserProfile.HomeFeedMode.PAGINATED)
        self.assertContains(response, '分页')
        self.assertNotContains(response, 'id="infinite-scroll-sentinel"')

    def test_authenticated_feed_mode_choice_is_saved(self):
        self.client.login(username='alice', password='password123')

        response = self.client.get(reverse('index'), {'feed': UserProfile.HomeFeedMode.INFINITE})

        self.assertEqual(response.status_code, 200)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.home_feed_mode, UserProfile.HomeFeedMode.INFINITE)
        self.assertContains(response, 'id="infinite-scroll-sentinel"')

    def test_share_partial_returns_next_page_cards(self):
        response = self.client.get(reverse('index'), {
            'feed': UserProfile.HomeFeedMode.INFINITE,
            'partial': 'shares',
            'page': 2,
        })

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn('html', data)
        self.assertFalse(data['has_next'])
        self.assertIsNone(data['next_page'])
        self.assertIn('分享 0', data['html'])
