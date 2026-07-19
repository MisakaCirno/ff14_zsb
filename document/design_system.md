# 粘鼠板儿视觉系统

本文件定义主站（不含“小抄儿”）的视觉基础与复用边界。页面样式只负责业务布局和特殊状态，通用外观必须由设计令牌或 `ui-*` 组件提供。

## 样式分层

样式入口按以下顺序加载：

1. `tokens.css`：颜色、字号、间距、圆角、阴影、动效和层级。
2. `foundations.css`：文档级基础样式。
3. `bootstrap-adapter.css`：把 Bootstrap 组件映射到站点令牌。
4. `components.css`：可跨页面复用的 `ui-*` 组件。
5. `*-page.css`：页面布局和业务状态，不重复定义通用容器外观。

## 视觉规则

- 圆角只有两档：按钮、输入框、导航项、筛选项和徽章统一使用 `--app-radius-control`（8px），卡片、面板和浮层统一使用 `--app-radius-surface`（12px）。只有宽高相等且必须呈正圆的图标、关闭按钮和提示点可以使用 `--app-radius-circle`。
- 禁止把普通链接、按钮、导航项或状态徽章做成胶囊；Bootstrap 的 `rounded-pill` 也被映射到 8px 控件圆角。
- 阴影只有三级：普通容器 `--app-shadow-surface`、悬浮内容 `--app-shadow-floating`、对话框 `--app-shadow-dialog`。
- 颜色只能在 `tokens.css` 中写原始色值；业务样式必须引用语义令牌。
- 不使用装饰性渐变。加载状态使用纯色脉冲，状态差异通过语义色、边框和文字表达。
- 间距使用 `--app-space-1` 至 `--app-space-7`，不得为相近场景另造随机数值。

## 通用组件

- `ui-page-header`：页面主标题容器；配合 `ui-eyebrow`、`ui-page-title`、`ui-page-summary`。
- `ui-panel`：业务面板；配合 `ui-panel__header`、`ui-panel__body`、`ui-panel__footer`。
- `ui-section-header`：列表或内容分区的标题栏。
- `ui-segmented-nav`：页面内分段导航；配合 `ui-segmented-nav__list`、`ui-segmented-nav__link`。
- `ui-icon-tile`：标题和面板使用的图标块；只通过尺寸和强调修饰类变体。
- `shares/includes/page_header.html`：带图标、眉题、标题和说明的完整页面头部模板。

业务类应与组件类组合使用，例如：

```html
<header class="ui-page-header my-page-hero">
    <p class="ui-eyebrow my-page-hero__eyebrow">分区</p>
    <h1 class="ui-page-title my-page-hero__title">页面标题</h1>
</header>
```

其中 `ui-*` 管理外观，`my-page-*` 只管理该页面独有的网格、对齐和响应式行为。

## 自动检查

运行 `npm --prefix frontend run check:design`。检查会阻止：

- 在令牌文件外写十六进制或 RGB 色值；
- 引入渐变；
- 使用已废弃的尺寸型圆角、阴影令牌；
- 在组件样式中写新的原始圆角数值。

完整前端验证使用 `npm --prefix frontend run verify`。
