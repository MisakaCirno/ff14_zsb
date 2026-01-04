from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import JsonResponse, HttpResponse
from django.db.models import Q, Count, Prefetch
from django.utils import timezone
from .models import Share, UserProfile, Report, Announcement
from .forms import ShareForm, UserProfileForm, CustomPasswordChangeForm, ReportForm
from io import BytesIO
import base64


def is_admin(user):
    return user.is_authenticated and (user.is_staff or user.is_superuser)


def index(request):
    """主页 - 显示所有公开且已通过审核的分享"""
    shares_list = Share.objects.filter(
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED
    )

    # 筛选分类
    category = request.GET.get('category')
    if category in ['entertainment', 'combat']:
        shares_list = shares_list.filter(category=category)

    # 筛选剧透/NSFW
    hide_spoiler = request.GET.get('hide_spoiler') == 'on'
    if hide_spoiler:
        shares_list = shares_list.filter(is_spoiler=False)

    hide_nsfw = request.GET.get('hide_nsfw') == 'on'
    if hide_nsfw:
        shares_list = shares_list.filter(is_nsfw=False)

    paginator = Paginator(shares_list, 12)  # 每页12个
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    # 获取最新站点动态
    latest_announcement = Announcement.objects.filter(is_active=True).order_by('-created_at').first()
    
    context = {
        'shares': shares,
        'current_category': category,
        'hide_spoiler': hide_spoiler,
        'hide_nsfw': hide_nsfw,
        'latest_announcement': latest_announcement,
    }
    return render(request, 'shares/index.html', context)


def announcement_list(request):
    """站点动态列表页"""
    # 管理员可以看到所有动态，普通用户只能看到激活的
    if request.user.is_staff or request.user.is_superuser:
        announcements_list = Announcement.objects.all().order_by('-created_at')
    else:
        announcements_list = Announcement.objects.filter(is_active=True).order_by('-created_at')
        
    paginator = Paginator(announcements_list, 10)
    page_number = request.GET.get('page')
    announcements = paginator.get_page(page_number)
    return render(request, 'shares/announcement_list.html', {'announcements': announcements})


@user_passes_test(is_admin)
def toggle_announcement_visibility(request, announcement_id):
    """切换站点动态可见性"""
    announcement = get_object_or_404(Announcement, id=announcement_id)
    announcement.is_active = not announcement.is_active
    announcement.save()
    status = "激活" if announcement.is_active else "隐藏"
    messages.success(request, f'站点动态 "{announcement.title}" 已{status}')
    return redirect('announcement_list')


