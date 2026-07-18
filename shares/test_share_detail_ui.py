import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Collection, CollectionItem, Share, ShareLog


class ShareDetailUiTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(
            username='detail-author',
            password='password123',
        )
        self.author.profile.nickname = '详情作者'
        self.author.profile.bio = '这是一段可以自然换行的作者简介。'
        self.author.profile.save(update_fields=['nickname', 'bio'])
        self.viewer = User.objects.create_user(
            username='detail-viewer',
            password='password123',
        )
        self.moderator = User.objects.create_user(
            username='detail-moderator',
            password='password123',
            is_staff=True,
        )
        self.share = Share.objects.create(
            title='当前第七项 <测试>',
            strategy_code='[stgy:detail-ui]',
            description='<p>先确认布局，再复制代码。</p>',
            author=self.author,
            category=Share.Category.COMBAT,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
            is_spoiler=True,
            is_nsfw=True,
            is_original=True,
            views=12,
            copies=5,
        )
        self.collection = Collection.objects.create(
            title='一个需要自然换行的相关合集标题',
            author=self.author,
            is_public=True,
        )
        for position in range(6):
            item_share = Share.objects.create(
                title=f'前置内容 {position + 1}',
                strategy_code=f'[stgy:detail-ui-{position}]',
                author=self.author,
                visibility=Share.Visibility.PUBLIC,
                status=Share.Status.APPROVED,
            )
            CollectionItem.objects.create(
                collection=self.collection,
                share=item_share,
                order=position,
            )
        CollectionItem.objects.create(
            collection=self.collection,
            share=self.share,
            order=6,
        )

    def detail_url(self, share=None):
        target = share or self.share
        return reverse('share_detail', args=[target.share_id])

    def test_anonymous_detail_starts_with_identity_and_one_combined_warning(self):
        response = self.client.get(self.detail_url())
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(content.count('<h1'), 1)
        self.assertLess(
            content.index('class="share-detail-hero"'),
            content.index('class="share-detail-panel share-detail-preview"'),
        )
        self.assertContains(response, 'data-content-revealed="false"')
        self.assertContains(response, 'data-content-warning="nsfw-spoiler"')
        self.assertEqual(content.count('data-content-overlay'), 1)
        self.assertContains(response, 'data-reveal-content')
        self.assertContains(response, '此分享可能包含令人不适和剧透内容。')
        self.assertContains(response, '登录后点赞，当前 0 个点赞')
        self.assertContains(response, '登录后收藏，当前 0 个收藏')
        self.assertNotContains(response, 'id="share-detail-actions-title"')
        self.assertRegex(
            content,
            re.compile(
                r'<button[^>]+data-generate-share-image[^>]+'
                r'aria-controls="shareImageModal"[^>]+'
                r'aria-describedby="share-image-warning-help" disabled>',
                re.DOTALL,
            ),
        )

    def test_owner_detail_exposes_actions_modal_and_current_collection_item(self):
        self.client.force_login(self.author)
        response = self.client.get(
            self.detail_url(),
            {'collection_id': self.collection.id},
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'hx-post="/share/')
        self.assertContains(response, '?fragment=detail"')
        self.assertContains(response, '添加到合集')
        self.assertContains(response, '编辑分享')
        self.assertContains(response, '删除分享')
        self.assertNotContains(response, '举报此分享')
        self.assertContains(response, 'id="addToCollectionModal"')
        self.assertContains(response, 'aria-labelledby="add-to-collection-title"')
        self.assertRegex(
            content,
            re.compile(
                rf'data-related-collection="{self.collection.id}"\s+open',
                re.DOTALL,
            ),
        )
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, '当前第七项 &lt;测试&gt;')

    def test_moderator_logs_use_native_disclosure_without_tabs(self):
        ShareLog.objects.create(
            share=self.share,
            user=self.moderator,
            action=ShareLog.ActionType.OTHER,
            details='只读审计记录',
        )
        self.client.force_login(self.moderator)

        response = self.client.get(self.detail_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<details class="share-detail-disclosure" data-share-logs>')
        self.assertContains(response, '操作日志')
        self.assertContains(response, '只读审计记录')
        self.assertNotContains(response, 'data-bs-toggle="tab"')
        self.assertNotContains(response, 'nav-tabs')

    def test_unwarned_detail_enables_share_image_generation(self):
        plain_share = Share.objects.create(
            title='普通分享',
            strategy_code='[stgy:plain-detail-ui]',
            author=self.author,
            visibility=Share.Visibility.PUBLIC,
            status=Share.Status.APPROVED,
        )

        response = self.client.get(self.detail_url(plain_share))
        content = response.content.decode()
        generate_button = re.search(
            r'<button[^>]+data-generate-share-image[^>]*>',
            content,
            re.DOTALL,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-content-revealed="true"')
        self.assertNotContains(response, 'data-content-overlay')
        self.assertIsNotNone(generate_button)
        self.assertNotIn('disabled', generate_button.group(0))

    def test_inputs_and_share_image_modal_have_accessible_names(self):
        response = self.client.get(self.detail_url())

        self.assertContains(response, 'for="share-detail-code"')
        self.assertContains(response, 'id="share-detail-code"')
        self.assertContains(response, 'for="share-detail-url"')
        self.assertContains(response, 'id="share-detail-url"')
        self.assertContains(response, 'aria-labelledby="share-image-modal-title"')
        self.assertContains(response, 'aria-describedby="share-image-modal-help"')
        self.assertContains(response, 'aria-label="关闭分享图片预览"')
        self.assertContains(response, 'role="img"')
        self.assertContains(response, '当前浏览器无法显示生成的分享图片预览。')

    def test_description_headings_remain_below_the_page_and_section_titles(self):
        self.share.description = '<h1>第一阶段</h1><h2>处理细节</h2>'
        self.share.save(update_fields=['description'])

        response = self.client.get(self.detail_url())
        content = response.content.decode()

        self.assertEqual(content.count('<h1'), 1)
        self.assertContains(response, '<h4>第一阶段</h4>', html=True)
        self.assertContains(response, '<h5>处理细节</h5>', html=True)

    def test_htmx_overlay_is_a_read_only_permission_aware_fragment(self):
        response = self.client.get(
            self.detail_url(),
            {'presentation': 'overlay'},
            HTTP_HX_REQUEST='true',
        )
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'shares/includes/share_detail_overlay.html')
        self.assertNotContains(response, '<!DOCTYPE html>')
        self.assertContains(response, 'data-share-detail-overlay')
        self.assertContains(response, 'data-share-detail-dialog-close')
        self.assertContains(response, '完整详情')
        self.assertEqual(content.count('<h1'), 1)
        self.assertIn('HX-Request', response.headers['Vary'])
        self.assertIn('Cookie', response.headers['Vary'])
        self.share.refresh_from_db()
        self.assertEqual(self.share.views, 12)

    def test_overlay_respects_explicit_content_display_preferences(self):
        shown = self.client.get(
            self.detail_url(),
            {
                'presentation': 'overlay',
                'spoiler': 'show',
                'nsfw': 'show',
            },
            HTTP_HX_REQUEST='true',
        )
        nsfw_masked = self.client.get(
            self.detail_url(),
            {
                'presentation': 'overlay',
                'spoiler': 'show',
                'nsfw': 'mask',
            },
            HTTP_HX_REQUEST='true',
        )

        self.assertContains(shown, 'data-content-revealed="true"')
        self.assertNotContains(shown, 'data-content-overlay')
        self.assertContains(nsfw_masked, 'data-content-warning="nsfw"')
        self.assertContains(nsfw_masked, '此分享可能包含令人不适的内容。')
        self.assertNotContains(nsfw_masked, '令人不适和剧透')

    def test_overlay_requires_htmx_and_keeps_full_page_fallback(self):
        response = self.client.get(
            self.detail_url(),
            {'presentation': 'overlay'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'shares/detail.html')
        self.assertContains(response, '<!DOCTYPE html>')
        self.assertNotContains(response, 'data-share-detail-overlay')

    def test_authenticated_overlay_uses_unique_interaction_fragment(self):
        self.client.force_login(self.viewer)

        response = self.client.get(
            self.detail_url(),
            {'presentation': 'overlay'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '?fragment=overlay"')
        self.assertContains(response, f'id="btn-like-overlay-{self.share.share_id}"')
        self.assertContains(
            response,
            f'id="share-interaction-form-overlay-{self.share.share_id}"',
        )

    def test_overlay_reaction_post_returns_the_same_unique_fragment(self):
        self.client.force_login(self.viewer)

        response = self.client.post(
            reverse('toggle_like', args=[self.share.share_id]) + '?fragment=overlay',
            {'target_state': 'active'},
            HTTP_HX_REQUEST='true',
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="btn-like-overlay-{self.share.share_id}"')
        self.assertContains(
            response,
            f'form="share-interaction-form-overlay-{self.share.share_id}"',
        )
        self.assertContains(response, 'aria-pressed="true"')
        self.assertTrue(self.share.likes.filter(pk=self.viewer.pk).exists())

    def test_overlay_cannot_bypass_private_share_permissions(self):
        private_share = Share.objects.create(
            title='私有分享',
            strategy_code='[stgy:private-overlay]',
            author=self.author,
            visibility=Share.Visibility.PRIVATE,
            status=Share.Status.APPROVED,
        )

        full_response = self.client.get(self.detail_url(private_share))
        overlay_response = self.client.get(
            self.detail_url(private_share),
            {'presentation': 'overlay'},
            HTTP_HX_REQUEST='true',
        )

        self.assertRedirects(full_response, reverse('index'))
        self.assertRedirects(overlay_response, reverse('index'))
