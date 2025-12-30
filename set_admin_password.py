import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ffxivshare.settings')
django.setup()

from django.contrib.auth.models import User

user = User.objects.get(username='admin')
user.set_password('admin123')
user.save()
print('管理员密码已设置为: admin123')
