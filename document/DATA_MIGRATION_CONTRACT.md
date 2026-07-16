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
- Django Admin 操作日志

### UserProfile 完整性

- 迁移前必须先应用 `0023_userprofile_integrity`。该迁移只为缺失资料的用户插入默认 `UserProfile`，不会更新、截断或删除任何已有资料；反向操作为空操作，生产回滚仍以完整数据库备份为准。
- 每个用户必须恰好对应一条 `UserProfile`。导出数据集中缺失资料、重复资料或资料引用未知用户都会使校验失败并进入隔离报告，不允许通过导入时静默补造记录来改变数据集摘要。
- 历史简介无论长度都按原文导出、导入和校验。当前交互表单与管理后台只拒绝新增或实际修改后超过上限的简介；编辑其他字段时，未改动的历史超长简介必须逐字节保留。

### SiteMessage 标题完整性

- `0024_widen_site_message_titles` 将站内信标题上限从 200 扩大到 255，不删除或重写任何已有站内信；迁移前会检查全部标题长度。
- 如果源库存在超过 255 个字符的历史标题，迁移会在修改结构前明确失败并列出样本主键，禁止截断后继续。此时必须保留不可变备份，先设计能够逐字保留标题的目标字段或旁路转换，再重新执行迁移。
- 反向迁移在缩回 200 之前检查 201–255 字符的标题；发现任意记录即拒绝回滚。生产回滚继续恢复完整备份，不允许依赖字段缩窄来丢弃上线后数据。
- 新通知统一通过站内信服务创建；服务只约束新标题的显示长度，完整通知正文、关联对象和元数据保持不变。导出、导入及摘要校验仍必须逐字段保留历史标题与正文。

当前导出格式为 v3。新版本导入器同时接受历史 v1、v2 和当前 v3：

- v3 使用规范化 JSONL、固定 UTC 六位微秒时间、显式自然键协议和冻结的语义结构指纹；同名字段改型、关系目标变化或编码协议变化必须发布新的数据集版本。
- v3 保存 Django Admin 操作日志、完整 ContentType/Permission 目录、源 migration 投影和自增序列高水位。
- v3 对数据库物理表进行完整分类；未知非空表和悬空的内嵌多对多记录一律阻止导出、导入或“已导入”判定。
- `django_session` 不导出会话载荷。清单只记录总数、未过期数和最晚过期时间，切换时强制所有用户重新登录；目标会话表必须为空。
- v2 显式保存 `Share` 的活动限制状态、原因、时间和操作人。
- v1 缺少上述四个字段；导入器会从已认可举报、当前审核拒绝及审核日志时序确定性恢复。
- v1、v2 的十类实体字段表已经按版本显式冻结；后续模型新增字段不得改变历史校验或摘要。
- 任意已认可举报都优先恢复为举报下架限制；后续普通审核通过不会自动解除。
- 没有已认可举报时，当前为拒绝状态，或最后一次拒绝晚于最后一次通过，恢复为审核拒绝限制。
- 没有上述证据但旧数据为私密时，恢复为 `legacy_private` 待确认限制。它不把私密内容认定为违规，只在管理员查明来源前阻止旧下架内容被意外重新开放。
- 推导只新增管理限制，不重写用户选择的 `visibility`、原审核状态或任何旧字段。
- 历史原因缺失时使用明确的缺失标记，不把举报人原文冒充管理员处理依据。
- 删除旧管理员或举报人时，审核日志和举报证据保留，操作人字段改为 `NULL`，不会级联删除审计记录。

## 工具命令

在冻结写入并取得不可变备份后，从旧应用环境导出：

```powershell
python manage.py export_site_data D:\migration\ffxivshare-export
python manage.py validate_site_data D:\migration\ffxivshare-export `
  --report D:\migration\evidence\validation-report.json
```

在已经执行完 migrations、且业务表为空的目标数据库中导入：

```powershell
python manage.py import_site_data D:\migration\ffxivshare-export `
  --report D:\migration\evidence\import-report.json `
  --confirm-exclusive-target
python manage.py preflight_share_restrictions --strict `
  --output D:\migration\share-restriction-preflight.json
```

导入完成后，从目标库重新导出一份 v3 数据集，并对两个已经分别通过
`validate_site_data` 的不可变导出执行独立比较：

```powershell
python ops\migration\Compare-SiteDataExports.py `
  --source D:\migration\ffxivshare-export `
  --target D:\migration\ffxivshare-target-export `
  --output D:\migration\evidence\site-data-comparison.json
