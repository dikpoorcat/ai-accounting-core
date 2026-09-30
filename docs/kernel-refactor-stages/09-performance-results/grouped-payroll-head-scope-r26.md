# r25-final 主 48 月员工页：工资历史头的消费范围（只读诊断）

固定源码 `.tmp/stage9-build-source-r25-final-release`、合成库 `.tmp/stage9-release-main-48`，调用真实 HTTP `employees`，2019-12、`preparation=deferred`、默认 100。原始计量：`stage9-r25-final-main48-payroll-head-scope.json`（默认列表）和 `stage9-r25-final-main48-payroll-head-detail.json`（单人详情）。本轮登记报告开始时为 `measuring`，且有并行核验，所有 wall 时间仅为诊断，不是验收或纯时延。数据库及固定源码未改。

| 消费点 | 固定 r25 实际输入/结果 | 必需语义 |
| --- | --- | --- |
| `dashboard_reads.adopted_head_metadata` → `_employees` | SQL 按 subject 选 2,400 个工资采用头，覆盖 48 月；返回 345,120 值字节、约 565,920 JSON 字节，1,126,800 VM。 | 头的候选全集由 publication 与各闭月已核 highwater / 当前采用决定；不能以可修复对象目录缺行删候选。此 SQL 只是每个 subject 一个，不是每人一个。 |
| 默认页 `current_role_matches` | 2,400 个工资 fact 加 50 个 profile fact，共 2,450 个事实；全部命中 50 位员工，每人恰 48 个工资头。 | 逐源事实与当前对象引用目录核对，防漏行、错对象、坏正文；这是全体名单完整性的证明，目前不能只验证 50 个代表。 |
| 当前工资金额 `_workforce_payroll_rows` / `_aggregates` | 当月 journal 50 行、50 subjects、50 人；零行状态另看本月头。 | 本期金额/状态只消费本月结果，不消费 2,400 个历史结果正文。函数传入 2,400 头后遍历筛本月零行。 |
| 员工名单和代表采用 | 2,400 头在 Python 按核对后的 employee id 去重成 50 个代表；`Calculations.selected` 只验证 50 个代表采用。profile 50 条另加入名单。 | 不能以 profile 存在推断工资已发布；代表顺序为 `(posting_period, calculation_id)`，且修正后的真实员工身份要参与分组。 |
| 默认付款汇总 | 冻结本月付款 150 行、50 个来源 subject；`Calculations.selected` 精确核对 50 个付款命中来源（该合成月），然后以 head→employee 归集。 | 只需验证实际付款命中来源及精确采用身份；不能把其它历史头的身份从完整名单证明中删去。 |
| 默认历史来源卡 | 0；`history_ids` 仅在显式 employee_id 时非空。 | 默认 100 员工行不返回历史卡。 |
| 单人详情 | 仍先核对全体 2,450 个身份事实；随后该员工 48 个历史 subject 调 `Calculations.selected`，来源卡再按 limit 分页；付款命中本页只有 1 个 subject。 | 明细需这位员工全部 48 个采用结果，不能压成一个代表。 |

判断：此前“全体员工必须从全部历史来源找到”正确，但若据此认为默认页必须把全部历史头的完整元数据和结果送入 Python 再分组，则混淆了**候选及逐源证明**与**展示消费**。默认页需要全部 2,400 个事实 ID 的候选完整性和身份核验；其本期金额、代表结果、付款结果分别只需本月/每人一项/本月命中集合。当前元数据 SQL 不是最大风险，约 1.13M VM 与 345KB 返回；当前角色完整核验仍处理 2,450 个原始事实，不能假称只改 SQL 就省掉这一成本。

下一轮可评估一个窄的同快照两段选取：第一段从权威 publication/close highwater 枚举**所有**采用工资源 fact_id（仅最小候选字段），对全体 ID 维持既有 `current_role_matches` / `verify_hits` 的完整原始事实和目录核验；成功后，第二段在同事务按已证对象身份用 SQL 选每人精确代表并只返回展示所需头，本月零行头与付款命中 subject 独立精确选取，单人详情仍选该人全史。不能先按 `entity_reference_current` 分组再验证，因为缺行会隐藏人员；不能从 profile 自动认定已发布。此方案可能因重复运行 publication 选择 SQL 而无净收益，尚未做等价、VM、损坏及工作量 AB，**不建议直接改生产**。若没有单次权威候选枚举与代表选择的复用方式，保留现状更稳妥。需覆盖目录缺行、错对象/修订、更正后身份变化、同人多 subject、无结果行工资、仅 profile 无工资、付款来源跨期及单人详情。

## 同组私有 A/B：范围与结果

