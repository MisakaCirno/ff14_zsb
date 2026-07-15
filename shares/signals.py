from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import UserProfile


@receiver(
    post_save,
    sender=get_user_model(),
    dispatch_uid='shares.ensure_user_profile_on_creation',
)
def ensure_user_profile_on_creation(
    sender,
    instance,
    created,
    raw=False,
    using=None,
    **kwargs,
):
    """Ensure every normally-created account starts with one profile row."""
    if created and not raw:
        UserProfile.objects.using(using).get_or_create(user_id=instance.pk)
