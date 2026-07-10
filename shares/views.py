from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse, HttpResponse
from django.db import IntegrityError, transaction
from django.db.models import Q, Count, Prefetch, Max, Exists, OuterRef, F
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from .models import Share, UserProfile, Report, Announcement, Collection, CollectionItem, ShareLog, SiteMessage
from .forms import ShareForm, UserProfileForm, CustomPasswordChangeForm, ReportForm, CollectionForm, AdminReviewRejectForm, ReportResolutionForm
from .services.messages import send_site_message
from .services.audit import log_share_action
from .selectors import admin_task_counts, annotate_share_cards
from .presentation import (
    HOME_FEED_MODES,
    build_query_string,
    get_home_feed_mode,
    render_share_cards_response,
)
from .rate_limits import consume_rate_limit, get_client_ip, request_identity
from .policies import (
    can_view_collection,
    can_view_share,
    is_moderator as is_admin,
    public_share_queryset,
    share_api_denial_status,
)
from .validation import SEARCH_QUERY_MAX_LENGTH
from io import BytesIO
import base64


@require_POST
def set_home_feed_mode(request):
    requested_mode = request.POST.get('feed')
    if requested_mode not in HOME_FEED_MODES:
        messages.error(request, '无效的浏览模式。')
        return redirect('index')

    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        if profile.home_feed_mode != requested_mode:
            profile.home_feed_mode = requested_mode
            profile.save(update_fields=['home_feed_mode', 'updated_at'])
    else:
        request.session['home_feed_mode'] = requested_mode

    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('index')


