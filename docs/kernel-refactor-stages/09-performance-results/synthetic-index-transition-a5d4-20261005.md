<!-- @format -->

# main12 / main48 隔离副本声明升级：2026-10-05

两个新的独占合成副本均由真实 `upgrade_company` 完成 c9→525 单步索引升级，状态为 `declared_upgrade_verified`。没有改写指纹、简化内容核验、替换原样本或运行服务。复制与升级采用两个不同的固定来源：复制 helper 绑定 ed96、严格检查已有 c9 合同；升级 helper 绑定 a5d4，并核对目标精确 525 合同。它们不是同一来源的一次资格或性能测试。

| 来源 | SOURCE SHA256 | source manifest SHA256 | 文件 |
| --- | --- | --- | --- |
| v2 复制 / 旧 c9 | `ed96dfd296cd96d4279745eb0ac26a9422db450c10d3bb9f8e7def11e2f9d8c8` | `58067bd14dcb1374b944cd891842c69ad4385ca020f37437464d39f7907a63cf` | 771 |
| 实际声明升级 / 525 | `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1` | `a95fce65d9fc6d5381f043d591caf2ab6562f1b08bba1ce24090c833e3639a0b` | 776 |

main12 使用已完成的 v2 copy receipt；main48 先读取指定原样本主文件元数据，预算按源公司与目录两倍大小加 2 GiB，共 `9306505216` bytes，当时可用 `191254859776` bytes，满足检查后才复制。v2 只写不存在的专用目标，目标先 DELETE，再 SQLite backup，恢复 WAL 并严格 TRUNCATE。main48 新副本 main 为 `3579371520` bytes，复制后实际 WAL 为 0。复制回执仍保留 `copied_content_unverified`，不能提前替代后续业务核验。

两次复制原件记录原样本 bytes、identity、state、history、sessions 及 ed96 完整源码清单前后相等。复制的目录登记只更新到专用副本路径，负责人及 session 原值保留。升级 helper 排它新建，不覆盖旧 helper/output/log；使用仓库 venv、`-B -X utf8`，核对 copy receipt/input SHA、副本 main SHA/size、路径位于指定 `.tmp` 根、无 reparse 路径、三个实际身份字段、只读 c9 结构与空 draft ancestry。随后核对 a5d4 完整清单和实际 import 来源，再执行真实迁移。观察器仅包围原内容验证及全表摘要函数，调用、返回及事务逻辑均保留。

| 实际结果 | main12 | main48 |
| --- | --- | --- |
| 三次内容核验 | 四项 scope 全部 verified，limitations `[]` | 四项 scope 全部 verified，limitations `[]` |
| facts / calculations / vouchers | 30225 / 12413 / 12309 | 121593 / 50519 / 49245 |
| closes / evidence | 11 / 85 | 47 / 337 |
| 原表摘要 | 247 表，首段原摘要复用、后摘要比较保留 | 247 表，首段原摘要复用、后摘要比较保留 |
| source 清单 / state / schema_history | 相等 / 不变 / 不变 | 相等 / 不变 / 不变 |
| 外键 / FK check / 事务 | 启用 / 空 / 已结束 | 启用 / 空 / 已结束 |
| checkpoint | `[0,0,0]` | `[0,0,0]` |
| 升级前公司 main bytes | 590266368 | 3579371520 |
| 升级后公司 main bytes | 592625664 | 3589439488 |
| 升级后 main SHA256 | `17abfb2fe41627bb047dd4dabe60390a8af81291812ac936dda08b423565fb6a` | `2c3ee01f6573a283498fdc32ac272a97562412526e1403463969ed8699cc7689` |

四项核验 scope 是 sources、historical_adoption、projections、read_indexes，三个相位各自保留真实 coverage/counts。身份通过只读前置 identity 行、复制目录登记以及实际 `expected_company_id/database_id/taxpayer_id` 严格绑定，未补造 `identity_unchanged` 字段。完整原表摘要和 retained-history digest 保全业务、审计与冻结依据；原 `schema_history` 不改写，增加一个真实 c9→525 draft 迁移回执。最终 `verify_schema` 与实际对象 fingerprint 都严格匹配 `52556e8ec6cbbb81ab9ad9a69bb87897401471dba9c693e7b7ebddd2a5b08bf4`。“当前精确合同”仅指这项结构检查，不表示新增业务精度专项。

| 诊断相位秒数 | main12 | main48 |
| --- | --- | --- |
| 锁前内容 / 锁内内容 / 目标内容 | 96.648 / 61.415 / 70.735 | 1063.415 / 586.458 / 632.793 |
| 原表前摘要 / 后摘要 | 11.015 / 11.728 | 72.305 / 74.664 |

main120 ce54 核验并行执行；这些 wall 相位不是纯计时、可复现因果比较、前台影响或资源绝对峰。helper 没有运行 root 级升级、备份恢复、浏览器、新完整 qualification、活动 preview_close 或 500ms。main12 的新浏览器 runner 在本任务时仅准备好，SHA 私有引用不表示执行；未继承旧 Q 或活动预览，不累计此前 279 / 159 等结果。没有访问真实资料/5173 或打开、修改正在运行的 main120 数据库与 timing runner。本组保持 `draft / 0`，不表示正式冻结或交付。

原件命名保持 `stage9-index-transition-main12-a5d4-upgrade-20261005` 与 `stage9-index-transition-main48-a5d4-upgrade-20261005`。结果 JSON SHA256 分别为 `89a58f4123124a62fafcfcbf098d0fbd5c99b1a95de9a249a3a07b9a7422666f`、`7aa7f1177fbfb986c6322d9741fd2053aac36efc424bb3b259528c3cbf181b67`；对应 log SHA 为 `0cf1096154ad725810d341e51a0a0d5f814173af5aef1cf707e69faad1d8848d`、`a675b4cd655030df972b9ffb6631ec40171ca7451c9db33cc17a673aad38f930`。

独立 [证据 manifest](synthetic-index-transition-a5d4-20261005-evidence/manifest.json) SHA256 为 `e2d03353c24656c7180d7c95304d25d45b38f98d1fb51ff740bbfecf42b59653`。10 件脱敏 gzip 保存两个规模的 copy/upgrade JSON、log 及两份 source manifest；7 件私有 SHA 引用保存 copy/source-guard/upgrade helpers、合成 input 和仅准备的 main12 browser runner。原件不覆写，私人 before/after 守卫用规范 JSON SHA 表示，路径/身份/PID/地址脱敏；每件记录原件、脱敏和 gzip SHA，mtime 0，并验证解压字节相等。数据库 SHA/size 来自完成回执，归档不再次读大库；不复制数据库、ZIP、密码或凭据。原有档案和主阶段/路线图/AGENTS 未修改。
