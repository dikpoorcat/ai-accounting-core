<!-- @format -->

# r64 员工、资产身份及名单历史增长只读审计

审计阶段只读取固定源码、既有剖析输出和测试源码，没有修改实现、测试、结构合同、业务库，也没有运行新的测量或测试。固定源码为 `.tmp/stage9-build-source-owner-r63-release`；下文审计源码位置均相对其中的 `src/ai_accounting/kernel/`。后续身份组实施决定及定向结果记录在文末；候选建议不能视为已实现或已验收。

## 证据及结论边界

对照资料为 `.tmp/stage9-resident-r63-profile.json` 与 `.tmp/stage9-resident-r63-main48-profile.json`，以及对应 `.log` 和各五份 `.prof`。12 月根为 `.tmp/stage9-owner-main12-r43`；48 月根为 `.tmp/stage9-owner-main48-r63`，查询期间以对应 profile 脚本为准。两份剖析均使用同一 r63 固定源、50 员工、每月 1,000 项业务，默认分页 20，在常驻读取池下先预热。`native_ms`、cProfile 和带 instrumentation 的时间只用于诊断，不代替官方页面验收。

父任务已确认：12 月五页实际页面均通过；48 月员工 30/30 慢，median 1,037.5 ms、max 1,096.9 ms；资产 2/30 慢、max 576.6 ms；简报 30 次慢、max 777 ms；报表 30 次慢、max 717.2 ms；资金通过。两根均完成官方注册、content verified 和 preview，source/contract 一致。这里不重做上述验收。

主要结论：员工增长已经不是旧工资 outcome 正文增长，而是全部历史工资业务头、事实角色、关账采用叶的身份证明规模增长；资产的 `_selected_asset_member_heads` 实际不是只读最终头，而是先读取全部历史采用 owner 的完整 outcome 与完整成员目录，再由 dashboard 折叠为卡片状态。两者必须分别处理：前者存在确实消费的完整身份范围，后者存在读取及折叠范围可以重新设计的机会，但现有目录不具备脱离 owner 的独立权威，不能先分页或先取最新目录行来跳过证明。

| 指标 | 员工 12 月 | 员工 48 月 | 资产 12 月 | 资产 48 月 |
| --- | ---: | ---: | ---: | ---: |
| SQL calls | 149 | 366 | 401 | 1,164 |
| SQLite VM steps | 604,700 | 1,568,000 | 325,500 | 599,300 |
| 返回行 | 11,299 | 35,750 | 4,585 | 10,094 |
| 返回值字节 | 5,055,183 | 13,844,770 | 2,886,105 | 9,274,520 |
| stdlib JSON loads | 1,416 | 4,713 | 599 | 2,639 |
| stdlib JSON input bytes | 2,404,368 | 7,901,872 | 1,147,540 | 6,168,550 |
| outcome 实际传输行 | 100 | 100 | 109 | 433 |
| outcome 实际传输字节 | 553,340 | 553,340 | 197,173 | 2,382,031 |
| adopted slice reads | 11 | 47 | 33 | 141 |
| adopted rows | 550 | 2,350 | 44 | 188 |

计数来自剖析 JSON 的 `work.counters`；传输、JSON、采用读数有各自定义，不能简单相加。员工 typed/raw fact JSON decode 都为零，并不表示没有读取和校验原始事实：`verify_hits` 经 `Store.fact_data_many` 读取规范化标量及子表，逐事实重建、计算 digest；这些工作不是上述两个 JSON decode 计数器的范围。

## 员工位置、消费与决定

