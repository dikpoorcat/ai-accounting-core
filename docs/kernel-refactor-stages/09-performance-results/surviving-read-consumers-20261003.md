# 第9阶段：保留读取消费者 E／F 组

2026-10-03。本记录汇集四类必要业务读取之后的 resident 中间守卫与金额消费清理结果；已执行的定向及166项统一回归见下文。本文编写仅只读核对已有源码与回执，没有另行重跑测试或启动运行任务。开发库仍为 draft／0，现有5173、资料根与身份原样；第9阶段仍未完成。本组不继承 fa5c 的资格、浏览器或开发包结果。

当前E／F代码已固定为 `.tmp/stage9-build-source-surviving-read-consumers-fixed-20261003`，753文件，源码SHA `dd8601e985cb6bed14f9bc0746bb5b387a20f9355ecd69e1e5ebae729b746a96`，实际manifest SHA `7c9590d5990cb2a63fa940d3d999fa13606bf1d98424637d3d34aac77126eb38`。该源fresh main48资格已complete，四coverage verified且limitations=[]；完整五页150样本FAIL50，新开发包产品自检通过。2026-10-04核对结果如下。独立MCP仅部分范围通过，另发现输入格式缺陷正在修复；当前主仓库后续改动不是已验性能固定源，dd86也不代表最终代码。

## E：同一次 warm 借用的中间主文件守卫

原路径为 `pool._signature → public validate_reused_read_connection → pool._signature`。当前 pool 仅在本次前置路径、ACL、stat 及 idle 文件身份比较通过后调用 `runtime._validate_reused_read_connection_at_private_path`；公共 validator 仍执行完整路径／ACL，再调用同一内部 continuation。后置 `_signature` 位于 SQLite 设置、结构及身份核验之后、交付业务读取之前。没有客户端跳过参数或跨请求 ACL proof。

定向测试 `test_warm_borrow_merges_only_middle_main_guard` 恢复原公共中间路由作为 control，在当前及 `historical_content(1)` 上下文执行相同真实 SQLite 校验和业务 SELECT。包装调用原 Windows `_assert_private_handle`，真实 GetSecurityInfo／ACE 校验继续运行；合成 WAL、SHM 均存在。

| 同次 warm 借用工作 | 原路由 | 当前 continuation | 保留范围 |
| --- | ---: | ---: | --- |
| main local-path | 3 | 2 | 借用前后 |
| OS 私有 ACL 总次数 | 5 | 4 | main 两次、WAL／SHM 各一次 |
| main ACL | 3 | 2 | 借用前后 |
| WAL／SHM ACL | 各1 | 各1 | 每个实际 sidecar 独立核验 |
| trace SQL 序列 | 相同 | 相同 | actual sqlite_schema SQL 指纹和身份核验保留 |

DBCONFIG、attached limit、五项 PRAGMA、cache_size、schema_meta／history、公司／税号／数据库身份、sidecar `tighten=False`、异常 discard、rollback 及 authorizer／trace／progress 清理保留。完整结构指纹未改为 schema_version cookie，业务核验范围未裁。工作次数变化不等于已经证明 CPU、TTFB 或整页毫秒收益。

首轮 scope 记录为18 PASS／1 FAIL：新增 standalone 断言写成 `schema_mismatch`，实际抛出 `schema_fingerprint_mismatch`。修正精确错误码并补 authorizer 清理后，两文件20 PASS／29.23s；独立 runtime／sidecar 选择8 PASS、43 deselected／5.57s。这些数字由保存的 scope 文本记录，未独立归档其原始 stdout；不是最新166项之外的新增通过数。

## F：金额消费者撤去未消费容器

`dashboard_funds.FundsRead._verified_money_summary` 继续消费已证明 money effects；仅 amount-only 路径不组装 movement_count／last_activity_date。普通收付款不再建立无消费者的逐 event 字典及 account set；内部 transfer 判定所需容器仍保留。完整资金页面继续提供次数、最新日期及账户详情，固定v1继续走完整读取。amount-only 还要求当前拥有的 snapshot、事务、当前 close reader、非v1、没有 identity_correction；不把关闭态或历史读取变为简略默认。

全量金额、三类通道、transfer、零账户、撤销符号、未知、真实来源、已有 effect proof 及 SQL fallback 保留。可能中间溢出或 UTF-8 编码不安全时继续原 SQL 消费及错误语义。测试对照 amount-only、完整消费与强制 SQL fallback 的财务金额一致；无逐 event 容器测试只隔离已证明 effects 的消费，source list 在测量之外，不证明来源工作减少或进程 RSS 改善。

定向原始 XML／log 为7 PASS，pytest12.40s（XML suite11.881s）。范围为 `test_verified_money_summary.py` 的7项：三通道／转账／零账户，大安全整数两种边界，proof与owned snapshot，算术与编码 fallback，空日期与撤销符号，以及12／120／1200收款 effects 的容器增长边界。

