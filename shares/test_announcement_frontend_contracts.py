import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Announcement


class AnnouncementFrontendSourceContractTests(SimpleTestCase):
    def read_template(self):
        return (
            Path(settings.BASE_DIR) / 'templates' / 'shares' / 'announcement_list.html'
        ).read_text(encoding='utf-8')

    def read_styles(self):
        return (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / 'styles' / 'announcement-page.css'
        ).read_text(encoding='utf-8')

    def read_main_styles(self):
        return (
            Path(settings.BASE_DIR) / 'frontend' / 'src' / 'styles' / 'main.css'
        ).read_text(encoding='utf-8')

    def test_page_uses_semantic_list_and_shared_components(self):
        source = self.read_template()

        self.assertIn(
            'data-announcement-page aria-labelledby="announcement-page-title"',
            source,
        )
        self.assertIn(
            '<h1 class="announcement-page__title" id="announcement-page-title">',
            source,
        )
        self.assertIn('<ol class="announcement-list" data-announcement-list>', source)
        self.assertIn('<article', source)
        self.assertIn('aria-labelledby="announcement-title-', source)
        self.assertIn('datetime="{{ announcement.created_at|date:\'c\' }}"', source)
        self.assertIn(
            "{% include 'shares/includes/empty_state.html' with "
            "icon='bi-megaphone' title='暂无站点动态'",
            source,
        )
        self.assertIn(
            "{% include 'shares/includes/pagination.html' with "
            "page_obj=announcements aria_label='站点动态分页' "
            "nav_class='announcement-page__pagination' only %}",
            source,
        )

        self.assertNotIn('list-group', source)
        self.assertNotIn('list-group-item-action', source)
        self.assertNotIn('announcements.paginator.page_range', source)
        self.assertNotIn('href="?page=', source)
        self.assertNotIn('style="', source)
        for legacy_utility in (
            'class="container',
            'd-flex',
            'justify-content-between',
            'align-items-center',
            'text-break',
            'bg-light',
            'shadow-sm',
        ):
            with self.subTest(legacy_utility=legacy_utility):
                self.assertNotIn(legacy_utility, source)

    def test_staff_visibility_and_sanitization_contracts_are_preserved(self):
        source = self.read_template()

        self.assertIn('{% if user.is_staff or user.is_superuser %}', source)
        self.assertIn(
            '<form method="post" action="{% url '
            "'toggle_announcement_visibility' announcement.id %}",
            source,
        )
        self.assertIn('{% csrf_token %}', source)
        self.assertIn('type="hidden" name="is_active"', source)
        self.assertIn(
            "value=\"{% if announcement.is_active %}0{% else %}1{% endif %}\"",
            source,
        )
        self.assertIn('{{ announcement.content|sanitize_html }}', source)

    def test_page_styles_wrap_long_content_and_stack_staff_actions(self):
        styles = self.read_styles()

        self.assertIn("@import './announcement-page.css';", self.read_main_styles())
        self.assertIn('container-name: announcement-page;', styles)
        self.assertIn('container-type: inline-size;', styles)
        self.assertRegex(
            styles,
            re.compile(
                r'\.announcement-card__title\s*\{[^}]*overflow-wrap:\s*anywhere;',
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r'@container announcement-page \(max-width: 32rem\)\s*\{.*?'
                r'\.announcement-card__header\s*\{[^}]*'
                r'grid-template-columns:\s*1fr;.*?'
                r'\.announcement-card__actions,\s*'
                r'\.announcement-card__visibility-form,\s*'
                r'\.announcement-card__visibility-toggle\s*\{[^}]*width:\s*100%;',
                re.DOTALL,
            ),
        )
        self.assertIn('@media (max-width: 575.98px)', styles)
        self.assertNotRegex(styles, re.compile(r'#[0-9a-f]{3,8}', re.IGNORECASE))


class AnnouncementFrontendRenderContractTests(TestCase):
    def test_empty_state_and_shared_pagination_render_accessibly(self):
        empty_response = self.client.get(reverse('announcement_list'))
        empty_content = empty_response.content.decode()

        self.assertEqual(empty_response.status_code, 200)
        self.assertIn('data-announcement-empty-state', empty_content)
        self.assertIn('class="card empty-state"', empty_content)
        self.assertEqual(empty_content.count('<h1'), 1)

        for index in range(11):
            Announcement.objects.create(
                title=f'动态 {index}',
                content=f'<p>动态正文 {index}</p>',
                is_active=True,
            )

        first_response = self.client.get(reverse('announcement_list'))
        first_content = first_response.content.decode()

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_content.count('data-announcement-item'), 10)
        self.assertIn('aria-label="站点动态分页"', first_content)
        self.assertIn('aria-current="page"', first_content)
        self.assertIn('aria-disabled="true"', first_content)
        self.assertIn('rel="next"', first_content)

        last_response = self.client.get(reverse('announcement_list'), {'page': 2})
        last_content = last_response.content.decode()

        self.assertEqual(last_response.status_code, 200)
        self.assertEqual(last_content.count('data-announcement-item'), 1)
        self.assertIn('rel="prev"', last_content)
        self.assertIn('aria-current="page"', last_content)
        self.assertIn('aria-disabled="true"', last_content)

    def test_staff_visibility_controls_keep_post_contract(self):
        admin = User.objects.create_user(
            username='announcement-admin',
            password='password123',
            is_staff=True,
        )
        visible = Announcement.objects.create(
            title='公开动态',
            content='<p>公开内容</p>',
            is_active=True,
        )
        hidden = Announcement.objects.create(
            title='隐藏动态',
            content='<p>隐藏内容</p>',
            is_active=False,
        )
        self.client.force_login(admin)

        response = self.client.get(reverse('announcement_list'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="csrfmiddlewaretoken"', content)
        self.assertIn(
            f'action="{reverse("toggle_announcement_visibility", args=[visible.id])}"',
            content,
        )
        self.assertIn(
            f'action="{reverse("toggle_announcement_visibility", args=[hidden.id])}"',
            content,
        )
        self.assertIn('name="is_active" value="0"', content)
        self.assertIn('name="is_active" value="1"', content)
        self.assertIn('aria-label="隐藏站点动态《公开动态》"', content)
        self.assertIn('aria-label="显示站点动态《隐藏动态》"', content)
        self.assertIn('data-announcement-state="hidden"', content)
