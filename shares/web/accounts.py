from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from shares.forms import CustomPasswordChangeForm, UserProfileForm
from shares.models import UserProfile
from shares.rate_limits import consume_rate_limit, get_client_ip


def _get_safe_login_return_url(request):
    return_url = request.POST.get(
        REDIRECT_FIELD_NAME,
        request.GET.get(REDIRECT_FIELD_NAME, ''),
    )
    if not return_url or not url_has_allowed_host_and_scheme(
        return_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return ''
    try:
        if not urlsplit(return_url).path.startswith('/'):
            return ''
    except ValueError:
        return ''
    return return_url


def register(request):
    """用户注册"""
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        rate_limit = consume_rate_limit('register_ip', f'ip:{get_client_ip(request)}')
        if not rate_limit.allowed:
            messages.error(request, '注册请求过于频繁，请稍后再试。')
            return render(request, 'shares/register.html', {'form': form}, status=429)
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
    return_url = _get_safe_login_return_url(request)
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        username = request.POST.get('username', '').strip().casefold()[:150]
        ip_limit = consume_rate_limit('login_ip', f'ip:{get_client_ip(request)}')
        account_limit = consume_rate_limit('login_account', f'account:{username}')
        if not ip_limit.allowed or not account_limit.allowed:
            messages.error(request, '登录尝试过于频繁，请稍后再试。')
            return render(
                request,
                'shares/login.html',
                {'form': form, REDIRECT_FIELD_NAME: return_url},
                status=429,
            )
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f'欢迎回来，{user.username}！')
            return redirect(return_url or settings.LOGIN_REDIRECT_URL)
    else:
        form = AuthenticationForm()
    return render(request, 'shares/login.html', {
        'form': form,
        REDIRECT_FIELD_NAME: return_url,
    })


@require_POST
def user_logout(request):
    """用户登出"""
    logout(request)
    messages.info(request, '已退出登录')
    return redirect('index')


@login_required
def profile_edit(request):
    """编辑个人资料"""
    if request.method == 'POST':
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        form = UserProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, '个人资料更新成功！')
            return redirect('profile_edit')
    else:
        try:
            profile = request.user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile(user=request.user)
        form = UserProfileForm(instance=profile)
    return render(request, 'shares/profile_edit.html', {'form': form, 'profile': profile})


@login_required
def password_change(request):
    """修改密码"""
    if request.method == 'POST':
        form = CustomPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, '密码修改成功！')
            return redirect('profile_edit')
    else:
        form = CustomPasswordChangeForm(user=request.user)
    return render(request, 'shares/password_change.html', {'form': form})