def index(request):
    """主页 - 显示所有公开且已通过审核的分享"""
    shares_list = public_share_queryset()

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

    # 排序
    sort_by = request.GET.get('sort', 'latest')
    
    shares_list = annotate_share_cards(shares_list, request.user)

    if sort_by == 'likes':
        shares_list = shares_list.order_by('-likes_count', '-created_at')
    elif sort_by == 'views':
        shares_list = shares_list.order_by('-views', '-created_at')
    elif sort_by == 'favorites':
        shares_list = shares_list.order_by('-favorites_count', '-created_at')
    elif sort_by == 'copies':
        shares_list = shares_list.order_by('-copies', '-created_at')
    else: # latest
        shares_list = shares_list.order_by('-created_at')

    paginator = Paginator(shares_list, 12)  # 每页12个
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)

    if request.GET.get('partial') == 'shares':
        return render_share_cards_response(request, shares)

    feed_mode = get_home_feed_mode(request)
    
    # 获取最新站点动态
    latest_announcement = Announcement.objects.filter(is_active=True).order_by('-created_at').first()
    
    context = {
        'shares': shares,
        'current_category': category,
        'sort_by': sort_by,
        'hide_spoiler': hide_spoiler,
        'hide_nsfw': hide_nsfw,
        'latest_announcement': latest_announcement,
        'feed_mode': feed_mode,
        'paginated_query': build_query_string(request, feed=UserProfile.HomeFeedMode.PAGINATED, page=None, partial=None),
        'infinite_query': build_query_string(request, feed=UserProfile.HomeFeedMode.INFINITE, page=None, partial=None),
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
@require_POST
def toggle_announcement_visibility(request, announcement_id):
    """切换站点动态可见性"""
    requested_state = request.POST.get('is_active')
    if requested_state not in {'0', '1'}:
        messages.error(request, '无效的站点动态状态')
        return redirect('announcement_list')

    with transaction.atomic():
        announcement = get_object_or_404(
            Announcement.objects.select_for_update(),
            id=announcement_id,
        )
        is_active = requested_state == '1'
        if announcement.is_active != is_active:
            announcement.is_active = is_active
            announcement.save(update_fields=['is_active', 'updated_at'])

    status = "激活" if announcement.is_active else "隐藏"
    messages.success(request, f'站点动态 "{announcement.title}" 已{status}')
    return redirect('announcement_list')


def share_detail(request, share_id):
    """分享详情页"""
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return render(request, '404.html', status=404)
    
    if not can_view_share(request.user, share):
        messages.error(request, '该分享不存在或您没有权限访问')
        return redirect('index')
    
    # 获取该分享所属的合集（仅显示公开的，或者作者自己的）
    related_collections = Collection.objects.filter(
        collectionitem__share=share
    ).distinct()
    related_collections = [
        collection
        for collection in related_collections
        if can_view_collection(request.user, collection)
    ]
    
    # 管理员查看详情时，预加载日志
    if is_admin(request.user):
        share_logs = share.logs.select_related('user').order_by('-created_at')
    else:
        share_logs = None

    # 获取用户的合集列表（用于添加到合集功能）
    user_collections = []
    if request.user.is_authenticated and share.author == request.user:
        user_collections = Collection.objects.filter(author=request.user).order_by('-updated_at')

    is_liked = False
    is_favorited = False
    if request.user.is_authenticated:
        is_liked = share.likes.filter(id=request.user.id).exists()
        is_favorited = share.favorites.filter(id=request.user.id).exists()

    return render(request, 'shares/detail.html', {
        'share': share,
        'related_collections': related_collections,
        'user_collections': user_collections,
        'share_logs': share_logs,
        'is_liked': is_liked,
        'is_favorited': is_favorited,
    })


def create_share(request):
    """创建新分享"""
    response_status = 200
    if request.method == 'POST':
        form = ShareForm(request.POST)
        rule_name = (
            'authenticated_create_user'
            if request.user.is_authenticated
            else 'anonymous_create_ip'
        )
        rate_limit = consume_rate_limit(rule_name, request_identity(request))
        if not rate_limit.allowed:
            messages.error(request, '创建请求过于频繁，请稍后再试。')
            response_status = 429
        elif form.is_valid():
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
            
            with transaction.atomic():
                share.save()

                # 记录创建日志
                if request.user.is_authenticated:
                    log_share_action(request.user, share, ShareLog.ActionType.CREATE, '用户创建分享')

                # 如果选择了合集，则添加到合集
                collection_id = request.POST.get('collection_id')
                if collection_id and request.user.is_authenticated:
                    try:
                        collection = Collection.objects.select_for_update().get(
                            id=collection_id,
                            author=request.user,
                        )
                        max_order = CollectionItem.objects.filter(collection=collection).aggregate(Max('order'))['order__max']
                        new_order = (max_order or 0) + 1
                        CollectionItem.objects.create(collection=collection, share=share, order=new_order)
                    except Collection.DoesNotExist:
                        pass

            if share.status == Share.Status.APPROVED:
                messages.success(request, '分享创建成功！')
            return redirect('share_detail', share_id=share.share_id)
    else:
        form = ShareForm()
    
    # 获取用户的合集列表
    user_collections = []
    if request.user.is_authenticated:
        user_collections = Collection.objects.filter(author=request.user).order_by('-updated_at')
    
    return render(
        request,
        'shares/create.html',
        {'form': form, 'user_collections': user_collections},
        status=response_status,
    )


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

            new_share.review_feedback = ''
            new_share.reviewed_at = None
            new_share.reviewed_by = None
            # 记录编辑日志
            changes = []
            if 'title' in form.changed_data:
                changes.append('标题')
            if 'strategy_code' in form.changed_data:
                changes.append('战术板代码')
            if 'description' in form.changed_data:
                changes.append('描述')
            if 'category' in form.changed_data:
                changes.append('分类')
            if 'visibility' in form.changed_data:
                changes.append('可见性')
                
            log_details = f"用户编辑内容: {', '.join(changes)}" if changes else "用户编辑分享"
            with transaction.atomic():
                new_share.save()
                log_share_action(request.user, new_share, ShareLog.ActionType.EDIT, log_details)

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
    tab = request.GET.get('tab', 'my_shares')
    
    if tab == 'likes':
        shares_list = request.user.liked_shares.all().annotate(
            likes_count=Count('likes', distinct=True), favorites_count=Count('favorites', distinct=True)
        ).order_by('-created_at')
    elif tab == 'favorites':
        shares_list = request.user.favorited_shares.all().annotate(
            likes_count=Count('likes', distinct=True), favorites_count=Count('favorites', distinct=True)
        ).order_by('-created_at')
    else:
        shares_list = Share.objects.filter(author=request.user).annotate(
            likes_count=Count('likes', distinct=True), favorites_count=Count('favorites', distinct=True)
        )
        if request.GET.get('order') == 'desc': # Optional preservation of existing ordering if any, though model default is desc
             shares_list = shares_list.order_by('created_at')
        else:
             shares_list = shares_list.order_by('-created_at')

    # Add is_liked and is_favorited annotations for all tabs
    shares_list = shares_list.annotate(
        is_liked=Exists(Share.likes.through.objects.filter(share_id=OuterRef('pk'), user_id=request.user.id)),
        is_favorited=Exists(Share.favorites.through.objects.filter(share_id=OuterRef('pk'), user_id=request.user.id))
    )

    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    # 获取我的合集
    collections = Collection.objects.filter(author=request.user).annotate(item_count=Count('collectionitem')).order_by('-updated_at')
    
    return render(request, 'shares/my_shares.html', {
        'shares': shares,
        'collections': collections,
        'current_tab': tab,
    })


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
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        username = request.POST.get('username', '').strip().casefold()[:150]
        ip_limit = consume_rate_limit('login_ip', f'ip:{get_client_ip(request)}')
        account_limit = consume_rate_limit('login_account', f'account:{username}')
        if not ip_limit.allowed or not account_limit.allowed:
            messages.error(request, '登录尝试过于频繁，请稍后再试。')
            return render(request, 'shares/login.html', {'form': form}, status=429)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f'欢迎回来，{user.username}！')
            return redirect('index')
    else:
        form = AuthenticationForm()
    
    return render(request, 'shares/login.html', {'form': form})


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
            update_session_auth_hash(request, user)  # 保持登录状态
            messages.success(request, '密码修改成功！')
            return redirect('profile_edit')
    else:
        form = CustomPasswordChangeForm(user=request.user)
    
    return render(request, 'shares/password_change.html', {'form': form})


