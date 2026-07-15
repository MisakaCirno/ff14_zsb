from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    UserCreationForm,
)
from django.core.exceptions import ValidationError
from .models import Share, UserProfile, Report, Collection
from .validation import (
    COLLECTION_DESCRIPTION_MAX_LENGTH,
    PROFILE_BIO_MAX_LENGTH,
    PROFILE_MISSING_VERSION,
    REPORT_REASON_MAX_LENGTH,
    RICH_TEXT_MAX_LENGTH,
    STAFF_REASON_MAX_LENGTH,
    STRATEGY_CODE_INPUT_MAX_LENGTH,
    normalize_strategy_code,
)


class CollectionForm(forms.ModelForm):
    """合集创建/编辑表单"""
    description = forms.CharField(
        label='描述',
        required=False,
        max_length=COLLECTION_DESCRIPTION_MAX_LENGTH,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': '添加描述（可选）'}),
    )

    class Meta:
        model = Collection
        fields = ['title', 'description', 'is_public']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '输入合集标题'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': '添加描述（可选）'}),
            'is_public': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'title': '标题',
            'description': '描述',
            'is_public': '公开合集',
        }


class ReportForm(forms.ModelForm):
    """举报表单"""
    reason = forms.CharField(
        label='举报原因',
        max_length=REPORT_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '请详细描述违规情况...'}),
    )

    class Meta:
        model = Report
        fields = ['reason']
        widgets = {
            'reason': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '请详细描述违规情况...'}),
        }
        labels = {
            'reason': '举报原因',
        }


class AdminReviewRejectForm(forms.Form):
    """管理员拒绝审核时填写反馈。"""
    reason = forms.CharField(
        label='拒绝原因',
        min_length=2,
        max_length=STAFF_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': '请说明审核未通过的原因，用户会在站内信中看到这段说明。',
        }),
    )


class ReportResolutionForm(forms.Form):
    """管理员处理举报时填写说明。"""
    reason = forms.CharField(
        label='处理说明',
        min_length=2,
        max_length=STAFF_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': '请填写处理依据，相关用户会在站内信中看到这段说明。',
        }),
    )


class RestrictionReleaseForm(forms.Form):
    """管理员解除活动内容限制时填写审计说明。"""
    reason = forms.CharField(
        label='解除说明',
        min_length=2,
        max_length=STAFF_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': '请填写解除限制的复核依据，分享作者会收到这段说明。',
        }),
    )


class RestrictionConfirmationForm(forms.Form):
    """管理员确认继续维持活动内容限制时填写审计说明。"""
    reason = forms.CharField(
        label='确认说明',
        min_length=2,
        max_length=STAFF_REASON_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': '请填写继续维持限制的复核依据，分享作者会收到这段说明。',
        }),
    )


class ShareForm(forms.ModelForm):
    """分享创建/编辑表单"""
    strategy_code = forms.CharField(
        label='战术板代码',
        help_text='可直接粘贴游戏导出的完整文本，系统会提取其中的 [stgy:...] 代码。',
        max_length=STRATEGY_CODE_INPUT_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 3,
            'placeholder': '粘贴战术板代码，例如：[stgy:a0+k-wvpr...]',
            'data-share-strategy-code': 'true',
        }),
    )
    description = forms.CharField(
        label='描述',
        help_text='可选。支持标题、列表、引用和代码块等常用排版。',
        required=False,
        max_length=RICH_TEXT_MAX_LENGTH,
        widget=forms.Textarea(attrs={
            'class': 'form-control',
            'rows': 4,
            'placeholder': '添加描述（可选）',
            'data-share-description': 'true',
        }),
    )

    class Meta:
        model = Share
        fields = ['title', 'strategy_code', 'description', 'category', 'visibility', 'is_spoiler', 'is_nsfw', 'is_original']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '输入标题'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'visibility': forms.Select(attrs={'class': 'form-select'}),
            'is_spoiler': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_nsfw': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_original': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'title': '标题',
            'category': '分类',
            'visibility': '可见性',
            'is_spoiler': '可能包含剧透',
            'is_nsfw': '可能令人不适',
            'is_original': '我是原创作者',
        }
        help_texts = {
            'category': '战斗适合副本攻略和机制解法；娱乐适合绘画、风景和趣味玩法。',
            'visibility': '公开内容需审核；不公开内容仅能通过链接或 ID 访问；私有内容仅自己可见。',
        }

    def clean_strategy_code(self):
        return normalize_strategy_code(self.cleaned_data['strategy_code'])