def share_detail(request, share_id):
    """分享详情页"""
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return render(request, '404.html', status=404)
    
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
    
    # 审核状态检查
    # 如果是待审核或已拒绝，仅作者和管理员可见，或者通过链接访问（待审核状态下）
    # 用户需求：待审核的相当于链接可见
    if share.status != Share.Status.APPROVED:
        # 如果是公开分享但未通过审核
        if share.visibility == Share.Visibility.PUBLIC:
            # 待审核状态：所有人可通过链接访问（类似于 Unlisted）
            # 已拒绝状态：仅作者和管理员可见
            if share.status == Share.Status.REJECTED:
                has_permission = request.user.is_authenticated and (
                    share.author == request.user or 
                    request.user.is_staff or 
                    request.user.is_superuser
                )
                if not has_permission:
                    messages.error(request, '该分享违反规定已被拒绝，无法访问')
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
                # 审核逻辑：如果是公开分享且用户不是管理员，则设为待审核
                if share.visibility == Share.Visibility.PUBLIC:
                    if not (request.user.is_staff or request.user.is_superuser):
                        share.status = Share.Status.PENDING
                        messages.info(request, '您的分享已提交，审核通过后将显示在公开列表中。在此期间，您可以通过链接分享给他人。')
                    else:
                        share.status = Share.Status.APPROVED
                else:
                    # 非公开分享不需要审核
                    share.status = Share.Status.APPROVED
            else:
                share.author = None
                # 匿名用户强制设为不公开（仅链接访问）
                share.visibility = Share.Visibility.UNLISTED
                share.status = Share.Status.APPROVED
            
            share.save()
            if share.status == Share.Status.APPROVED:
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
            new_share = form.save(commit=False)
            
            # 审核逻辑：如果修改为公开，或者原本是公开且进行了修改，且用户不是管理员
            if new_share.visibility == Share.Visibility.PUBLIC:
                if not (request.user.is_staff or request.user.is_superuser):
                    # 只要是普通用户编辑公开分享，都需要重新审核
                    new_share.status = Share.Status.PENDING
                    messages.info(request, '修改已保存，需要重新审核后才能在公开列表中显示。')
                else:
                    new_share.status = Share.Status.APPROVED
            else:
                # 如果改为非公开，则自动通过
                new_share.status = Share.Status.APPROVED
                
            new_share.save()
            if new_share.status == Share.Status.APPROVED:
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

    # 普通搜索 - 仅显示公开且已通过审核的分享
    shares_list = Share.objects.filter(
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED
    ).filter(
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


def page_not_found(request, exception):
    """自定义404页面"""
    return render(request, '404.html', status=404)


@user_passes_test(is_admin)
def admin_review_list(request):
    """管理员审核列表"""
    pending_shares = Share.objects.filter(status=Share.Status.PENDING).order_by('-created_at')
    paginator = Paginator(pending_shares, 20)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    return render(request, 'shares/admin_review_list.html', {'shares': shares})


@user_passes_test(is_admin)
def admin_approve_share(request, share_id):
    """管理员通过审核"""
    share = get_object_or_404(Share, share_id=share_id)
    share.status = Share.Status.APPROVED
    share.save()
    messages.success(request, f'分享 "{share.title}" 已通过审核')
    return redirect('admin_review_list')


@user_passes_test(is_admin)
def admin_reject_share(request, share_id):
    """管理员拒绝审核"""
    share = get_object_or_404(Share, share_id=share_id)
    share.status = Share.Status.REJECTED
    share.visibility = Share.Visibility.PRIVATE
    share.save()
    messages.warning(request, f'分享 "{share.title}" 已被拒绝并设为私有')
    return redirect('admin_review_list')


@login_required
def report_share(request, share_id):
    """举报分享"""
    share = get_object_or_404(Share, share_id=share_id)
    
    if request.method == 'POST':
        form = ReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.share = share
            report.reporter = request.user
            report.save()
            messages.success(request, '举报已提交，管理员将尽快处理。')
            return redirect('share_detail', share_id=share_id)
    else:
        form = ReportForm()
    
    return render(request, 'shares/report_share.html', {'form': form, 'share': share})


@user_passes_test(is_admin)
def admin_report_list(request):
    """管理员举报处理列表 - 按分享聚合"""
    # 查找所有有待处理举报的分享
    reported_shares = Share.objects.annotate(
        pending_count=Count('reports', filter=Q(reports__status=Report.Status.PENDING))
    ).filter(
        pending_count__gt=0
    ).prefetch_related(
        Prefetch('reports', queryset=Report.objects.filter(status=Report.Status.PENDING).select_related('reporter'), to_attr='pending_reports')
    ).order_by('-pending_count', '-updated_at')
    
    paginator = Paginator(reported_shares, 10)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    return render(request, 'shares/admin_report_list.html', {'shares': shares})


@user_passes_test(is_admin)
def admin_resolve_report(request, report_id, action):
    """管理员处理单条举报"""
    report = get_object_or_404(Report, id=report_id)
    
    if action == 'resolve':
        # 认可举报：将分享设为私有，标记举报为已处理
        report.status = Report.Status.RESOLVED
        share = report.share
        share.visibility = Share.Visibility.PRIVATE
        share.save()
        messages.success(request, f'举报已认可，分享 "{share.title}" 已被设为私有')
    elif action == 'dismiss':
        # 驳回举报
        report.status = Report.Status.DISMISSED
        messages.info(request, '举报已驳回')
    else:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')
        
    report.resolved_at = timezone.now()
    report.resolved_by = request.user
    report.save()
    
    return redirect('admin_report_list')


@user_passes_test(is_admin)
def admin_resolve_share_reports(request, share_id, action):
    """批量处理某分享的所有待处理举报"""
    share = get_object_or_404(Share, share_id=share_id)
    pending_reports = share.reports.filter(status=Report.Status.PENDING)
    
    if not pending_reports.exists():
        messages.warning(request, '该分享没有待处理的举报')
        return redirect('admin_report_list')

    if action == 'resolve':
        # 认可举报：分享设为私有，所有待处理举报设为已解决
        share.visibility = Share.Visibility.PRIVATE
        share.save()
        pending_reports.update(status=Report.Status.RESOLVED, resolved_at=timezone.now(), resolved_by=request.user)
        messages.success(request, f'已认可举报，分享 "{share.title}" 已设为私有，相关举报已标记为处理。')
        
    elif action == 'dismiss':
        # 驳回举报：所有待处理举报设为已驳回
        pending_reports.update(status=Report.Status.DISMISSED, resolved_at=timezone.now(), resolved_by=request.user)
        messages.info(request, f'已驳回分享 "{share.title}" 的所有举报。')
        
    return redirect('admin_report_list')


def user_public_profile(request, username):
    """用户公开个人主页"""
    author = get_object_or_404(User, username=username)
    
    # 获取该用户发布的所有公开且已通过审核的分享
    shares_list = Share.objects.filter(
        author=author,
        visibility=Share.Visibility.PUBLIC,
        status=Share.Status.APPROVED
    ).order_by('-created_at')
    
    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    return render(request, 'shares/user_public_profile.html', {
        'author': author,
        'shares': shares,
    })