@login_required
def site_message_list(request):
    """站内信列表"""
    messages_list = SiteMessage.objects.filter(
        recipient=request.user,
        archived_at__isnull=True,
    ).select_related('sender', 'related_share', 'related_report')

    paginator = Paginator(messages_list, 20)
    page_number = request.GET.get('page')
    site_messages = paginator.get_page(page_number)

    return render(request, 'shares/site_message_list.html', {
        'site_messages': site_messages,
    })


@login_required
def site_message_detail(request, message_id):
    """站内信详情"""
    site_message = get_object_or_404(
        SiteMessage.objects.select_related('sender', 'related_share', 'related_report'),
        id=message_id,
        recipient=request.user,
    )

    return render(request, 'shares/site_message_detail.html', {
        'site_message': site_message,
    })


@login_required
@require_POST
def open_site_message(request, message_id):
    site_message = get_object_or_404(
        SiteMessage,
        id=message_id,
        recipient=request.user,
    )
    if site_message.read_at is None:
        site_message.read_at = timezone.now()
        site_message.save(update_fields=['read_at'])
    return redirect('site_message_detail', message_id=site_message.id)


@login_required
@require_POST
def mark_all_site_messages_read(request):
    """标记全部站内信为已读"""
    updated = SiteMessage.objects.filter(
        recipient=request.user,
        read_at__isnull=True,
        archived_at__isnull=True,
    ).update(read_at=timezone.now())
    messages.success(request, f'已将 {updated} 条站内信标记为已读')
    return redirect('site_message_list')


