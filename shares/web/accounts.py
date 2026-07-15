from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from shares.forms import (
    AccountLoginForm,
    AccountRegistrationForm,
    CustomPasswordChangeForm,
    UserProfileForm,
)
from shares.models import UserProfile
from shares.rate_limits import consume_rate_limit, get_client_ip
from shares.services.profiles import (
    ProfileBioTooLongError,
    ProfileEditConflictError,
    ProfileUnavailableError,
    update_user_profile_from_form,
)


def _get_safe_auth_return_url(request):
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


def _auth_switch_url(view_name, return_url):
    url = reverse(view_name)
    if not return_url:
        return url
    return f'{url}?{urlencode({REDIRECT_FIELD_NAME: return_url})}'


def _registration_context(form, return_url):
    return {
        'form': form,
        REDIRECT_FIELD_NAME: return_url,
        'login_url': _auth_switch_url('login', return_url),
    }


def _login_context(form, return_url):
    return {
        'form': form,
        REDIRECT_FIELD_NAME: return_url,
        'register_url': _auth_switch_url('register', return_url),
    }


def _rate_limited_response(request, template_name, context, message, *rate_limits):
    exhausted_limits = (
        rate_limit
        for rate_limit in rate_limits
        if not rate_limit.allowed or rate_limit.count >= rate_limit.limit
    )
    messages.error(request, message)
    response = render(request, template_name, context, status=429)
    response.headers['Retry-After'] = str(max(
        rate_limit.retry_after
        for rate_limit in exhausted_limits
    ))
    return response


def _submitted_username(request, form_class):
    username_field = form_class().fields['username']
    username = username_field.to_python(request.POST.get('username', ''))
    if username_field.max_length is not None:
        username = username[:username_field.max_length]
    return username


@sensitive_post_parameters('password1', 'password2')
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def register(request):
    """用户注册"""
    return_url = _get_safe_auth_return_url(request)
    if request.method == 'POST':
        rate_limit = consume_rate_limit('register_ip', f'ip:{get_client_ip(request)}')
        if not rate_limit.allowed:
            form = AccountRegistrationForm(initial={
                'username': _submitted_username(request, AccountRegistrationForm),
            })
            return _rate_limited_response(
                request,
                'shares/register.html',
                _registration_context(form, return_url),
                '注册请求过于频繁，请稍后再试。',
                rate_limit,
            )
        form = AccountRegistrationForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = form.save()
                UserProfile.objects.get_or_create(user=user)
            login(request, user)
            messages.success(request, '注册成功！')
            return redirect(return_url or settings.LOGIN_REDIRECT_URL)
    else:
        form = AccountRegistrationForm()
    return render(
        request,
        'shares/register.html',
        _registration_context(form, return_url),
    )


@sensitive_post_parameters('password')
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def user_login(request):
    """用户登录"""
    return_url = _get_safe_auth_return_url(request)
    if request.method == 'POST':
        ip_limit = consume_rate_limit('login_ip', f'ip:{get_client_ip(request)}')
        if not ip_limit.allowed:
            submitted_username = _submitted_username(request, AccountLoginForm)
            form = AccountLoginForm(
                request=request,
                initial={'username': submitted_username},
            )
            return _rate_limited_response(
                request,
                'shares/login.html',
                _login_context(form, return_url),
                '登录尝试过于频繁，请稍后再试。',
                ip_limit,
            )
        submitted_username = _submitted_username(request, AccountLoginForm)
        username = submitted_username.casefold()
        account_limit = consume_rate_limit('login_account', f'account:{username}')
        if not account_limit.allowed:
            form = AccountLoginForm(
                request=request,
                initial={'username': submitted_username},
            )
            return _rate_limited_response(
                request,
                'shares/login.html',
                _login_context(form, return_url),
                '登录尝试过于频繁，请稍后再试。',
                ip_limit,
                account_limit,
            )
        form = AccountLoginForm(request=request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f'欢迎回来，{user.username}！')
            return redirect(return_url or settings.LOGIN_REDIRECT_URL)
    else:
        form = AccountLoginForm(request=request)
    return render(
        request,
        'shares/login.html',
        _login_context(form, return_url),
    )


@never_cache
@require_POST
def user_logout(request):
    """用户登出"""
    logout(request)
    messages.info(request, '已退出登录')
    return redirect('index')


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def profile_edit(request):
    """编辑个人资料"""
    profile = UserProfile.objects.filter(user=request.user).first()
    if profile is None:
        profile = UserProfile(user=request.user)
    response_status = 200

    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=profile)
        if form.is_valid():
            try:
                result = update_user_profile_from_form(
                    form=form,
                    actor=request.user,
                )
            except ProfileBioTooLongError:
                form.add_error('bio', '个人简介过长，请缩短后重新提交。')
            except ProfileEditConflictError:
                response_status = 409
                form.add_error(
                    None,
                    '个人资料已在此页面打开后发生变化，请刷新后重新编辑。',
                )
            except ProfileUnavailableError:
                response_status = 409
                form.add_error(
                    None,
                    '个人资料暂时不可用，请刷新后重试。',
                )
            else:
                profile = result.profile
                if result.changed:
                    messages.success(request, '个人资料更新成功！')
                else:
                    messages.info(request, '个人资料没有修改。')
                return redirect('profile_edit')
        elif 'version' in form.errors:
            response_status = 409
    else:
        form = UserProfileForm(instance=profile)
    return render(
        request,
        'shares/profile_edit.html',
        {'form': form, 'profile': profile},
        status=response_status,
    )


@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
@login_required
def password_change(request):
    """修改密码"""
    if request.method == 'POST':
        ip_limit = consume_rate_limit(
            'password_change_ip',
            f'ip:{get_client_ip(request)}',
        )
        if not ip_limit.allowed:
            form = CustomPasswordChangeForm(user=request.user)
            return _rate_limited_response(
                request,
                'shares/password_change.html',
                {'form': form},
                '密码修改尝试过于频繁，请稍后再试。',
                ip_limit,
            )
        user_limit = consume_rate_limit(
            'password_change_user',
            f'user:{request.user.pk}',
        )
        if not user_limit.allowed:
            form = CustomPasswordChangeForm(user=request.user)
            return _rate_limited_response(
                request,
                'shares/password_change.html',
                {'form': form},
                '密码修改尝试过于频繁，请稍后再试。',
                ip_limit,
                user_limit,
            )
        form = CustomPasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, '密码修改成功！')
            return redirect('profile_edit')
    else:
        form = CustomPasswordChangeForm(user=request.user)
    return render(request, 'shares/password_change.html', {'form': form})
