from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .forms import CollectionForm, ReportForm, ShareForm, UserProfileForm
from .models import Share
from .validation import (
    COLLECTION_DESCRIPTION_MAX_LENGTH,
    PROFILE_BIO_MAX_LENGTH,
    REPORT_REASON_MAX_LENGTH,
    RICH_TEXT_MAX_LENGTH,
    SEARCH_QUERY_MAX_LENGTH,
    STRATEGY_CODE_INPUT_MAX_LENGTH,
    normalize_strategy_code,
)


def share_form_data(**overrides):
    data = {
        'title': '测试分享',
        'strategy_code': '[stgy:test-code]',
        'description': '',
        'category': Share.Category.ENTERTAINMENT,
        'visibility': Share.Visibility.UNLISTED,
    }
    data.update(overrides)
    return data


class StrategyCodeValidationTests(TestCase):
    def test_extracts_code_from_pasted_text_and_normalizes_brackets(self):
        self.assertEqual(
            normalize_strategy_code('游戏导出：【stgy:abc+123-xyz】 请复制'),
            '[stgy:abc+123-xyz]',
        )

    def test_share_form_persists_normalized_code(self):
        form = ShareForm(data=share_form_data(strategy_code='前缀 [stgy:normalized] 后缀'))

        self.assertTrue(form.is_valid(), form.errors)
        share = form.save()
        self.assertEqual(share.strategy_code, '[stgy:normalized]')

    def test_rejects_malformed_or_oversized_strategy_code_input(self):
        malformed = ShareForm(data=share_form_data(strategy_code='not-a-strategy-code'))
        oversized = ShareForm(data=share_form_data(
            strategy_code='[stgy:' + ('a' * STRATEGY_CODE_INPUT_MAX_LENGTH) + ']',
        ))

        self.assertFalse(malformed.is_valid())
        self.assertIn('strategy_code', malformed.errors)
        self.assertFalse(oversized.is_valid())
        self.assertIn('strategy_code', oversized.errors)


class InputLengthValidationTests(TestCase):
    def test_share_description_has_a_bounded_length(self):
        form = ShareForm(data=share_form_data(description='x' * (RICH_TEXT_MAX_LENGTH + 1)))

        self.assertFalse(form.is_valid())
        self.assertIn('description', form.errors)

    def test_report_collection_and_profile_text_have_bounded_lengths(self):
        report = ReportForm(data={'reason': 'x' * (REPORT_REASON_MAX_LENGTH + 1)})
        collection = CollectionForm(data={
            'title': '合集',
            'description': 'x' * (COLLECTION_DESCRIPTION_MAX_LENGTH + 1),
            'is_public': True,
        })
        profile = UserProfileForm(data={
            'nickname': '用户',
            'bio': 'x' * (PROFILE_BIO_MAX_LENGTH + 1),
            'home_feed_mode': 'infinite',
        })

        self.assertIn('reason', report.errors)
        self.assertIn('description', collection.errors)
        self.assertIn('bio', profile.errors)

    def test_oversized_search_is_rejected_before_querying_content(self):
        response = self.client.get(reverse('search'), {'q': 'x' * (SEARCH_QUERY_MAX_LENGTH + 1)})

        self.assertRedirects(response, reverse('index'))

    def test_existing_valid_workflow_remains_accepted(self):
        author = User.objects.create_user(username='author', password='password123')
        self.client.force_login(author)

        response = self.client.post(reverse('create_share'), share_form_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Share.objects.get().strategy_code, '[stgy:test-code]')
