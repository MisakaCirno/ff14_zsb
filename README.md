# 粘鼠板儿

面向《最终幻想 XIV》玩家的战术板分享站。用户可以发布、搜索、收藏和整理战术板代码，也可以通过公开、仅链接可见和私有三种可见性控制内容。主站包含审核、举报、站内信、合集、站点动态，以及剧透／令人不适内容的隐藏、遮挡、显示三态浏览策略。

项目已进入可持续维护阶段。当前主站采用 Django 5.2 LTS、SQLite（可切换 PostgreSQL）、Vite、TypeScript、HTMX 和 Bootstrap。这个技术路线与网站的服务端渲染、渐进增强和中等规模社区业务相匹配，暂不需要重写为前后端分离架构。

“小抄儿”是独立功能，不属于本轮主站重构范围。`static/overlay/`、`static/viewer*` 和 `sb_renderer/` 的现有行为及生产 `/n/` 路由必须保持兼容。

## 本地启动（Windows）

要求 Python 3.11、Node.js 22 和 PowerShell。以下命令均在仓库根目录执行：

```powershell
py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd ci --prefix frontend

Copy-Item .env.sample .env
venv\Scripts\python.exe -B manage.py migrate
npm.cmd --prefix frontend run build
venv\Scripts\python.exe -B manage.py runserver
```

访问 <http://127.0.0.1:8000/>。Windows 的 `cmd.exe` 不支持 `source venv/bin/activate`；本项目也不要求激活虚拟环境，直接调用 `venv\Scripts\python.exe` 最稳定。

前端监听构建可在另一个终端运行：

```powershell
npm.cmd --prefix frontend run dev
```

如需管理后台，交互创建本地管理员，不要把密码写进代码或文档：

```powershell
venv\Scripts\python.exe -B manage.py createsuperuser
```

## 验证

日常开发使用 Fast 档位。它执行业务、权限、界面、配置等快速 Django 测试，以及前端行为测试、类型检查、设计系统和基础运维契约；历史迁移与完整数据可移植测试留给 Full 档位：

```powershell
.\verify.ps1
```

合并或候选发布前使用：

```powershell
.\verify.ps1 -Profile Full
```

Release 档位额外执行生产副本的合成离线端到端演练，以及隔离临时数据库上的 Playwright/Chromium 核心流程和 axe WCAG A/AA 扫描：

```powershell
Push-Location frontend
npx.cmd playwright install chromium
Pop-Location
.\verify.ps1 -Profile Release
```

只运行浏览器回归：

```powershell
.\ops\testing\Test-BrowserFlows.ps1 `
    -RepositoryRoot . `
    -PythonExecutable .\venv\Scripts\python.exe `
    -NpmExecutable npm.cmd
```

项目不使用 GitHub Actions；部署结论以目标 Windows 服务器的发布预检为准。需要检查依赖漏洞时，可按需安装 `requirements-audit.txt` 后在本地运行 `pip-audit`，并在 `frontend` 目录运行 `npm audit`。

## 数据与发布安全

- 本地 `db.sqlite3` 只是开发数据；正式数据在线上。R19 已使用线上不可变捕获完成两轮独立离线演练；正式切换仍必须在停写后的最终备份副本上迁移和校验。
- 迁移、导出、导入和校验工具均保留旧格式兼容；不得用旧数据库覆盖已经产生新用户写入的数据库。
- 生产 Waitress 只能监听 `127.0.0.1`，由同机 Nginx 提供 HTTPS、静态文件和媒体文件。
- 当前限流默认使用进程内缓存，只适用于单应用进程。扩展为多进程或多实例前必须配置共享缓存；`manage.py check --deploy` 会以 `shares.W001` 提醒这一边界。
- CSP 默认以 Report-Only 运行。收集并确认生产页面没有违规后，才可将 `CSP_REPORT_ONLY=False` 切换为强制模式。
- Vite 指纹资源可由 Nginx 永久缓存；普通静态文件只使用短缓存，用户媒体不设置永久缓存。
- 生产服务器使用仓库根目录的 `start_ffxivshare.bat` 作为唯一入口。它会检查远程快进更新并在确认后自动同步依赖、构建前端、收集静态文件和执行发布预检；需要数据库迁移时仍会先创建校验备份并单独请求确认。

## 目录

```text
ffxivshare/            Django 配置、环境、安全头、健康检查与可观测性
shares/                主站领域模型、页面、策略、服务、迁移和测试
frontend/src/          TypeScript 与设计系统源码
frontend/e2e/          Playwright 核心流程与 axe 无障碍回归
templates/             Django 服务端模板
ops/                   Windows 服务、Nginx、备份和数据迁移工具
document/              架构、测试、生产运行和迁移手册
static/overlay/        独立“小抄儿”功能（本轮不改）
sb_renderer/           独立渲染器（本轮不改）
```

## 战术板预览缓存版本

分享卡片、详情页和弹窗统一通过 `shares.preview_urls.build_board_preview_url` 生成 `/n/board/<分享码>?rv=<版本>`。`ffxivshare.settings.BOARD_RENDER_CACHE_VERSION` 应与 node-zsb 的 `RENDER_CACHE_VERSION` 保持一致；当前均为 `2`。

只有渲染输出可能变化时才升级该版本。推荐先部署带新版本参数的 FFXIVShare，再部署新版 node-zsb；两个服务短暂不一致时，渲染器会以不缓存的临时跳转纠正版本，不会把新图片写入旧版浏览器缓存。

## 进一步阅读

- [重构执行计划](document/REFACTORING_PLAN.md)
- [重构问题清单](document/REFACTORING_BACKLOG.md)
- [项目当前状态](document/PROJECT_STATUS.md)
- [测试指南](document/TESTING_GUIDE.md)
- [测试套件审查](document/TEST_SUITE_REVIEW.md)
- [Windows 生产运行手册](document/WINDOWS_PRODUCTION.md)
- [生产副本演练手册](document/PRODUCTION_COPY_REHEARSAL.md)
- [R20 现有 Git 仓库精简发布手册](document/R20_GIT_DEPLOYMENT.md)
- [可观测性约定](document/OBSERVABILITY.md)
- [HTTP 契约](document/HTTP_CONTRACTS.md)

项目使用并兼容 [Ennea/ffxiv-strategy-board-viewer](https://github.com/Ennea/ffxiv-strategy-board-viewer) 的战术板相关能力。《FINAL FANTASY XIV》相关商标和素材权利归其权利人所有。
