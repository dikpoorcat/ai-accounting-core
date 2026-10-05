<!-- @format -->

# direct-adoption index/migration 与 reportflow 范围组（2026-10-05）

本记录整理清理前已执行的两组代码、定向回执与既有诊断，不运行数据库、测试或性能测量。本组尚未整体 source freeze；清单只记录归档时选定 18 个代码、合同和测试文件的 SHA，并确认归档开始/结束之间相等。这不证明这些字节就是每次测试运行时的全部源码，也不是固定全仓来源或正式阶段完成。

## direct-adoption index 与显式开发迁移

旧 locator 按 `reference_type/reference_id/close_period` 找到引用后筛 direct-adoption path，累计 readiness 引用扩大候选工作。当前 draft 新增 `close_reference_direct_adoption`，key 为 `(reference_type,reference_id,path,close_period,position)`，partial predicate 为两个直接采用路径的 OR。`dashboard_reads._adopted_heads_sql` 的两条 fact/calculation 定位 lane 使用这个索引，仍定位 subject、事实、计算及冻结 highwater；目录命中不是采用权威，消费者仍核验 header、publication 与 close leaf。

调用排查覆盖 adopted head metadata、payroll heads、资产及资金消费者。相对 ed96，`dashboard_reads.py` 仅改变两处 `INDEXED BY`；真实 `reference_types` 前缀枚举及包含损坏类型的行为已经存在，本组保留它们，未新增该机制。测试区分定位与完整核验：两种计划保留相同候选，完整 read-index 检查拒绝两种 path 的损坏；页面只消费 calculation locator 时，不能强迫未消费的 fact locator 损坏成为该页面错误，或以页面成功替代全核验。

迁移保持 `draft/0`，精确来源链为 `923584…→c9f9f7…→52556e…`，第二步只添加该索引。保留 intermediate 合同、原始表值及既有历史，不伪造新安装目标的迁移历史。`offline_development_upgrade` 在 `BEGIN IMMEDIATE` 后重查精确结构和公司身份；root 入口先完成备份，再核对来源与登记身份，阻断同结构公司文件替换及身份竞争。涉及当前 draft、`schema_bundle`、开发升级、合成书准备/rebase 工具和 SQL 消费者。fixed-v1 的已审路径及 spy 测试只证明 verifier、ZIP、repair 不调用当前 head SQL，不涵盖全部 fixed-v1 看板。

最终 `.tmp/direct-adoption-identity-final-20261005.log/.xml` 为 **42 PASS**，实际五模块：direct adoption index、direct adoption development upgrade、direct adoption v1 isolation、development upgrade、development backup rollover。覆盖保存全部原值/历史、索引 DDL/提交前原子回滚、锁内来源重查、显式两步与拒绝捷径、混合来源、无伪造历史、合同包清单，以及公司/数据库/税号身份竞争和备份后换文件阻断。`.tmp/direct-adoption-dummy-sql-20261005.log/.xml` 为另一次 **3 PASS**，仅 payroll head line scope 的内存 SQL fixture，不是额外迁移验收。

早期 targeted 41、step2 repair 7、query repair 5、final 32 与最终 42 有重叠，全部回执保留但不相加。step2 原 XML 为 21 项、5 failures：三项结构/目标合同不一致，两项要求未消费 fact locator 损坏必须使页面抛错的断言没有抛错。后续 repair 和最终回执通过；不能把这五项统一归类为生产缺陷或 fixture 错误，失败原件不改写。另一次 v1 运行因 `No space left on device` 中止，XML 为零字节，不能记为 PASS；后续最终42组才提供新的 v1 isolation 回执。

## reportflow 的命名来源与对象范围

月度冻结 flow 仍逐个核对所有命名分类 header、digest、kind、voucher binding 和期间，并验证冻结内容。相对 ed96，本组 `report_flow.py` 的变化仅为正常输入从 JSON 二元数组改为 ID→expected digest 对象，SQL 用 `json_each` 的 key/value 替代逐项 `json_extract` 和 requested materialization，并保留编码退路。聚合返回一个判定、同月不传无关 voucher 集、跨月按原行路径处理等行为在 ed96 已存在，本组没有新增这些机制。

重复 ID、包含 NUL 的 ID key 或非字符串 digest 回到数组编码；NUL digest 仍是合法字符串，正常走对象 value。非标准 period/ID 类型保留原逐行比较路径，不用对象或 SQLite affinity 改变原比较意义。缺失、digest 错误、跨月/NULL及排序优先级、失败不发布 cache和同一活动 snapshot 的逐月成功缓存均属保留行为。

