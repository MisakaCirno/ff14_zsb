from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
import random


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
    author = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shares', verbose_name='作者', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')
    
    class Visibility(models.TextChoices):
        PUBLIC = 'public', '公开'
        UNLISTED = 'unlisted', '不公开 (仅链接/ID可见)'
        PRIVATE = 'private', '私有 (仅自己可见)'

    class Status(models.TextChoices):
        PENDING = 'pending', '待审核'
        APPROVED = 'approved', '已通过'
        REJECTED = 'rejected', '已拒绝'

    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        default=Visibility.PUBLIC,
        verbose_name='可见性'
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.APPROVED,
        verbose_name='审核状态'
    )
    
    views = models.IntegerField(default=0, verbose_name='浏览量')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '战术板分享'
        verbose_name_plural = '战术板分享'

    def save(self, *args, **kwargs):
        if not self.share_id:
            self.share_id = self._generate_unique_id()
        super().save(*args, **kwargs)

    def _generate_unique_id(self):
        """生成符合规则的唯一ID"""
        # 规则：8位，数字和字母交替，数字不含01，字母不含oil
        digits = '23456789'
        letters = 'abcdefghjkmnpqrstuvwxyz'
        blacklist = ['b2b', 'c4', 'j8', 'm9', '3p', '8x8']
        
        while True:
            # 生成8位字符：数字-字母-数字-字母-数字-字母-数字-字母
            chars = []
            for i in range(8):
                if i % 2 == 0:
                    chars.append(random.choice(digits))
                else:
                    chars.append(random.choice(letters))
            
            new_id = ''.join(chars)
            
            # 检查黑名单
            is_valid = True
            for bad_word in blacklist:
                if bad_word in new_id:
                    is_valid = False
                    break
            
            if not is_valid:
                continue
                
            # 检查唯一性
            if not Share.objects.filter(share_id=new_id).exists():
                return new_id

    def __str__(self):
        return f"{self.title} ({self.share_id})"

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('share_detail', kwargs={'share_id': self.share_id})


class Report(models.Model):
    """举报模型"""
    share = models.ForeignKey(Share, on_delete=models.CASCADE, related_name='reports', verbose_name='被举报分享')
    reporter = models.ForeignKey(User, on_delete=models.CASCADE, related_name='submitted_reports', verbose_name='举报人')
    reason = models.TextField(verbose_name='举报原因')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='举报时间')
    
    class Status(models.TextChoices):
        PENDING = 'pending', '待处理'
        RESOLVED = 'resolved', '已处理(认可)'
        DISMISSED = 'dismissed', '已驳回'

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name='处理状态'
    )
    
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name='处理时间')
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='resolved_reports', verbose_name='处理人')

    class Meta:
        ordering = ['-created_at']
        verbose_name = '举报'
        verbose_name_plural = '举报'

    def __str__(self):
        return f"举报: {self.share.title} - {self.get_status_display()}"