| 位置及调用方 | 实际范围、消费及证据 | 处理决定 |
| --- | --- | --- |
| `dashboard.py:3447 _employees` → `dashboard_reads.py:205 payroll_head_metadata` / `:121 adopted_head_metadata` | 枚举截至选定期每个业务 subject 的精确采用头，不是每员工一个头。12→48 工资枚举行 600→2,400、VM 83,300→335,900；业务头随月份增加，即使只有 50 员工。`representative_heads` 的员工归并发生在完整来源定位之后。 | 保留所有已消费的业务头；不能用每员工最新工资、当前月工资或管理名单替代。优化从 SQL 驱动范围、元数据复用和同快照证明开始，不能用减少身份范围伪造常量成本。 |
| `adopted_head_metadata` 的 publication/close 选择 | 当前 SQL 从 publication 联 subject，并按关账高水位、同月后继和开放 current head 选择，再 window 按 subject 折叠。历史上真实最新采用的规则是必需的；线数已经只读取本期头，其他头 `None`，不是零。 | 按 kind/subject 驱动 publication 的候选值得检查，沿用精确高水位及开放终端完整性检查。未核对执行计划前不声称发生全 publication 扫描，也不承诺改变 JOIN 就有效。 |
| `dashboard.py:3508` → `entity_references.py:521 current_role_matches` / `:556 verify_hits` / `:371 _expected_rows` | 开放月对工资头及当期金额来源完整验证 current role。48 月 `_expected_rows` 两次；原始 fact 元数据返回 2,450 行，工资标量返回 2,429 行，全部 current 引用返回 2,471 行。第一批覆盖全部 2,400 工资头，另一批为名单见证。所读事实确实用于重算完整 role 集合及 digest，不能只读取 employee 字段就当作全部来源证明。 | 把“原事实内容已验证、recorded 完整角色已验证、current 完整角色及纠错已验证”分清后由同一 `QueryReads` 快照承载成功证明；独立连接调用仍完整验证。允许复用同 fact 的成功证明，不允许把 metadata identity、seal 或 indexed role 当成内容证明。规模主因仍是 2,400 个必要事实；50 行重叠去重只是一部分收益。 |
| `current_role_matches` 的角色结果 SQL | `SELECT fact_id,entity_id FROM entity_reference_current WHERE fact_id IN(...) AND role=?`；12→48 返回 600→2,400，VM 23,800→93,700。PK 为 `(fact_id,path)`，另有 `(role,period DESC,fact_id DESC)`。 | 候选是请求 fact IDs 驱动的精确 role lookup，并继续拒绝同角色多对象。不加表、不改冻结合同的 SQL 改进优先；实际使用哪一索引须有执行计划支持，不能把已有 role 索引声明当成实际计划。 |
| `dashboard_reads.py:382 payroll_head_identities` | 闭期路径验证 recorded 完整 role，随后读取每个 adopted fact 的 employee，检查 kind、period、source_digest 和覆盖完整性。当前两份诊断是开放末月，不能以其中没有该函数热点声称闭期免费。 | 保留闭期 recorded 语义及旧零行头；同类 role 复用候选覆盖这里。禁止用 current role 重写冻结人员或用 selected page 限缩这一步。 |
| `dashboard_reads.py:235 verified_adopted_head_identities` / `:373 verified_payroll_heads` | 全部工资头的 metadata、seal、事实身份、精确 publication 及独立冻结 adopted leaf 均消费。48 月 seal/事实身份 SQL 返回 2,400、VM 110,400；publication identity 返回 2,400、VM 72,100；open terminal 查 50 行但 VM 51,900。冻结叶结果摘要是工资头身份的独立锚。 | 不删除未成为代表头的证明。可以共享相同精确 publication/seal/metadata 检查的成功结果，并减少重复传输字段；禁止仅核验代表头，也不能把身份成功写入 full-content proof 集合。 |
| `query_reads.py:1682/1688` → `close_storage.py:949/960` | `subjects_by_period` 已把工资来源分到真实 posting month；authority 批读一次，再逐关闭月份读采用桶。12→48 adopted rows 550→2,350；实际 accounting blocks 返回 508→2,136，字节 1,035,742→4,429,791。不是整 manifest decode；读数也没有 full manifest/section decode。 | 现有每期 scope 不应回退成“所有工资 subject × 每个 close”。优先分析桶冲突及 repeated authority/metadata 工作，保持原始 root、leaf、遗漏检测。若要从历史来源发现缩减到人员集合，需要正式、独立认证的人员发现依据；目前没有这样的证明，不作为本轮立即修正。 |
| `query_reads.py:698 metadata` | 48 月调用 54 次，own 112.145 ms、inclusive 129.151 ms；首个批 metadata 的 identity/state=False 为 2,400，另一条缺失 metadata SQL 为 2,350。缓存检查遍历请求 identifiers；返回也重建请求字典，但没有遍历整个 `_metadata` cache。 | 不把 `metadata` 缓存误报为全缓存复制或确定的二次复杂度。检查真正重复的请求/返回和冷字段补读，再决定直接 key 取值或按期预分组。不能据 own time 单独改行为。 |
| `entities.py:176 employee_entities` → `dashboard_metadata.py:54 Records._query` / `:159 __iter__` / `:368 prime_profiles` | latest person profile 决定负向成员资格，员工见证进一步验证 role；开放读取使用 `_report_snapshot_cache` 缓存一次人员集合。48 月 `employee_entities` 一次 inclusive 13.943 ms；profile prime 批次一次 inclusive 3.233 ms。已验证旧 profile JSON 冲突及 resolved 判定用于名单；不是每员工重复执行整份名单。 | 保留 unknown、ended、no-payroll、未建 payroll 的正式人员。可以让这里的见证复用同快照角色成功证明。已观察收益上界较小，不先改名单 semantics，不把它列为 main48 员工主热点。 |
| `dashboard.py:3554–3589` 状态/过滤/20 项分页 | 全部人员状态及 payroll 聚合用于总数、过滤、摘要，必须在 pagination 前消费。完整工资金额只读当期；管理资料为 known 人员批读。 | 正确边界是“完整名单及摘要 → 20 项内容”，不能只验证显示 20 人后报告完整人数/金额。 |
| `dashboard.py:3593–3644` 员工 payroll_sources 详情 | 默认没有 `employee_id` 时不建历史 cards。指定一位员工时先选其所有历史 subject，`Calculations.selected` 会完整选择、证明这些来源，然后按来源排序分页。范围已从所有员工缩到一人，但仍随该人的月份增长。 | 候选为在已验证精确身份和排序标量之后对该员工历史页选键，再只水合该页结果。若摘要/清偿仍消费全部来源，其必要证明保留。详情的旧来源完整内容及遗漏、无影响复核、更正语义不可省。此候选未测量、未证明等价。 |
| `dashboard.py:3643` → `settlement_freeze.py:1451 frozen_employee_net_summary` / `payroll_cohort_identities_match` | 所有 wage heads 用来构建 net/primary obligation keys，与固定工资 cohort 归属交叉核对；月末未付包含旧来源。12→48 `_scope` 增长，但本期 tail rows 始终 1,246、VM 72,300，不能把全部 `_scope` 时间归因工资头 identity SQL。 | 保留工资头完整集合和 current/recorded 分工。settlement 是另一条共享根问题，应与相应审计合并决策；不能把只看当期变动当成月末未付。 |
| `dashboard.py:3902` labor_sources | 枚举当期 labor heads；没有 employee_id 时先20键后完整选择显示来源。指定员工时通过已验证 scalar fact 匹配 person；清偿事件详情显式读该员工历史 subjects。 | 当期有界路径保留；不要为统一工资 API 把 labor 退回全历史。管理/实体匹配的复用覆盖，但本次没有证明该部分造成48月增长。 |

