# 项目当前状态

更新时间：2026-07-20

## 结论

当前技术路线适合继续维护：Django 服务端渲染负责权限、表单和内容工作流，HTMX/TypeScript 负责渐进增强，Vite 管理可缓存的前端产物。对这个以内容分享、审核和账户功能为主的网站，切换为前后端分离或从零重写不会带来与风险相称的收益。

R00–R19 已完成，核心业务、设计系统、Windows 运行工具、迁移工具和自动验证均已建立。2026-07-19 已使用线上不可变数据库捕获和经确认的空媒体快照完成两次独立离线迁移演练，冻结 Pair verifier 未发现解释不了的差异。正式发布仍未完成；R19 证据固定 `cutover_authorized=false`，不能替代 R20 的停写、最终备份、切换与上线授权。

“小抄儿”及其 `/n/` 生产路由不在本批重构范围内。

## 本批完成

- 本地测试数据库仍停留在 `shares/0025`，本次开发没有就地修改它；统一启动器会在需要时提示是否对备份候选库执行 `0026`–`0029`，拒绝升级则保持停止。
- Fast/Full/Release 分档，所有步骤输出独立耗时；Django 测试支持 4 worker 并行。当前 Fast 为 87 秒。
- 移除未使用的 Alpine 运行时，生产 JS 从约 168.30 KB 降至 111.59 KB，gzip 从约 49.26 KB 降至 32.28 KB。
- 将数据可移植模块拆分为 schema、codec、projection、I/O 和事务编排层；当前 v4 完整保存回收站状态，冻结的 v1/v2/v3 数据格式保持兼容。
- 生产数据库升级新增独立只读语义门禁：源表、源列、已有行和序列下限必须保留，新增行与默认值必须命中精确白名单；已用线上 `0018` 不可变快照演练至 `0029`。
- 分享与合集删除改为可恢复回收站；点赞、收藏、举报、合集成员和审计日志不再因网页删除而级联丢失，用户与分享的 Django Admin 物理删除入口已关闭。
- 增加 CSP Report-Only、限流部署检查、Python/npm 漏洞审计和 Nginx 分级静态缓存。
- 修复 `python-dotenv` 已知漏洞，移除不再需要的 `django-ckeditor`。旧 0011 迁移保留原名称，字段替换经 `sqlmigrate` 证明为数据库 no-op。
- 开发渲染器代理增加 GET/HEAD 限制、超时、16 MiB 上限、no-store 和通用错误响应。
- 增加 Playwright/Chromium 与 axe 回归，覆盖敏感内容三态、详情弹层、焦点恢复、登录回跳、WCAG A/AA 和移动端横向溢出。
- 重写入口与前端文档，补充 Windows、测试、安全、部署和数据门禁说明。
- 完成线上生产副本的 Proposal、人工 Review、Approval、双次独立离线演练和 Pair verifier 复核；源库不变，未解释差异为 0，非敏感摘要见 `R19_PRODUCTION_REHEARSAL_REPORT.md`。

## 最近验证

- Fast：606 项 Django 测试通过，7 项按环境跳过；78 项 Vitest 通过；设计系统、77 组颜色对比度、TypeScript、ESLint、Vite、Windows 运维、WinSW 和 Nginx 契约通过。
- Full 独有阶段：生产副本捕获门禁、SQLite 备份、快照检查、媒体清单、导出比较、handoff、bootstrap、proposal、approval、rehearsal、pair verifier 和 Waitress 回环冒烟均通过。
- 浏览器：5 项 Playwright 测试通过，包含桌面核心页和 390px 移动端 axe 扫描，无页面运行时异常。
- 依赖：Python 主运行／PostgreSQL 运行依赖和前端生产依赖的已知漏洞扫描为 0。

## 保留边界与已接受决策

- 按产品决策，不提高“仅链接可见”分享 ID 的强度。不要在后续维护中把它当作待办自动修改。
- CSP 目前仅报告。确认真实生产页面没有违规后，才能在独立发布中切换为强制模式。
- HTMX 包源码包含可选 eval 路径，所以 Vite 会给出静态警告；应用已设置 `htmx.config.allowEval = false`，CSP 也未允许 `unsafe-eval`。后续升级 HTMX 时重新评估，当前不修改第三方源码或隐藏警告。
- 当前限流 cache 为进程内存。单应用进程部署有效；多进程或多实例前必须换成共享 cache，部署检查会提示 `shares.W001`。
- HSTS 子域和 preload 保持关闭，必须在确认全部子域永久 HTTPS 后再评估。

## 尚未完成

1. 确认线上 `.env` 键名以及 Node/npm 构建能力，不读取或归档任何秘密值。
2. 根据已确认的手动 BAT、同机 Nginx/Waitress/Bun 拓扑生成精简 go/no-go 运行单以及最终备份、固定 commit 切换和回滚命令。
3. 在维护窗口前验证依赖、候选数据库迁移、Nginx 恢复方式和外部 Chrome/Edge 验收步骤。
4. 获得明确上线授权后才停止写入并执行正式迁移与切换。

在完成第 1–3 项并获得第 4 项明确授权前，不得对线上数据库执行 `migrate`，不得把新服务配置指向线上数据，也不得声称重构版本已经发布。
