from html import unescape
from html.parser import HTMLParser

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from .models import Share
from .validation import RICH_TEXT_MAX_LENGTH


def _visible_text(markup):
    return ' '.join(unescape(strip_tags(markup)).split())


class _MarkupProbe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.editor_form_csrf_inputs = []
        self._inside_editor_form = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == 'form' and 'data-share-editor' in attributes:
            self._inside_editor_form = True
        elif (
            tag == 'input'
            and self._inside_editor_form
            and attributes.get('name') == 'csrfmiddlewaretoken'
        ):
            self.editor_form_csrf_inputs.append(attributes)

    def handle_endtag(self, tag):
        if tag == 'form' and self._inside_editor_form:
            self._inside_editor_form = False

    def matching(self, *, tag=None, attribute=None):
        return [
            attrs
            for element_tag, attrs in self.elements
            if (tag is None or element_tag == tag)
            and (attribute is None or attribute in attrs)
        ]


@override_settings(RATE_LIMIT_ENABLED=False)
class ShareEditorPageTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='editor-author',
            password='password123',
        )
        self.share = Share.objects.create(
            title='待编辑的公开分享',
            strategy_code='[stgy:editor-ui]',
            description='<p>保留的描述</p>',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

    def response_markup(self, response):
        markup = response.content.decode(response.charset)
        probe = _MarkupProbe()
        probe.feed(markup)
        return markup, probe

    def assert_shared_editor_contract(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'shares/includes/share_editor_form.html',
            [template.name for template in response.templates],
        )

        markup, probe = self.response_markup(response)
        self.assertEqual(len(probe.matching(tag='form', attribute='data-share-editor')), 1)
        self.assertEqual(len(probe.editor_form_csrf_inputs), 1)
        self.assertTrue(probe.editor_form_csrf_inputs[0].get('value'))
        self.assertEqual(len(probe.matching(tag='h1')), 1)
        self.assertGreaterEqual(len(probe.matching(tag='fieldset')), 1)
        self.assertGreaterEqual(len(probe.matching(tag='legend')), 1)

        source = probe.matching(attribute='data-share-description-source')
        self.assertEqual(len(source), 1)
        self.assertNotIn('hidden', source[0])
        self.assertNotIn('d-none', source[0].get('class', '').split())

        rich_text_shell = probe.matching(attribute='data-share-rich-text-shell')
        self.assertEqual(len(rich_text_shell), 1)
        self.assertIn('hidden', rich_text_shell[0])
        self.assertEqual(
            len(probe.matching(attribute='data-share-rich-text-editor')),
            1,
        )
        return markup, probe

    def test_anonymous_create_explains_ownership_consequences(self):
        response = self.client.get(reverse('create_share'))

        markup, probe = self.assert_shared_editor_contract(response)
        self.assertEqual(
            len(probe.matching(attribute='data-anonymous-share-notice')),
            1,
        )
        page_text = _visible_text(markup)
        self.assertRegex(page_text, r'(?:未登录|匿名).*?(?:不会|无法).*?绑定.*?账号')
        self.assertRegex(page_text, r'(?:无法|不能).*?编辑.*?(?:删除|管理)')

    def test_public_edit_explains_that_changes_require_review(self):
        self.client.force_login(self.author)

        response = self.client.get(
            reverse('edit_share', args=[self.share.share_id]),
        )

        markup, probe = self.assert_shared_editor_contract(response)
        self.assertEqual(
            len(probe.matching(attribute='data-public-review-notice')),
            1,
        )
        page_text = _visible_text(markup)
        self.assertRegex(page_text, r'公开.*?(?:修改|保存).*?重新(?:进入)?审核')
        self.assertIn('name="version"', markup)

    def test_restricted_edit_explains_that_saving_does_not_lift_restriction(self):
        self.share.restriction_state = Share.RestrictionState.REPORT_TAKEDOWN
        self.share.restriction_reason = '管理员确认的下架原因'
        self.share.restricted_at = timezone.now()
        self.share.restricted_by = self.author
        self.share.visibility = Share.Visibility.PRIVATE
        self.share.status = Share.Status.REJECTED
        self.share.review_feedback = '复审后仍需修改标题'
        self.share.save(
            update_fields=[
                'restriction_state',
                'restriction_reason',
                'restricted_at',
                'restricted_by',
                'visibility',
                'status',
                'review_feedback',
            ],
        )
        self.client.force_login(self.author)

        response = self.client.get(
            reverse('edit_share', args=[self.share.share_id]),
        )

        markup, probe = self.assert_shared_editor_contract(response)
        self.assertEqual(
            len(probe.matching(attribute='data-share-restriction-notice')),
            1,
        )
        page_text = _visible_text(markup)
        self.assertIn('管理员确认的下架原因', page_text)
        self.assertIn('复审后仍需修改标题', page_text)
        self.assertRegex(page_text, r'保存实际修改后.*?重新提交审核.*?审核通过')
        self.assertIn('保存并重新提交审核', page_text)
        self.assertIn('按照你选择的可见范围开放', page_text)
        self.assertNotIn('其他用户才能访问', page_text)
        submit = probe.matching(attribute='data-share-editor-submit')
        self.assertEqual(len(submit), 1)
        self.assertIn(
            'share-editor-restriction-notice',
            submit[0].get('aria-describedby', '').split(),
        )

    def test_review_rejected_edit_uses_the_review_specific_explanation(self):
        self.share.restriction_state = Share.RestrictionState.REVIEW_REJECTED
        self.share.restriction_reason = '需要补充内容说明'
        self.share.restricted_at = timezone.now()
        self.share.restricted_by = self.author
        self.share.save(update_fields=[
            'restriction_state',
            'restriction_reason',
            'restricted_at',
            'restricted_by',
        ])
        self.client.force_login(self.author)

        response = self.client.get(
            reverse('edit_share', args=[self.share.share_id]),
        )

        markup, probe = self.assert_shared_editor_contract(response)
        self.assertEqual(
            len(probe.matching(attribute='data-share-restriction-notice')),
            1,
        )
        page_text = _visible_text(markup)
        self.assertIn('尚未通过内容审核', page_text)
        self.assertNotIn('因举报处理下架', page_text)

    def test_create_validation_errors_have_a_linked_alert_summary(self):
        response = self.client.post(
            reverse('create_share'),
            {
                'title': '',
                'strategy_code': '',
                'description': '',
                'category': Share.Category.ENTERTAINMENT,
                'visibility': Share.Visibility.PUBLIC,
            },
        )

        markup, probe = self.assert_shared_editor_contract(response)
        summaries = probe.matching(attribute='data-form-error-summary')
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].get('role'), 'alert')
        self.assertIn('href="#id_title"', markup)
        self.assertIn('href="#id_strategy_code"', markup)

    def test_description_error_summary_targets_the_visible_field_container(self):
        response = self.client.post(
            reverse('create_share'),
            {
                'title': '描述过长',
                'strategy_code': '[stgy:description-error]',
                'description': 'x' * (RICH_TEXT_MAX_LENGTH + 1),
                'category': Share.Category.ENTERTAINMENT,
                'visibility': Share.Visibility.UNLISTED,
            },
        )

        markup, probe = self.assert_shared_editor_contract(response)
        self.assertIn('href="#share-editor-description-field"', markup)
        description_fields = [
            attrs
            for attrs in probe.matching(attribute='data-share-field')
            if attrs.get('data-share-field') == 'description'
        ]
        self.assertEqual(len(description_fields), 1)
        self.assertEqual(description_fields[0].get('id'), 'share-editor-description-field')
        self.assertEqual(description_fields[0].get('tabindex'), '-1')

    def test_missing_edit_version_is_visible_in_the_error_summary(self):
        self.client.force_login(self.author)
        response = self.client.post(
            reverse('edit_share', args=[self.share.share_id]),
            {
                'title': '不会保存的标题',
                'strategy_code': self.share.strategy_code,
                'description': self.share.description,
                'category': self.share.category,
                'visibility': self.share.visibility,
            },
        )

        markup, probe = self.assert_shared_editor_contract(response)
        summaries = probe.matching(attribute='data-form-error-summary')
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].get('role'), 'alert')
        self.assertIn('编辑页面缺少版本信息', _visible_text(markup))
        self.share.refresh_from_db()
        self.assertEqual(self.share.title, '待编辑的公开分享')
