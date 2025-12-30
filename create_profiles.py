import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ffxivshare.settings')
django.setup()

from django.contrib.auth.models import User
from shares.models import UserProfile

# 为所有现有用户创建 UserProfile
for user in User.objects.all():
    profile, created = UserProfile.objects.get_or_create(user=user)
    if created:
        print(f'为用户 {user.username} 创建了 UserProfile')
    else:
        print(f'用户 {user.username} 已有 UserProfile')

print('\n所有用户的 UserProfile 已创建完成！')