def search(request):
    """搜索分享"""
    query = request.GET.get('q', '').strip()
    
    if not query:
        return redirect('index')

    if len(query) > SEARCH_QUERY_MAX_LENGTH:
        messages.error(request, f'搜索内容不能超过 {SEARCH_QUERY_MAX_LENGTH} 个字符。')
        return redirect('index')
        
    # 优先匹配 share_id (不再限制长度，兼容不同版本的ID格式)
    try:
        share = Share.objects.get(share_id=query)
        if can_view_share(request.user, share):
            return redirect('share_detail', share_id=share.share_id)
    except Share.DoesNotExist:
        pass

    # 普通搜索 - 仅显示公开且已通过审核的分享
    shares_list = public_share_queryset().filter(
        Q(title__icontains=query) |
        Q(description__icontains=query) |
        Q(author__profile__nickname__icontains=query) |
        Q(author__username__icontains=query)
    ).distinct()

    shares_list = annotate_share_cards(shares_list, request.user).order_by('-created_at')
    
    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)

    if request.GET.get('partial') == 'shares':
        return render_share_cards_response(request, shares)

    feed_mode = get_home_feed_mode(request)
    
    return render(request, 'shares/index.html', {
        'shares': shares,
        'search_query': query,
        'sort_by': 'latest',
        'feed_mode': feed_mode,
        'paginated_query': build_query_string(request, feed=UserProfile.HomeFeedMode.PAGINATED, page=None, partial=None),
        'infinite_query': build_query_string(request, feed=UserProfile.HomeFeedMode.INFINITE, page=None, partial=None),
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
    pending_shares = Share.objects.filter(status=Share.Status.PENDING).prefetch_related(
        Prefetch('logs', queryset=ShareLog.objects.select_related('user').order_by('-created_at'), to_attr='share_logs')
    ).order_by('-created_at')
    paginator = Paginator(pending_shares, 20)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    context = {'shares': shares, 'reject_form': AdminReviewRejectForm()}
    context.update(get_admin_counts())
    return render(request, 'shares/admin_review_list.html', context)


@user_passes_test(is_admin)
@require_POST
def admin_approve_share(request, share_id):
    """管理员通过审核"""
    with transaction.atomic():
        share = get_object_or_404(
            Share.objects.select_for_update(),
            share_id=share_id,
        )
        if share.status != Share.Status.PENDING:
            messages.warning(request, f'分享 "{share.title}" 已处理，无需重复审核')
            return redirect('admin_review_list')

        share.status = Share.Status.APPROVED
        share.review_feedback = ''
        share.reviewed_at = timezone.now()
        share.reviewed_by = request.user
        share.save(update_fields=['status', 'review_feedback', 'reviewed_at', 'reviewed_by', 'updated_at'])
        log_share_action(request.user, share, ShareLog.ActionType.REVIEW_APPROVE, '管理通过审核')

    messages.success(request, f'分享 "{share.title}" 已通过审核')
    return redirect('admin_review_list')


@user_passes_test(is_admin)
@require_POST
def admin_reject_share(request, share_id):
    """管理员拒绝审核"""
    form = AdminReviewRejectForm(request.POST)
    if not form.is_valid():
        messages.error(request, '拒绝原因不能为空')
        return redirect('admin_review_list')

    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        share = get_object_or_404(
            Share.objects.select_for_update(),
            share_id=share_id,
        )
        if share.status != Share.Status.PENDING:
            messages.warning(request, f'分享 "{share.title}" 已处理，无需重复审核')
            return redirect('admin_review_list')

        share.status = Share.Status.REJECTED
        share.visibility = Share.Visibility.PRIVATE
        share.review_feedback = reason
        share.reviewed_at = timezone.now()
        share.reviewed_by = request.user
        share.save(update_fields=['status', 'visibility', 'review_feedback', 'reviewed_at', 'reviewed_by', 'updated_at'])
        log_share_action(request.user, share, ShareLog.ActionType.REVIEW_REJECT, f'管理员拒绝审核并设为私有。原因：{reason}')
        if share.author:
            send_site_message(
                recipient=share.author,
                sender=request.user,
                message_type=SiteMessage.MessageType.SHARE_REJECTED,
                title=f'分享「{share.title}」审核未通过',
                content=f'你的分享「{share.title}」审核未通过。\n\n原因：{reason}\n\n你可以修改后重新提交审核。',
                related_share=share,
                metadata={'action_url': share.get_absolute_url()},
            )

    messages.warning(request, f'分享 "{share.title}" 已被拒绝并设为私有')
    return redirect('admin_review_list')


@login_required
def report_share(request, share_id):
    """举报分享"""
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        raise Http404('Share not found')
    
    if request.method == 'POST':
        form = ReportForm(request.POST)
        rate_limit = consume_rate_limit('report_user', request_identity(request))
        if not rate_limit.allowed:
            messages.error(request, '举报请求过于频繁，请稍后再试。')
            return render(
                request,
                'shares/report_share.html',
                {'form': form, 'share': share},
                status=429,
            )
        if form.is_valid():
            existing_report = Report.objects.filter(
                share=share,
                reporter=request.user,
                status=Report.Status.PENDING,
            ).exists()
            if existing_report:
                messages.warning(request, '你已经提交过待处理的举报，请等待管理员处理。')
                return redirect('share_detail', share_id=share_id)

            report = form.save(commit=False)
            report.share = share
            report.reporter = request.user
            try:
                with transaction.atomic():
                    report.save()
            except IntegrityError:
                if Report.objects.filter(
                    share=share,
                    reporter=request.user,
                    status=Report.Status.PENDING,
                ).exists():
                    messages.warning(request, '你已经提交过待处理的举报，请等待管理员处理。')
                    return redirect('share_detail', share_id=share_id)
                raise
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
        Prefetch('reports', queryset=Report.objects.filter(status=Report.Status.PENDING).select_related('reporter'), to_attr='pending_reports'),
        Prefetch('logs', queryset=ShareLog.objects.select_related('user').order_by('-created_at'), to_attr='share_logs')
    ).order_by('-pending_count', '-updated_at')
    
    paginator = Paginator(reported_shares, 10)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    context = {'shares': shares, 'resolution_form': ReportResolutionForm()}
    context.update(get_admin_counts())
    return render(request, 'shares/admin_report_list.html', context)


@user_passes_test(is_admin)
@require_POST
def admin_resolve_report(request, report_id, action):
    """管理员处理单条举报"""
    if action not in {'resolve', 'dismiss'}:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')

    form = ReportResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, '处理说明不能为空')
        return redirect('admin_report_list')

    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        report = get_object_or_404(
            Report.objects.select_for_update().select_related('reporter'),
            id=report_id,
        )
        if report.status != Report.Status.PENDING:
            messages.warning(request, '该举报已处理，无需重复操作')
            return redirect('admin_report_list')

        share = Share.objects.select_for_update().get(pk=report.share_id)
        resolved_at = timezone.now()

        if action == 'resolve':
            report.status = Report.Status.RESOLVED
            share.visibility = Share.Visibility.PRIVATE
            share.save(update_fields=['visibility', 'updated_at'])
            log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, f'认可举报 ID:{report_id}，设为私有。说明：{reason}')
            send_site_message(
                recipient=report.reporter,
                sender=request.user,
                message_type=SiteMessage.MessageType.REPORT_RESOLVED,
                title=f'你对「{share.title}」的举报已处理',
                content=f'你对分享「{share.title}」的举报已处理，感谢反馈。\n\n处理说明：{reason}',
                related_share=share,
                related_report=report,
                metadata={'action_url': share.get_absolute_url()},
            )
            if share.author:
                send_site_message(
                    recipient=share.author,
                    sender=request.user,
                    message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
                    title=f'分享「{share.title}」已被设为私有',
                    content=f'你的分享「{share.title}」因举报处理被设为私有。\n\n处理说明：{reason}',
                    related_share=share,
                    related_report=report,
                    metadata={'action_url': share.get_absolute_url()},
                )
        else:
            report.status = Report.Status.DISMISSED
            log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, f'驳回举报 ID:{report_id}。说明：{reason}')
            send_site_message(
                recipient=report.reporter,
                sender=request.user,
                message_type=SiteMessage.MessageType.REPORT_DISMISSED,
                title=f'你对「{share.title}」的举报未被采纳',
                content=f'你对分享「{share.title}」的举报未被采纳。\n\n处理说明：{reason}',
                related_share=share,
                related_report=report,
                metadata={'action_url': share.get_absolute_url()},
            )

        report.resolved_at = resolved_at
        report.resolved_by = request.user
        report.resolution_reason = reason
        report.save(update_fields=['status', 'resolved_at', 'resolved_by', 'resolution_reason'])

    if action == 'resolve':
        messages.success(request, f'举报已认可，分享 "{share.title}" 已被设为私有')
    else:
        messages.info(request, '举报已驳回')
    
    return redirect('admin_report_list')


