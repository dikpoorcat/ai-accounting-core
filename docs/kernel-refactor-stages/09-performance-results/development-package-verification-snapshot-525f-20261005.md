# 525f核验快照开发包与小型MCP闭环（2026-10-05）

核验快照修正的新固定来源已通过一次正常默认开发包构建、实际搬移自检和小型非空stdio MCP→HTTP核验／备份／恢复闭环。默认自检实际161次CLI输入；补充桥接最终19次MCP工具调用通过。第一次桥接脚本的jobs容器解析错误、10次实际调用及原始失败回执全部保留，未与最终19次拼成一次成功运行。本记录不继承a5d4资格、不证明500ms或大库收益，不构成正式合同冻结或入口切换。

实现与小型反例范围见[完整核验快照边界修正](verification-snapshot-boundaries-20261005.md)。源自a5d4的[独立完整业务验收](independent-mcp-a5d4-20261005.md)保持原标签，本次没有重跑工资、老板确认和整案280次业务操作，没有重跑249／279项测试、前端或真实资料。

## 来源与默认包

固定来源为`.tmp/stage9-build-source-verification-snapshot-converged-20261005`，777项，SHA `525f02d6718561bb51b22b559b03b3dc6f2aea968eb9a3a13484a532c0cc1d9f`；manifest SHA `23cf3745f887ec981bab163c6aa1594b6dda9b162bf1310c652677f8ac4db7c9`。执行前逐项核对777个文件，以及前组的六个生产文件、新测试与旧迁移测试SHA；33个frozen-v1、139个frontend、33个generated、26个static、4个合同及7个实际页面／关账消费者字节守卫全部一致，分类有重叠。

独占目标为`.tmp/stage9-development-delivery-verification-snapshot-525f-20261005/package`。构建使用仓库`.tmp-kernel-venv/Scripts/python.exe -I -B -X utf8`，私有runner只绑定新来源及新目录，调用固定来源原有`package_local_kernel.py --output ...`，没有skip-validation。真实默认builder生成ZIP、解包搬移并验证完整软件inventory，再以包内隔离runtime运行原有`verify_local_package.py`。未改生产、固定源、builder、selftest或已有原件。

2026-10-04 21:35:00.679852—21:41:45.854094 UTC默认构建及自检exit0，status=passed；所有原有布尔验收项为true。实际161个CLI参数JSON与返回cli_calls一致，数量包含后台任务轮询，不套用旧包159次口径。保留非空业务、完整核验、正常备份任务、普通及关账／更正后恢复、冻结内容不变、真实搬移导入、HTTP页面及当前合同、正常stdio读取、合成凭据清理等默认自检；draft包的`packaged_offline_upgrade=null`，不冒充正式released前向迁移。

所有application modules在固定来源、package、software ZIP及relocated目录的字节一致；33个fixed-v1模块和4个精确合同均实际入包并匹配。fixed来源执行前后全inventory相同，未产生缓存或源码变化。Python3.12.13／SQLite3.53.1／隔离参数有实际回执，导入路径绑定新来源或新包。

| 原件 | SHA256 |
| --- | --- |
| 新包build ID | `local-kernel-2:d2c837b6793375b220a05ca41eaa3afba9f8de7285406af795ca44441a19b326` |
| package manifest | `a634dadaa4d700a8b12cb1c21915150986172c6369448fd22ed31b941bebfd2d` |
| software ZIP | `ff8a8fc40d8ea5ad19c81d78bc8c0e517f6aea9061d3d05fc72dbafb12971f82` |
| 默认验证JSON | `93368da1a21fd3db063402f47237437e9b565c5f6c15ed86e7deb7b2d8e0a251` |
| 构建保全回执 | `968d0eca712fa8c91105aac265b31cb104c6d0ed75f6e91c7127efce629664e2` |
| 构建日志 | `5e5a1871e0701f9887930063992e4b22758864ecaa19dd9e7977a45afdfbcc55` |

## 实际MCP补充范围

只读检查原有selftest：stdio MCP块实际调用overview、business_status、period_readiness、workflow、request_result；完整verify、closed_report、backup及restore主要在CLI→HTTP调用中。它不足以证明这四种动作经stdio MCP传输，因此本次另补一条窄闭环。

