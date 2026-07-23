# R20 现有 Git 仓库精简发布手册

本文适用于当前“单台 Windows 服务器、线上目录是 Git clone、SQLite 数据库随现有部署运行”的发布方式。目标是用最少步骤完成安全升级，不要求为了本次发布先建设 release 目录、`current` junction、WinSW 或制品平台。

“小抄儿”不属于本次改造。正式 Nginx 中现有 `/n/` 路由必须保留；主站切换和回滚不得移动或覆盖它的数据与进程。

## 不可省略的边界

- Git 只管理代码，不保护 SQLite 用户数据、`.env`、运行进程或 Nginx 配置。
- 启动器先把 `origin/master` 解析为固定的 40 位 commit SHA，展示更新内容并取得人工确认后，才允许执行 `--ff-only` 快进；不执行目标会漂移的裸 `git pull`。
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

#### 一个 BAT 生成脱敏发布预检报告

切换到目标 commit、安装锁定依赖、构建前端并执行 `collectstatic` 后，运行仓库根目录的 `preflight_ffxivshare.bat`，并粘贴运行单中已经批准的完整 40 位目标 commit SHA；也可以预先用 `FFXIVSHARE_TARGET_COMMIT` 传入同一 SHA。工具不允许用分支名、短 SHA 或“当前代码看起来正确”替代目标绑定。它默认检查仓库根目录 `.env`；如果生产配置在仓库外，先为当前 PowerShell 进程设置 `FFXIVSHARE_ENV_FILE` 为该文件的绝对路径。报告写入当前驱动器的 `FFXIVShare-R20\Readiness`，不会写进 Git 工作区。

预检只读取 Git、环境文件、运行时版本、已安装依赖、构建产物和 SQLite schema；不会执行 Git 更新、依赖安装、前端构建、`collectstatic`、migration、服务控制或数据库写入。环境值只在进程内验证，报告仅包含配置键名、检查结论和非秘密路径，并固定记录 `values_recorded=false`、`cutover_authorized=false`。

它会阻断以下问题：工作区中的主站运行文件与目标 commit 不一致；目标不是当前完整 40 位 commit；Python 不是仓库内 Python 3.11 venv；`requirements.txt` 精确版本未安装；Node 不满足 `>=22.12.0 <23`；npm 依赖或 lockfile 不一致；Vite/`collectstatic` 产物过期；生产环境核心值、安全设置、SQLite 路径或耐久性参数不合格；数据库身份、sidecar 或 migration 历史异常；Waitress 仍在监听 8000 端口。非主站本机文件、缺少有安全默认值的显式键、尚未创建的媒体目录和正常 pending migration 会作为警告列出。

- 退出码 0：`ready_for_maintenance=true`，可以继续运行单；
- 退出码 2：正常生成报告但结论为 NO-GO，按 `blockers` 修复后重跑；
- 其它退出码：工具或输入错误，不能继续。

预检通过不等于批准切换。报告 SHA-256 需要写入正式运行单，最终 go/no-go 仍由操作者在维护窗口明确确认。

#### 一个 BAT 完成更新、准备、升级选择和启动

正式切换后，日常只运行仓库根目录的 `start_ffxivshare.bat`。不再手工组合 `git pull`、依赖安装、前端构建、`collectstatic`、预检以及“带迁移／不带迁移”启动脚本。统一入口按以下顺序工作：

1. 确认 `127.0.0.1:8000` 已停止，关键运行文件没有本机修改；
2. 获取 `origin/master` 并固定为完整 commit；发现快进更新时展示提交列表，由操作者选择“更新并启动”“继续使用当前版本”或“停止”；
3. 只有版本变化或本机准备状态缺失时，才同步锁定的 Python/npm 依赖、构建 Vite、执行 `collectstatic` 并运行完整发布预检；普通重启直接复用已经验证的产物；
4. 以 SQLite `immutable=1` 只读连接比较代码 migration 图和数据库 `django_migrations`。

数据库判断结果仍遵循以下规则：

- 完全一致：直接启动 Waitress；
- 存在 pending migration：列出待应用 migration，并让操作者选择“创建已校验备份、安全升级并启动”或“暂不升级、保持停止”；
- 出现未知 migration、依赖断裂或 SQLite sidecar：不提供升级选项，保持停止并要求检查。

选择升级后，内部流程先确认 `127.0.0.1:8000` 没有监听者，在 `C:\FFXIVShare-R20\Upgrades` 的私有时间戳目录中保存两份迁移前数据库副本。`migrate` 只在候选库上执行；随后用独立只读比较器逐表证明源库已有表、列、行和序列下限全部保留，新增数据只能来自明确列入白名单的 migration，新增回收站字段必须保持未删除默认值。语义比较、schema 检查、限制预检、部署检查和 SQLite Backup API 校验全部通过后，才在同一磁盘上原子替换活动库并启动 Waitress。切换前失败不会修改活动库；切换后检查失败会尝试原子恢复迁移前数据库并核对 SHA-256。