@user_passes_test(is_admin)
@require_POST
def admin_resolve_share_reports(request, share_id, action):
    """批量处理某分享的所有待处理举报"""
    if action not in {'resolve', 'dismiss'}:
        messages.error(request, '无效的操作')
        return redirect('admin_report_list')

    form = ReportResolutionForm(request.POST)
    if not form.is_valid():
        messages.error(request, '处理说明不能为空')
        return redirect('admin_report_list')

    reason = form.cleaned_data['reason'].strip()
    with transaction.atomic():
        share = get_object_or_404(
            Share.objects.select_for_update(),
            share_id=share_id,
        )
        reports = list(
            Report.objects.select_for_update()
            .filter(share=share, status=Report.Status.PENDING)
            .select_related('reporter')
        )
        if not reports:
            messages.warning(request, '该分享没有待处理的举报')
            return redirect('admin_report_list')

        resolved_at = timezone.now()
        report_ids = [report.id for report in reports]
        target_status = (
            Report.Status.RESOLVED
            if action == 'resolve'
            else Report.Status.DISMISSED
        )
        Report.objects.filter(id__in=report_ids).update(
            status=target_status,
            resolved_at=resolved_at,
            resolved_by=request.user,
            resolution_reason=reason,
        )

        if action == 'resolve':
            share.visibility = Share.Visibility.PRIVATE
            share.save(update_fields=['visibility', 'updated_at'])
            log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, f'批量认可所有举报，设为私有。说明：{reason}')
            for report in reports:
                send_site_message(
                    recipient=report.reporter,
                    sender=request.user,
                    message_type=SiteMessage.MessageType.REPORT_RESOLVED,
                    title=f'你对「{share.title}」的举报已处理',
                    content=f'你对分享「{share.title}」的举报已处理，感谢反馈。\n\n处理说明：{reason}',
                    related_share=share,
                    related_report=report,
                    metadata={'action_url': share.get_absolute_url()},
                )
            if share.author:
                send_site_message(
                    recipient=share.author,
                    sender=request.user,
                    message_type=SiteMessage.MessageType.SHARE_TAKEDOWN,
                    title=f'分享「{share.title}」已被设为私有',
                    content=f'你的分享「{share.title}」因举报处理被设为私有。\n\n处理说明：{reason}',
                    related_share=share,
                    metadata={'action_url': share.get_absolute_url()},
                )
        else:
            log_share_action(request.user, share, ShareLog.ActionType.REPORT_HANDLE, f'批量驳回所有举报。说明：{reason}')
            for report in reports:
                send_site_message(
                    recipient=report.reporter,
                    sender=request.user,
                    message_type=SiteMessage.MessageType.REPORT_DISMISSED,
                    title=f'你对「{share.title}」的举报未被采纳',
                    content=f'你对分享「{share.title}」的举报未被采纳。\n\n处理说明：{reason}',
                    related_share=share,
                    related_report=report,
                    metadata={'action_url': share.get_absolute_url()},
                )

    if action == 'resolve':
        messages.success(request, f'已认可举报，分享 "{share.title}" 已设为私有，相关举报已标记为处理。')
    else:
        messages.info(request, '举报已全部驳回')
    
    return redirect('admin_report_list')