```

- 比较器不连接数据库，也不信任 manifest 中的摘要声明；它重新读取并校验固定 v3 目录、canonical JSONL 字节、实际 SHA、记录数量、模型和主键序列。
- 业务实体和依赖引用必须一致；目标 ContentType、Permission 和 migration 只允许是可解释的前向超集。
- 目标自增序列只能等于或高于源高水位，目标会话证据必须为空；导出时间、应用版本和数据库引擎只作为来源证据记录，不用于掩盖业务差异。
- 输出必须位于两个不可变数据集之外且默认拒绝覆盖；任何额外文件、链接、未知结构、摘要变化或不规范 JSON 都会生成失败证据并返回非零状态。
- 比较证据固定包含 `cutover_authorized=false`，不能替代限制预检、目标数据库备份校验或 R20 发布授权。

- 导出目录已存在时命令默认拒绝覆盖；只有显式传入 `--overwrite` 才会替换。
- 校验与导入报告必须显式写到数据集目录之外；导入和重复校验不得修改不可变源数据集。
- `--confirm-exclusive-target` 是硬门禁：执行前必须停止所有目标应用写入者。命令仍会在锁内重新判定目标状态，并使用 SQLite exclusive locking mode 或 PostgreSQL session advisory lock 阻止并发导入；PostgreSQL advisory lock 不能替代停止应用服务的人工证明。
- 业务行在独立 durable 事务中导入，并在提交前完成内容摘要校验；失败时业务行整体回滚，序列尚未修改。
- 自增序列在业务事务提交后执行幂等、只升不降的第二阶段收尾。收尾中断时业务数据保持已提交，报告标记 `finalization_incomplete`；使用同一数据集重跑会从内容摘要匹配状态继续，禁止清空目标库重来。
- 文件报告与数据库提交无法组成同一原子事务。命令会在写数据库前发布 `started` 证据；若最终证据写入失败，重跑会根据数据库摘要恢复 `already_imported` 证据。
- 目标库已有不同数据时拒绝导入；v3 同时比较结构投影、依赖、migration、实体摘要和序列下限，v2 使用冻结字段摘要，v1 额外校验推导后的限制语义。
- 导入保留主键、密码哈希、分享 ID、时间字段和所有关系；序列高于最低要求属于安全空洞，不得向下重置或复用已删除 ID。
- 所有 R19 导入报告固定包含 `cutover_authorized=false`；只有后续 R20 切换验收才能授权正式切换。
- 限制预检不会修改数据；它按举报认可、审核拒绝/通过、确认维持和解除限制的完整时序反向检查当前状态，并输出所有相关 `share_ids`，不截断为抽样列表。
- 默认模式会报告待人工分类项；`--strict` 在任一历史私密来源或举报/通过时序歧义未处理时返回失败。
- 对 `legacy_private`，管理员必须在审核中心选择“确认为历史下架”或“确认为作者私密”。前者写入 `confirm_restriction` 并继续 fail-closed，后者写入 `release_restriction`，且两者都保留原 `visibility`。
- 对“举报下架后又出现审核通过”的记录，管理员必须明确“确认维持”或“解除限制”；不能为了通过预检而被迫开放内容。
- 每次人工处理后重新运行严格预检，只有 `blocking_errors` 和 `manual_review` 都为空才允许切换。

## 强制校验

迁移前后至少比较：

- 每类实体行数
- 用户主键、用户名和密码哈希摘要
- Share 主键、`share_id`、作者、可见性、审核状态、活动限制和时间字段摘要
- 点赞、收藏、合集成员和外键关系数量
- 悬空外键和重复唯一键
- 每个用户恰好一条 UserProfile，且全部资料字段（包括历史超长简介）无截断、无隐式改写
- 所有无法映射或被清洗的字段差异
- 已认可举报但没有活动下架限制、已拒绝但没有活动限制、限制元数据不完整等业务不变量
- 历史私密但未完成来源分类、已有证据被更晚事件覆盖却仍保留限制、或限制原因仅含空白字符

任何数量减少、主键冲突、密码哈希变化或未解释的字段截断都视为迁移失败。

`0022_add_share_restrictions` 只允许空开发库安全回退。非空库一律拒绝反向迁移；生产回滚必须恢复迁移前的完整数据库备份，避免删除上线后产生的限制状态和审计语义。

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

- 命令会对备份执行 `PRAGMA integrity_check` 和 `foreign_key_check`，并原子生成数据库、同名 `.sha256` 与 `.metadata.json` 证据集；已有任一输出时默认拒绝覆盖。
- 把三件套移到离线介质后，先在独立证据目录验证文件名、精确校验和、元数据契约、SQLite 文件头及读取期间稳定性：

```powershell
$Database = 'D:\FFXIVShareBackups\site-2026-07-11.sqlite3'
$ExpectedSha256 = (Get-FileHash -LiteralPath $Database -Algorithm SHA256).Hash.ToLowerInvariant()
python ops\migration\Verify-SQLiteBackupSet.py `
  --database $Database `
  --checksum "${Database}.sha256" `
  --metadata "${Database}.metadata.json" `
  --output D:\FFXIVShareEvidence\backup-set-verification.json
python ops\migration\Inspect-SQLiteSnapshot.py `
  --database $Database `
  --expected-sha256 $ExpectedSha256 `
  --output D:\FFXIVShareEvidence\sqlite-snapshot-inspection.json
```

- 三件套验证器不通过 Django 或 SQLite 打开数据库，只证明本次读取到的三份文件彼此自洽，并确认该数据库具备进入独立快照检查的前置条件。元数据中的 `integrity_check=ok` 和 `foreign_key_check=ok` 仍只是备份生产者声明，不能替代随后由 `Inspect-SQLiteSnapshot.py` 执行的只读 `PRAGMA integrity_check`、`foreign_key_check`、表、migration 和序列清点。
- 两份证据都使用新文件发布；备份集报告固定包含 `cutover_authorized=false` 和 `inspection_required=true`。无论哪一份报告都不证明线上来源、停写时点或允许正式切换。输入数据库旁出现 `-wal`、`-shm` 或 `-journal` 时必须拒绝并重新取得一致备份。
- 备份应复制到另一物理存储，并定期在隔离环境中执行恢复、迁移、数据校验和关键流程冒烟。
- PostgreSQL 使用 `requirements-postgres.txt` 和环境变量切换；CI 会在 PostgreSQL 16 上执行同一套迁移与测试。正式备份使用 `pg_dump`，不使用 SQLite 备份命令。
