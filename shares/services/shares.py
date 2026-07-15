from dataclasses import dataclass

from django.db import transaction
from django.db.models import Max

from shares.models import Collection, CollectionItem, Share, ShareLog
from shares.policies import is_moderator

from .audit import log_share_action


SHARE_EDITABLE_FIELDS = (
    'title',
    'strategy_code',
    'description',
    'category',
    'visibility',
    'is_spoiler',
    'is_nsfw',
    'is_original',
)

_SHARE_FIELD_LABELS = {
    'title': '标题',
    'strategy_code': '战术板代码',
    'description': '描述',
    'category': '分类',
    'visibility': '可见性',
    'is_spoiler': '剧透标记',
    'is_nsfw': '不适内容标记',
    'is_original': '原创标记',
}


class CollectionUnavailableError(RuntimeError):
    """所选合集在表单校验后已不可用。"""


class ShareEditConflictError(RuntimeError):
    """分享在编辑页打开后已被其他操作更新。"""


@dataclass(frozen=True, slots=True)
class ShareMutationResult:
    share: Share
    changed_fields: tuple[str, ...]

    @property
    def changed(self):
        return bool(self.changed_fields)

    @property
    def requires_review(self):
        return self.share.status == Share.Status.PENDING


def _submission_status(actor, visibility):
    if (
        actor is not None
        and actor.is_authenticated
        and visibility == Share.Visibility.PUBLIC
        and not is_moderator(actor)
    ):
        return Share.Status.PENDING
    return Share.Status.APPROVED


@transaction.atomic
def create_share_from_form(*, form, actor):
    """原子创建分享、审计日志和可选合集关联。"""
    share = form.save(commit=False)
    if actor is not None and actor.is_authenticated:
        share.author = actor
    else:
        share.author = None
        share.visibility = Share.Visibility.UNLISTED
    share.status = _submission_status(actor, share.visibility)
    share.save()

    if actor is not None and actor.is_authenticated:
        log_share_action(actor, share, ShareLog.ActionType.CREATE, '用户创建分享')

        selected_collection = form.cleaned_data.get('collection_id')
        if selected_collection is not None:
            try:
                collection = Collection.objects.select_for_update().get(
                    pk=selected_collection.pk,
                    author=actor,
                )
            except Collection.DoesNotExist as exc:
                raise CollectionUnavailableError from exc
            max_order = CollectionItem.objects.filter(
                collection=collection,
            ).aggregate(Max('order'))['order__max']
            CollectionItem.objects.create(
                collection=collection,
                share=share,
                order=(max_order or 0) + 1,
            )

    return ShareMutationResult(
        share=share,
        changed_fields=SHARE_EDITABLE_FIELDS,
    )


@transaction.atomic
def update_share_from_form(*, form, actor):
    """锁定最新分享，只应用本次表单真实修改的业务字段。"""
    share = Share.objects.select_for_update().get(
        pk=form.instance.pk,
        author=actor,
    )
    expected_version = form.cleaned_data.get('version')
    if expected_version is None or share.updated_at != expected_version:
        raise ShareEditConflictError

    changed_fields = tuple(
        field_name
        for field_name in SHARE_EDITABLE_FIELDS
        if field_name in form.changed_data
        and form.cleaned_data[field_name] != getattr(share, field_name)
    )
    if not changed_fields:
        return ShareMutationResult(share=share, changed_fields=())

    for field_name in changed_fields:
        setattr(share, field_name, form.cleaned_data[field_name])

    share.status = (
        Share.Status.PENDING
        if share.is_restricted
        else _submission_status(actor, share.visibility)
    )
    share.review_feedback = ''
    share.reviewed_at = None
    share.reviewed_by = None
    share.save(update_fields=[
        *changed_fields,
        'status',
        'review_feedback',
        'reviewed_at',
        'reviewed_by',
        'updated_at',
    ])

    changes = ', '.join(_SHARE_FIELD_LABELS[name] for name in changed_fields)
    log_share_action(
        actor,
        share,
        ShareLog.ActionType.EDIT,
        f'用户编辑内容: {changes}',
    )
    return ShareMutationResult(share=share, changed_fields=changed_fields)
