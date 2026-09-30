# 阶段 9：存储 JSON 双解析边界审查

固定 r30 源清单 SHA-256 为 `bbb18c584ee827224daade05fbb138d688b4249d35b850d3d02171dcff84da2b`。`calculation.outcome` 的原始复现取自当时 current 合成 `test_banking` 夹具；对象档案复现另在固定 r30 源的新隔离合成库执行。两者不是对正式公司库的修改。四份原始结果以无损 gzip 保存，原文字节数、原文 SHA-256、压缩包 SHA-256 见 [证据清单](stage9-g-json-dual-reader-evidence-r30.json)；生成时已逐字节解压回验。相应复现脚本保留在 `.tmp/stage9-json-profile-duplicate-r30.py` 与 `.tmp/stage9-json-profile-fullverify-r30.py`。

核心问题是逻辑摘要基于 Python `json.loads` 后的对象；含重复键的原始 JSON 可让 Python 采用末值，而 SQLite JSON1 在 SQL 选择条件中采用首值。后置内容核验只检查 SQL 已选中的行时，负选仍会漏掉应检查的来源。下表将已实证、明确静态风险、已核查保护和未验证范围分开；它是修复范围审查，不表示列举的每个入口已有业务级复现。

| 来源与消费链 | 证据等级及边界 | 处理决定 |
| --- | --- | --- |
| `calculation.outcome`：资金、准备等使用 JSON1 的入口 | **已实证**。`stage9-json-outcome-current-r30b/c` 将首个 `balances` 键改为 ghost，末键仍为原值，逻辑摘要匹配；完整核验接受，但默认资金和准备读出 ghost。`r30c` 还记录 `values` 重复键。 | current 共享严格 JSON 读取须覆盖 SQL JSON1 前的全部候选；仅修选中行的 decoder 不够。普通空白及键序兼容、正常原文字节摘要快路均需保留。 |
| `entity_profile_revision.content`：员工候选 | **已独立实证**。固定 r30 的新公司中，未就业档案原文前插 `employment_start` 日期、末尾保留原 `null`。Python 读出原值且逻辑摘要仍匹配，SQLite SQL 把空名单变为 ghost 员工；`verify_entities` 接受，完整 `verify_integrity` 返回 `verified` 且 limitations 为空。原始结果见 `stage9-json-profile-duplicate-r30`、`stage9-json-profile-fullverify-r30`。 | 先认证所有可能影响名单负选的最新 person profile，再执行相同选择规则。只对入选员工调用严格解析仍会漏行。 |
| `fact_settlement.first/second`：业务清偿详情 | **明确静态风险，未做业务级复现**。`BusinessQueries._settlement_collection` 在来源验证前用 `json_extract(...,'$.source_id')` 选 slot；复合事实字段以 Python 解码并计算逻辑摘要。首末 `source_id` 冲突可能改变详情候选和分页。 | 对 SQL 前的完整候选范围校验原始复合字段；需定向证明正常/损坏行为。 |
| `QueryReads.settlement_subjects` 的历史 `calculation.outcome` | **已有业务级复现与定向回归**。`json_array_length(...,'$.values.obligations')>0` 原先先筛所有 calculation 的 subject；固定 r30 合成真实全局清偿列表接受了首键空、末键非空的结果。r31 守卫后拒绝，见 [已保存 JSON 的 SQL 筛选解释](grouped-stored-json-selection-r31.md)及 `tests/kernel/test_stored_json_selection.py`。 | JSON1 负选前对可被排除的精确候选检查决定该筛选的路径唯一性；不是完整来源证明。 |
| 期初发布与员工采用头的 `calculation.outcome` | **已有业务级定向回归**。`Engine` 的既有期初判断、`adopted_head_metadata` 的工资行数筛选，均由 `tests/kernel/test_stored_json_selection.py` 注入首末冲突键并验证拒绝。 | 期初现有 current 集合与工资需行数的精确头 ID 均在 JSON1 值影响业务前核对；不把局部晚验当负选保护。 |
| `business_duplicate_check.manifest`：查重位置与强候选 | **已有业务级定向回归**。原先多处 `json_each` 在 `_require_check_record` 前选候选；`tests/kernel/test_stored_json_selection.py` 已覆盖强候选隐匿及 current／固定 v1 解析。 | current 对相关 SQL 预选范围先做结构歧义守卫，后续命中记录仍按原摘要证明；其它位置传播的大库增长尚未量化。 |
| `period_close.manifest`、current/v1 冻结 family／目录／块；`report_open_contribution.content` 与报告等冻结投影 | **已核查保护**。正常读取先以原文字节 SHA 或规范原文等式验证根和正文，再解析内容；直接改变原字节会先破坏绑定。公开 Display 路径在 SQL `json_each` 前亦先经过 close 内容验证。 | 保留现有原文证明与调用顺序；不把所有 `json.loads` 都归为漏洞。协调篡改根及私有函数独立直调不由本证据覆盖。 |
| 固定 v1 历史 reader | **尚无同型运行时证据**。检查到的 v1 JSON SQL 主要消费 Python 构造的 ID 参数，未见对存储 outcome 的同样前置 JSON1 负选；v1 自有逻辑合同仍须独立检查。 | 不将 current 的新 decoder 隐式套入 v1，也不因同名字段断言 v1 已被绕过。 |

