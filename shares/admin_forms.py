from django import forms

from .forms import UserProfileVersionField, profile_text_matches_stored_value
from .models import Announcement, Share, UserProfile
from .validation import PROFILE_BIO_MAX_LENGTH, PROFILE_MISSING_VERSION
from .widgets import QuillWidget


class AnnouncementAdminForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = '__all__'
        widgets = {
            'content': QuillWidget(attrs={'data-quill-placeholder': '请输入站点动态内容…'}),
        }


class ShareAdminForm(forms.ModelForm):
    class Meta:
        model = Share
        fields = '__all__'
        widgets = {
            'description': QuillWidget(attrs={'data-quill-placeholder': '请输入分享描述…'}),
        }


class UserProfileAdminForm(forms.ModelForm):
    """Keep legacy long biographies editable without accepting new long text."""

    version = UserProfileVersionField(
        required=True,
        widget=forms.HiddenInput(),
        error_messages={
            'required': '资料版本缺失，请刷新后台页面后重新提交。',
            'invalid': '资料版本无效，请刷新后台页面后重新提交。',
        },
    )

    class Meta:
        model = UserProfile
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # ModelForm strips text by default. Disable that implicit mutation so
        # unchanged legacy values can be preserved exactly.
        self.fields['nickname'].strip = False
        self.fields['bio'].strip = False
        if self.instance and self.instance.pk:
            self.fields['version'].initial = self.instance.updated_at
        else:
            self.fields['version'].initial = PROFILE_MISSING_VERSION

    def clean_version(self):
        expected_version = self.cleaned_data['version']
        if not self.instance or not self.instance.pk:
            if expected_version != PROFILE_MISSING_VERSION:
                raise forms.ValidationError('资料已发生变化，请刷新后台页面。')
            return expected_version

        database = self.instance._state.db or 'default'
        current_version = (
            UserProfile.objects.using(database)
            .filter(pk=self.instance.pk)
            .values_list('updated_at', flat=True)
            .first()
        )
        if (
            current_version is None
            or expected_version == PROFILE_MISSING_VERSION
            or current_version != expected_version
        ):
            raise forms.ValidationError(
                '资料已被其他操作更新，请刷新后台页面后重新编辑。'
            )
        return expected_version

    def clean_user(self):
        user = self.cleaned_data['user']
        if self.instance and self.instance.pk and user.pk != self.instance.user_id:
            raise forms.ValidationError('已有资料不能改绑到其他用户。')
        return user

    def clean_nickname(self):
        nickname = self.cleaned_data['nickname']
        if self.instance and self.instance.pk and nickname == self.instance.nickname:
            return nickname
        return nickname.strip()

    def clean_bio(self):
        bio = self.cleaned_data['bio']
        if (
            self.instance
            and self.instance.pk
            and profile_text_matches_stored_value(bio, self.instance.bio)
        ):
            return self.instance.bio
        bio = bio.strip()
        if len(bio) <= PROFILE_BIO_MAX_LENGTH:
            return bio
        raise forms.ValidationError(
            f'个人简介不能超过 {PROFILE_BIO_MAX_LENGTH} 个字符。'
        )
