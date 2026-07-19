"""Seed the isolated browser-test database with deterministic site data."""

import os
from pathlib import Path
import sys

project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ffxivshare.settings')

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402

from shares.models import Share  # noqa: E402


test_root_value = os.environ.get('FFXIVSHARE_E2E_ROOT', '')
if settings.APP_ENV != 'test' or not test_root_value:
    raise RuntimeError('Browser data may only be seeded in the isolated test environment.')

test_root = Path(test_root_value).resolve()
database_path = Path(settings.DATABASES['default']['NAME']).resolve()
media_root = Path(settings.MEDIA_ROOT).resolve()
if not database_path.is_relative_to(test_root) or not media_root.is_relative_to(test_root):
    raise RuntimeError('Browser-test database and media paths must stay inside the test root.')
if User.objects.exists() or Share.objects.exists():
    raise RuntimeError('Browser-test database must be empty before seeding.')

author = User.objects.create_user(
    username='e2e-user',
    password='E2e-password-42',
)
author.profile.nickname = '自动化测试作者'
author.profile.save(update_fields=['nickname'])

common = {
    'author': author,
    'description': '<p>用于真实浏览器回归的隔离测试数据。</p>',
    'status': Share.Status.APPROVED,
    'visibility': Share.Visibility.PUBLIC,
}
Share.objects.create(
    share_id='2a3b4c5d',
    title='普通公开分享',
    strategy_code='[stgy:e2e-standard]',
    category=Share.Category.ENTERTAINMENT,
    **common,
)
Share.objects.create(
    share_id='3e4f5g6h',
    title='剧透内容分享',
    strategy_code='[stgy:e2e-spoiler]',
    category=Share.Category.COMBAT,
    is_spoiler=True,
    **common,
)
Share.objects.create(
    share_id='4j5k6m7n',
    title='令人不适内容分享',
    strategy_code='[stgy:e2e-sensitive]',
    category=Share.Category.ENTERTAINMENT,
    is_nsfw=True,
    **common,
)
