from django import forms
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from .models import Share, UserProfile, Report


class ReportForm(forms.ModelForm):
    """举报表单"""
    class Meta:
        model = Report
        fields = ['reason']
        widgets = {
            'reason': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '请详细描述违规情况...'}),
        }
        labels = {
            'reason': '举报原因',
        }


class ShareForm(forms.ModelForm):
    """分享创建/编辑表单"""
    class Meta:
        model = Share
        fields = ['title', 'strategy_code', 'description', 'visibility', 'is_spoiler', 'is_nsfw', 'is_original']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '输入标题'}),
            'strategy_code': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': '粘贴战术板代码，例如：[stgy:a0+k-wvpr...]'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '添加描述（可选）'}),
            'visibility': forms.Select(attrs={'class': 'form-select'}),
            'is_spoiler': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_nsfw': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'is_original': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }
        labels = {
            'title': '标题',
            'strategy_code': '战术板代码',
            'description': '描述',
            'visibility': '可见性',
            'is_spoiler': '可能包含剧透',
            'is_nsfw': '可能令人不适',
            'is_original': '我是原创作者',
        }


class UserProfileForm(forms.ModelForm):
    """用户资料编辑表单"""
    class Meta:
        model = UserProfile
        fields = ['nickname', 'bio']
        widgets = {
            'nickname': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '设置你的昵称'}),
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': '介绍一下自己（可选）'}),
        }
        labels = {
            'nickname': '昵称',
            'bio': '个人简介',
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
