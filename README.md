# FFXIV 战术板分享平台

**该项目绝大部分内容由AI生成，暂未进行完全的内容审查**
**请根据情况谨慎使用该项目，避免造成意料之外的损失**

> ✅ **项目状态**: 已完成初始化并成功运行！
> 🆕 **最新更新**: 用户系统功能扩展完成（昵称设置、密码修改）

## 项目简介

当前项目为便于玩家互相分享自己战术板配置的交流平台，游戏内可以把配置导出成类似`[stgy:a0+k-wvprSQA8rHpf1cY9Fk5R6ZNO5n7lvBSj9+rmE3OpbNbquadZFNuf34LKtB6Vvu+VwRuRlBjVWYHUviSUm70CoiZFyhI4mL2zvz2dqd2H+n24dmIzgMnUiI7BIEsAjEu6Yw8QNW73V4PV9for2+LXvcEt7lWK15eZwHDQojZm4juqzJiypDd5BkvBnZNs5j2tK]`的分享代码，其他人可以使用代码导入。

我们的平台作为分享战术板使用，用户可以创建一个战术板分享，输入标题、战术板代码等项，创建成一个链接，访问这个链接就可以获取到代码以及查看预览。

平台也有用户系统，用户可以增删改查自己的分享，也可以列出和查看其他人的公开分享。

## ✨ 核心功能

### 分享管理
- 📝 创建战术板分享
- 👁️ 预览战术板配置
- 🔗 生成分享链接
- 📱 生成二维码
- ✏️ 编辑/删除自己的分享
- 🔒 公开/私有分享控制

### 用户系统
- 👤 用户注册和登录
- 🎭 **设置个性化昵称**
- 📝 **编辑个人简介**
- 🔑 **修改密码功能**
- 📊 查看账户信息
- 💼 我的分享管理

### 社区功能
- 🏠 瀑布流分享广场
- 👥 **昵称显示系统**（设置后其他人看到昵称）
- 📈 浏览量统计
- 🔍 分享浏览

## 快速开始

### 访问地址
- **主页**: http://127.0.0.1:8000/
- **个人资料**: http://127.0.0.1:8000/profile/edit/
- **修改密码**: http://127.0.0.1:8000/profile/password/
- **管理后台**: http://127.0.0.1:8000/admin/

### 管理员账户
- 用户名: `admin`
- 密码: `admin123`

### 启动服务器
```bash
# 激活虚拟环境
.\venv\Scripts\Activate.ps1

# 启动开发服务器
python manage.py runserver
```

## 📚 文档

- 📖 **详细启动指南**: [STARTUP_GUIDE.md](STARTUP_GUIDE.md)
- 🆕 **用户系统更新说明**: [USER_SYSTEM_UPDATE.md](USER_SYSTEM_UPDATE.md)
- 🎛️ **管理后台使用指南**: [ADMIN_GUIDE.md](ADMIN_GUIDE.md)

## 目标功能
1. 主页面为广场，其有瀑布流的分享详情卡片列表
1. 有导航栏，其中包括主页、个人信息和单独的创建新分享按钮
1. 使用分享按钮呼出模态窗口，其可让用户将战术板配置分享代码等信息输入并提交成新的分享
2. 创建完成后将用户转置该代码的分享页面
1. 页面上包含用户反馈预览+链接+预览画板，并且一获取分享图片的功能，图片会将预览框截图并添加当前链接的二维码在角落

## 技术细节

### 预览
预览使用现有的库，其在当前目录下，使用方式类似于
```html
<style>
    iframe {
        width: 1024px;
        height: 768px;
        border: none;
    }
</style>

<iframe src="./static/viewer/embed.html#[stgy:a0+k-wvprSQA8rHpf1cY9Fk5R6ZNO5n7lvBSj9+rmE3OpbNbquadZFNuf34LKtB6Vvu+VwRuRlBjVWYHUviSUm70CoiZFyhI4mL2zvz2dqd2H+n24dmIzgMnUiI7BIEsAjEu6Yw8QNW73V4PV9for2+LXvcEt7lWK15eZwHDQojZm4juqzJiypDd5BkvBnZNs5j2tK]"></iframe>
```

### 技术栈
- 后端使用django实现，SQLite数据库
- 前端使用bootstrap+vue3
- 短链接使用nanoid
- 使用虚拟环境

## 📄 致谢

本项目引用了 [Ennea/ffxiv-strategy-board-viewer](https://github.com/Ennea/ffxiv-strategy-board-viewer) 实现战术板相关的部分。