## 统一业务保护回执与入口排查

最新统一 runner 使用仓库虚拟环境、9个完整测试文件和11个精确 node：**166 PASS／4个 record_property 警告，pytest306.90s，runner315.2938566207886s，exit_code=0、files_unchanged=true**。XML 为166 tests、0 failures、0 errors、0 skipped。runner 保存19个相关生产／测试文件的运行前SHA，运行后逐项核对不变。20、8、7项与此组存在重叠，不相加；统一回归只证明所选业务保护范围。

| 入口／保护 | 状态与具体证据 | 未覆盖边界 |
| --- | --- | --- |
| 五页 context、summary、默认明细、过滤、分页 | resident pool／projection／transport 合成回归 verified；static 路由保持每次前后守卫 | dd86主48五页及冷态／首次已记录；切公司unavailable，按需及文件计时未验 |
| 目录、CLI／MCP 独立 connect／bind | static：公共完整路径、ACL、结构、身份仍保留；runtime 合成回归 verified | 没有本组逐 CLI／MCP 回放或目录大回归 |
| 完整资金与v1 | summary金额／SQL fallback、历史零账户、fixed_v1完整读取节点 verified | 没有本组新冻结v1包或完整历史内容资格 |
| 报表与完整核验 | 报表 authoritative source 边界、闭月精确 preview export、source损坏拒绝节点 verified | 不是当前主规模完整 integrity 或新 qualification |
| close／close preview | 不可变精确 summary／无caller alias节点 verified；写入和race guard static保留 | 不是整套关账回归或真实写入 |
| repair | source损坏不可修饰节点 verified；修复后 verify_schema 与引用核验 static保留 | 未执行实际维修 |
| backup／recover | committed WAL在线备份、便携 round trip／rollover节点 verified；源与目标独立守卫 static保留 | 没有本组大规模mixed备份／恢复或真实资料操作 |
| 非空历史升级 | v1冻结采用不被v2当前模型覆盖节点 verified | 不是新包升级／回滚完整验收 |

源码定位：`runtime.py` 公共 wrapper／private continuation；`resident_reads.py` 同次 `_signature` 前后；`dashboard_funds.py` amounts_only gating／verified consumer；`test_resident_read_guard_work.py` 与 `test_verified_money_summary.py`。完整运行命令和精确node以归档 runner／JSON／XML 为准，不把静态审阅写成执行通过。

## 独立诊断与证据限制

四个已完成、任务自有隔离 MCP host 的退休原始JSON记录：4 hosts、8 children，正常独占stop marker清理后 remaining=0；无forcekill、无删文件／库、无修改包／源、无触及真实服务。它是已完成任务的生命周期清理，不能说明 CPU 收益；本记录没有再次操作这些进程。含进程身份的原件不纳入源码归档，仅在manifest记录源SHA。

fa5c report flow reference overlap 原探针误用年份 ordinal，选到开放／未来月，rows=[]、0／0；原件保留，不能据此排除重叠。fix1 选定12个月，每月411引用，raw_header_sum=4932、unique_references=4932，月间 seen=0。它只是 readonly metadata overlap diagnostic，`semantic_authentication=false`，不是语义核验、性能验收或所有调用路径不重叠的证明。

strict line validator 是隔离候选脚本，未合入生产。probe比较旧 `_lines` 与严格 list／dict／str／int候选，并在v1／非plain subclass／错误路径 fallback；final把失败 fallback 移出try，避免 identifier重复coercion，另加两个计数identifier错误对照。既有智能体声明348个合成精确案例加2个identifier案例，线程cycles诊断约9%–22%下降。当前只有probe／cycles／final脚本，没有持久 stdout 原始回执；这些数字仅为声明，未独立验证，不编造JSON／log，也不写生产优化已实现或CPU性能通过。

最新既有 fa5c 五页仍为旧代码 **FAIL58／150成功／0错误**；它早于 E／F，不能冒充当前结果。dd86主48资格complete／四coverage verified、limitations=[]，五页FAIL50／150成功／0错误；开发包产品自检一次通过，独立MCP部分范围通过。其余六样本、当前证据规模及正式冻结／包／运行切换边界见阶段主文档。

两份后续只读审查原文见[独立审查归档](surviving-read-consumers-20261003-audit-raw/manifest.json)。报表来源锚定审查确认同快照贡献与绑定已分批去重：贡献1004、锚定来源1005；不能把两次调用当作重复读取整组。锚定函数119.354ms self time包含游标stepping／行转换等可能成本，直接SHA创建、digest及UTF-8编码合计约4.097ms，不能承诺可收回119ms；raw-cache overlap缺少当前实际命中证据，未实施来源裁减或性能修复。共享守卫审查只确认draft公司同一核验内schema_history的一行记录SQL重复这一小候选；完整fingerprint并未重复多次。该候选未实现，收益未证明，auth／catalog不同事务、sidecar与前后对象边界继续保留。两份审查没有新测试、数据库、profile或浏览器计时，不改变上表的验证状态；strict line CPU候选也仍未进入生产。

