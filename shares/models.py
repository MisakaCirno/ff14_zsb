from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from nanoid import generate


class UserProfile(models.Model):
    """用户资料扩展模型"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile', verbose_name='用户')
    nickname = models.CharField(max_length=50, blank=True, verbose_name='昵称')
    bio = models.TextField(blank=True, verbose_name='个人简介')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '用户资料'
        verbose_name_plural = '用户资料'

    def __str__(self):
        return f"{self.user.username} 的资料"

    def get_display_name(self):
        """获取显示名称（优先使用昵称）"""
        return self.nickname if self.nickname else self.user.username


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    """创建用户时自动创建用户资料"""
    if created:
        UserProfile.objects.create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    """保存用户时自动保存用户资料"""
    if hasattr(instance, 'profile'):
        instance.profile.save()


class Share(models.Model):
    """战术板分享模型"""
    share_id = models.CharField(max_length=21, unique=True, editable=False, db_index=True)
    title = models.CharField(max_length=200, verbose_name='标题')
    strategy_code = models.TextField(verbose_name='战术板代码')
    description = models.TextField(blank=True, verbose_name='描述')
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shares', verbose_name='作者')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    is_public = models.BooleanField(default=True, verbose_name='公开')
    views = models.IntegerField(default=0, verbose_name='浏览量')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '战术板分享'
        verbose_name_plural = '战术板分享'

    def save(self, *args, **kwargs):
        if not self.share_id:
            self.share_id = generate(size=10)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.share_id})"

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('share_detail', kwargs={'share_id': self.share_id})
