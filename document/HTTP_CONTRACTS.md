# 主站 HTTP 契约

本文记录 R11 固定的公开 API 和主站局部响应边界。后续前端重构可以替换实现，但不得在没有迁移方案的情况下改变这些契约。

## 小抄儿公开 API

以下路径保持不变：

- `GET /api/share/<share_id>/code/`
- `GET /api/collection/<collection_id>/codes/`

成功响应始终是 JSON 数组。数组元素保留且只依赖以下字段：

```json
[
  {
    "title": "分享标题",
    "code": "[stgy:...]"
  }
]
```

单分享接口成功时返回一个元素；合集接口按 `order, added_at` 返回可访问的分享，空合集返回 `[]`。现有直链策略保持不变：普通访客可以读取公开或仅链接可见的已通过、待审核内容；所有者和管理员可以读取自己的受限内容。调整待审核内容的可见性属于独立产品决策，不在兼容重构中暗改。

错误响应保持 JSON 对象：

- 资源不存在或需要隐藏审核状态：`404 {"error": "... not found"}`
- 私有资源无权访问：`403 {"error": "Permission denied"}`
- 非 `GET`、`HEAD` 方法：`405 {"error": "Method not allowed"}`，并返回 `Allow: GET, HEAD`

所有分支都设置 `Vary: Cookie` 和私有 `no-store` 缓存策略。同一 URL 对匿名用户、所有者和管理员可能返回不同结果，禁止共享缓存。

## 首页和搜索局部响应

首页 `/` 与搜索 `/search/` 按请求类型协商响应：

- 普通请求：完整服务端 HTML 页面。
- `?partial=shares`：兼容现有瀑布流脚本的 JSON，字段为 `html`、`has_next`、`next_page`。
- `HX-Request: true`：默认返回纯 `text/html` 卡片片段，不包含导航、页面外壳或瀑布流 sentinel。
- `HX-Request: true` 且带 `feed=infinite&continuation=1`：返回本页卡片和下一页 sentinel；末页改为结束标记。sentinel 通过 `outerHTML` 替换自己，保留搜索、分类、筛选和排序参数。
- 同时出现 `partial=shares` 和 `HX-Request: true` 时，HTMX HTML 优先。

完整页面和局部响应都设置 `Vary: HX-Request, Cookie`。局部 HTML 与兼容 JSON 包含用户点赞、收藏状态，因此使用私有 `no-store` 缓存策略。

HTMX 搜索遇到空查询、超长查询或精确分享 ID 时返回 `204` 和 `HX-Redirect`，由浏览器执行整页导航；普通请求继续使用标准 `302`。这样可以防止完整页面或详情页被交换进卡片网格。

首页与局部响应共用 `templates/shares/includes/share_cards.html`。瀑布流 continuation 使用 `templates/shares/includes/share_cards_page.html` 包装卡片与下一页状态。普通完整分页的登录与互动返回地址保留 Paginator 解析后的实际页码；首页移除 `page`。局部 HTML、兼容 JSON、continuation 和 infinite 传输场景会移除 `partial`、`page` 与 `continuation`，并对登录链接的 `next` 进行 URL 编码，返回后不会误落入局部响应。

`continuation` 是 R12 的内部传输参数，只在 HTMX 瀑布流请求中生效；旧 `partial=shares` JSON 与普通 HTMX 卡片片段继续保持 R11 契约。

## 点赞与收藏响应

点赞 `/share/<share_id>/like/` 和收藏 `/share/<share_id>/favorite/` 只接受 `POST`：