选择不升级不会修改数据库，也不会启动新代码。非交互调用绝不默认批准代码更新或数据库升级。Git 只允许快进，不执行 `reset`、`clean`、自动 stash 或强制覆盖；关键工作区有修改、历史分叉、构建失败、预检失败或数据库状态异常时都保持停止。启动器仍不控制 Nginx 或独立的 Bun `/n/` 服务。

### C. 短维护窗口与切换

严格按运行单顺序执行：

1. 启用维护页或阻断主站写请求，同时保持 `/n/` 原路由。
2. 停止所有 Django/Waitress 写入者，确认 `127.0.0.1:8000` 已经停止监听；不要停止独立的 `/n/` Bun 进程。
3. 运行仓库根目录的 `start_ffxivshare.bat`。启动器会展示固定的远程目标 commit；选择更新后自动完成快进、依赖同步、前端构建、`collectstatic` 和预检。若提示数据库需要升级，确认列出的 migration 后选择 1；不准备继续时选择 2，数据库和主站都保持原状。
4. `preflight_ffxivshare.bat` 保留为独立诊断入口，日常启动不再要求手工先运行它。
5. 选择 1 后等待候选库迁移、源库/候选库语义比较、校验、原子切换和最终 schema 检查全部完成；任一步失败都不要手工启动 Waitress。
6. Waitress 启动后先做回环健康检查，再验证 HTTPS、登录、创建/编辑、审核后台、CSRF、静态资源和 `/n/`。
7. 交互轮换线上管理员密码；记录账户、时间和验证结果，不记录密码。
8. 全部通过后才撤下维护页并恢复写入，并保留本次 `C:\FFXIVShare-R20\Upgrades\<时间戳>` 目录作为回滚证据。

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
- 发布预检报告路径与 SHA-256；
- 精确停止/启动命令及全部写入者列表；
- 最终数据库备份路径、SHA-256 和恢复命令；
- 候选数据库迁移与比较结果；
- Nginx 维护页、主站代理和 `/n/` 保持方式；
- 管理员密码轮换记录位置；
- 恢复写入前的验收结果；
- 操作者、批准人、UTC 时间和最终 go/no-go 结论。

运行单不得包含密码、`SECRET_KEY`、数据库口令或访问令牌。缺失任一项时结论只能是 no-go。

## 当前进度

R19 已完成。2026-07-20 已取得并验证线上只读环境清单及启动/Nginx 配置审查包，阶段 A 完成。该清单记录的切换前拓扑为：

- Windows Server 2022 单机；Nginx 在 `0.0.0.0:80/443`，Waitress 在 `127.0.0.1:8000`，独立 Bun 渲染器在 `[::1]:3000`；
- 没有 Windows 服务、计划任务或自动启动要求，操作者手动运行 BAT；
- 主站当时由仓库根目录的未跟踪 `start_waitress_migrate.bat` 启动，该文件会在每次启动前直接执行 `migrate`，正式切换后不得继续使用；
- 线上 Git 为 `master` / `origin/master`；清单当时记录的 commit 为 `244c32734e9fab5af05bf544a654615eeab31404`；
- SQLite 为仓库根目录 `db.sqlite3`，检查时没有 WAL/SHM/journal；媒体目录不存在；
- 正式 `/n/` 由 Nginx 独立转发到 Bun，主站升级不需要停止或修改它。

仓库已新增统一根入口 `start_ffxivshare.bat`。它委托 `ops/release/Start-DirectGitWaitress.bat` 完成固定目标的快进更新、按需发布准备、发布预检和 schema 判断：没有更新且产物已验证时直接启动；存在代码更新时先展示并确认，再自动准备；存在 pending migration 时由操作者在同一界面选择安全升级或保持停止；异常历史不会被自动处理。升级流程已用隔离数据库完整验证候选迁移、源库/候选库逐表语义比较、双份源库回滚副本、SHA-256、原子替换和最终 schema 检查；还使用 2026-07-19 的只读生产快照完成了 `0018` 到当前 migration leaf 的演练。启动器不激活环境，不执行强制 Git 操作，也不控制 Nginx/Bun；代理信任和 traceback 参数与当前生产安全契约一致。`ops/nginx/ffxivshare.direct-git.locations.conf.example` 只替换主站、静态和健康检查块，不定义 `/n/`。

仓库也已新增 `preflight_ffxivshare.bat`：它以脱敏报告自动检查完整目标 commit、关键脏文件、环境键及生产值、Python/Node/npm、锁定依赖、前端和 `collectstatic` 产物、SQLite 文件身份及 schema。测试已证明 NO-GO 和 READY 两条路径都不包含 `SECRET_KEY`；完整生产形态合成配置取得 `ready_for_maintenance=true`，pending migration 只作为后续安全升级提示。所有检查前后数据库 SHA-256 相同，也没有创建 sidecar。统一启动器还会用当前 Git HEAD 覆盖进程内 `APP_VERSION`，使运行日志和数据库备份绑定真实 commit，而不依赖 `.env` 中可能过期的版本值。

2026-07-23 已完成首次 R20 直接 Git 切换、`0019` 至 `0030` 的安全迁移和回环健康检查。后续版本使用统一启动器进行快进更新和按需准备；独立预检脚本继续用于故障诊断和人工审计。