## 资产位置、消费与决定

| 位置及调用方 | 实际范围、消费及证据 | 处理决定 |
| --- | --- | --- |
| `dashboard.py:4051 _asset_card_sources` | 获取 acquisition、accepted batch、activation、disposal、project 最新采用身份及 scalar事实；activation member 没有独立 publication，另经 batch adoption/reversal 选头。该集合用于 status、资产全量 count、过滤及摘要。48 月 inclusive 127.707 ms；不能只用 displayed page 建它。 | 保持独立 acquisition 身份和非独立 activation 成员的区别。身份正文已分离，不恢复全历史 outcome。activation成员选择与 consumption头选择可共用同快照 owner权威，但不混淆 proof级别。 |
| `dashboard.py:4230–4318` carrying 快路径 | current、undisposed、非 opening_asset 卡片用冻结 carrying 和已核验 acquisition 的非charge贡献判断 cost-carrying；本期余额再验证。opening、disposed及条件失败走完整历史。 | 现有两路径职责合理，不能把历史不可适用卡片强塞快路径。所需 acquisition outcome 及完整金额证明是真消费。 |
| `business_queries.py:827 _selected_asset_member_heads` → `:782 _selected_asset_owner_events(complete_owners=True)` | 名字是 heads，实际枚举全部 `asset_activation_batch` 和 `asset_consumption_month` subjects， `_selected_accounting` 读取全部历史 adopted owner/voucher events。读取 12→48 owner outcome 24→96，字节 78,899→1,111,505；成员目录 78→1,176，字节 65,904→1,015,512、VM 5,900→86,600。完整owner outcome参与 membership_digest、成员全序号/行范围证明，因此当前代码真消费；dashboard随后只保留最后 positive、latest period、activation/source参考。 | 此处是资产首要增长根。候选先严格声明“最终状态/最近采用所需事件”和“完整来源历史”两个业务消费，再用精确 publication/reversal authority 选候选、对必需 owner 保留完整 membership证明。不能直接对未验证 `asset_batch_member` 取 max、limit或先按资产过滤后宣称完整批次。需要证明被省略历史不会改变最终状态，并保留缺目录/错owner/withdrawal/反向选择的检测。未获这一证明前保持全历史；不要把函数改名当成性能修复。 |
| 同路径 owner outcome 与 `QueryReads` | `_selected_accounting` 及 metadata/SQL outcome 验证已有 outcome工作；随后 heads函数直接 `SELECT id,kind,period,outcome,digest` 手工 `verify_outcome_bytes`，没有将此次成功结果放进 `QueryReads._verified_sql_outcomes/_raw_calculation_outcomes`。48 月另一个 `SELECT c.id,c.outcome,c.digest` 聚合6 calls、145行、1,132,004bytes。profile `verify_outcome_bytes` 241次。 | 候选由 QueryReads同快照承载“owner exact outcome 已验证/已解码”和成员目录成功证明，heads消费者读取精确缓存值，完整 member-content proof仍独立。聚合SQL不记录绑定IDs，不能宣称这145行全部与96行重复；交集需后续绑定级证据。绝不跨请求cache。 |
| `dashboard.py:4375–4439` latest折叠及 `asset_members_many` | 先全部events排序，再保留每卡 latest positive/period/batchref。对最终相关 owner再做完整 members核验，检查 exactmember及asset_id，使用已验证 member.summary 的zero_reason。48 月 `asset_members_many` 2次 inclusive12.352ms，`_validate_members`49次 inclusive9.514ms；后者49是实际批次验证，不是重复调用次数的直接证明。 | 可以把已成功的同owner完整证明复用，保证最新状态与完整结果同source；不要只看最终member summary或目录seal降低证明。要区分先前仅directory身份证明与此处 full-member result证明。 |
| `business_queries.py:706 _selected_asset_members` / `asset_batches.py:47 frozen_members_many` / `query_reads.py:809 asset_members_many` | fallback及activation成员完整历史按owner批读，再将所有成员metadata加载后过滤。目录完整性、成员seal、依赖、foreign ownership、outcome digest、summary、owner拼接分录/余额均校验。完整数据也用于fallback累积charge及reversal。 | full-history路径必须继续存在。若该owner已在本快照完整验证，允许共享；仅identity proof不能命中full-members cache。metadata可在owner完整验证后按真实消费members缩到精确kind/subjects/assets，完整批证明仍保留。 |
| `dashboard.py:4335` 独立 `asset_consumption` 汇总 | 无批次的独立consumption从完整 journal筛 kind，核验outcome后按asset聚合；与member charge不是同一publication lane。即使本剖析没有独立consumption行，选择SQL仍有VM工作。 | 保留能力。更窄候选必须保留独立、opening、disposed、zero reason和冲正，不能按旧名称删除。没有实际返回行时减少source selector开销属于候选，不证明存在错误范围。 |
| `dashboard.py:4441–4495` 卡片全量摘要再20分页 | all_items中cost、charge、bookvalue及status用于金额摘要与过滤；显示页才读 typedfact、parties、management、source details。 | 全卡片标量摘要是真消费；分页可限制正文，不能限制汇总证明。资产page/calculation已分层，不重写成通用“所有操作分页”。 |
| `dashboard.py:4495–4537` source、settlement详情 | 只为selected卡片追accepted/project来源、该卡lifecycle及清偿。清偿按source，明确整批成本来源不分摊为卡付款。 | 保持来源语义及精确entity scope。单卡历史详情未来可按验证键分页，但不能以卡片head替换整批资金归属。 |

