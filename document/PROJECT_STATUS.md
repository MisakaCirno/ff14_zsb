# 项目当前状态

更新时间：2026-07-19

## 结论

当前技术路线适合继续维护：Django 服务端渲染负责权限、表单和内容工作流，HTMX/TypeScript 负责渐进增强，Vite 管理可缓存的前端产物。对这个以内容分享、审核和账户功能为主的网站，切换为前后端分离或从零重写不会带来与风险相称的收益。

仓库侧 R00–R18 已完成，核心业务、设计系统、Windows 运行工具、迁移工具和自动验证均已建立。正式发布仍未完成：没有线上数据库与媒体的不可变副本就不能完成 R19，也不能授权 R20 切换。

“小抄儿”及其 `/n/` 生产路由不在本批重构范围内。

## 本批完成

- 本地测试数据库升级至当前迁移；升级前有校验备份，升级后通过 SQLite 完整性与外键检查。
- Fast/Full/Release 分档，所有步骤输出独立耗时；Django 测试支持 4 worker 并行。当前 Fast 为 87 秒。
- 移除未使用的 Alpine 运行时，生产 JS 从约 168.30 KB 降至 111.59 KB，gzip 从约 49.26 KB 降至 32.28 KB。
- 将数据可移植模块拆分为 schema、codec、projection、I/O 和事务编排层；冻结 v1/v2/v3 数据格式保持兼容。
- 增加 CSP Report-Only、限流部署检查、Python/npm 漏洞审计和 Nginx 分级静态缓存。
- 修复 `python-dotenv` 已知漏洞，移除不再需要的 `django-ckeditor`。旧 0011 迁移保留原名称，字段替换经 `sqlmigrate` 证明为数据库 no-op。
- 开发渲染器代理增加 GET/HEAD 限制、超时、16 MiB 上限、no-store 和通用错误响应。
- 增加 Playwright/Chromium 与 axe 回归，覆盖敏感内容三态、详情弹层、焦点恢复、登录回跳、WCAG A/AA 和移动端横向溢出。
- 重写入口与前端文档，补充 Windows、测试、安全、部署和数据门禁说明。

## 最近验证

- Fast：579 项 Django 测试通过，5 项按环境跳过；78 项 Vitest 通过；设计系统、77 组颜色对比度、TypeScript、ESLint、Vite、Windows 运维、WinSW 和 Nginx 契约通过；总耗时 87 秒。
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

1. 从线上取得有来源、时间点和摘要证明的只读数据库副本及媒体快照。
2. 按 `PRODUCTION_COPY_REHEARSAL.md` 在隔离目录完成两轮 R19 演练并归档证据。
3. 只读审查真实 Nginx、TLS、域名、`/n/` 和 Windows 服务拓扑。
4. 准备 R20 go/no-go 运行单、维护窗口、停写证明、最终备份、管理员密码轮换和回滚演练。
5. 获得明确上线授权后才执行正式迁移与切换。

在完成第 1–4 项前，不得对线上数据库执行 `migrate`，不得把新服务配置指向线上数据，也不得声称重构版本已经发布。
