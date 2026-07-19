# 主站前端

本目录只构建 Django 主站使用的 TypeScript、HTMX 和 CSS。以下独立功能不属于这条流水线：

- `../static/overlay/`
- `../static/viewer*/`
- `../sb_renderer/`

## 常用命令

```powershell
npm.cmd ci --prefix frontend
npm.cmd --prefix frontend run verify
npm.cmd --prefix frontend run dev
```

`verify` 包含 78 项 Vitest/jsdom 行为测试、设计系统契约、颜色对比度、主站与 E2E TypeScript 类型检查、ESLint 和生产构建。生成物写入 `../static/app/`，不进入 Git；Django 通过 `static/app/manifest.json` 加载带内容哈希的入口文件。

真实浏览器回归使用 Playwright、Chromium 和 axe。它覆盖首页敏感内容三态、详情弹层、焦点恢复、登录回跳、桌面核心页面 WCAG A/AA，以及 390px 移动端横向溢出。不要直接连接日常或线上数据库；从仓库根目录运行隔离脚本：

```powershell
Push-Location frontend
npx.cmd playwright install chromium
Pop-Location

.\ops\testing\Test-BrowserFlows.ps1 `
    -RepositoryRoot . `
    -PythonExecutable .\venv\Scripts\python.exe `
    -NpmExecutable npm.cmd
```

脚本会在系统临时目录创建数据库、媒体目录和随机回环端口，结束后清理服务进程与全部临时数据。Release CI 会自动安装 Chromium 并执行同一套测试。

## 样式分层

主站以本地 Bootstrap 5.3 为组件底座，自有样式由 `src/styles/main.css` 按层导入：

1. `tokens.css`：颜色、排版、间距、圆角、阴影、焦点、动效和层级令牌。
2. `foundations.css`：页面根布局和基础规则。
3. `bootstrap-adapter.css`：统一 Bootstrap 变量与交互状态。
4. `components.css` 及同级文件：可复用业务组件和页面组合。

新增视觉规则应复用令牌与语义 class。`data-*` 仅作为 TypeScript 行为钩子；不要在生成的 `static/app/` 中手工修改样式。

颜色契约可单独运行：

```powershell
npm.cmd --prefix frontend run check:contrast
```

普通文本至少 4.5:1，大号文本和非文本界面至少 3:1。自动扫描不能替代键盘、读屏和真实用户验收。
