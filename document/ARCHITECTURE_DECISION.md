# 主站目标架构决策

## 结论

主站继续使用 Django，但不保留当前的单文件业务组织、全局 Vue、模板内联脚本和开发服务器式部署。

目标架构如下：

- Django 5.2 LTS，继续使用 Django Auth、Admin、ORM、Forms 和 Templates。
- SQLite 作为本批生产数据库，启用适合单机部署的并发与备份配置。
- 所有 ORM、约束和迁移同时在 PostgreSQL CI 环境验证，为以后迁移预留能力。
- HTMX 负责分页、瀑布加载、点赞收藏、表单和模态框等局部服务端交互。
- TypeScript 功能模块负责局部交互和客户端临时状态；不引入未实际使用的运行时框架。
- Vite + TypeScript 管理主站 JavaScript、CSS、Quill 和第三方前端依赖。
- 主站使用自有 CSS 设计令牌和组件类，逐步移除 Bootstrap 视觉依赖和内联样式。
- Quill 2 提供富文本编辑；服务端使用 nh3 白名单清洗，客户端编辑器不承担安全边界。
- Windows Server 使用 Waitress 托管 WSGI 应用，并通过 WinSW 注册 Windows 服务。
- 现有 Nginx 在本批初期继续作为反向代理；是否替换网关单独评估和发布，不与应用重构捆绑。

## 为什么不更换整个技术栈

本站的主要能力是账号、内容发布、关系数据、审核举报、管理后台和服务端搜索。这些能力与 Django 的成熟边界高度一致。

改成 React SPA、Next.js 或 FastAPI + SPA 会重新实现认证、表单、权限、管理后台和服务端数据装配，同时增加前后端契约与部署复杂度。当前没有实时协作、离线应用或重客户端状态需求，因此收益不足以抵消迁移风险。

本批采用深层次的 Django 内部重构：保留成熟框架能力，替换不合适的数据库使用方式、业务组织、展示层和部署方式。

## 目标分层

```text
HTTP views
    -> policies       权限与可见性
    -> forms          输入解析与字段校验
    -> services       状态变化与事务
    -> selectors      查询、注解与展示数据
    -> presentation   页面 ViewModel 与 HTMX 片段
    -> models         持久化结构与约束
```

视图只负责 HTTP 编排。模板不得承载核心权限和状态判断，JavaScript 不得成为数据校验或安全规则的唯一实现。

## 主站前端结构

```text
frontend/
    src/
        api/
        components/
        pages/
        styles/
            tokens.css
            base.css
            components/
            pages/
```

主站保持服务端渲染。HTMX 响应返回 HTML 片段，并正确设置 `Vary: HX-Request`。客户端行为在 TypeScript 功能入口中注册，避免继续增加散落的全局函数。

默认视觉方向为浅色、克制的游戏社区风：暖白背景、深灰正文、酒红主色、旧金色强调色，并为战斗、娱乐、警告和审核状态保留独立语义色。

## 明确排除

“小抄儿”本批不做任何功能、代码或视觉调整，包括：

- `static/overlay/stgy.html`
- `static/viewer_new/`
- `sb_renderer/`
- 小抄儿的编辑、导入和渲染行为

主站重构必须保持以下接口的路径和成功响应结构：

- `/api/share/<share_id>/code/`
- `/api/collection/<collection_id>/codes/`

接口数组中的 `title` 和 `code` 字段不得改名。主站公开分享链接 `/s/<share_id>` 和已有 `share_id` 也保持稳定。

## 部署边界

Waitress 只监听本机回环地址，TLS、静态文件和外部访问仍由反向代理承担。WinSW 配置、服务恢复策略和滚动日志纳入仓库。

Nginx Windows 版存在官方已知扩展限制，因此只作为当前兼容入口。若后续更换 Caddy 或 IIS，必须在独立发布中复现全部现有路由，并对小抄儿和 `/n/` 预览链路进行兼容验证。
