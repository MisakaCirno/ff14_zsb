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

首页与局部响应共用 `templates/shares/includes/share_cards.html`。瀑布流 continuation 使用 `templates/shares/includes/share_cards_page.html` 包装卡片与下一页状态。匿名登录链接的 `next` 参数会移除 `partial`、`page` 和 `continuation` 并进行 URL 编码，登录后不会误落入局部响应。

`continuation` 是 R12 的内部传输参数，只在 HTMX 瀑布流请求中生效；旧 `partial=shares` JSON 与普通 HTMX 卡片片段继续保持 R11 契约。

## 点赞与收藏响应

点赞 `/share/<share_id>/like/` 和收藏 `/share/<share_id>/favorite/` 只接受 `POST`：

- 已登录的普通请求继续返回原 JSON 字段，不受前端迁移影响。
- HTMX 请求必须指定 `fragment=card` 或 `fragment=detail`，返回与场景样式匹配的单个按钮 HTML，并由按钮以 `outerHTML` 替换自身。
- 缺少或传入未知 fragment 时返回 `400`，且不得改变点赞或收藏关系。
- 登录会话失效时返回 `204` 和 `HX-Redirect`。登录回跳优先采用经过同源校验的 `HX-Current-URL`，禁止外站地址进入 `next`。

响应按 `HX-Request` 和 `Cookie` 区分，并使用私有 `no-store` 缓存策略。CSRF 保护保持启用，浏览器端由全局 HTMX 请求钩子注入 `X-CSRFToken`。