class CreateShareForm(ShareForm):
    """创建分享表单，包含按当前用户收敛的可选合集。"""

    collection_id = forms.ModelChoiceField(
        label='添加到合集',
        help_text='可选。新分享会追加到所选合集的末尾。',
        queryset=Collection.objects.none(),
        required=False,
        empty_label='-- 不添加到合集 --',
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user is not None and user.is_authenticated:
            collections = Collection.objects.filter(author=user).order_by('-updated_at', '-pk')
            self.fields['collection_id'].queryset = collections
            self.show_collection_field = bool(
                collections.exists()
                or (self.is_bound and self.data.get('collection_id'))
            )
        else:
            self.fields.pop('collection_id')
            self.fields['visibility'].initial = Share.Visibility.UNLISTED
            self.show_collection_field = False

    def clean_visibility(self):
        if self.user is None or not self.user.is_authenticated:
            return Share.Visibility.UNLISTED
        return self.cleaned_data['visibility']


class EditShareForm(ShareForm):
    """编辑分享表单，携带页面加载时的版本用于并发保护。"""

    version = forms.DateTimeField(
        required=True,
        widget=forms.HiddenInput(),
        error_messages={
            'required': '编辑页面缺少版本信息，请刷新后重新提交。',
            'invalid': '编辑页面版本无效，请刷新后重新提交。',
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['version'].initial = self.instance.updated_at


def _normalize_profile_form_newlines(value):
    return value.replace('\r\n', '\n').replace('\r', '\n')


def profile_text_matches_stored_value(submitted, stored):
    """Treat browser-normalized textarea newlines as the same stored text."""
    return (
        _normalize_profile_form_newlines(submitted)
        == _normalize_profile_form_newlines(stored)
    )


class UserProfileVersionField(forms.DateTimeField):
    """A timestamp version with a disjoint token for an absent profile row."""

    def to_python(self, value):
        if value == PROFILE_MISSING_VERSION:
            return PROFILE_MISSING_VERSION
        return super().to_python(value)


class UserProfileForm(forms.ModelForm):
    """用户资料编辑表单"""
    version = UserProfileVersionField(
        required=True,
        widget=forms.HiddenInput(),
        error_messages={
            'required': '资料页面缺少版本信息，请刷新后重新提交。',
            'invalid': '资料页面版本无效，请刷新后重新提交。',
        },
    )
    bio = forms.CharField(
        label='个人简介',
        required=False,
        strip=False,
        widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '介绍一下自己（可选）'}),
    )

    class Meta:
        model = UserProfile
        fields = ['nickname', 'bio', 'home_feed_mode']
        widgets = {
            'nickname': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '设置你的昵称'}),
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '介绍一下自己（可选）'}),
            'home_feed_mode': forms.Select(attrs={'class': 'form-select'}),
        }
        labels = {
            'nickname': '昵称',
            'bio': '个人简介',
            'home_feed_mode': '主页浏览模式',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Preserve an unchanged legacy value byte-for-byte. Changed values are
        # normalized explicitly in the clean methods below.
        self.fields['nickname'].strip = False
        if self.instance and self.instance.pk:
            self.fields['version'].initial = self.instance.updated_at
        else:
            self.fields['version'].initial = PROFILE_MISSING_VERSION

    def clean_nickname(self):
        nickname = self.cleaned_data['nickname']
        if (
            self.instance
            and self.instance.pk
            and nickname == self.instance.nickname
        ):
            return nickname
        return nickname.strip()

    def clean_bio(self):
        """Grandfather legacy long biographies without accepting new ones."""
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

        # Let the locked mutation service report a version conflict first when
        # an old page submits a formerly-valid long biography. The service
        # repeats the length check against the authoritative locked row.
        if self.instance and self.instance.pk:
            raw_version = self.data.get(self.add_prefix('version'))
            try:
                submitted_version = self.fields['version'].to_python(raw_version)
            except ValidationError:
                submitted_version = None
            if (
                submitted_version is not None
                and submitted_version != self.instance.updated_at
            ):
                return bio

        raise ValidationError(
            f'个人简介不能超过 {PROFILE_BIO_MAX_LENGTH} 个字符。'
        )


def _add_form_control_class(field):
    """Add the shared control class without replacing Django's auth widgets."""
    classes = field.widget.attrs.get('class', '').split()
    if 'form-control' not in classes:
        classes.append('form-control')
    field.widget.attrs['class'] = ' '.join(classes)


class AccountRegistrationForm(UserCreationForm):
    """Registration form that preserves Django's password-field semantics."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            'username': '设置登录用户名',
            'password1': '设置密码',
            'password2': '再次输入密码',
        }
        for field_name, placeholder in placeholders.items():
            field = self.fields[field_name]
            _add_form_control_class(field)
            field.widget.attrs['placeholder'] = placeholder


class AccountLoginForm(AuthenticationForm):
    """Login form with project styling layered onto Django's auth fields."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        placeholders = {
            'username': '输入用户名',
            'password': '输入密码',
        }
        for field_name, placeholder in placeholders.items():
            field = self.fields[field_name]
            _add_form_control_class(field)
            field.widget.attrs['placeholder'] = placeholder


class CustomPasswordChangeForm(PasswordChangeForm):
    """Password-change form that retains Django's validators and metadata."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field_options = {
            'old_password': ('当前密码', '输入当前密码'),
            'new_password1': ('新密码', '输入新密码'),
            'new_password2': ('确认新密码', '再次输入新密码'),
        }
        for field_name, (label, placeholder) in field_options.items():
            field = self.fields[field_name]
            field.label = label
            _add_form_control_class(field)
            field.widget.attrs['placeholder'] = placeholder