def get_admin_counts():
    """Compatibility alias while moderation views are being split."""
    return admin_task_counts()


@user_passes_test(is_admin)
def admin_review_logs(request):
    """审核日志列表"""
    log_types = [
        ShareLog.ActionType.REVIEW_APPROVE,
        ShareLog.ActionType.REVIEW_REJECT,
    ]
    
    logs_list = ShareLog.objects.filter(
        action__in=log_types
    ).select_related('user', 'share').order_by('-created_at')
    
    paginator = Paginator(logs_list, 20)
    page_number = request.GET.get('page')
    logs = paginator.get_page(page_number)
    
    context = {'logs': logs}
    context.update(get_admin_counts())
    return render(request, 'shares/admin_review_logs.html', context)


@user_passes_test(is_admin)
def admin_report_logs(request):
    """举报日志列表"""
    log_types = [
        ShareLog.ActionType.REPORT_HANDLE,
    ]
    
    logs_list = ShareLog.objects.filter(
        action__in=log_types
    ).select_related('user', 'share').order_by('-created_at')
    
    paginator = Paginator(logs_list, 20)
    page_number = request.GET.get('page')
    logs = paginator.get_page(page_number)
    
    context = {'logs': logs}
    context.update(get_admin_counts())
    return render(request, 'shares/admin_report_logs.html', context)