- 请求体必须明确提交 `target_state=active` 或 `target_state=inactive`，分别表示确保关系存在或不存在；接口不再接受含义不确定的空请求体切换。
- 相同目标状态的重复或并发请求是幂等的。服务端在短事务内重新检查访问权限并返回事务完成后的实际状态与计数。
- 点赞和收藏按钮以携带 CSRF 令牌、显式目标状态和返回地址的原生 HTML 表单为基础能力；HTMX 只负责渐进增强局部更新，JavaScript 未加载或执行失败时仍可提交操作。
- 已登录的普通请求未提交 `next` 时继续返回精确的原 JSON 字段：点赞为 `status`、`is_liked`、`likes_count`，收藏为 `status`、`is_favorited`、`favorites_count`。
- 已登录的普通请求显式提交 `next` 时，安全的同源站内地址返回 `302`；非法、外站或不可解析的 `next` 回退到当前分享的 canonical detail 地址，不能形成开放重定向。
- HTMX 请求必须指定 `fragment=card` 或 `fragment=detail`，返回与场景样式匹配的单个按钮 HTML，并由按钮以 `outerHTML` 替换自身；即使请求体同时包含 `next`，HTMX fragment 契约仍优先，不返回 `302`。
- 缺少或传入未知 fragment、缺少或传入未知目标状态时返回 `400`，且不得改变点赞或收藏关系。
- 登录会话失效时，HTMX 请求返回 `204` 和 `HX-Redirect`；普通表单进入登录页。登录回跳只采用经过同源校验的当前页面、显式 `next` 或来源页，无安全候选时回退到 canonical detail，绝不能落到仅接受 `POST` 的点赞或收藏动作地址。

响应按 `HX-Request` 和 `Cookie` 区分，并使用私有 `no-store` 缓存策略。CSRF 保护保持启用：原生表单携带 CSRF 字段，浏览器端仍由全局 HTMX 请求钩子注入 `X-CSRFToken`。

R15 部署时必须同步发布后端与按钮模板。旧页面中的空请求体不会回退到有竞态的 toggle 语义，而会收到 `400`；用户刷新页面后即可获得携带显式目标状态的新按钮。该变更不影响“小抄儿”公开 API。

## 账户页面与认证响应

登录、注册、个人资料和修改密码页面只接受 `GET`、`HEAD` 与 `POST`，其他方法返回 `405` 和精确的 `Allow: GET, HEAD, POST`。退出登录只接受带 CSRF 令牌的 `POST`。这些页面及退出响应始终使用私有 `no-store` 缓存策略。

登录、注册与修改密码请求中的密码字段被标记为敏感参数，不得进入异常报告或响应 HTML。账户表单保留 Django 原生密码字段语义；密码首尾空格属于密码本身，不得被静默裁剪。

登录和注册共用同源 `next` 校验，并在两个页面之间继续传递经过验证的目标。POST 中显式提交的无效目标不会回退到 GET 中的目标；成功后跳转到安全目标，没有安全目标时使用 `LOGIN_REDIRECT_URL`。注册时用户和 `UserProfile` 必须在同一事务中成功创建或一并回滚。

注册、登录和修改密码分别执行 IP、账户或用户限流。超限响应为 `429`，携带正整数秒数的 `Retry-After`，并使用未绑定密码数据的表单渲染；超限后不得继续执行认证、用户名唯一性检查、密码验证或密码保存。

## 站内信响应与状态变更

站内信列表 `/messages/` 与详情 `/messages/<id>/` 只接受已登录用户的 `GET`、`HEAD`，并且不得隐式改变已读或归档状态。用户只能读取自己的消息；其他用户（包括普通 staff）的消息统一返回 `404`。列表按 `created_at, pk` 稳定倒序分页，并通过 `mailbox=inbox|unread|archived` 明确划分收件箱、未读和归档箱。

以下状态变更只接受带 CSRF 令牌的 `POST`：

- `/messages/<id>/open/`：只设置首次阅读时间；重复提交不得覆盖原时间。
- `/messages/mark-all-read/`：只批量更新当前用户、未归档且尚未阅读的消息；重复提交更新数为零。
- `/messages/<id>/archive/`：必须提交 `target_state=archived` 或 `target_state=inbox`，按目标状态幂等归档或恢复，不采用 toggle 语义。

上述页面与操作响应始终使用私有 `no-store` 缓存策略。返回上下文只接受经过枚举和正整数校验的 `mailbox`、`page`，不接受任意跳转地址。关联分享只有在当前用户仍通过统一分享可见性策略时才显示链接；否则只显示不可访问提示，站内信正文仍完整保留。

站内信属于用户数据而不是可清理缓存。归档只移动邮箱分区，不删除记录；Django Admin 仅允许只读查看，禁止新增、修改、批量操作和删除。