r25/main48 完整核验报告后来成为 `complete / verified`。仍仅做只读诊断，不称纯计时。SQL 探针 `stage9-r25-payroll-sql-candidate.py` 的原始结果在 `stage9-r25-payroll-sql-candidate.json`：原单次完整头选择 2,400 行、313,920 值字节、1,135,700 VM、约 203ms CPU；仅取 2,400 个权威 fact ID 降为 76,800 字节、1,112,400 VM、约 125ms CPU；另跑按已核对象目录的每人代表 SQL 得 50 行、6,540 字节，但新增 1,370,000 VM、约 172ms CPU。**两查询合计约 248 万 VM / 297ms CPU，高于原查询，不采用“两段重跑完整采用选择”**。它只在完整合成样本上证明 50 个代表 ID 与 Python 原结果相同；该 SQL 自身没有做 2,400 源证明，不可独立上线。

更窄的只读 HTTP 原型在原头 SQL 保留全部 2,400 个身份、subject、期间、kind 的前提下，仅使非本期 `line_count` 为 NULL；本期仍从 outcome 计算该值。`_workforce_payroll_rows` 只在 posting_period 为本月时消费 `line_count`，代表、付款和详情不消费旧月该字段。它不改变 `_expected_rows`、`current_role_matches`、`verify_hits` 或采用来源选择。私有脚本用 `STAGE9_PAYROLL_MODE=current_line_count_only` 替换一处 SELECT 表达式，不改固定源码/业务库。

| 两轮真实默认员工 HTTP（仪器化诊断） | 原 SQL | 旧月不解 `line_count` |
| --- | ---: | ---: |
| 返回头行数 | 2,400 | 2,400 |
| 头查询值字节 | 345,120 | 326,320 |
| 头查询 VM | 1,126,800 | 1,129,300 |
| 头查询 execute 时间 | 204.1 / 209.0ms | 156.2 / 156.3ms |
| 整次请求进程 CPU | 828.1 / 843.8ms | 781.3 / 750.0ms |
| 完整响应 SHA256 | 两轮均 `df5248ba...b8db04` | 同左 |

默认 HTTP 响应的全部 JSON 字节相同。单人详情也核对了全部 48 个历史 subject；跨进程随机哈希种子导致既有历史卡顺序不同，设相同 `PYTHONHASHSEED=0` 后两路径完整响应 SHA256 同为 `a287e62a...24451ddb5f2`。这不是产品改动。私有原始正文保存在各 `*-response.json`。原始事实 payload 在读取时注入一处更改，原版及候选均调用 `_expected_rows` 一次并以相同 HTTP 500、相同 `content_integrity_failed / component=fact / record_id` 响应拒绝；见 `*-fault-raw_fact_payload.json`。该故障注入未改数据库，不能替代真实目录缺行/身份纠错等正式回归。

身份原文工作量：默认 `_expected_rows` 处理 2,450 个事实，原始 `fact_data_many` 返回 2,450，仅 50 个原文已在 `QueryReads` 快照中；其加载 SQL 返回约 4,900 行/555,420 值字节，单次约 56ms 墙时、62.5ms 粗粒度进程 CPU；整段 `_expected_rows` 约 170ms 墙时/172ms CPU。`references_from_data` 对 2,400 工资事实展开 9,600 条，其中 7,200 是随后过滤掉的 business refs，entity 为 2,400；全展开约 14ms，私有仅实体声明构造约 6ms。这里有范围可缩，但不足以解释数百毫秒；`object.__new__(Store)` 造成的同快照原文重叠仅 50/2,450。固定 r25 源中不存在名为 `fact_reference_rows` 的函数，此处实际链是 `_expected_rows → Store.fact_data_many → references_from_data → current bindings`。不应为了约 8–11ms 加业务缓存或改 v1 规则。

其它消费者边界：`_asset_card_sources` 也枚举所有历史资产/生命周期候选，随后 `verified_scalar_facts` 验证所有候选源，再以 `asset_id` 组成卡片；它不是工资的 employee 对象引用/身份纠错分组，不能直接套工资 SQL。`find_entities` 由 `profiles` 全体及 `entity_reference_current` 的使用期间构造搜索结果，不调用工资采用头，且它的对象搜索口径不证明工资已发布。当前身份纠错 `_current_bindings` 按精确 subject/path 链重建，源事实及被恢复事实都必须计入；事先按当前目录裁剪会漏更正。固定 v1 通过 `payroll_head_identities` 的 recorded 身份与 `content_v1_semantics.v1_current_bindings` 自有规则，未做 v1 动态 A/B，不能把 current 诊断当作 v1 等价证明。

