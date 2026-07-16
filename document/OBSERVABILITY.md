# 可观测性与性能基线

本文约定主站的请求日志、健康探针和 R17 性能硬预算。“小抄儿”不在本约定范围内。

## 请求日志

生产环境默认启用请求日志；开发和测试默认关闭输出，避免测试套件为每个请求打印一行。需要本地观察时设置：

```dotenv
REQUEST_LOG_ENABLED=True
```

设置变更后需要重启 Django 进程。日志由应用写到标准输出，每个请求恰好一条 `http.request` JSON；未处理异常还会追加一条可用同一 `request_id` 关联的 `django.request.error` 安全错误事件。Windows 生产契约中的 WinSW 只负责捕获和轮转标准输出，不再配置第二个应用文件日志处理器；服务目录、轮转和检查方式见 [WINDOWS_PRODUCTION.md](WINDOWS_PRODUCTION.md)。

请求记录只允许以下字段：

| 字段 | 含义 |
| --- | --- |
| `timestamp` | UTC ISO 8601 时间，精确到毫秒 |
| `level` | `INFO`、`WARNING` 或 `ERROR` |
| `logger` | 固定为 `ffxivshare.request` |
| `event` | 固定为 `http.request` |
| `request_id` | 应用生成的 32 位十六进制 UUID |
| `method` | HTTP 方法 |
| `route` | Django 路由模板，不含路径参数值；404 时为 `null` |
| `view` | Django URL 名称；无法解析时为 `null` |
| `status` | HTTP 状态码 |
| `duration_ms` | 从进入最外层中间件到取得响应对象的耗时 |
| `db_queries` | 同一阶段默认数据库连接实际执行的语句次数 |
| `response_bytes` | 非流式响应正文长度；HEAD 为 0，未知时为 `null` |
| `user_id` | 业务已经解析出的登录用户主键；匿名或尚未解析时为 `null` |
| `exception_type` | 可选，仅保留未处理异常的类名 |

应用始终重新生成 `request_id`，不会信任客户端或反向代理传入的 `X-Request-ID`；同一个值通过响应 `X-Request-ID` 返回。请求上下文使用 `ContextVar`，请求完成或异常退出时都会恢复，不能复用到下一个请求。请求内的其他 Python 日志可自动取得当前 ID。

查询计数使用 Django `connection.execute_wrapper()`，只累加次数，不启用调试游标，也不保存 SQL 或参数。中间件位于整个 Django 中间件栈最外层，因此同步响应包含认证、视图和 session 响应处理产生的查询。日志不会为了识别用户主动解析懒加载认证对象。

流式响应在取得响应对象时记录一次，不消费流；存在有效 `Content-Length` 时记录该值，否则 `response_bytes` 为 `null`。流迭代期间才执行的查询和耗时不计入本条记录。当前主站没有流式 HTTP 端点；未来新增时必须为迭代阶段建立单独、不会泄漏上下文的指标，不能把本字段误作完整传输耗时。

### 敏感数据边界

日志不得包含原始路径、路径参数值、query string、请求或响应正文、Cookie、任意请求头、IP、用户名、SQL、SQL 参数、战术板代码、内容正文、举报理由、审核说明或密码。格式化器不复制任意 `LogRecord` 属性，也不输出日志 message；第三方提供的 event 和 exception_type 字段不受信任，只有代码内白名单事件与真实 `exc_info` 类型可以进入输出。`django.server` 和 `django.request` 的普通访问日志被关闭，避免与统一请求记录重复或输出原始 URL；生产环境仅保留经过同一安全格式化器处理的 `django.request` ERROR 事件，以覆盖内层中间件异常。

应用还保留三类有界运维事件：Admin 可见性批次选择／执行失败只记录批号、批大小和目标可见性；限流缓存失败只记录规则名；readiness 失败只记录异常类型。它们都不会记录所选主键、限流身份、数据库错误正文或用户输入。

## 健康探针

| 地址 | 方法 | 成功 | 依赖与查询 | 失败 |
| --- | --- | --- | --- | --- |
| `/health/live/` | GET、HEAD | 200 `{"status":"ok"}` | 无依赖，0 次数据库查询 | 进程无法响应 |
| `/health/ready/` | GET、HEAD | 200 `{"status":"ok"}` | 默认数据库 `SELECT 1`，恰好 1 次查询 | 503 `{"status":"unavailable"}` |

两条探针的所有应用响应都带 `Cache-Control: no-store`；其他方法返回 405 和 `Allow: GET, HEAD`，且不会查询数据库。readiness 捕获依赖检查的所有异常，但响应和日志都不包含异常正文，只记录异常类型。

生产设置只对以上两个精确路径豁免 Django 的 `SECURE_SSL_REDIRECT`。因此 Waitress 的本地回环探针可以直接访问：

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8000/health/ready/ -UseBasicParsing
```

相邻路径、缺少末尾斜杠的路径和普通业务路径仍由 Django 执行 HTTPS 重定向。Nginx 继续负责外部 HTTP 到 HTTPS 的跳转，并应只允许回环地址或明确的监控网络访问健康路径。Nginx 可关闭这两个 location 的 access log，使用应用返回的 `X-Request-ID` 与 WinSW 捕获的 JSON stdout 关联检查结果。

健康请求也会生成一条普通 `http.request` 记录。liveness 的 `db_queries` 必须为 0，readiness 成功时必须为 1；这同时验证探针本身没有引入隐藏依赖。

## R17 性能硬预算

这些值是自动化测试中的确定性上限，不是对生产硬件延迟的承诺。毫秒耗时受数据库、数据分布、磁盘、CPU 和缓存状态影响，目前只观测 `duration_ms`，不设置未经线上副本验证的固定毫秒门槛。

| 场景 | 硬预算 | 自动化证据 |
| --- | --- | --- |
| 相关合集预览 | 固定 3 条查询；每页 6 个合集；每个合集最多 5 个可见条目 | `DetailPerformanceContractTests` |
| 详情审核日志 | 只取最近 25 条；长说明只读取有界预览 | `DetailPerformanceContractTests` |
| 审核队列 | 每次请求不超过 10 条查询；响应正文小于 300000 B | `ModerationQueuePerformanceContractTests` |
| 举报队列 | 每次请求不超过 10 条查询；响应正文小于 350000 B | `ModerationQueuePerformanceContractTests` |
| Django Admin 可见性批处理 | 批大小固定 100；显式数值 `IN` 列表不超过 100 个主键 | `ShareAdminActionTests` |

可重复执行硬预算和探针测试：

```powershell
venv\Scripts\python.exe manage.py test shares.test_detail_performance -v 2
venv\Scripts\python.exe manage.py test shares.test_moderation_performance -v 2
venv\Scripts\python.exe manage.py test shares.test_admin_share_actions -v 2
venv\Scripts\python.exe manage.py test ffxivshare.test_observability -v 2
```

测量真实请求时使用与目标环境一致的数据副本、数据库后端和进程配置，开启 `REQUEST_LOG_ENABLED` 后重复请求同一 `route`，分别观察冷启动和预热后的 `duration_ms`、`db_queries` 与 `response_bytes`。不要把开发服务器、测试数据库建库时间或整套测试耗时当作单请求基线。
