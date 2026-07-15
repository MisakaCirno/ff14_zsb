from django.db import transaction
from django.utils import timezone
from django.utils.text import Truncator

from shares.models import SiteMessage


SITE_MESSAGE_TITLE_MAX_LENGTH = SiteMessage._meta.get_field('title').max_length


def _bounded_site_message_title(title):
    return Truncator(str(title)).chars(SITE_MESSAGE_TITLE_MAX_LENGTH)


def send_site_message(
    *,
    recipient,
    message_type,
    title,
    content,
    sender=None,
    related_share=None,
    related_report=None,
    metadata=None,
):
    """Create a site message through one shared entry point."""
    if not recipient:
        return None

    return SiteMessage.objects.create(
        recipient=recipient,
        sender=sender if getattr(sender, 'is_authenticated', False) else None,
        message_type=message_type,
        title=_bounded_site_message_title(title),
        content=content,
        related_share=related_share,
        related_report=related_report,
        metadata=metadata or {},
    )


@transaction.atomic
def mark_site_message_read(*, recipient, message_id):
    """Mark one owned message read while preserving the first read time."""
    site_message = SiteMessage.objects.select_for_update().get(
        pk=message_id,
        recipient=recipient,
    )
    changed = site_message.read_at is None
    if changed:
        site_message.read_at = timezone.now()
        site_message.save(update_fields=['read_at'])
    return site_message, changed


def mark_all_inbox_site_messages_read(*, recipient):
    """Mark unread inbox messages read in one idempotent update."""
    return SiteMessage.objects.filter(
        recipient=recipient,
        read_at__isnull=True,
        archived_at__isnull=True,
    ).update(read_at=timezone.now())


@transaction.atomic
def set_site_message_archive_state(*, recipient, message_id, archived):
    """Set an owned message's archive state without toggle races."""
    site_message = SiteMessage.objects.select_for_update().get(
        pk=message_id,
        recipient=recipient,
    )
    changed = (
        (archived and site_message.archived_at is None)
        or (not archived and site_message.archived_at is not None)
    )
    if changed:
        site_message.archived_at = timezone.now() if archived else None
        site_message.save(update_fields=['archived_at'])
    return site_message, changed
