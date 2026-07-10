# 线上数据迁移契约

## 数据来源

仓库根目录的 `db.sqlite3` 仅用于本地开发和测试，不代表线上真实数据。

正式迁移以线上数据库的不可变备份为唯一源数据。取得线上副本前，可以实现迁移框架和测试，但不能声称生产迁移已经验证完成。

## 基本原则

- 不在唯一一份线上数据库上试运行迁移。
- 不静默丢弃、截断或覆盖无法识别的数据。
- 不改变用户密码哈希、公开分享 ID、原始创建时间和业务归属关系。
- 所有无法导入的记录进入隔离报告，并阻止正式切换。
- 迁移工具必须版本化、幂等、可中断重跑，并纳入自动化测试。
- 旧库在切换后保持只读并至少保留一个完整观察周期。

## 迁移方式

本批优先采用旁路新库迁移：

1. 线上进入短维护窗口并冻结写入。
2. 生成最终数据库备份和文件摘要。
3. 使用旧版本导出命令生成带版本清单的 JSONL 数据集。
4. 使用新版本 migrations 创建全新的目标 SQLite 数据库。
5. 按依赖顺序导入用户、资料、分享、关系数据、审核举报、日志、公告和站内信。
6. 执行结构、数量、摘要、外键、唯一性和业务不变量校验。
7. 启动新应用并执行关键用户流程冒烟。
8. 校验成功后切换数据库路径；失败则恢复旧应用和旧库。

旧库不会被就地转换或覆盖，因此回滚不依赖反向迁移。

## 中间数据格式

导出目录必须包含：

- `manifest.json`：格式版本、应用版本、导出时间、源数据库类型和各实体数量。
- 每类实体独立的 UTF-8 JSONL 文件。
- 文件 SHA-256 摘要。
- 隔离记录和告警报告。

导出内容必须覆盖：

- Django 用户及权限关系
- UserProfile
- Share 及审核字段
- Likes 和 Favorites
- Collection 和 CollectionItem
- Report
- ShareLog
- Announcement
- SiteMessage

## 工具命令

在冻结写入并取得不可变备份后，从旧应用环境导出：

```powershell
python manage.py export_site_data D:\migration\ffxivshare-export
python manage.py validate_site_data D:\migration\ffxivshare-export
```

在已经执行完 migrations、且业务表为空的目标数据库中导入：

```powershell
python manage.py import_site_data D:\migration\ffxivshare-export
```

- 导出目录已存在时命令默认拒绝覆盖；只有显式传入 `--overwrite` 才会替换。
- 校验结果写入 `validation-report.json`，导入结果写入 `import-report.json`。
- 任何校验或导入错误都会列入隔离记录，并使整个导入事务回滚。
- 目标库已有不同数据时拒绝导入；若目标库与数据集逐文件摘要完全一致，则重复执行安全返回，不重复写入。
- 导入保留主键、密码哈希、分享 ID、时间字段和所有关系，并按数据库后端重置自增序列。

## 强制校验

迁移前后至少比较：

- 每类实体行数
- 用户主键、用户名和密码哈希摘要
- Share 主键、`share_id`、作者、状态和时间字段摘要
- 点赞、收藏、合集成员和外键关系数量
- 悬空外键和重复唯一键
- 所有无法映射或被清洗的字段差异

任何数量减少、主键冲突、密码哈希变化或未解释的字段截断都视为迁移失败。

## PostgreSQL 兼容

本批生产环境继续使用 SQLite，但：

- 禁止新增 SQLite 专用业务 SQL。
- 数据迁移中间格式不依赖数据库引擎。
- CI 使用 SQLite 和 PostgreSQL 两套数据库运行模型、迁移和核心服务测试。
- 未来迁移 PostgreSQL 时复用相同的导出、导入和校验流程。

## SQLite 运行和备份

- 生产库放在服务器本地持久化 NTFS 目录，不把数据库或 WAL 文件放在网络共享中。
- 默认启用 WAL、`FULL` 同步、30 秒锁等待和短事务 `IMMEDIATE` 模式；出现持续锁等待时迁移 PostgreSQL，而不是继续放宽超时。
- 不直接复制正在运行的 `db.sqlite3`、`-wal` 或 `-shm` 文件。在线备份使用 SQLite Backup API：

```powershell
python manage.py backup_database D:\FFXIVShareBackups\site-2026-07-11.sqlite3
```

- 命令会对备份执行 `PRAGMA integrity_check`，并生成同名 `.sha256` 文件；已有文件默认拒绝覆盖。
- 备份应复制到另一物理存储，并定期在隔离环境中执行恢复、迁移、数据校验和关键流程冒烟。
- PostgreSQL 使用 `requirements-postgres.txt` 和环境变量切换；CI 会在 PostgreSQL 16 上执行同一套迁移与测试。正式备份使用 `pg_dump`，不使用 SQLite 备份命令。
