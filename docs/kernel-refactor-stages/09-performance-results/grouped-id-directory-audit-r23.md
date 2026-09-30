# 第 9 阶段 A 类：小 ID 集与认证目录连接的全局审查

范围：固定 r23 源、main48 原 DB 的只读 SQL/计划与既有 r21/r23 六入口插桩；本轮只改 `.tmp` 审计和探针，不改生产、测试、封存源码或公司库。原始计数见 `.tmp/stage9-grouped-main48-r23-all-pages.json`，查询计划及目录基数见 `.tmp/stage9-id-directory-plan-r23-main48.json`。探针时 r23/main48 完整核验报告仍为 `measuring`，因此不是发布验收；同时有全后端回归，不使用 wall 作纯时延。r23 本库 `close_reference` 1,516,979 行，`audit_reference` 119,753 行，`job_reference` 0 行；每个近末闭月 `vouchers[*].id` 约 1,005 行。

先前遗漏的原因明确：第一轮只按 `QueryReads.verify_selected_voucher_adoptions` 的函数名追踪，虽然 r21 SQL 原始计数已呈现同形资产大查询，SQL 计量器将调用阶段记为 `unattributed`，没有区分另一个执行点 `dashboard_reads.Journal.hydrate`。后者被 `Journal.__iter__` 的每 100 行批次及 `Journal.page` 的默认/按需明细调用，资产页碰到 47 个冻结凭证后触发同一个低选择度 join。今后先全局按 **SQL 形态和认证目录表** 枚举，再按调用者归属，不能把修好一个 `QueryReads` 方法误报为修完所有同形查询。

| 位置与入口 | 精确查询/认证职责 | r23/main48 计划和工作量 | 处理决定 |
| --- | --- | --- | --- |
| `dashboard_reads.Journal.hydrate`；五页凭证行、默认 100 与按需页，CLI/MCP 共用 Dashboard | 小冻结 voucher ID 集取所有 `close_period<=cutoff` 的 `r.*`，逐条 `verify_close_references`；原实现**不另做缺行判定**。 | 资产 1 次仅 47 行／8KB，9.305M VM；计划先扫 `close_reference_lookup(reference_type=?)`，后扫 JSON ID。 | **确认大扫描，下一轮窄修**：候选 ID 驱动 `(reference_type,reference_id,close_period)`，保留所有后续同 ID 引用、多重集、所有字段和现有 verifier。不可暗增/减缺行推断。 |
| `QueryReads.verify_selected_voucher_adoptions`；报告/资产及其它精确来源读 | 同一截止范围，但额外核对每个 `(voucher_id,其原 close_period)` 必须命中，再执行同事务 verifier。 | 已在 r23 固定源码用 JSON ID 外循环、复合索引；私有 47 ID 9.305M→1,000 VM、逐行集合相同。 | **已修，保留**。可以抽只负责选行的公共小函数，但两处各自的缺行语义和认证调用保持独立；不能把 `QueryReads` 的成功缓存传跨请求或跨 worker。 |
 | `BusinessQueries._selected_accounting`；资金/员工/资产、业务状态与详情 | 候选 `(voucher_id,close_period)` 精确 path=`vouchers[*].id`，故意不先过滤 `reference_type`，这样被篡改成错 type 的同 path 行仍进入 verifier 并拒绝。 | 资产 3 次共约 1.32M VM、234 行；`SCAN ids` 后按主键 `(close_period,path)` 搜索，每个候选会扫描该期 path 约 1,005 行。 | **确有重复扫描，不属于必要证明字节**；下方无 DDL 私有半连接探针显著减少 VM，且保留错 type。待回归解冻再考虑窄改；未改生产。 |
