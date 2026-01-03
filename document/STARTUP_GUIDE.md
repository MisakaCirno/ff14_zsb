# 粘鼠板儿 - 快速启动指南

## 项目已成功初始化！✅

项目已完成从头开始的初始化和配置，所有文件已创建完毕，数据库已迁移，服务器正在运行中。

## 当前状态

- ✅ Django 项目结构创建完成
- ✅ Python 虚拟环境已配置
- ✅ 所有依赖包已安装
- ✅ 数据库迁移已完成
- ✅ 管理员账户已创建
- ✅ 开发服务器正在运行
- ✅ 浏览器已打开项目主页

## 管理员账户信息

```
用户名: admin
密码: admin123
管理后台: http://127.0.0.1:8000/admin/
```

## 项目访问地址

- **主页**: http://127.0.0.1:8000/
- **管理后台**: http://127.0.0.1:8000/admin/
- **用户注册**: http://127.0.0.1:8000/register/
- **用户登录**: http://127.0.0.1:8000/login/
- **创建分享**: http://127.0.0.1:8000/create/

## 如何使用

### 1. 注册并登录
- 访问 http://127.0.0.1:8000/register/ 注册新账户
- 或使用管理员账户登录

### 2. 创建战术板分享
- 登录后点击导航栏的"创建分享"按钮
- 填写标题、粘贴战术板代码（格式如 `[stgy:a0+k-wvpr...]`）
- 添加描述信息（可选）
- 选择是否公开分享
- 提交后会跳转到分享详情页

### 3. 查看和分享
- 在详情页可以看到：
  - 战术板代码（可一键复制）
  - 分享链接（可复制分享给他人）
  - 战术板预览（iframe）
  - 二维码生成功能
- 点击"生成二维码"可获得分享链接的二维码

### 4. 管理分享
- 访问"我的分享"可查看、编辑或删除自己的分享
- 只有作者本人可以编辑和删除分享

## 项目结构

```
FFXIVShare/
├── ffxivshare/          # Django 主项目
│   ├── settings.py      # 项目配置
│   ├── urls.py          # URL 路由
│   ├── wsgi.py          # WSGI 配置
│   └── asgi.py          # ASGI 配置
├── shares/              # 分享应用
│   ├── models.py        # 数据模型（Share）
│   ├── views.py         # 视图函数
│   ├── forms.py         # 表单
│   ├── urls.py          # 应用路由
│   ├── admin.py         # 管理后台配置
│   └── migrations/      # 数据库迁移文件
├── templates/           # HTML 模板
│   ├── base.html        # 基础模板
│   └── shares/          # 分享相关模板
├── static/              # 静态文件
│   └── viewer/          # 战术板预览器
│       └── embed.html   # 战术板预览页面
├── venv/                # Python 虚拟环境
├── manage.py            # Django 管理脚本
├── requirements.txt     # 依赖包列表
└── db.sqlite3           # SQLite 数据库
```

## 技术栈

- **后端**: Django 4.2.8
- **数据库**: SQLite3
- **前端**: Bootstrap 5.3 + Vue 3
- **短链接**: nanoid
- **二维码**: qrcode + Pillow

## 常用命令

### 启动开发服务器
```bash
# 激活虚拟环境
.\venv\Scripts\Activate.ps1

# 启动服务器
python manage.py runserver
```

### 数据库管理
```bash
# 创建迁移文件
python manage.py makemigrations

# 应用迁移
python manage.py migrate

# 创建超级用户
python manage.py createsuperuser
```

### 其他命令
```bash
# 进入 Django Shell
python manage.py shell

# 收集静态文件（生产环境）
python manage.py collectstatic

# 运行测试
python manage.py test
```

## 后续开发建议

### 功能增强
1. **搜索功能**: 添加战术板搜索和过滤
2. **标签系统**: 为分享添加标签分类
3. **点赞/收藏**: 用户可以点赞或收藏喜欢的分享
4. **评论系统**: 允许用户评论和讨论战术板
5. **图片上传**: 支持上传战术板截图
6. **导出功能**: 将战术板导出为图片（带二维码）

### 优化改进
1. **性能优化**: 
   - 添加缓存机制
   - 优化数据库查询
   - 使用 CDN 加载静态资源

2. **安全增强**:
   - 添加 CSRF 保护验证
   - 实现速率限制
   - 添加用户邮箱验证

3. **用户体验**:
   - 添加实时预览
   - 优化移动端适配
   - 添加暗色主题

### 部署准备
1. 修改 `settings.py` 中的 `SECRET_KEY`
2. 设置 `DEBUG = False`
3. 配置 `ALLOWED_HOSTS`
4. 使用 PostgreSQL 替代 SQLite
5. 配置静态文件服务（如 Nginx）
6. 设置 HTTPS

## 示例战术板代码

可以使用以下示例代码进行测试：
```
[stgy:a0+k-wvprSQA8rHpf1cY9Fk5R6ZNO5n7lvBSj9+rmE3OpbNbquadZFNuf34LKtB6Vvu+VwRuRlBjVWYHUviSUm70CoiZFyhI4mL2zvz2dqd2H+n24dmIzgMnUiI7BIEsAjEu6Yw8QNW73V4PV9for2+LXvcEt7lWK15eZwHDQojZm4juqzJiypDd5BkvBnZNs5j2tK]
```

## 问题排查

### 服务器无法启动
- 确保虚拟环境已激活
- 检查端口 8000 是否被占用
- 查看错误日志

### 静态文件无法加载
- 确认 `STATIC_URL` 配置正确
- 运行 `python manage.py collectstatic`
- 检查浏览器控制台错误

### 数据库错误
- 删除 `db.sqlite3` 和 `migrations/` 文件
- 重新运行 `makemigrations` 和 `migrate`

## 项目维护

- 定期备份数据库
- 更新依赖包版本
- 监控服务器日志
- 定期清理无效数据

## 联系方式

如有问题或建议，欢迎反馈！

---

**祝你使用愉快！** 🎮✨