def user_public_profile(request, username):
    """用户公开个人主页"""
    author = get_object_or_404(User, username=username)
    
    # 获取该用户发布的所有公开且已通过审核的分享
    shares_list = public_share_queryset(Share.objects.filter(author=author)).order_by('-created_at')
    
    paginator = Paginator(shares_list, 12)
    page_number = request.GET.get('page')
    shares = paginator.get_page(page_number)
    
    # 获取用户的公开合集
    collections = Collection.objects.filter(
        author=author,
        is_public=True
    ).annotate(item_count=Count('collectionitem')).order_by('-updated_at')
    
    return render(request, 'shares/user_public_profile.html', {
        'author': author,
        'shares': shares,
        'collections': collections,
    })


@login_required
def create_collection(request):
    """创建合集"""
    if request.method == 'POST':
        form = CollectionForm(request.POST)
        if form.is_valid():
            collection = form.save(commit=False)
            collection.author = request.user
            collection.save()
            messages.success(request, '合集创建成功！')
            
            # 支持 next 参数重定向
            next_url = request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
                
            return redirect('my_shares')  # 或者跳转到合集详情页
    else:
        form = CollectionForm()
    
    return render(request, 'shares/create_collection.html', {'form': form})


@login_required
def edit_collection(request, collection_id):
    """编辑合集"""
    collection = get_object_or_404(Collection, id=collection_id, author=request.user)
    
    if request.method == 'POST':
        form = CollectionForm(request.POST, instance=collection)
        if form.is_valid():
            form.save()
            messages.success(request, '合集更新成功！')
            return redirect('collection_detail', collection_id=collection.id)
    else:
        form = CollectionForm(instance=collection)
    
    return render(request, 'shares/edit_collection.html', {'form': form, 'collection': collection})


@login_required
def delete_collection(request, collection_id):
    """删除合集"""
    collection = get_object_or_404(Collection, id=collection_id, author=request.user)
    
    if request.method == 'POST':
        collection.delete()
        messages.success(request, '合集已删除')
        return redirect('my_shares')
    
    return render(request, 'shares/delete_collection.html', {'collection': collection})


def collection_detail(request, collection_id):
    """合集详情页"""
    collection = get_object_or_404(Collection, id=collection_id)
    
    if not can_view_collection(request.user, collection):
        messages.error(request, '该合集不存在或您没有权限访问')
        return redirect('index')
        
    # 获取合集内的分享（按顺序）
    collection_items = CollectionItem.objects.filter(collection=collection).select_related('share', 'share__author', 'share__author__profile').order_by('order', 'added_at')
    
    visible_items = [
        item for item in collection_items
        if can_view_share(request.user, item.share)
    ]
            
    return render(request, 'shares/collection_detail.html', {
        'collection': collection,
        'items': visible_items,
    })


@login_required
@require_POST
def add_share_to_collection(request, share_id):
    """将分享添加到合集"""
    share = get_object_or_404(Share, share_id=share_id)
    
    # 只能添加自己的分享到自己的合集
    if share.author != request.user:
        messages.error(request, '只能将自己的分享添加到合集')
        return redirect('share_detail', share_id=share_id)
        
    collection_id = request.POST.get('collection_id')
    with transaction.atomic():
        collection = get_object_or_404(
            Collection.objects.select_for_update(),
            id=collection_id,
            author=request.user,
        )
        
        # 检查是否已存在
        if CollectionItem.objects.filter(collection=collection, share=share).exists():
            messages.warning(request, '该分享已在合集中')
        else:
            # 获取当前最大排序值
            max_order = CollectionItem.objects.filter(collection=collection).aggregate(Max('order'))['order__max']
            new_order = (max_order or 0) + 1

            CollectionItem.objects.create(collection=collection, share=share, order=new_order)
            log_share_action(request.user, share, ShareLog.ActionType.ADD_TO_COLLECTION, f'加入合集: {collection.title}')
        
    return redirect('share_detail', share_id=share_id)


@login_required
@require_POST
def remove_share_from_collection(request, collection_id, share_id):
    """从合集移除分享"""
    with transaction.atomic():
        collection = get_object_or_404(
            Collection.objects.select_for_update(),
            id=collection_id,
            author=request.user,
        )
        share = get_object_or_404(Share, share_id=share_id)
        item = get_object_or_404(
            CollectionItem.objects.select_for_update(),
            collection=collection,
            share=share,
        )
        item.delete()
        log_share_action(request.user, share, ShareLog.ActionType.REMOVE_FROM_COLLECTION, f'移出合集: {collection.title}')

    messages.success(request, '分享已从合集中移除')
    return redirect('collection_detail', collection_id=collection.id)