| `dashboard_funds.FundsRead.events`；资金页/相关明细 | 已选 journal 派生行连 `close_reference` 精确 voucher ID 与 close_period，逐条 verifier。 | r23 资金 2 次 51.8k/24.2k VM，均 0 返回；目前非长尾。 | 保持。若后续负载命中大集合再检查 join 顺序，不能把 0 行解释为不需要核验。 |
| `Reports._report` 与 `_report_vouchers`；报表页、准备、关账 | 本月/本季所选凭证 `IN(json_each(ids))` + type + cutoff，再逐条 verifier；税确认也按精确 calculation ID。 | r23 多页该形态每次约 19.8k VM／0 行；计划使用复合 `(reference_type,reference_id,close_period)`，不是全 type 扫描。 | 保持。closed/open 报表选择与期末差异仍分别核对。read_impl 负责报告事实范围，A 类不覆盖其语义筛选。 |
| `report_projection._authoritative_rows`、`dashboard_reads.Journal._sql`、`query_reads.selected_voucher_sql`；开放/冻结混合选择 | 按 voucher version、期间查有无冻结引用或最早采用期；找不到时仍有后续权威 close/出版身份检查。 | 现用 voucher/period 或 calculation subject 候选驱动 `close_reference_lookup`，或相关子查询按 full key；未见高 VM 的目录扫描。 | 保持；不能仅凭可修逆索引缺行认定没有冻结采用。 |
| `read_indexes.close_rows`；精确 period/fact/calculation/subject 候选，供普通读、关账与核验 | subject 分支已显式 `json_each(subject)→calculation_subject→close_reference_lookup`；fact/calculation ID 分支 `IN` 命中对应期，再对读出的 close 执行完整目录多重集与封签核对。 | 主样本无该分支独立大 VM；源码已有防止先扫低选择度 type 的注释/驱动顺序。 | 保持，缺行不可作阴性证明；小 ID 分支如有新 workload 再独立量。 |
| `dashboard_metadata.Management._query` / `FrozenManagementIds`；员工/资产管理展示与按需身份 | 特定闭月 `path` 的管理引用与实体记录连结，`__contains__` 还限定单 ID 并验叶；`__iter__` 必须取得该 path 全部成员。 | main48 本组未给出独立高 VM；不是同形 `json_each` 小 ID 反向扫全 type。 | 保持，真实非空闭月管理详情工作量尚未验证。 |
| `report_flow` 与固定 `report_flow_v1`；报表分类信号 | 先按真实 typed fact/期间产生候选，再 `EXISTS close_reference_lookup(fact,id,cutoff,path)`；v1 同形但独立历史规则。 | 候选优先且复合索引，A 类未见大 VM；其 typed missing 分支另由 read_impl 审查。 | 保持。历史 v1 不自动换用当前规则。 |
| 固定 `position_v1._position_classification_ids` / `report_projection_v1`；非空正式 v1 读取/核验 | 前者按闭期 path 选历史事实，后者 voucher ID 相关 `min(close_period)` 用复合索引，均须保留正式历史身份与损坏拒绝。 | 前者是历史集合读取，不是小 JSON 集低选择度 join；旧 48/120 增长需单独归类。 | 保持 v1 自有规则，不把当前 `Journal` helper 注入历史核验。 |
| `read_indexes.verify_sources('close')`、`verify_read_indexes`；完整核验、关账权威检查、修复、备份/恢复 | 按指定 close_period 将 `close_reference` **全部发生项**与已认证 close 根逐项、多重集、顺序比较；完整核验还核 source marker、孤儿和存储。维修前独立重建权威，维修后复验。 | `WHERE close_period IN(json_each)` 用 `(close_period,path,position,type)` 主键；完整多重集总行随历史增长是合同所需，非仅小 ID 命中。 | **必要全量，不能缩到页命中**。备份源、ZIP 验证和恢复目标各自调用独立完整核验，亦不共享请求证明。 |
| `read_indexes.audit_rows`、`provenance`；五页来源说明/按需详情、CLI/MCP | `(source_type,source_id)` 小集取 audit_id，随后对所选 audit 源做 marker/完整引用多重集验证。 | main48 目录 119,753 行；EXPLAIN 为 JSON 候选驱动 covering `audit_reference_lookup(source_type,source_id)`，未见全 type 扫。 | 保持；来源记录随页范围读取属于必要认证。 |
| `read_indexes.job_rows`、`BusinessQueries._file_jobs`、worklist；文件任务/按需详情、CLI/MCP | subject/period 候选的 job_reference union，返回 job 后 `verify_sources('job')` 对该 job 全引用比较。 | main48 `job_reference=0`，精确 subject 分支计划能用 `job_reference_lookup(type,id)`，其余 union 在此样本无性能结论。 | 保持，非空 jobs 增长工作量未验证；不凭空加事务缓存或分页跳过完整 job 来源。 |

