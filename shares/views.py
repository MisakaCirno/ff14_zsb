from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from .models import Share, UserProfile
from .forms import ShareForm, UserProfileForm, CustomPasswordChangeForm
import qrcode
from io import BytesIO
import base64


def index(request):
    """主页 - 显示所有公开分享"""
    shares_list = Share.objects.filter(is_public=True)
    paginator = Paginator(shares_list, 12)  # 每页12个
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    return render(request, 'shares/index.html', {'shares': shares})


def share_detail(request, share_id):
    """分享详情页"""
    share = get_object_or_404(Share, share_id=share_id)
    
    # 检查权限
    if not share.is_public and share.author != request.user:
        messages.error(request, '该分享不存在或不公开')
        return redirect('index')
    
    # 增加浏览量
    share.views += 1
    share.save(update_fields=['views'])
    
    # 生成分享链接
    share_url = request.build_absolute_uri(share.get_absolute_url())
    
    return render(request, 'shares/detail.html', {
        'share': share,
        'share_url': share_url,
    })


@login_required
def create_share(request):
    """创建新分享"""
    if request.method == 'POST':
        form = ShareForm(request.POST)
        if form.is_valid():
            share = form.save(commit=False)
            share.author = request.user
            share.save()
            messages.success(request, '分享创建成功！')
            return redirect('share_detail', share_id=share.share_id)
    else:
        form = ShareForm()
    
    return render(request, 'shares/create.html', {'form': form})


@login_required
def edit_share(request, share_id):
    """编辑分享"""
    share = get_object_or_404(Share, share_id=share_id, author=request.user)
    
    if request.method == 'POST':
        form = ShareForm(request.POST, instance=share)
        if form.is_valid():
            form.save()
            messages.success(request, '分享更新成功！')
            return redirect('share_detail', share_id=share.share_id)
    else:
        form = ShareForm(instance=share)
    
    return render(request, 'shares/edit.html', {'form': form, 'share': share})


@login_required
def delete_share(request, share_id):
    """删除分享"""
    share = get_object_or_404(Share, share_id=share_id, author=request.user)
    
    if request.method == 'POST':
        share.delete()
        messages.success(request, '分享已删除')
        return redirect('my_shares')
    
    return render(request, 'shares/delete.html', {'share': share})


@login_required
def my_shares(request):
    """我的分享列表"""
    shares_list = Share.objects.filter(author=request.user)
    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    return render(request, 'shares/my_shares.html', {'shares': shares})


def register(request):
    """用户注册"""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, '注册成功！')
            return redirect('index')
    else:
        form = UserCreationForm()
    
    return render(request, 'shares/register.html', {'form': form})


def user_login(request):
    """用户登录"""
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            if user is not None:
                login(request, user)
                messages.success(request, f'欢迎回来，{username}！')
                return redirect('index')
    else:
        form = AuthenticationForm()
    
    return render(request, 'shares/login.html', {'form': form})


def user_logout(request):
    """用户登出"""
    logout(request)
    messages.info(request, '已退出登录')
    return redirect('index')


def generate_qr_code(request, share_id):
    """生成分享二维码"""
    share = get_object_or_404(Share, share_id=share_id)
    share_url = request.build_absolute_uri(share.get_absolute_url())
    
    # 生成二维码
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(share_url)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    # 转换为base64
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    img_str = base64.b64encode(buffer.getvalue()).decode()
    
    return JsonResponse({'qr_code': f'data:image/png;base64,{img_str}'})


@login_required
def profile_edit(request):
    """编辑个人资料"""
    profile, created = UserProfile.objects.get_or_create(user=request.user)
    
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, '个人资料更新成功！')
            return redirect('profile_edit')
    else:
        form = UserProfileForm(instance=profile)
    
    return render(request, 'shares/profile_edit.html', {'form': form, 'profile': profile})


@login_required
def password_change(request):
    """修改密码"""
    if request.method == 'POST':
        form = CustomPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)  # 保持登录状态
            messages.success(request, '密码修改成功！')
            return redirect('profile_edit')
    else:
        form = CustomPasswordChangeForm(user=request.user)
    
    return render(request, 'shares/password_change.html', {'form': form})