本组新编码 helper 的生产调用来自普通报表月/季度读取和 `report_projection` 经 `read_report_flow` 的路径。完整核验使用 `require_report_flow→compare_report_flow`，repair 也调用 `compare_report_flow`；这些路径不调用新的 classification header 编码 helper，必须与普通读取分开记录。fixed-v1 flow reader 的分派 spy 保留，不能扩大为全部公共入口或完整 fixed-v1 看板验收。

`.tmp/stage9-flow-object-scope-directed-20261005.log/.xml` 为 **87 PASS**，实际仅 `test_report_flow_classification_scope`、`test_report_flow_header_aggregate`。包括原行决定相等、特殊类型/编码退路、错误优先级、命名来源损坏、新月份缓存隔离、失败不缓存、正式闭月与季度 statements，以及 fixed-v1 分派。

XML 实际保存的工作计数如下，均为隔离测试场景，不是当前主规模整页耗时。前四行是本组 array→object 对照；后四行来自保留的历史原组 `_former_headers_match` 逐行→聚合场景，不是相对 ed96 的本组新增收益：

| 场景 | 旧 → 新 |
| --- | --- |
| 4932 命名 header，十二月 array → object SQL calls | 12 → 12 |
| 同场景返回行 / 返回值字节 | 12 → 12 / 288 → 288 |
| 同场景 SQL 参数字节 | 448920 → 439056 |
| 同场景 SQLite VM | 266763 → 222267 |
| 历史原组：400-header former rows → 单状态返回行 | 400 → 1 |
| 历史原组：同场景返回值字节 | 36000 → 24 |
| 历史原组：同场景 SQL 参数字节 | 6401 → 33209 |
| 历史原组：同场景 VM | 15221 → 18030 |

这些不同来源的比较不能合并成参数或 VM 全面下降的结论。保留的400-header聚合范围场景在移除或增加10000个无关 voucher 时参数均为33209字节、VM为18027；加入12000个无关分类后范围仍保持相同。这证明保留的范围约束，不是本组新增“同月不传 voucher”。另一个4932来源读取场景记录全部 SQL 返回3行、VM221991，成功重复读取复用同 snapshot 缓存；删除命名事实后两次均抛错，未缓存失败。

## 既有诊断与未验证边界

[TEMP shadow 独立记录](adopted-index-shadow-20261005.md) 的来源仍为旧 ed96/771。8933123 行完整 TEMP 表及原索引的三列对照中，资产 VM 为239500/239500/96700；其他四调用为28000/28000/28400、400/400/400、56300/56300/53200、112100/112200/109000，全部 multiset 与原样本相等。100-step量化、TEMP介质/布局及热态不同使单次耗时不能成为稳定因果证据；约2GB是整张TEMP表与全部索引，不是新增索引大小。

[path-first 负实验](adopted-locator-scope-20261005.md) 的五次真实 SQL 对照在主要调用增加 VM 工作，两个拓扑不采用。首次缺 `fact_current` 的 memory DDL失败原件与后续15个 memory-only multiset场景保留；它们没有调用业务检查器，不能代替 migration、backup、fixed-v1 或内容完整性验证。

上述旧诊断 JSON 仅作为来源 SHA 引用，不改写成当前新代码的性能结果。42、3、87三个回执不合并计数；TEMP、内存与原生结果不替代浏览器500ms或新资格。尚缺当前整组固定来源、新鲜完整资格/实际预览及实际主规模浏览器门槛；当前库的大样本正式迁移/备份链也不能由隔离fixture测试自动推定。本次没有读取真实资料、5173服务或数据库，没有修改生产、测试、AGENTS、主阶段、路线图或既有档案。

独立 [manifest.json](adopted-index-and-flow-groups-20261005-evidence/manifest.json) SHA 为 `3c00d924b4fbeb8e714753740fefda711044c17e0a3e5bb062f2ed63994b8d56`。18 件日志/XML保存为脱敏 gzip，包括空 XML及失败；8 件已封诊断/私有 helper 仅引用原始 SHA。清单保存选定源码 SHA、实际 XML模块/结果/计数，以及原件、脱敏、gzip SHA与字节数。根路径、内部ID、PID和地址脱敏，gzip `mtime=0`，全部解压回验相等。
