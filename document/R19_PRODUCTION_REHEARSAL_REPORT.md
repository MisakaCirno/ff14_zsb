# R19 生产副本迁移演练报告

状态：已完成

完成日期：2026-07-19

范围：离线生产副本迁移演练，不包含正式停写、最终迁移或线上切换

## 结论

使用具有来源、版本和摘要证明的线上 SQLite 不可变捕获完成了 Proposal、人工 Review、Approval、两次全新 RunRoot 的独立离线演练和冻结 verifier 的双轮复核。两轮均以 0 退出，原始源数据库保持不变，实体、媒体、migration、数据库结构和最终目标数据库业务语义一致，未解释差异为 0。

R19 已满足完成门禁。所有结果继续固定 `cutover_authorized=false`；本报告不授权 R20 的停写、迁移、服务切换或发布。

## 权威输入

- 生产应用版本：`244c32734e9fab5af05bf544a654615eeab31404`
- 捕获 ZIP SHA-256：`4e3dfa32c43fd745b0ef1d9a0c0c4d442c44f19b59e0e1ab9ca0b35055ac0f8e`
- 源数据库 SHA-256：`b834cf3fdf53a03f289b64409c89f39d51ba62b445801912c11f544582481484`
- 捕获 preflight SHA-256：`6d03d501191d8b14725a865ded96a7dd08b21eb333483e578060cff51cec05b3`
- 捕获 final SHA-256：`105ff788eef8fdfd96bc23abc8ec30218037b4148100dddb011515b381ac4d29`
- Handoff SHA-256：`fe5f1779771a887fed6303f8701b255942b6b0f4e7bcc392788a18d57e3c0b31`
- 媒体快照：部署目录没有媒体目录，模型也没有上传字段；已按 0 文件、0 字节生成并验证空媒体快照及两个独立副本。

捕获期间源数据库未被修改。原始捕获、数据库三件套、handoff、媒体 manifest、审批材料、两轮 RunRoot 和双轮报告保留在私有 NTFS 范围，不进入 Git。

## 审批绑定

- Proposal：`r19-production-20260719-p04`，SHA-256 `52560282cbafb5b0907a2d60841d72b7f5bc82b201740451c9652a777d877fc6`
- Review：`r19-production-20260719-p04-review-01`，SHA-256 `87741e619d0d504a75280596cabc086a709677dc787cd9c9f6ab0d0a721d255b`
- Policy：`r19-production-20260719-p04-policy`，SHA-256 `1ee7fcedd4b7c853ae514c03b97aa783475145d5ca2da4830df53f45001fc52b`
- 源迁移叶节点：`shares/0018_default_home_feed_waterfall`
- 目标迁移叶节点：`shares/0028_normalize_announcement_column_order`

人工审阅确认四条历史 `legacy_private` 记录的既有私密可见性全部保留：一条确认为历史下架，三条确认为作者私密并仅解除迁移保护。迁移不公开这些内容，也不修改其正文、标题、作者或分享 ID。

## 双次演练

| 项目 | 第一轮 `r04` | 第二轮 `r05` |
| --- | --- | --- |
| 退出码 | 0 | 0 |
| 结果 | `completed` | `completed` |
| 问题数 | 0 | 0 |
| 结构问题数 | 0 | 0 |
| 限制人工复核数 | 0 | 0 |
| 事件数 | 39 | 39 |
| Ledger head | `6fcbfd13193cf92d296ed44c163faf99242bb1b2d0d54570570006544dda6b2f` | `d513f74ef331b4322c4d5bcf90f8e1fdf61048a738417009e93a0b630e9e8fa5` |
| Result SHA-256 | `fa73bf3a86e1f1b50e133c090b081402f3ab0e496c5b34a9c3c9c625993a47b7` | `d15a78c28f819f037f0598c2f69146e389712fd8163b778c28a14037ba9d53d7` |

两轮最终目标均包含 361 个用户、482 条分享、19 个合集、95 条合集项目、4 条举报、598 条分享日志和 11 条站点动态，共 1967 条记录。媒体文件为 0，符合已批准的生产捕获事实。

## 双轮验证与保留

- Pair verifier 状态：`verified`
- Pair 报告 SHA-256：`26baadbd30c147987ad6b4b0c190c8963b2694bc1ccca374a13104d357169383`
- 业务语义投影 SHA-256：`e74532dcabcd65198b984de29f3e6174a236620e149920916002e223643463ed`
- 实体清单 SHA-256：`578131c2464d43b6e0d5567c58f6e787ecff3cf2731d2c7c01a826e7a5f9acbc`
- 媒体清单 SHA-256：`37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570`
- migration 摘要 SHA-256：`638544f8d8ab6eef11b226c44eba918bc0db3e1bb95495068d2506e30f3fdafb`
- 数据库结构保真 SHA-256：`ad64cedf218d58ac80863f3f923790b51ffb3ffae3a2d39b49b344ed18e4527c`
- 未解释差异：0
- 线上 handoff 内容复核：通过
- handoff 权限基线复核：通过

双轮报告属于 `self_consistent_local_chain`，不是数字签名或不可篡改存储证明。报告和所引用的生产数据证据需按敏感数据制度保留并最终安全销毁。

2026-07-19T13:46:27Z 完成终态后的本机进程检查，没有发现命令行仍引用 `r04` 或 `r05` RunRoot 的遗留进程。

## 后续边界

下一阶段是 R20 的准备工作：只读审查真实服务拓扑和 Nginx 配置，建立 go/no-go 运行单，演练停写、最终备份、旁路迁移、服务切换和保留新写入的回滚方案。只有得到新的明确上线授权，才能在维护窗口操作线上服务或数据。
