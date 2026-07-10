from shares.models import ShareLog


def log_share_action(user, share, action_type, details=''):
    """Record one authenticated share action inside the caller's transaction."""
    if not user.is_authenticated or not share:
        return None
    return ShareLog.objects.create(
        user=user,
        share=share,
        action=action_type,
        details=details,
    )
