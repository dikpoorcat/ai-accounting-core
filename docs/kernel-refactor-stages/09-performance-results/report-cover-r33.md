# 精确事实 ID → 主体种类的覆盖索引范围

仅对 `Reports.browser_report_details` 的两项页面计数固定覆盖索引；完整报表计划仍保存全部 `report_fact_ids`，且不把 typed 表或部分已验来源当作所有 ID 的权威种类。入口为 `Dashboard.quarterly_report`，经 service 的 `dashboard_quarterly_report` 可供页面及 CLI/MCP。前端只显示“报表分类／所得税确认”数量；计数不参与金额、准备状态或冻结选择。

| 位置与入口 | 需要的列／证明 | 本轮决定 |
| --- | --- | --- |
| `reports.browser_report_details`；季度页、同命令的 CLI/MCP | 对完整计划中每个精确 ID 查 `fact_revision.subject_id` 与 `subject.kind`，只返回两个 count | 新建 `fact_revision(id,subject_id)`、`subject(id,kind)`，此查询分别 `INDEXED BY`，保留原 inner join／FILTER／缺 ID 语义 |
| `reports._report`、`check_report_readiness`、`report_flow`；公开完整报告、准备、导出、关账 | 需要完整采用 ID、摘要、期间、typed 正文、封签及冲突检查；某些 exact-ID 联接还有 `f.digest/period` | 保留全计划及原证明；不强制新索引、不缩历史 |
| `Store.fact_data_many`、`content_v1.load_v1_fact_data_many` | 先取 `(id,kind)` 分组，再逐 typed 表解码；缺行须拒绝 | 可受新索引自然改善，但未单独量化，不强制查询计划；v1 内容读取规则不改 |
| `Store.facts`、`QueryReads.fact_versions`、详情／资金／员工／资产 | 需要完整 `fact_revision` 行及 typed 子表／证据 | 新索引不能覆盖完整行，原选择与核验保留 |
| `report_flow_v1`、`position_v1`、`report_classification_directory_v1` | 固定历史选择读 digest／period／typed 关系，独立完整重建 | 不改固定 v1 规则，不把当前页面计数快路接入历史读取 |
| `Periods` 关账／预览、`integrity`、`maintenance` 修复 | 全源验证与独立投影比较；通常扫描完整表或取多列 | 不将头部计数代替全量验证；不指定新索引 |
| `backup`／恢复 | 流式保存和核对完整 `subject`、`fact_revision` 行；结构合同须含新索引 | 不缩包内容，不强制索引；新开发合同建新库后验证 |
| `discovery_indexes`、`duplicates`、`display` 等局部 exact-ID join | 有的只取头部，有的需要其他列／来源证明，未测收益 | 原 SQL 不变；不凭同形字符串认定性能问题 |

48 月主样本是 r32 独立完整核验过的合成书；使用 r33 固定源码读，原库只读，`sqlite3.backup` 建独立 3.56 GB 副本。私有索引前后 19,746 个计划 ID 的两个结果始终为 19,729／16。每臂三次交错读：原 PK 双表回查约 256.7k VM、189 MB 进程读、172–188 ms CPU；两张覆盖索引约 296.2k VM、34 MB、47–63 ms。单张索引分别约 105–116 MB、109–141 ms。VM 数增加而表回查及读取字节明显下降，故不能单凭 VM 判负。

副本未运行 `ANALYZE` 时自然计划只用 fact 覆盖；`ANALYZE` 后能自然选两张，但正式读取不依赖统计状态。页面计数 SQL 明确使用两张覆盖索引，其余消费者不强制。私有副本加索引改变了结构合同，整页比较只在副本上临时绕过 **结构合同** 核验；公司身份与原业务内容核验保留。原库与副本完整季度 deferred 响应仅 `generated_at`、`checked_at` 有差异；这不是新合同正式验收。新合同必须建新合成库，不能改旧书指纹或迁移真实业务。

原始件：`.tmp/stage9-report-count-covering-r33.json`、`.tmp/stage9-report-count-index-plan-r33.json`、`.tmp/stage9-report-count-index-plan-analyzed-r33.json`、`.tmp/stage9-report-count-response-parity-r33.json`。旧 `subject_kind` 强制查询负试验见 `docs/kernel-refactor-stages/09-performance-results/report-count-scope-r30.md`；它不等于本轮 id-leading 双覆盖试验。

当前改动已落地，开发合同检查通过，适配器 10 项、结构导出与 bundle 21 项定向通过；待下一固定候选成组回归。当前坏种类测试针对本页真实消费来源；不声称普通计数页核验了计划内所有未消费历史事实的正文。原始诊断和脚本按 SHA 归档于 [清单](report-cover-r33-manifest.json)。
