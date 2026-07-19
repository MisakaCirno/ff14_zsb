# Windows 生产服务手册

本文约定 R18 的 Windows 生产运行方式。仓库已经提供 Waitress、WinSW、Nginx 兼容配置和自动化契约，但本文不授权直接修改当前线上服务器；正式数据演练属于 R19，停写、迁移和切换属于 R20。

“小抄儿”不在本批改造范围内。现有 `/n/` Nginx location 必须原样保留，仓库示例不会定义或重写它。

## 拓扑前提

固定链路为：

```text
Internet -> Nginx HTTPS -> 127.0.0.1:8000 Waitress -> Django
```

Waitress 只能监听 `127.0.0.1`。因此 Nginx 与 Django 必须运行在同一台 Windows 主机。若正式 Nginx 仍在另一台 Linux 主机，必须在 R20 前重新确认网关迁移或同机部署方案；不得把 Waitress 改成 `0.0.0.0`、`[::]` 或公网地址来绕过这个门禁。

当前 Linux `deploy.sh` 暂时保留，作为旧部署路径的证据和回滚材料。R20 正式切换完成前不要删除或改写它。

## 固定目录

建议使用两个相互独立的本地 NTFS 根目录：

| 路径 | 用途 | 服务账户权限 |
| --- | --- | --- |
| `D:\FFXIVShareApp\releases\<release-id>` | 不可变应用版本及该版本的 `venv` | 读取和执行 |
| `D:\FFXIVShareApp\current` | 指向当前版本的目录或 junction | 读取和执行 |
| `D:\FFXIVShareApp\service` | WinSW 二进制和 XML | 读取和执行 |
| `D:\FFXIVShareData\config\ffxivshare.env` | 生产配置和秘密 | 只读 |
| `D:\FFXIVShareData\database` | SQLite、WAL 和 SHM | 修改 |
| `D:\FFXIVShareData\media` | 持久化媒体 | 修改 |
| `D:\FFXIVShareData\logs` | WinSW 捕获的 stdout/stderr | 修改 |
| `D:\FFXIVShareData\backups` | 数据和 Nginx 配置备份 | 不授予应用服务写入 |

不要把配置、数据库、媒体、日志或备份放进 release。回滚应用版本时不得覆盖这些持久化目录。

WinSW 服务 ID 固定为 `FFXIVShare`，使用无需人工维护密码的虚拟账户 `NT SERVICE\FFXIVShare`。安装脚本只追加该服务运行所需的 ACE，不会清除机器上已有的管理员或备份账户权限。

## 上线前准备

1. 从已完整验证的提交准备一个独立 release，不要在生产目录直接 `git pull` 覆盖当前版本。
2. 在 release 内创建虚拟环境并安装 `requirements.txt`。
3. 构建主站前端，并在 release 内运行 `collectstatic`。
4. 把 `.env.production.sample` 复制到 `D:\FFXIVShareData\config\ffxivshare.env` 后替换所有占位值。`APP_VERSION` 必须填写本次部署的不可变 release 标识；数据库备份 metadata 会记录它，生产副本 handoff 会拒绝空值、`unknown` 和示例占位符。
5. 确认 `DATABASE_PATH` 指向 `D:\FFXIVShareData\database\ffxivshare.sqlite3`，`MEDIA_ROOT` 指向 `D:\FFXIVShareData\media`。
6. 让 `D:\FFXIVShareApp\current` 指向已经准备好的 release。

生产配置选择器必须由服务进程设置：

```text
FFXIVSHARE_ENV_FILE=D:\FFXIVShareData\config\ffxivshare.env
```

该变量只接受已经存在的绝对普通文件。应用不会从当前工作目录向上搜索 `.env`，进程环境变量优先于文件值。WinSW XML 只保存配置文件路径，不保存文件内容、密码或 `SECRET_KEY`。

在取得线上数据库只读副本并完成 R19 前，不要把新的服务配置指向线上数据库，也不要对线上数据库运行 `migrate`。

## 仓库侧验证

以下检查不安装服务、不访问真实 Nginx，也不使用浏览器：

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\Test-OpsContracts.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\Test-WinSWServiceContract.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\windows\Test-WaitressSmoke.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\ops\nginx\Test-NginxContracts.ps1
```

Waitress 冒烟使用临时环境文件、临时 SQLite 和随机回环端口，结束后必须没有残留进程、监听端口或临时目录。`verify.ps1 -Profile Full` 和 `-Profile Release` 会在 Windows 上执行这些契约；默认 `Fast` 档位跳过重量级迁移契约和真实 Waitress 进程冒烟，`-SkipTests` 则进一步跳过 Django 测试。

## 安装 WinSW 服务

仓库不下载也不提交 WinSW 二进制。由运维人员从 [WinSW 官方 `v2.12.0` release](https://github.com/winsw/winsw/releases/tag/v2.12.0) 获取 `WinSW-x64.exe`，通过独立可信渠道记录预期 SHA256，再把二进制放到临时 staging 目录。不要把“刚对同一个未知文件计算出的哈希”当作可信预期值。

先进行无写入预演：

```powershell
$expectedSha256 = '<trusted-64-character-sha256>'

.\ops\windows\Install-FFXIVShareService.ps1 `
    -AppRoot 'D:\FFXIVShareApp' `
    -DataRoot 'D:\FFXIVShareData' `
    -WinSWBinaryPath 'C:\staging\WinSW-x64.exe' `
    -WinSWSha256 $expectedSha256 `
    -WhatIf
```

确认输出、目录和哈希后，以管理员 PowerShell 安装但暂不启动：