CLI、MCP 与 HTTP 的业务入口调用同一 Dashboard/BusinessQueries/Periods/Maintenance；没有第二套小 ID→`close_reference` SQL 可单独删掉。正式关账与预览会额外进行关闭期权威与冻结比较，完整核验会走全部来源；这和普通页精确命中认证是不同责任。`create_portable`/`verify_portable`/`restore_portable` 经 `schema_bundle.verify_company_with_registry→integrity.verify_integrity` 完整核验，还核 ZIP 成员与字节；不能借普通页命中缓存削减。维修 `Maintenance._repair` 先 `verify_integrity(... include_indexes=False)`，再按目标修复目录/投影，后 `verify_integrity` 复验；`read_indexes.repair_read_indexes` 的全来源重建与完整目录多重集检查为必需。

公共窄职责建议只针对两条**完全同形的全 cutoff voucher 引用 SQL**：在 `read_indexes` 设私有纯选择函数，以候选 ID 为驱动返回所有 `r.*`；`QueryReads` 和 `Journal.hydrate` 各保留自己的验证/缺行合同。精确对照需覆盖返回**多重集**（不只 set）、错 type/path、同 voucher 多闭月引用、未闭/已闭混合、更正冲正、缺可修目录行、新快照重试和 v1 正式包；不得把 `source_kind/marker` 成功结果当跨请求缓存。

同一 snapshot 的 Journal 叶复用已由 release_review 的私有 AB 证实，原始 `.tmp/stage9-journal-verified-leaf-ab-r23.json`：仅将 Journal 选出的引用交给既有 `snapshot.reads.verify_close_references`，完整资产页 A/B/B/A 除 `generated_at` 外逐字段相同；`accounting` family 调用 94→47，叶 187→140，正好省掉已在本 snapshot 验过的 47 个叶。QueryReads 以完整字段及类型构成 key，成功后才记缓存；生产仍待回归解冻。测试须覆盖 Journal 在先/在后、错 type、坏摘要/正文/marker、失败不缓存和新事务重新核验。

### 按期/path 的无 DDL 半连接候选（只读原型）

原始报告 `.tmp/stage9-period-path-semijoin-ab-r23.json`，探针 `.tmp/stage9-period-path-semijoin-ab.py`。固定 r23/main48 上，旧查询对每个 `(id,close_period)` 都按主键扫描该期 `vouchers[*].id` 路径约 1,005 行。有效候选把 `wanted(id,close_period)` 物化，再按 `r.close_period IN(wanted)` 与精确 `path` 让 SQLite 每期只扫描一次，用 `(r.reference_id,r.close_period) IN (SELECT id,close_period FROM wanted)` 临时 tuple 查找筛选。最后与原 `wanted` 连接，恢复重复候选的原多重集；**没有新增 `reference_type` 过滤**，所有命中路径的异常 type 仍进入原 verifier。

| 固定 r23/main48 候选 | 旧 VM | 半连接 VM | 旧/新返回行与 value bytes | 多重集 |
| --- | ---: | ---: | --- | --- |
| 单月 47 pair | 331,400 | 21,900 | 47 / 8,036 B | 相同 |
| 三个月 141 pair | 994,300 | 118,800 | 141 / 24,100 B | 相同 |

合成内存目录另包含同 ID 跨月、同 ID 同期错 `reference_type`、异 `path`、重复输入 pair；旧/新 8 行逐字段多重集相同。只按月 `grouped` 而没有 tuple 半连接的试法在三个月反而升到 1.716M VM，**弃用**。该实验与同时进行的完整核验并行，`r23/main48` 独立报告采样时仍为 `measuring`；wall 只作诊断，不是纯时延或发布通过声明。接下来需要有界候选规模增长及原函数行为回归后才可改生产，且不可把此 SQL 工作量下降写成五页已过 500ms。
