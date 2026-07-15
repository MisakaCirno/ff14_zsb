from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from shares.models import UserProfile
from shares.validation import PROFILE_BIO_MAX_LENGTH, PROFILE_MISSING_VERSION


PROFILE_EDITABLE_FIELDS = (
    'nickname',
    'bio',
    'home_feed_mode',
)


class ProfileUnavailableError(RuntimeError):
    """The account disappeared while its profile mutation was in flight."""


class ProfileEditConflictError(RuntimeError):
    """The profile existence or timestamp changed after the page was opened."""


class ProfileBioTooLongError(ValueError):
    """A mutation attempted to introduce a biography above the current limit."""


@dataclass(frozen=True, slots=True)
class ProfileMutationResult:
    profile: UserProfile
    changed_fields: tuple[str, ...]
    created: bool = False

    @property
    def changed(self):
        return self.created or bool(self.changed_fields)


def _validate_changed_bio(*, changed_fields, bio):
    if 'bio' in changed_fields and len(bio) > PROFILE_BIO_MAX_LENGTH:
        raise ProfileBioTooLongError


@transaction.atomic
def update_user_profile_from_form(*, form, actor):
    """Create or update a profile under one per-user concurrency boundary."""
    User = get_user_model()
    try:
        locked_user = User.objects.select_for_update().get(pk=actor.pk)
    except User.DoesNotExist as exc:
        raise ProfileUnavailableError from exc

    profile = (
        UserProfile.objects.select_for_update()
        .filter(user=locked_user)
        .first()
    )
    expected_version = form.cleaned_data.get('version')

    if profile is None:
        if expected_version != PROFILE_MISSING_VERSION:
            raise ProfileEditConflictError

        values = {
            field_name: form.cleaned_data[field_name]
            for field_name in PROFILE_EDITABLE_FIELDS
        }
        _validate_changed_bio(
            changed_fields=PROFILE_EDITABLE_FIELDS,
            bio=values['bio'],
        )
        try:
            # The savepoint keeps the outer transaction usable if a writer
            # that does not share our User-row lock wins the unique race.
            with transaction.atomic():
                profile = UserProfile.objects.create(user=locked_user, **values)
        except IntegrityError as exc:
            raise ProfileEditConflictError from exc
        return ProfileMutationResult(
            profile=profile,
            changed_fields=PROFILE_EDITABLE_FIELDS,
            created=True,
        )

    if (
        expected_version == PROFILE_MISSING_VERSION
        or expected_version is None
        or profile.updated_at != expected_version
    ):
        raise ProfileEditConflictError

    changed_fields = tuple(
        field_name
        for field_name in PROFILE_EDITABLE_FIELDS
        if form.cleaned_data[field_name] != getattr(profile, field_name)
    )
    _validate_changed_bio(
        changed_fields=changed_fields,
        bio=form.cleaned_data['bio'],
    )
    if not changed_fields:
        return ProfileMutationResult(profile=profile, changed_fields=())

    for field_name in changed_fields:
        setattr(profile, field_name, form.cleaned_data[field_name])
    profile.save(update_fields=[*changed_fields, 'updated_at'])
    return ProfileMutationResult(
        profile=profile,
        changed_fields=changed_fields,
    )
