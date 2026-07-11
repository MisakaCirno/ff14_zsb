from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from .models import Share, UserProfile, Report, Collection
from .validation import (
    COLLECTION_DESCRIPTION_MAX_LENGTH,
    PROFILE_BIO_MAX_LENGTH,
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


class ShareForm(forms.ModelForm):
    """分享创建/编辑表单"""
    strategy_code = forms.CharField(
        label='战术板代码',
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
            'strategy_code': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': '粘贴战术板代码，例如：[stgy:a0+k-wvpr...]'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '添加描述（可选）'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'visibility': forms.Select(attrs={'class': 'form-select'}),
            'is_spoiler': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_nsfw': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_original': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'title': '标题',
            'strategy_code': '战术板代码',
            'description': '描述',
            'category': '分类',
            'visibility': '可见性',
            'is_spoiler': '可能包含剧透',
            'is_nsfw': '可能令人不适',
            'is_original': '我是原创作者',
        }
    def clean_strategy_code(self):
        return normalize_strategy_code(self.cleaned_data['strategy_code'])


class UserProfileForm(forms.ModelForm):
    """用户资料编辑表单"""
    bio = forms.CharField(
        label='个人简介',
        required=False,
        max_length=PROFILE_BIO_MAX_LENGTH,
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


class CustomPasswordChangeForm(PasswordChangeForm):
    """自定义密码修改表单"""
    old_password = forms.CharField(
        label='当前密码',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '输入当前密码'})
    )
    new_password1 = forms.CharField(
        label='新密码',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '输入新密码'})
    )
    new_password2 = forms.CharField(
        label='确认新密码',
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': '再次输入新密码'})
    )
