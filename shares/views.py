from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.db.models import Q
from .models import Share, UserProfile
from .forms import ShareForm, UserProfileForm, CustomPasswordChangeForm
from io import BytesIO
import base64


def index(request):
    """主页 - 显示所有公开分享"""
    shares_list = Share.objects.filter(visibility=Share.Visibility.PUBLIC)
    paginator = Paginator(shares_list, 12)  # 每页12个
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    return render(request, 'shares/index.html', {'shares': shares})


def share_detail(request, share_id):
    """分享详情页"""
    share = get_object_or_404(Share, share_id=share_id)
    
    # 检查权限
    # 私有分享仅作者和管理员可见
    if share.visibility == Share.Visibility.PRIVATE:
        has_permission = request.user.is_authenticated and (
            share.author == request.user or 
            request.user.is_staff or 
            request.user.is_superuser
        )
        if not has_permission:
            messages.error(request, '该分享不存在或您没有权限访问')
            return redirect('index')
    
    # 精准浏览量统计：使用Cookie防止重复计数
    # Cookie名称：viewed_shares，存储已浏览的分享ID列表
    viewed_shares = request.COOKIES.get('viewed_shares', '')
    viewed_list = viewed_shares.split(',') if viewed_shares else []
    
    # 如果该分享未被当前访客浏览过，则增加浏览量
    if share_id not in viewed_list:
        share.views += 1
        share.save(update_fields=['views'])
        # 将该分享ID添加到已浏览列表
        viewed_list.append(share_id)
        # 限制Cookie大小，最多保留最近100个浏览记录
        if len(viewed_list) > 100:
            viewed_list = viewed_list[-100:]
    
    response = render(request, 'shares/detail.html', {
        'share': share,
    })
    
    # 更新Cookie（30天有效期）
    if share_id not in viewed_shares.split(','):
        response.set_cookie(
            'viewed_shares',
            ','.join(viewed_list),
            max_age=30*24*60*60,  # 30天
            httponly=True,  # 防止JavaScript访问
            samesite='Lax'  # CSRF保护
        )
    
    return response


def create_share(request):
    """创建新分享"""
    if request.method == 'POST':
        form = ShareForm(request.POST)
        if form.is_valid():
            share = form.save(commit=False)
            if request.user.is_authenticated:
                share.author = request.user
            else:
                share.author = None
                # 匿名用户强制设为不公开（仅链接访问）
                share.visibility = Share.Visibility.UNLISTED
            
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
    share = get_object_or_404(Share, share_id=share_id)
    
    # 权限检查：作者或管理员
    if not (request.user == share.author or request.user.is_staff or request.user.is_superuser):
        messages.error(request, '您没有权限删除此分享')
        return redirect('share_detail', share_id=share_id)
    
    if request.method == 'POST':
        share.delete()
        messages.success(request, '分享已删除')
        # 如果是作者删除，跳转到我的分享；如果是管理员删除，跳转到主页
        if request.user == share.author:
            return redirect('my_shares')
        else:
            return redirect('index')
    
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


def search(request):
    """搜索分享"""
    query = request.GET.get('q', '').strip()
    
    if not query:
        return redirect('index')
        
    # 优先匹配 share_id (不再限制长度，兼容不同版本的ID格式)
    try:
        share = Share.objects.get(share_id=query)
        # 检查权限：
        # 1. 公开或不公开(Unlisted) -> 允许访问
        # 2. 私有(Private) -> 作者或管理员允许访问
        can_view = (share.visibility != Share.Visibility.PRIVATE) or \
                   (request.user.is_authenticated and (
                       share.author == request.user or 
                       request.user.is_staff or 
                       request.user.is_superuser
                   ))
                   
        if can_view:
            return redirect('share_detail', share_id=share.share_id)
    except Share.DoesNotExist:
        pass

    # 普通搜索 - 仅显示公开分享
    shares_list = Share.objects.filter(visibility=Share.Visibility.PUBLIC).filter(
        Q(title__icontains=query) |
        Q(description__icontains=query) |
        Q(author__profile__nickname__icontains=query) |
        Q(author__username__icontains=query)
    ).distinct()
    
    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    return render(request, 'shares/index.html', {
        'shares': shares,
        'search_query': query
    })


def about(request):
    """关于页面"""
    return render(request, 'about.html')

