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

分享卡片、详情页和弹窗统一通过 `shares.preview_urls.build_board_preview_url` 生成图片地址，由 `shares.render_version.get_board_render_version` 请求 node-zsb 的 `GET /render-meta` 自动发现版本。响应必须为 HTTP 200、`ok: true`，且 `data.renderVersion` 是非空字符串。版本按不透明字符串原样使用，不转成数字、不手动同步版本号；分享码始终编码成单一路径段，`rv` 也单独进行 URL 编码。

后端请求地址与浏览器图片地址分开配置：

| 配置 | 默认值与用途 |
| --- | --- |
| `BOARD_RENDER_META_URL` | 生产：`http://localhost:3000/render-meta`，直连同机 node-zsb，兼容其 `localhost` / IPv6 回环监听；开发：`https://ff14hub.com/n/render-meta`，与现有 Django `/n/` 开发代理使用同一线上渲染器；测试环境未显式配置时为空，避免测试访问网络。显式设为空可禁用发现，使用无版本图片地址。 |
| `BOARD_RENDER_META_TIMEOUT_SECONDS` | 默认 `1` 秒，限制元数据网络操作和并发等待时间。 |

浏览器始终请求同源 `/n/board/<编码后的分享码>?rv=<编码后的版本>`；后端元数据 URL 必须是后端可访问的完整地址，不能直接填写 `/n/render-meta`。生产 node-zsb 不在同机或使用不同监听地址时，可改为实际直连 URL 或 `https://ff14hub.com/n/render-meta`。本地默认无须启动 node-zsb；如果改为本地联调，需要同时让浏览器 `/n/` 图片代理指向同一个本地渲染器，只改元数据地址不会改变图片代理目标。

版本使用进程内线程安全缓存，最长复用 60 秒，扣除上游 `Age` 和本次请求耗时，并遵守更短的 `max-age`。过期后的并发请求合并刷新，等待超过超时的请求直接使用无版本地址。接口尚未部署、超时、HTTP 错误、无效 JSON/版本或已无剩余新鲜期时，立即放弃旧版本，回退到 `/n/board/<编码后的分享码>`，利用 node-zsb 已有的 `no-cache` + ETag 校验；只缓存这个失败结果 5 秒以避免逐卡片重试。正常响应限制为 16 KiB。

该缓存覆盖当前单 Waitress 进程的所有线程；增加进程或实例时，每个进程各自合并刷新，不跨进程合并（配置 Django 共享 cache 也不会自动改变这一点）。元数据流量上限通常是每进程每 60 秒一次，带 `Age` 时会更早刷新。

主站没有 Django 整页或模板片段缓存；动态 HTML 统一使用私有 `no-store`，卡片 JSON 片段也保留现有 `no-store`。基础模板禁用 HTMX 历史快照，防止浏览器历史缓存恢复旧预览地址；历史恢复请求不发送 `HX-Request`，确保取得新的完整页面。仓库 Nginx 示例未启用 HTML 代理缓存；生产额外的 CDN／代理规则也必须遵守这些响应头，不能强制缓存 HTML。已经打开的页面需刷新后获取新地址。

FFXIVShare 可以先于元数据接口上线，期间自动使用无版本地址；接口可用后自动恢复版本化图片。今后仅由 node-zsb 维护和部署渲染版本，无须再次更新分享站、修改作品数据、清理图片缓存或重新发布作品。

本次接入的离线回归测试：

```powershell
$env:APP_ENV = 'test'
$env:BOARD_RENDER_META_URL = ''
venv\Scripts\python.exe -B manage.py test shares.test_render_version shares.test_preview_url_contracts shares.test_preview_page_cache
```

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
