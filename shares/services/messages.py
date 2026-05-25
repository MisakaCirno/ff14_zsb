from shares.models import SiteMessage


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
        title=title,
        content=content,
        related_share=related_share,
        related_report=related_report,
        metadata=metadata or {},
    )