## dd86主48资格、五页与开发包（2026-10-04核对）

[本轮8份原件manifest](surviving-read-consumers-dd86-main48-package-20261004-raw/manifest.json)独立于旧12项、2审查及fa5c归档。含benchmark PID的pure-control／run.log原件不归档，仅保存原始路径与SHA；下文保留它们的实际结论，不改写原字节。qualification complete：sources／historical_adoption／projections／read_indexes均verified，limitations=[]；facts121593、calculations50519、vouchers49245、closes47、evidence337。integrity1102540.71ms、preview649362.82ms、qualification总1754361.94ms仅为wall time。

五页各预热3次、默认20条、30次纯样本。原始150个数值均完整且有限；300条ResourceTiming响应均200，HTTP／dispatch诊断数组为空。结果over_target：50次≥500ms，150成功、0错误。每格为median／p95／max毫秒，最后列保留全部慢样本计数。

| 页面 | fa5c | dd86 | fa5c→dd86 ≥500ms |
| --- | --- | --- | --- |
| brief | 517.10／575.80／633.30 | 515.50／557.20／615.20 | 28→19 |
| funds | 415.90／476.50／494.40 | 395.70／435.70／590.60 | 0→1 |
| employees | 354.70／395.90／412.40 | 336.60／414.50／496.10 | 0→0 |
| assets | 415.20／474.60／474.80 | 413.60／455.80／472.40 | 0→0 |
| reports | 576.20／675.70／694.70 | 575.60／694.50／756.50 | 30→30 |
| 全部 | 150成功／0错误／FAIL58 | 150成功／0错误／FAIL50 | 58→50 |

这是各一次、不同源码和执行条件的观察，不能据58→50声称E／F导致稳定收益；report尾部更慢、funds新出现1个慢样本均保留。dd86冷态brief／funds／employees／assets／reports为676.20／980.20／1002.60／1498.00／972.50ms，首次1798.00ms；浏览器启动至首次渲染3868.51ms单列，切公司unavailable（single_eligible_company）。

本轮pure controller先核对旧四host已正常退休，新MCP也在纯窗前正常退出；targets={}、process_mutations=0，纯窗113.3184s。没有暂停／resume动作或targets CPU前后比较，不能继承fa5c的CPU不变证明。runner exit1对应over_target，elapsed2032.96298289299s含资格与总过程；book_unchanged、manifest_unchanged均true。

新开发包搬移产品自检status=passed：160实际CLI调用、33个独立v1模块、4212软件文件、3份合同。只是一次构建／搬移产品自检，不是正式包交付。753项固定source选定清单与manifest不变；full_source_inventory_unchanged=false，派生cache目录造成全目录inventory不同，不能写成所有文件不变。大receipt及全源码清单不归档，源SHA见新manifest；verified软件文件数与source753是不同口径。

独立GPT实际MCP receipt SHA `d30feb879efa35950da1093aff33c37fe5138abe6ca0c124151de81f0538d121`：67次agent调用加12次host preflight，status=partial_actual_mcp_acceptance。已覆盖A／B精确范围回答及实际发布、响应丢失后request_result／原payload与request_id精确重放、过期preview拒绝与刷新、跨公司旧payload拒绝、同root／package中断接续、backup pending到verified ZIP、失败3次有界停止后显式同job重试成功、两公司integrity verified无limitations。正常4个进程退出、DP凭据移除由私有receipt记录；不归档私人relay工具、凭据或进程身份。

成功单月preview／native批准／close与冻结读、人的密码窗口、restore、external completion、实际payroll发布仍未验，不借产品自检结果替代独立AI覆盖。错误调用提交一字符evidence_digest，实际返回internal_error且无entity写入；不是PASS。输入格式分类修复组执行中，HTTP pipeline诊断亦另行执行；尚无其最终结果，不能把dd86 qualification／五页／包自检向这些后续源码传播。

## 小型原件归档

原始12文件按字节gzip（mtime=0），逐项解压回环与SHA-256核对；路径、大小、源／压缩SHA见[manifest](surviving-read-consumers-20261003-raw/manifest.json)。归档仅scope、所选runner／测试回执、overlap原件及候选脚本，拒绝已有目标，不删除任何原件。排除数据库、WAL／SHM、实际PID／凭据文件、ZIP、全源码清单、pstats和真实业务资料；压缩总量上限512KiB、原始总量上限5MiB。没有提交Git或标记阶段完成。