新包内`runtime/python.exe -I -B -X utf8`启动私有桥，使用生产LocalService、HTTP server、包内CLI stdio MCP及生产JobRunner。所有业务调用经`finance_local_schema`／`finance_local_command`，没有直接Engine或SQL业务写入；负责人provision／login仅是独占新root的合成host准备，不代表真人密码窗口。package manifest、实际build、各导入模块文件SHA和源manifest均绑定新包／525f。

使用已保全A合成便携ZIP，只读SHA `d8c1b0a96a77185005a27629efafe6dfd1e3be1303913f04d109dbc7ab06893a`；原数据库、材料、旧包和此前host均不修改。最终v2桥的两个不存在目标为：

- `.tmp/stage9-development-delivery-verification-snapshot-525f-20261005/mcp-nonempty-bridge-v2/source-restored/data`
- `.tmp/stage9-development-delivery-verification-snapshot-525f-20261005/mcp-nonempty-bridge-v2/backup-restored/data`

先通过MCP确认空companies，正常restore_company并读取operations、companies、company_context；三身份与原件一致，数据库路径落在第一个新root。实际MCP完整verify为verified，四项coverage全verified、limitations=[]、read_repair_revision=0，counts为45 facts／12 calculations／12 vouchers／1 close／10 evidence。9月整份closed_report与原验收回执逐字段相同，不排除日期、ID、digest或未知差异；这是同draft／0合同恢复后的冻结读取，没有重新关账或用当前结果覆盖历史。

然后通过MCP提交新的backup，先收到pending；生产worker实际执行，MCP jobs确认该精确job succeeded、attempts=1及已验证ZIP。新ZIP SHA `09869f915b0e75a90c7573e73229637671950c0ab3e7eee0a4f583b6ef4acb83`。第二root经MCP空目录确认、restore_company、operations和三身份核对，再次完整verify与整份冻结响应分别与第一root及原回执完全相同。restore生产入口实际验证ZIP，源新ZIP恢复前后SHA相同；不把pending或中间SQLite当成功备份。

最终v2在2026-10-04 21:43:56.255605—21:44:15.728446 UTC完成，19次实际工具调用包含读取与轮询，不是19次业务写入。helper SHA `751745f017780f650cede20c79b0a9188d07e979d24c57b95b69b9d5d0e7b8da`；完整回执SHA `ce4e737f5b46f0fa0a6ac70181866c5002df0394e13938fb82f522de911e4860`。源777文件及原A ZIP再次核对不变。ExitStack正常结束两个worker、HTTP服务、stdio子进程并清理合成会话，进程exit0；静态GetProcess按桥host精确PID查不到进程。

## 原失败与未测边界

第一次桥在独占`mcp-nonempty-bridge`目录真实执行10次工具调用：恢复、三身份、整份冻结、非空完整verify和backup pending均成功，jobs返回真实`{"items": [...]}`。私有helper错误地迭代外层dict，触发`TypeError: string indices must be integers, not 'str'`，不是产品MCP错误。原raw请求／响应、日志、receipt及helper均未改；receipt SHA `fad8e94c87e56102dad2d29412df9dfd7be93bd1540581a854ea59ffa17961ee`。失败进程exit1、服务／worker／stdio正常收尾；第二root尚未创建，不能称首次闭环成功。

只在新helper改为`jobs['items']`，并使用两个全新v2目标，不覆盖失败目录或重跑包构建。两次MCP运行分开保全，不合并通过数。准备脚本的一次PowerShell引号错误未创建helper或打开数据库，随后用独立文件准备v2；不归为产品缺陷。

本次没有真实用户／真人密码窗口、现有服务／5173、业务再登记、工资支付、申报、主120迁移、fixed-v1实际MCP业务、released前向恢复、并发大库核验、响应丢失或500ms验收。默认包、桥接时间有并行负载，全部仅诊断；当前a5的12／48／120资格和纯计时仍属原来源。全部自有负载已于桥结束停止，后续只做文件归档；不修改主阶段文档、路线图或AGENTS，不提交Git。

[公开原件manifest](development-package-verification-snapshot-525f-20261005-evidence/manifest.json) SHA `d662b682ce6d7392a58799ace420e40b1d66e114478640bd517651c8342a70f2`，13份gzip逐份解压比对通过，包括默认包回执／软件manifest／日志、首次桥失败与真实jobs响应、最终两份完整verify，以及脱敏分析和私有原件SHA索引。完整冻结响应只公开SHA和相等断言；不公开数据库、便携ZIP、合成凭据、服务capability、带密码helper源码或业务材料内容。所有私有原件在独占忽略目录原样保留。