## 同根范围、brief复用及固定 v1

`QueryReads.snapshot`（`query_reads.py:489`）的缓存是一次受控只读事务范围，退出清空；常驻池只复用连接，不证明跨请求内容。`asset_members_many`已有精确owner cache，`close_accounting_many`和adopted-only有不同scope cache。`Snapshot.asset_member_events`（`dashboard.py:397`）仅在无条件完整结果已存在时由该结果筛子集；其他cache按 `(kinds,subjects,asset_ids)` 区分，不能把任意子集当作全集或由子集宣布缺失。

同类使用 `verify_hits` 的调用包括：`entities.py:176 employee_entities`、`dashboard_funds.py:530`资金party归属见证、`dashboard.py:3384 _workforce_cost_from_rows`，以及 `business_queries.py:2342`、`discovery.py:472`和`entities.py:447`的源引用读取。`dashboard_owner.py:144 business_profiles`则从精确adopted/latest事实取实体引用并按kind批取profile；员工展示也走counterparty精确身份，不应为了复用名单额外引入employee名单查询。候选成功proof复用应有明确 `registry/content version`、connection、fact_id、current/recorded边界；完整校验/维修仍对请求全量事实运行，不能从普通列表缓存推导“全库complete”。这不是新增通用框架的理由，先在entity reference职责内部处理内容验证和current-binding reuse。

