# R20 现有 Git 仓库精简发布手册

本文适用于当前“单台 Windows 服务器、线上目录是 Git clone、SQLite 数据库随现有部署运行”的发布方式。目标是用最少步骤完成安全升级，不要求为了本次发布先建设 release 目录、`current` junction、WinSW 或制品平台。

“小抄儿”不属于本次改造。正式 Nginx 中现有 `/n/` 路由必须保留；主站切换和回滚不得移动或覆盖它的数据与进程。

## 不可省略的边界

- Git 只管理代码，不保护 SQLite 用户数据、`.env`、运行进程或 Nginx 配置。
- 发布使用经验证的 40 位 commit SHA，不执行目标会漂移的裸 `git pull`。
- 不对脏工作区执行 `reset --hard`、`clean` 或自动 stash。先记录并保护本机修改和未跟踪启动脚本；与目标 commit 冲突时停止发布。
- migration 只在最终备份的工作副本上执行。活动数据库不会成为首次试跑对象。
- 迁移、检查和登录验收完成前保持主站停写。Nginx 可以继续提供维护页以及原有 `/n/`。
- 所有 R19 证据仍为 `cutover_authorized=false`。只有本次运行单由操作者明确批准后，才允许进入维护窗口。

## 精简后的四个阶段

### A. 一次性只读环境清单

先识别线上真实状态，不预设它使用 WinSW、计划任务、手工批处理或其它启动方式。把 `Get-CurrentGitDeploymentFacts.ps1` 单独复制到仓库外运行；不要为了取得脚本先更新线上仓库：

```powershell
$Output = 'C:\FFXIVShare-R20\inventory-20260720.json'

.\Get-CurrentGitDeploymentFacts.ps1 `
    -RepositoryRoot 'C:\Users\Administrator\Desktop\srv\ff14_zsb' `
    -DatabasePath 'C:\Users\Administrator\Desktop\srv\ff14_zsb\db.sqlite3' `
    -OutputPath $Output

