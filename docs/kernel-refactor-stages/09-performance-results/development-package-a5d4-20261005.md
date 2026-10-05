<!-- @format -->

# a5d4 开发包默认自检：2026-10-05

本次独立开发包默认构建与搬移自检记录为 PASS。SOURCE 为 `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1`，776 文件；source manifest SHA256 为 `a95fce65d9fc6d5381f043d591caf2ab6562f1b08bba1ce24090c833e3639a0b`。默认脚本未跳过自检，`default_validation_invocations=1`、`default_validation_skipped=false`。原件记录 UTC 2026-10-04 19:33:36 至 19:40:46；日期按本地 2026-10-05 归档，不将构建耗时作为性能证据。

| 核对项 | 实际记录 |
| --- | --- |
| build / 实际 packaged calculator build | `local-kernel-2:5c689d17374baad801e4d73778715f9f5c9a71642a990d682ec7dba99575d3e2`，来源、包和实际 calculator 一致 |
| 软件文件 | 4213，verification 的 `verified_files` 与 receipt 一致 |
| CLI | 159，verification 与实际输入计数一致 |
| application 模组 | 171，receipt 的逐模组来源/包/ZIP/搬移 SHA 核对表；不与 fixed-v1 数相加 |
| fixed-v1 模组 | 33，独立历史模组表与合同保留检查 |
| ZIP SHA256 | `42c73e5e6dee558dd71fa60b60692d582e0cbc1f1c07e5af45d5d6079737b394` |
| 包 manifest SHA256 | `bd802ceb8d370151ad3a36366f734eced7700dbd80e547cc06451666cd58cc49` |
| verification SHA256 | `aa83179090503bf60180282315b9ff991f64504dc9d984579b95725501c73424` |

归档时独立重算 ZIP、包 manifest、verification 和 runner SHA，与 receipt 相符；比较 receipt 内完整及 selected 来源前后逐项映射，均相等。原完整 inventory 前后 SHA 都为 `b8fa56f0892b5644a76c3d4f1a956b3af578b6e05645713da973dede0be7c8a8`，source manifest 前后相等。171 模组的四处内容一致及搬移库存检查是已完成 runner 的记录；归档没有再次执行构建、自检、导入软件模组或全源重哈希。

verification 真实为 true 的范围包括：实体登记与原子身份更正、对应发现结果；合成资金渠道、工资确认、关闭期更正、冻结快照保全和晚到资料处置；备份恢复及完整性、后台备份、原生批准关账后的备份与冻结；CLI/MCP 的业务状态/关账准备结果、外部完成复核、失败任务显式重试；HTTP 页面与认证接口、五条看板路由/历史入口、七项认证查询、当前 schema/集合及整数分字符串；相对导入、CMD/PowerShell 启动器与默认包内根、stdio MCP 握手/查询、pythonw 窗口/私有传输、tk 合成登录、Windows 私有 metadata ACL，以及合成凭据撤销清除。完整布尔和合成场景明细保存在脱敏 verification，不扩展为未列能力证明。

这些是默认软件包自检中的合成场景。`native_pythonw_window_and_private_transport`、`tk_form_synthetic_login` 不表示老板真人操作或完整 AI GUI 会计验收；stdio 握手、共享命令结果检查也不表示独立 AI MCP 业务验收。`packaged_offline_upgrade=null`，本次没有 packaged offline upgrade 实绩。目录和公司合同均保持 `draft / 0`；公司指纹 525、目录指纹 b484。没有正式合同冻结、正式交付、真实当前库前向调整、负责人身份替换、实际浏览器 500ms 或纯计时通过结论，不借用此前 9ac 结果。

ce54 首次尝试仍是构建前 cache/inventory 守卫拒绝、自检 0 次的失败，原档案不变。17 模块复制/保留摘要统一组现已在[独立报告](unpublished-copy-and-retention-20261005.md)记录 279 PASS；它与本包自检各有范围，不能累计。main120 大库迁移/内容核验仍在执行，独立 MCP 隔离 host 仅准备好，AI 业务验收未开始，不封存未完成输出。

独立 [证据 manifest](development-package-a5d4-20261005-evidence/manifest.json) SHA256 为 `65d780c852460078d5c43bd910e10a9a372dba1c83323e5a8398d5c9f9733836`。4 件 gzip 保存完整脱敏 receipt、verification、run.log 和包 manifest；记录原件 SHA、脱敏 SHA、gzip SHA、mtime 0，并逐件解压回验。runner 与 ZIP 仅保留私有路径/SHA/大小引用，不复制 helper、ZIP、数据库或凭据。归档只做原件核对，不是独立重跑或数据库复验。
