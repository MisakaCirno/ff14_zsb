from django.template.loader import get_template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .content_sanitizer import sanitize_rich_text
from .models import Announcement, Share


class RichTextSanitizerTests(TestCase):
    def test_preserves_supported_quill_formatting(self):
        source = (
            '<h2 class="ql-align-center">标题</h2>'
            '<p><strong>粗体</strong>和<em>斜体</em></p>'
            '<span class="ql-size-large" style="color: red; background-color: #fff">彩色</span>'
            '<ol><li class="ql-indent-2">列表</li></ol>'
            '<a href="https://example.com" target="_blank">链接</a>'
        )

        cleaned = sanitize_rich_text(source)

        self.assertIn('<h2 class="ql-align-center">标题</h2>', cleaned)
        self.assertIn('<strong>粗体</strong>', cleaned)
        self.assertIn('class="ql-size-large"', cleaned)
        self.assertIn('color:red', cleaned)
        self.assertIn('background-color:#fff', cleaned)
        self.assertIn('class="ql-indent-2"', cleaned)
        self.assertIn('rel="noopener noreferrer nofollow"', cleaned)

    def test_removes_executable_markup_and_unsupported_attributes(self):
        source = (
            '<p onclick="alert(1)" class="evil ql-align-right">正文</p>'
            '<script>alert(1)</script>'
            '<a href="javascript:alert(1)" onmouseover="alert(1)">危险链接</a>'
            '<span style="color: blue; position: fixed">文字</span>'
            '<svg><script>alert(2)</script></svg>'
        )

        cleaned = sanitize_rich_text(source)

        self.assertNotIn('onclick', cleaned)
        self.assertNotIn('onmouseover', cleaned)
        self.assertNotIn('javascript:', cleaned)
        self.assertNotIn('alert(1)', cleaned)
        self.assertNotIn('alert(2)', cleaned)
        self.assertNotIn('class="evil', cleaned)
        self.assertNotIn('position', cleaned)
        self.assertIn('class="ql-align-right"', cleaned)
        self.assertIn('color:blue', cleaned)

    def test_empty_and_repeated_sanitization_are_stable(self):
        self.assertEqual(sanitize_rich_text(None), '')
        self.assertEqual(sanitize_rich_text(''), '')

        cleaned = sanitize_rich_text('<p><strong>安全</strong></p>')
        self.assertEqual(sanitize_rich_text(cleaned), cleaned)


class ClientRenderingSafetyTests(SimpleTestCase):
    def test_base_template_does_not_interpolate_history_or_messages_as_html(self):
        source = get_template('base.html').template.source

        self.assertNotIn('li.innerHTML', source)
        self.assertNotIn('alertDiv.innerHTML', source)
        self.assertIn('title.textContent = itemTitle', source)
        self.assertIn('encodeURIComponent(item.id)', source)
        self.assertIn("messageText.textContent = String(message ?? '')", source)

    def test_share_id_is_escaped_before_embedding_in_history_script(self):
        source = get_template('shares/detail.html').template.source

        self.assertIn('{{ share.share_id|escapejs }}', source)


class RichTextPersistenceTests(TestCase):
    def test_share_description_is_sanitized_when_saved(self):
        share = Share.objects.create(
            title='测试分享',
            strategy_code='[stgy:test]',
            description='<p onload="alert(1)"><strong>正文</strong><script>bad()</script></p>',
        )

        self.assertEqual(share.description, '<p><strong>正文</strong></p>')
        self.assertEqual(Share.objects.get(pk=share.pk).description, share.description)

    def test_announcement_content_is_sanitized_when_saved(self):
        announcement = Announcement.objects.create(
            title='测试动态',
            content='<p>动态</p><iframe src="https://example.com">bad</iframe>',
        )

        self.assertEqual(announcement.content, '<p>动态</p>')
        self.assertEqual(Announcement.objects.get(pk=announcement.pk).content, announcement.content)


class LegacyRichTextRenderingTests(TestCase):
    def test_legacy_share_content_is_sanitized_at_render_time(self):
        share = Share.objects.create(
            title='旧数据',
            strategy_code='[stgy:test]',
            description='<p>初始内容</p>',
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )
        Share.objects.filter(pk=share.pk).update(
            description='<p class="ql-align-center" onclick="bad()"><strong>旧正文</strong></p>'
            '<script>window.legacyXss = true</script>'
        )

        response = self.client.get(reverse('share_detail', args=[share.share_id]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<p class="ql-align-center"><strong>旧正文</strong></p>', content)
        self.assertNotIn('onclick="bad()"', content)
        self.assertNotIn('window.legacyXss', content)

    def test_legacy_announcement_content_is_sanitized_at_render_time(self):
        announcement = Announcement.objects.create(title='旧动态', content='<p>初始内容</p>')
        Announcement.objects.filter(pk=announcement.pk).update(
            content='<p><em>旧动态正文</em></p><a href="javascript:bad()">危险链接</a>'
        )

        response = self.client.get(reverse('announcement_list'))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('<p><em>旧动态正文</em></p>', content)
        self.assertNotIn('javascript:bad()', content)