Get-FileHash -LiteralPath $Output -Algorithm SHA256
```

如果已经知道 Nginx、外部 `.env` 或媒体目录，再显式增加 `-NginxRoot`、`-EnvironmentFile` 或 `-MediaRoot`。工具只生成指定 JSON：它不读取 `.env` 内容，不打开或哈希活动数据库，不读取 Nginx 配置正文，不控制服务，也不执行 Git 更新、migration 或依赖安装。报告包含已脱敏的运行命令等运维元数据，应按内部资料保存。

清单必须回答以下问题后才能继续：

1. 当前 commit、分支、上游及脏工作区文件是什么？
2. Django 由哪个进程、服务或批处理启动，精确停止和启动命令是什么？
3. SQLite、`.env`、静态目录和媒体目录的真实路径是什么？
4. Nginx 在本机还是另一台主机，谁负责 HTTPS 和 `/n/`？
5. 主站停写时，是否仍有计划任务、第二个进程或其它写入者？

### B. 发布前离线准备

在开发机完成，不修改线上环境：

1. 选择完整的 40 位目标 commit，并记录当前线上 commit。
2. 对目标 commit 运行 `verify.ps1 -Profile Full`；需要正式浏览器验收时由操作者在外部 Chrome/Edge 执行步骤。
3. 检查线上脏文件是否与目标 commit 冲突。未跟踪的启动脚本和本机配置先复制到仓库外，原件不删除。
4. 预先下载并校验 Python/npm 依赖，避免维护窗口等待网络。
5. 根据阶段 A 的真实启动方式生成一份短运行单，写明停止、启动、健康检查和 Nginx 维护页命令。

本阶段不要求把应用迁移到 `releases/current` 目录。WinSW 仍是可选的后续运行方式改进，但不与首次安全升级捆绑。

#### 启动时无需人工判断 migration

正式切换后，日常只运行 `ops/release/Start-DirectGitWaitress.bat`。它在启动 Waitress 前调用 `check_deployment_schema --require-current`，以 SQLite `immutable=1` 只读连接比较代码 migration 图和数据库 `django_migrations`：

- 完全一致：正常启动 Waitress；
- 存在 pending migration：拒绝启动，并明确提示运行维护升级流程；
- 出现未知 migration、依赖断裂或 SQLite sidecar：视为状态异常，保持停止并要求检查。

检查不会执行 migration，也不会创建 WAL/SHM/journal。操作者不需要根据代码改动猜测是否升级；始终先运行普通启动入口，只有收到确定的“Database upgrade required”结果时才使用维护升级脚本。

### C. 短维护窗口与切换

严格按运行单顺序执行：

1. 启用维护页或阻断主站写请求，同时保持 `/n/` 原路由。
2. 停止所有 Django/Waitress 写入者，确认相关进程和监听端口已经退出。
3. 使用 R19 已验证的捕获工具制作最终 SQLite 备份三件套，记录源 commit、UTC、操作者和 SHA-256。
4. 从最终备份创建候选数据库；新代码只对候选数据库执行 `migrate`、完整性/外键检查、限制预检和数据比较。
5. 在线上 Git clone 中 `fetch`，确认目标对象存在，再切换到固定 commit。若本机修改与目标冲突，立即停止，不自动清理。
6. 使用锁定文件更新虚拟环境，执行前端构建和 `collectstatic`。
7. 交互轮换线上管理员密码；记录账户、时间和验证结果，不记录密码。
8. 用已验证候选数据库替换运行路径中的数据库，同时保留迁移前原库和最终备份。
9. 启动主站，先做回环健康检查，再验证 HTTPS、登录、创建/编辑、审核后台、CSRF、静态资源和 `/n/`。
10. 全部通过后才撤下维护页并恢复写入。

每一步都必须成功才进入下一步。任一失败都保持停写，不用旧库覆盖候选数据库后继续尝试。

### D. 最小回滚策略

恢复写入前发现问题最简单：停止新进程，切回原 commit 和原启动文件，恢复迁移前数据库，验证后重新开放旧站。

恢复写入后不得直接恢复旧数据库，否则会丢失切换后用户数据。此时先重新停写并制作事故时点备份，再选择向前修复；只有证明旧代码能读取当前 schema 时才允许只回退代码。无法证明时保持维护状态并人工决定数据转换方案。

因此，登录、创建、编辑、审核和 `/n/` 验收都安排在恢复公开写入之前完成。

## 最小 go/no-go 清单

正式维护窗口只需要一张短运行单，但下列字段不能缺失：

- 当前 commit 与目标 commit；
- R19 Pair 报告 SHA-256；
- 线上环境清单 JSON 与 SHA-256；
- 精确停止/启动命令及全部写入者列表；
- 最终数据库备份路径、SHA-256 和恢复命令；
- 候选数据库迁移与比较结果；
- Nginx 维护页、主站代理和 `/n/` 保持方式；
- 管理员密码轮换记录位置；
- 恢复写入前的验收结果；
- 操作者、批准人、UTC 时间和最终 go/no-go 结论。

运行单不得包含密码、`SECRET_KEY`、数据库口令或访问令牌。缺失任一项时结论只能是 no-go。

## 当前进度

R19 已完成。2026-07-20 已取得并验证线上只读环境清单及启动/Nginx 配置审查包，阶段 A 完成。实际拓扑为：

- Windows Server 2022 单机；Nginx 在 `0.0.0.0:80/443`，Waitress 在 `127.0.0.1:8000`，独立 Bun 渲染器在 `[::1]:3000`；
- 没有 Windows 服务、计划任务或自动启动要求，操作者手动运行 BAT；
- 主站由仓库根目录的未跟踪 `start_waitress_migrate.bat` 启动，该文件会在每次启动前直接执行 `migrate`，正式切换后不得继续使用；
- 线上 Git 为 `master` / `origin/master`，当前 commit `244c32734e9fab5af05bf544a654615eeab31404`；本机 `dev_init.bat` 修改和两个未跟踪启动 BAT 必须保留，不得自动清理；
- SQLite 为仓库根目录 `db.sqlite3`，检查时没有 WAL/SHM/journal；媒体目录不存在；
- 正式 `/n/` 由 Nginx 独立转发到 Bun，主站升级不需要停止或修改它。

仓库已新增 `ops/release/Start-DirectGitWaitress.bat`：它先只读判断 schema 是否匹配，再启动 Waitress；不激活环境、不执行 migration、依赖安装、Git 更新或静态构建。代理信任和 traceback 参数与当前生产安全契约一致。`ops/nginx/ffxivshare.direct-git.locations.conf.example` 只替换主站、静态和健康检查块，不定义 `/n/`。

R20 当前进入阶段 B。下一步是确认线上 `.env` 仅包含哪些键（不采集值）、Node/npm 是否满足前端构建要求，并准备固定目标 commit 的依赖与候选数据库流程；仍未进入维护窗口。