def get_share_code(request, share_id):
    """API: 获取单个分享的分享码"""
    try:
        share = Share.objects.get(share_id=share_id)
    except Share.DoesNotExist:
        return JsonResponse({'error': 'Share not found'}, status=404)
    
    if not can_view_share(request.user, share):
        status = share_api_denial_status(share)
        error = 'Permission denied' if status == 403 else 'Share not available'
        return JsonResponse({'error': error}, status=status)
            
    data = [{
        "title": share.title,
        "code": share.strategy_code
    }]
    return JsonResponse(data, safe=False)


@require_POST
def record_view(request, share_id):
    """记录一次分享浏览；Cookie 用于避免同一访客重复计数。"""
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)

    viewed_shares = request.COOKIES.get('viewed_shares', '')
    viewed_list = viewed_shares.split(',') if viewed_shares else []

    if share_id not in viewed_list:
        view_limit = consume_rate_limit('view_counter_ip', f'ip:{get_client_ip(request)}')
        if view_limit.allowed:
            Share.objects.filter(share_id=share_id).update(views=F('views') + 1)
            share.refresh_from_db()
            viewed_list.append(share_id)
            if len(viewed_list) > 100:
                viewed_list = viewed_list[-100:]

    response = JsonResponse({
        'status': 'success',
        'views_count': share.views,
    })
    response.set_cookie(
        'viewed_shares',
        ','.join(viewed_list),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite='Lax',
    )
    return response


@require_POST
def record_copy(request, share_id):
    """记录分享被复制的次数，使用Cookie防止重复计数"""
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    
    # 使用Cookie防止重复计数
    copied_shares = request.COOKIES.get('copied_shares', '')
    copied_list = copied_shares.split(',') if copied_shares else []
    
    if share_id not in copied_list:
        copy_limit = consume_rate_limit('copy_counter_ip', f'ip:{get_client_ip(request)}')
        if copy_limit.allowed:
            Share.objects.filter(share_id=share_id).update(copies=F('copies') + 1)
            share.refresh_from_db()
            copied_list.append(share_id)
            if len(copied_list) > 100:
                copied_list = copied_list[-100:]
    
    response = JsonResponse({
        'status': 'success',
        'copies_count': share.copies
    })
    
    # 设置Cookie，有效期30天
    response.set_cookie(
        'copied_shares',
        ','.join(copied_list),
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite='Lax'
    )
    return response


@login_required
@require_POST
def toggle_like(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    
    if share.likes.filter(id=request.user.id).exists():
        share.likes.remove(request.user)
        is_liked = False
    else:
        share.likes.add(request.user)
        is_liked = True
        
    return JsonResponse({
        'status': 'success', 
        'is_liked': is_liked,
        'likes_count': share.likes.count()
    })


@login_required
@require_POST
def toggle_favorite(request, share_id):
    share = get_object_or_404(Share, share_id=share_id)
    if not can_view_share(request.user, share):
        return JsonResponse({'status': 'error', 'message': 'Share not found'}, status=404)
    
    if share.favorites.filter(id=request.user.id).exists():
        share.favorites.remove(request.user)
        is_favorited = False
    else:
        share.favorites.add(request.user)
        is_favorited = True
        
    return JsonResponse({
        'status': 'success', 
        'is_favorited': is_favorited,
        'favorites_count': share.favorites.count()
    })


def get_collection_codes(request, collection_id):
    """API: 获取合集内所有分享的分享码"""
    try:
        collection = Collection.objects.get(id=collection_id)
    except Collection.DoesNotExist:
        return JsonResponse({'error': 'Collection not found'}, status=404)
    
    if not can_view_collection(request.user, collection):
        return JsonResponse({'error': 'Permission denied'}, status=403)
    
    shares = []
    collection_items = CollectionItem.objects.filter(collection=collection).select_related('share').order_by('order', 'added_at')
    
    for item in collection_items:
        share = item.share
        
        if can_view_share(request.user, share):
            shares.append({
                "title": share.title,
                "code": share.strategy_code
            })
            
    return JsonResponse(shares, safe=False)