新接线的只读核对发现，资金 journal 的 `basis_calculation_id`、期初 metadata，以及当月准备的 bank JSON1 已在解析前验精确 outcome；资产消费与 `metric_rows` 也有同形前验。`BusinessQueries._settlement_collection` 在解析 `fact_settlement.first/second` 前调用 `reads.fact_versions`，当前 `Store.decode_fields` 已严格拒绝重复键，因此原表中该项是修复前的静态风险；它自身仍没有单独的重复键业务级复现。`duplicates.py` 的存储 manifest 预选已有结构守卫和定向回归，不能再记为尚未收口。

尚未动态穷尽查重位置传播、清偿详情所有分页，以及私有旧展示入口。对所有新接线的关键复核条件是：原始 JSON 影响**入选或排除**前，候选全集是否已在同一只读快照受权威来源及严格解析约束；失败不得缓存为成功。必要的源摘要、根、封签和完整核验不能改由后置页面结果替代。

## r31 接线的范围与工作量复核

本节只按当前调用链做静态复核，未运行新的整页、完整核验或大库计时。`verify_sql_outcomes` 对传入的精确计算 ID 读取原 outcome 与 digest，逐个严格解码并核摘要；它是 **JSON1 负选前的解释一致性守卫**，不认证事实、发布、封签或依赖。`QueryReads` 只在活跃快照中记成功 ID，失败不记；`verify_selected_content` 后续仍按原职责重建完整来源。两者命中相同 ID 时可能各解析一次 outcome，当前守卫没有将原文写入完整来源缓存；这是有意保留不同证明边界的额外工作，不能把守卫成功当作来源成功。目前未量到该交集足以构成阶段性能阻断。

| 调用链 | 精确候选与新增成本 | 同请求完整证明及结论 |
| --- | --- | --- |
| `FundsRead.events`、投资事件、`dashboard_reads.metric_rows` | 先从所选 journal 查询 `basis_calculation_id`，再按 ID 解析 outcome；`FundsRead` 在同一对象的事件查询参数有缓存，`QueryReads` 对重复守卫 ID 有成功缓存。范围可含所请求的历史 journal，但不是另行枚举所有 calculation。 | 原 close reference、journal 与后续来源读取保持原样；与完整来源检查重合的二次原文解析尚未定量，不据代码相似性判作全史重复。 |
| `QueryReads.metadata(state=True)`、工资采用头与资金期初 | 仅对请求且缺少 state metadata 的 ID 做守卫，随后 JSON1 计算行数／期初；`state=False` 不做这项扫描。工资 `line_count_period` 仍只点查所需期。 | 同一 `QueryReads` 中重复 ID 命中守卫成功集合；后续需要完整内容时仍单独认证。 |
| `BusinessQueries._settlement_collection` | 先以现有选择结果按种类分组，对会在 SQL 中读冻结 settlement slot 的组按精确 calculation ID 守卫，再读取 typed 子表；并未因此装载全部历史 outcome 到 Python。 | `fact_versions` 另证明精确事实，后续清偿解析与来源证明不由守卫替代。 |
| `employee_entities` | 只枚举每个 `person` 的**最新一版** profile，而非全部历史修订；每个候选经 `_profile_record` 严格解码和摘要核验后才判断员工负选，随后只对入选名单的精确 witness 做 `verify_hits`。 | `dashboard_metadata` 在同一 `QueryReads` 快照缓存成功名单；`Display._metadata_snapshot` 是另一入口，不能借页面缓存。相比原 SQL 负选确有按 person 数增长的 Python 工作，但这是覆盖可影响名单的非入选 profile 所需；当前没有 12/48/120 档 CPU 量测。 |
| 查重 `source_locations`、`strong_candidates` SQL | `_require_sql_manifest_scope` 用 SQLite `json_tree` 对相应 `business_duplicate_check` 范围检查所有层重复键，未把全史 manifest 搬进 Python。批量候选在该次 `candidate_cache` 成功后免再扫；`create_separate` 限其 action；`business_detail` 因任意 check 的 manifest 都可能指向该 subject，目前守卫全表。 | `_require_check_record` 只对后续实际读取的记录严格解析并核记录摘要；`verify_duplicate_checks` 的全记录遍历仍是独立完整核验。全表 SQL guard 的增长成本应由统一组计量观察，不能把它写成全史 Python 解码。 |
| `QueryReads.settlement_subjects` | obligations 索引的负选前，以 SQL 检查可能被排除的非 `SETTLEMENT_KINDS` 候选对象路径唯一性；不装载全部结果正文到 Python，也不填完整证明缓存。 | 原候选索引和命中后的关系／来源核验不变。现有 12/48 月只读诊断显示该守卫额外约 0.64M/2.63M SQLite VM，原选择约 0.54M/2.14M VM，见 `.tmp/stage9-g-settlement-actual-r31.json`；这是实付成本，墙钟为并行诊断。 |

`tests/kernel/test_stored_json_selection.py` 已覆盖正常非规范 JSON、重复键负选、工资、员工、期初、清偿和 current／v1 查重的业务拒绝；现有文件没有断言 12→48→120 月守卫 VM、Python 解码次数或同 ID 与完整来源核验交集不会增长。当前未发现必须在 r31 固定前阻断的接线错误，但大样本工作量仍是明确的验收观察项，尤其是查重全表 SQL guard 和员工最新 profile 数量。不能用这项成本为由缩小负选候选或跳过原完整证明。
