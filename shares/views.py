"""Legacy view imports kept for third-party and deployment compatibility.

New URL routing and application code should import from ``shares.web`` modules
directly.  This facade can be removed after downstream integrations have moved
off the historical ``shares.views`` import path.
"""

from .selectors import admin_task_counts as get_admin_counts
from .services.audit import log_share_action
from .services.messages import send_site_message
from .web.accounts import (
    password_change,
    profile_edit,
    register,
    user_login,
    user_logout,
)
from .web.api import get_collection_codes, get_share_code
from .web.browse import (
    about,
    announcement_list,
    index,
    page_not_found,
    search,
    set_home_feed_mode,
    toggle_announcement_visibility,
    user_public_profile,
)
from .web.collections import (
    add_share_to_collection,
    collection_detail,
    create_collection,
    delete_collection,
    edit_collection,
    remove_share_from_collection,
)
from .web.content import create_share, delete_share, edit_share, my_shares, share_detail
from .web.interactions import record_copy, record_view, toggle_favorite, toggle_like
from .web.messages import (
    mark_all_site_messages_read,
    open_site_message,
    site_message_detail,
    site_message_list,
)
from .web.moderation import (
    admin_approve_share,
    admin_reject_share,
    admin_takedown_share,
    admin_report_list,
    admin_report_logs,
    admin_report_share,
    admin_resolve_report,
    admin_resolve_share_reports,
    admin_review_list,
    admin_review_logs,
    report_share,
)

__all__ = [
    'about',
    'add_share_to_collection',
    'admin_approve_share',
    'admin_reject_share',
    'admin_takedown_share',
    'admin_report_list',
    'admin_report_logs',
    'admin_report_share',
    'admin_resolve_report',
    'admin_resolve_share_reports',
    'admin_review_list',
    'admin_review_logs',
    'announcement_list',
    'collection_detail',
    'create_collection',
    'create_share',
    'delete_collection',
    'delete_share',
    'edit_collection',
    'edit_share',
    'get_admin_counts',
    'get_collection_codes',
    'get_share_code',
    'index',
    'log_share_action',
    'mark_all_site_messages_read',
    'my_shares',
    'open_site_message',
    'page_not_found',
    'password_change',
    'profile_edit',
    'record_copy',
    'record_view',
    'register',
    'remove_share_from_collection',
    'report_share',
    'search',
    'send_site_message',
    'set_home_feed_mode',
    'share_detail',
    'site_message_detail',
    'site_message_list',
    'toggle_announcement_visibility',
    'toggle_favorite',
    'toggle_like',
    'user_login',
    'user_logout',
    'user_public_profile',
]