```powershell
.\ops\windows\Install-FFXIVShareService.ps1 `
    -AppRoot 'D:\FFXIVShareApp' `
    -DataRoot 'D:\FFXIVShareData' `
    -WinSWBinaryPath 'C:\staging\WinSW-x64.exe' `
    -WinSWSha256 $expectedSha256 `
    -Confirm:$false
```

脚本会再次验证复制后的二进制，为服务设置虚拟账户和独立 SID，并设置最小目录 ACL。已有服务或已有服务文件默认都会使安装失败；`-ReplaceServiceFiles` 只用于人工确认过的未注册残留文件，不能作为常规升级方式。

只有在 R19/R20 数据门禁已经满足、迁移完成并确认配置正确后，才使用 `-StartService`，或显式运行：

```powershell
D:\FFXIVShareApp\service\FFXIVShareService.exe start
```

若域组策略覆盖了本机的“作为服务登录”权限，虚拟账户可能无法启动；此时应修复组策略，不要临时改成管理员账户或 LocalSystem。

## 服务检查

服务启动后先使用只读命令验证：

```powershell
Get-Service -Name FFXIVShare
Invoke-WebRequest http://127.0.0.1:8000/health/live/ -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:8000/health/ready/ -UseBasicParsing
netstat.exe -ano -p tcp | Select-String '127.0.0.1:8000'
```

预期结果：

- 服务状态为 `Running`。
- 两个探针都返回 200 和 `{"status":"ok"}`。
- 响应包含应用重新生成的 32 位十六进制 `X-Request-ID`。
- 8000 端口只出现在 `127.0.0.1`，不出现在 `0.0.0.0`、公网地址或 `[::]`。
- `ready` 失败时停止切换并检查数据库，不要继续放量。

## WinSW 恢复和日志

WinSW 直接拥有以下进程，不经过 `waitress-serve.exe` 包装层：

```text
current\venv\Scripts\python.exe -m waitress ...
```

服务使用延迟自动启动；异常退出后依次等待 10、30、60 秒重启，第三次重启仍失败后停止，避免无限崩溃循环。失败计数在一小时后重置。

应用结构化日志由 stdout/stderr 进入 `D:\FFXIVShareData\logs`。WinSW 按 25 MB 轮转并保留 14 个旧文件；不要再给 Django 增加第二套文件 handler。日志字段和敏感数据边界见 [OBSERVABILITY.md](OBSERVABILITY.md)。

## Nginx 兼容和备份

在改动真实 Nginx 前，先备份完整 `conf` 目录：

```powershell
$backup = .\ops\nginx\Backup-NginxConfig.ps1 `
    -NginxRoot 'C:\nginx' `
    -BackupRoot 'D:\FFXIVShareData\backups\nginx' `
    -Confirm:$false

$backup
Get-FileHash -LiteralPath $backup.ArchivePath -Algorithm SHA256
```

脚本创建唯一 UTC 名称的 ZIP 和 `.sha256` sidecar，不覆盖、不删除旧备份，也不会启动、停止或重载 Nginx。备份目录必须在 Nginx 根目录之外。

把 `ops/nginx/ffxivshare.locations.conf.example` 的内容合并或 include 到现有 HTTPS `server` 块。该示例：

- 只代理到 `127.0.0.1:8000`。
- 使用 `$remote_addr` 覆盖客户端传入的 `X-Forwarded-For`，不追加伪造链。
- 显式覆盖 `Host` 和 `X-Forwarded-Proto`。
- 只允许本机访问两个精确健康路径。
- 从 `current\staticfiles` 和外部 `media` 提供静态文件。
- 不定义 `/n/`，因此必须保留正式配置中现有的小抄儿 location。

先在目标服务器执行 Nginx 自带的配置检查：

```powershell
C:\nginx\nginx.exe -t -p C:\nginx\ -c conf\nginx.conf
```

只有检查成功且已核对真实域名、证书、HTTP 到 HTTPS 跳转、静态路径和 `/n/` location 后，才按现有运维流程重载。仓库脚本故意不自动重载真实 Nginx。

正式配置尚未进入仓库，因此 R20 前必须取得并只读审查线上 Nginx 配置；示例文件不能直接证明线上 `/n/`、证书或其他站点路由已经兼容。

## 卸载边界

先预演，再以管理员权限注销服务：

```powershell
.\ops\windows\Uninstall-FFXIVShareService.ps1 `
    -AppRoot 'D:\FFXIVShareApp' `
    -WhatIf

.\ops\windows\Uninstall-FFXIVShareService.ps1 `
    -AppRoot 'D:\FFXIVShareApp' `
    -Confirm:$false
```

卸载脚本只停止并注销服务，不删除 service 文件、release、配置、数据库、媒体、日志或备份。若服务曾恢复写入，不得直接用旧数据库覆盖当前库；先冻结写入并制作事故时点备份，再按 R20 回滚记录决定恢复目标。

## 进入 R19/R20 的门禁

R18 仓库实现完成不等于已经上线。继续前必须满足：

- 已确认 Nginx 与 Waitress 的同机拓扑，或完成单独的网关迁移决策。
- 已取得线上数据库的只读副本，并能证明副本时间点和来源。
- 已备份并审查真实 Nginx 配置，保留 `/n/` 当前行为。
- 已在隔离目录用真实 WinSW 2.12.0 做安装、启动、停止、自动恢复和日志轮转演练。
- 已确定 release 构建、`current` 切换和旧版本保留方式。
- 未在演练过程中修改线上数据库或删除旧版本、旧库和备份。

以上任一项缺失时不得进入正式切换。
