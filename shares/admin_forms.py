from django import forms

from .models import Announcement, Share
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
