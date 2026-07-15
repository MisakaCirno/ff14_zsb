# 主站前端

本目录只构建 Django 主站使用的 TypeScript、HTMX、Alpine 和 CSS。

明确不属于此管线的目录：

- `../static/overlay/`
- `../static/viewer_new/`
- `../sb_renderer/`

首次安装：

```powershell
npm --prefix frontend ci
```

验证和生产构建：

```powershell
npm --prefix frontend run verify
```

仅运行前端行为测试：

```powershell
npm --prefix frontend test
```

行为测试使用 Vitest 与 jsdom，覆盖剪贴板超时和手动复制、内容警告揭示、分享图模态焦点、Canvas 字素簇截断，以及点赞／收藏的失败反馈、权威列表刷新、脱离 DOM 后的请求生命周期、并发响应和焦点恢复。测试不替代浏览器回归；涉及真实 HTMX 交换、Bootstrap 模态或响应式布局的改动仍需在隔离数据库中验收。

仅检查颜色令牌和交互状态对比度：

```powershell
npm --prefix frontend run check:contrast
```

对比度检查覆盖正文、弱化文字、语义状态、按钮各状态、禁用态、表单边框、焦点环、暗色导航和图片遮罩。普通文本最低为 4.5:1，非文本界面最低为 3:1；它会随 `verify` 自动执行。

开发监听构建：

```powershell
npm --prefix frontend run dev
```

输出目录为 `../static/app/`，该目录是生成物且不进入 Git。Vite manifest 固定输出为 `static/app/manifest.json`，供 Django 模板加载带哈希的入口文件。

## 样式分层

主站继续以本地 Bootstrap 5.3 作为组件底座，自有样式统一由 `src/styles/main.css` 按顺序导入：

1. `tokens.css`：颜色、排版、间距、圆角、阴影、焦点、动效和层级令牌。
2. `foundations.css`：页面根布局和不依赖具体组件的基础规则。
3. `bootstrap-adapter.css`：只覆盖 Bootstrap 的共性变量和精确状态，不压平语义变体与尺寸变体。
4. `components.css` 及同级组件文件：主站业务组件样式。

新增或迁移的视觉规则使用 CSS class；现存模板内联属性会按页面批次继续收口。`data-*` 属性只作为 TypeScript 行为钩子。第三方 CSS 保留在 `../static/css/`，Vite 构建产物中不直接维护样式。