当前brief不调用 `_employees` 或 `_assets`；main48 profile也没有这些函数。`_long_term_assets`（`dashboard.py:4140`）与 `_brief_workforce_cost`（`:3407`）均只有定义、无当前调用，不能据这些相似代码把其asset identity/工资名单成本算入brief，也不应添加一次全卡/员工枚举来“统一复用”。后者自身只用当期工资金额，不构建员工全历史名单。brief活动页、funds、open_items、当期amount会共享当前快照proof；profile的brief slice24→96和funds23→95提示另一类按月采用权威增长，不能归给本审计员工名单路径。报表outcome行保持1,006但JSON loads685→4,441，需由报表历史选择/关账读取审计解释，不以员工修复宣称其会自动通过。

固定v1对应路径如下，逐项保留只读历史语义：

| 固定位置 | 对应职责及决定 |
| --- | --- |
| `content_v1_semantics.py:289 v1_current_bindings` 对应 `entity_references.py:415 _current_bindings` | 都按精确subject/path、rowid顺序追retained/reinstated/opening correction；不是entity全局alias。每条change对全部output迭代，复杂纠错历史可能产生facts×changes工作，但本次main48只有current版本路径，未证明纠错链是热点。候选只预分组受该subject链影响的row、同correction plan成功解码复用，原先后顺序及恢复owner链不得改变。固定v1不随意回写实现/内容fingerprint。 |
| `content_v1.py:223 load_v1_fact_data_many` 与 `storage.py:360 fact_data_many` | 原typed历史字段由固定schema解码，不跑当前Fact默认值。后续role proof复用必须保留版本选择与完整原始事实digest；不能把当前raw字段映射作为v1authority。 |
| `history_reads_v1.py:20 V1Reads` 与 `QueryReads` | v1具有fact、calculation、parent及relationcache；`parents`按key直接读，不复制全集；fact/data按请求批读。没有本次main48对v1的profile，不能主张这里是现有热点。 |
| `query_reads.py:1707` released reader分支 | 非current close reader走每月完整 `close_accounting`，不能把current adopted-only权威包交给v1或把`subjects_by_period`候选优化冒充v1已支持。 |
| `asset_membership_v1.py:14 frozen_members` 与 `asset_batches.py:58 _checked_members_many` | v1按owner读取完整目录、owner/memberbody及分录拼接；current已批读。若优化当前batch proof，v1规则仍固定；没有授权从当前head缓存推导v1成员完整性。 |
| `asset_card_adoption_v1.py:73/:137` 与 `asset_card_adoption.py:73/:158` | accepted card与batch的唯一parent、明确manifestrole、完整_card_shape采用关系不可丢。两个文件相似不等于都是本剖析调用路径；无batchcard历史profile不能据此优化冻结合同。 |

## 下一步候选及未验证

建议先收敛不改变身份范围的三项：精确IDs驱动的role/head查询；entity-reference职责内同快照成功fact/role/current纠错proof复用；资产owner原文/唯一JSON解码与不同等级成员proof复用。随后再评估资产最终事件选择、员工详情先分页后水合：这两项需要完整证明被省略历史不会影响身份、摘要或状态，不能从耗时反推业务可省。