决定：两查询压到 50 头的方案明确负收益，保留负结果。旧月 `line_count` 条件计算是一个无需格式/DDL变化的窄候选，有实际同源诊断 CPU 下降与默认/详情完整响应、单处坏源拒绝证据；仍须在正式实施前验证不同 posting_period、零凭证结果、闭月和固定 v1 版本、身份更正/目录缺行的完整回归及纯负载下稳定收益。当前未修改生产代码。

## 采用的窄改与全调用点（实施前决策）

触发根因：共同 `adopted_head_metadata` 在所有工资历史 subject 上都执行 `json_array_length(c.outcome,'$.lines')`；`_employees` 给 `_workforce_payroll_rows` 的实际唯一消费是本期无凭证状态行。旧月的线数不构成名单、付款、代表采用、单人历史卡或完整来源核验的依据。48月主样本有2,400候选，只50本期；候选 fact/身份仍全量核对。

检索固定源码的消费者：`dashboard._employees` 全史工资头需要本月线数；`_workforce_payroll_rows` 未传入 heads 时按本月 posting_period 读，旧语义本来就只本月；`dashboard._employees` 的 labor 头显式 `posting_period=snap.period`，完整线数默认保留。通用 `adopted_head_metadata` 的其它当前/v1调用保留默认完整线数，未知旧月线数将用 `None` 表示而不能当零；需旧月线数的调用必须不传优化参数或另行精确获取。`business_queries`、`query_reads`、资产批次与close-review中其它 `line_count` 均为不同来源、不同用途，没有本次修改。CLI/MCP只经 Dashboard 员工页触发该范围，独立关账/完整核验/修复/备份及 fixed-v1 原始内容规则不改。旧版内容若经同一个 Dashboard 员工页，其旧月线数同样只在此页面丢弃，来源与身份版本规则不变。

另发现单人详情跨进程哈希种子使 `payroll_sources.items` 的历史卡顺序不同：页面选页键已按工资期间/subject 排序，随后 `displayed_wages` 从 `Calculations.selected(...).values()` 原迭代次序重建卡列表。需确认分页成员不变且只修该已定选页键顺序，不对全部列表排序扩范围。

## 本轮实施及相邻 SQL 审查

现工作树仅对 `dashboard_reads.adopted_head_metadata` 增加可选 `line_count_period`，缺省保持全量原值；opt-in 时只有该 posting 月求 SQLite JSON 行数，旧月返回 `None`（未知）。`payroll_head_metadata` 透传，只有 `_employees` 以 `snap.period` 指定；全工资候选 SQL、source fact ID、role 原文核验、闭期 highwater 与代表/付款采用证明原样。`_workforce_payroll_rows` 只在本期头消费此字段。真实生产函数工作量测试覆写内存 SQLite 的 `json_array_length` 计数，而非检查 SQL 文本：200 旧月 + 2 本月时，全值模式解码 202 次，员工页模式 2 次；再增加 500 旧月仍为 2 次。旧月为 `None`、本期零行是 0、有行是 2。真实公司夹具闭月 January/开放 February 员工详情与原完整线数路径逐字段相等；损坏工资原文、缺/坏对象目录、计算正文损坏与 no-impact 采用原有定向均通过。

历史卡顺序也只在 `_employees` 内按原 `page_keys` 之前已确定的 `(payroll_period 或 period, subject_id)` 键重排 `displayed_wages`；人为把 `Calculations.selected` 的两个工资来源逆序返回，卡片仍按 January、February 呈现。分页成员和 cursor 逻辑不改。

相邻同类检索：`QueryReads.metadata(state=True)` 会抽 `line_count` 和 `opening`，但 `BusinessQueries` 闭期/开放期无凭证 state 枚举直接用线数判定，`Dashboard` 的 `CalculationView` 及若干调用消费 opening；`state=False` 原路径已避免这些 JSON 标量。部分 identity-only 调用（如资产卡片关系）可能无需它们，但没有同组实际大历史调用及消费者证明，本轮不变。`selected_voucher_sql` 选择的是 voucher/计算身份和 digest，SQL 本身不读取 `c.outcome`，不属于这个问题。`dashboard_reads.metric_rows` 仅对选中 journal 计算所需的指定金额字段作 `json_extract`，结果参与本期金额；`dashboard_funds`、资产批次及关账中的 outcome 解析各有内容/分类/关系消费者，未证明可删。没有扩大改动。

定向测试：`test_payroll_head_line_scope.py` 1项；`test_dashboard_workforce_assets.py` 新增闭/开放响应、历史卡排序2项；已有坏源/目录2组、计算正文1项、无影响采用2项，共9项通过；相关文件 Ruff 通过。大型全量回归与固定 v1 完整核验留给 root 统一 freeze 验证。