初始profile JSON没有SQL绑定参数或EXPLAIN QUERY PLAN。父任务随后提供 `.tmp/stage9-owner-plans-r64.json`，由实际五页调用的真实绑定生成计划，仅保留SQL、调用方及计划，不保存参数。员工 `current_role_matches` 的计划明确为 `SEARCH entity_reference_current USING INDEX entity_current_role (role=?)`，再执行请求事实的list/Bloom筛选。该计划资料的EXPLAIN会增加VM工作，不能拿其中计数替代前述baseline或页面验收。

身份组已经完成下文的定向实施与验证；其他候选的实现及收益、12/48页面重新验收、source/contract复验、不同人员规模及复杂纠错链、unknown/retired/ended完整名单矩阵、资产opening/disposal/fallback及独立/批次consumption仍未由本组验证。既有 `test_employee_frozen_cohort_reads.py`、`test_asset_identity_reads.py`、`test_asset_batch_reads.py`、`test_history_reads_v1_warm_cache.py` 等说明需保留的语义，但不在本组45项收敛运行内。不能把下文定向结果扩大为全部候选或五页验收通过。

不重试已回退的24月案例或跨请求cache；不新增空库、重放业务、改冻结结果/合同或为通过验收削减必要proof。

## 身份组实施决定

父任务已授权实施本同类组；只编辑 `entity_references.py`、`dashboard_reads.py`、新增定向测试和本文件。其他组独占 `business_queries.py`、`close_storage.py`、`query_reads.py`；资产完整成员正文复用由父任务处理。

已将 `current_role_matches` 和 `payroll_head_identities` 的 recorded/current 精确fact查询改为请求IDs驱动，并显式使用既有 `(fact_id,path)` 主键索引。增长回归实测发现仅改CROSS JOIN时SQLite仍选role索引，因此不能仅靠JOIN顺序宣称有界；最终计划确认为事实主键lookup。`verify_hits` 的完整来源、全部角色、current correction 检查继续执行，不新增DDL或修改固定v1规则。`employee_entities`、资金party witness按entity与role/period定位，discovery按公开角色过滤发现来源，Entities usage按entity定位，本来具有不同消费范围，不强行改成已知fact查询。CLI/MCP仍走共同内核，fullverify/repair仍完整核验/重建，v1仍选择固定来源解码及绑定语义。

采用头身份验证现利用本调用已经成功得到的精确 `metadata(state=False)`：该方法对每个请求 ID 检查 calculation 存在，以及 `(subject_id,kind,period)` 与源 fact/subject 一致。已把 seal 检查并入已有精确 publication 查询，取消重复传输整组 calculation/fact 身份字段的独立查询；保留 metadata 与 head 全字段比较、全部来源两种seal、精确publication/current/successor及独立frozen adopted leaf。只有同一次受控读取调用中先完成的事实身份可复用，不新增成功身份cache，更不写入任何全文verified cache。按真实月份预计算 `YearMonth` 字符串减少逐头重复格式化；不推断或改写冻结期间。没有宣称解决 `QueryReads.metadata` 自身的全部成本，也没有因own-time推断GC问题。

新增 `test_entity_role_bounded_reads.py` 与 `test_adopted_head_identity_batch.py`。收敛命令为仓库虚拟环境运行这两文件及 `test_employee_head_adoption_reads.py`、`test_employee_role_identity_scope.py`、`test_entity_reference_entity_only.py`；结果45 passed，184.23秒，日志 `.tmp/stage9-identity-r64-directed.log`。两个生产文件和两个新增测试的 `ruff check` 通过。测试含完整原事实、source_digest、role、遗漏、真实current纠错归属损坏，原fact主体/kind/period损坏，head全部身份字段，既有seal/current/publication/冻结叶、旧零行/late review和身份成功不进入全文proof的负例。

24个请求事实增加100个无关对象、各3个历史修订前后：7 SQL、144行、10,870字节，各JSON/decode计数为0；VM 2,300→2,400，在100指令采样误差以内，实际计划为 `SEARCH r USING INDEX sqlite_autoindex_entity_reference_current_1 (fact_id=?)`。另增加40个无关对象、各3个历史修订：冻结recorded角色读取21 SQL、24行、8,939字节、VM200均不变；开放current角色读取22 SQL、18行、3,305字节、VM200均不变，对应两表均为fact主键lookup。冻结例实际传输1个本期outcome、开放例0个，前后不变，未隐藏消费正文。

这些是定向同类增长及语义验证，尚不是新包官方content验证、同源12/48实际五页或最终性能验收；本组未运行大回归或pure。
