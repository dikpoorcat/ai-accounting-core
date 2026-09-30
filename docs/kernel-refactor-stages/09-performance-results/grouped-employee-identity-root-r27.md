# 员工名单历史身份来源：r27 静态审查

范围：当前 `Dashboard._employees`、`dashboard_reads.payroll_head_metadata/payroll_head_identities`、对象引用、关账逻辑正文及现有派生根；另用固定 r27 源对主 48 月合成公司做了一次只读函数级诊断。没有改产品或运行回归。结论是**现有已冻结内容没有逐工资采用头的 `fact_id → employee_id` 完整承诺**；不能用目录行或余额根直接替换当前的逐命中事实核验。

| 路径 | 当前名单如何确定及核验 | 现有冻结内容能做什么／缺什么 |
| --- | --- | --- |
| 开放当前月 | `adopted_head_metadata` 按所有不晚于页面月的正式发布，已关月依各自高水位、开放月依 `calculation_current` 取每个 subject 最新头；含 `payroll`、`payroll_bounded`、`annual_bonus`、`opening_payroll_payable`。`current_role_matches` 对所有候选 fact ID 先 `verify_hits(identity_match='current')`，由原事实重建角色并核可修目录，随后才取 employee。 | 当前角色目录存 `entity_id`、`source_digest`，但目录可修且 `source_digest` 仅复制事实摘要，不把 `entity_id` 纳入不可变 close 根。仅凭目录取名单可把旧月 fact 的 employee 改成另一合法员工而摘要字段不变，漏报。 |
| 已闭页面 | 对每个候选工资头走 `payroll_head_identities`，先 `verify_hits(identity_match='recorded')` 核实际原事实，再对目录中的 role、kind、period、digest 和每个 fact 恰一身份核对。页面使用当时记录身份，不能套今天的 current 纠错绑定。 | 逻辑 close 的 `adopted_results` 承诺 publication、calculation、fact、subject、result digest、posting period 及 state-only/voucher role，但不含工资员工 ID。`owner_review.adopted_bases` 仅列计算／fact 摘要引用；工资确认卡仅列计算和确认 fact 引用，不含每个工资头人员。 |
| 关账管理快照 | `management_snapshot.employee_entities` 在关账时从对象 profile 和当时 `entity_reference_current` 产生，并由 close 私有管理根认证；冻结人员档案读取已利用它定位**候选实体集合**。 | 集合含与已采用工资无关的 employee-role facts、显式就业档案，也未编码每个工资 fact 属于哪个 employee。即使 A/B 均在集合，损坏某旧工资头由 A 改 B 仍无法被集合发现；因此不能直接代替每人最近采用头的选择、付款按来源归属及详情 subject 定位。`readiness[*].facts` 是引用 ID 集，不是角色映射。 |
| 清偿／余额／报表派生根 | 清偿 state 有源 subject/kind/fact、counterparty 与发生额；期间余额只有 category/key/金额；报表 party 投影依凭证行。 | 这些仅覆盖形成对应余额/义务/凭证的业务。零额、无凭证、无影响复核仍可贡献员工身份；缺源事件不能证明工资头不存在。不能把有金额的 counterparty 当完整工资名单。 |
| 显式 `employee_id` 与详情 | `_employees` 在分页和 `total_count` 前确定全体 `known`，包含所有 wage heads、当月实际付款、payroll profile、明确档案；即使显式人员，仍先核候选头。详情只对该人命中的 subject 装完整计算和历史来源。 | 可以在全体身份已认证之后把正文装载限制为该人的详情；不能先用未经核验的目录按人筛头，否则坏旧绑定会被筛出而静默缺失。 |
| 修订、无影响、闭期更正、撤去、期初 | 开放替换取新 `calculation_current`，撤去的头不再采用；`review_no_impact` 可沿旧凭证却有新无凭证结果，头仍须计；闭期更正有后来独立发布，按页面截止月/关账高水位选择；`opening_payroll_payable` 也具 employee role。 | 旧 close 只承诺当月完整采用，未给出跨月、经身份纠错后的完整工资角色映射。不能只从 vouchers 或 settlement 正金额反推。旧闭月的 recorded 员工不随当前身份纠错重写；开放当前身份则须按纠错链重新绑定。 |
| 身份纠错及完整核验 | `verify_hits(current)` 经 `_current_bindings` 逐项核纠错记录、来源/替换 subject 和期初绑定结果；`recorded` 保留原始角色。完整核验从所有事实重建 `entity_reference_{recorded,current}`，维修重建可修目录。 | 纠错审计可标记受影响对象，但未与现有 close 根合成一个证明“旧工资头映射及其不存在项”的完整目录。普通页若只看当前可修索引会绕开旧来源损坏。 |

## 当前读取成本与标量路径边界

固定 r27 源对已核验主 48 月合成库的预热后员工默认页做一次 `cProfile`，全请求约 0.574 秒；这是函数归因诊断，不能作为浏览器验收。`current_role_matches` 累计约 190 毫秒，其中两次 `verify_hits` 约 188 毫秒、`_expected_rows` 约 167 毫秒（这些是嵌套时间，不可相加）。本次请求解码 2,600 个事实字段字典：`Store.fact_data_many` 约 53 毫秒，`decode_fields` 约 19 毫秒；2,500 次引用提取约 38 毫秒，2,651 次事实摘要约 36 毫秒。角色证明读源、解码、提取、比对是同一条必要链，不能仅凭调用次数把这 190 毫秒全部称为可省工作。原始函数记录在 `.tmp/stage9-employee-role-cost-r27-r2.json` 和 `.tmp/stage9-employee-role-cost-r27.prof`。

四类工资模型都支持现有 `_scalar_fact_json_sql`，但其 `_scalar_fact_hashes` 是“规范 JSON 字节摘要相等就免去 Python 字段解码”的窄入口；直接移到员工身份核验会跳过 `decode_fields` 的损坏拒绝。此前该快路在非法年月存储值上未保持原拒绝语义，已经否决。若仍逐字段执行原 `decode_fields`、加载相同子项并计算原摘要，额外 SQL JSON 构造并不会减少必需的事实原文读取；仅免去若干 Python 行转字典和编码步骤，现有占比未显示超过 50 毫秒的安全净收益。当前不实施标量替代，也不把正常样本的摘要相等当作坏内容可免验的证明。

## 可行投影的信任边界与处理决定

在当前合同下保留所有命中工资头的 `verify_hits` 与本页精确采用证明。要把长历史名单改成“冻结基线＋开放尾”，需新增或改变现有私有格式的 close-rooted、按 fact/subject 可定位的完整工资角色目录；每个已采用头至少绑定事实摘要、recorded 员工、正式发布身份、适用期间及可验证缺失范围。按月增量封存，读时从已认证关账根定位并与开放权威发布及身份纠错链接续；完整核验、维修从原始事实独立重建。根与块损坏须拒绝，不能由可修目录缺行证明不存在。固定 v1 必须有独立读取规则，不能让未来 current 规则重解历史。

然而，新目录只能证明当时封存的映射。如果普通员工页仍须拒绝**任何一个已采用历史工资头的原始事实正文**被篡改，即使该头未在最终名单或付款详情中显示，页面仍须逐头读取并按原字段解码、核事实摘要。否则修改一个未命中的旧正文而不改根，页面无法观察到损坏。把未读取旧正文交给独立完整核验发现，是显式改变普通页既有检查范围，不能作为本次投影优化的隐含前提。以原范围不变为条件，新根最多省去部分引用提取／纠错遍历，不能消除历史事实原文的线性读取；现有函数诊断不足以证明新增结构的净收益。单改 SQL、复用 `management_snapshot.employee_entities` 或可修 `entity_reference` 也缺逐头身份绑定。该结构设计**尚未实施**；Stage 9 已授权技术性能改造，但任何改变普通页命中内容核验范围的提案都必须单独写明并验证。

若未来需要隔离原型，最小格式应以权威 close 的 `adopted_results` 建每月完整工资 fact 集及缺失证明，以 `(fact_id, subject_id, publication_id, posting_period)` 作键，叶子包含 fact 摘要、recorded employee、角色 path、业务种类、计算与发布摘要；只新增月差，未变叶复用旧块，不复制全历史。根由同次关账私有 `derived_roots` 绑定，读时先认证 close 根再按键/范围找块，开放尾从正式发布和纠错链构造，不能用可修引用索引定义空范围。完整核验和维修独立从 `fact_revision`、typed 原文、正式采用及纠错记录重建叶集合；坏根、缺叶、额外叶、错身份、错事实摘要均拒绝。隔离原型须比较期初、开放替换／撤去、无影响复核、闭期更正、身份纠错前后与当前／历史两口径，并分别报告“只核命中原文”和“逐个旧头原文仍核验”的工作量，不能把前者的速度记成既有口径的收益。

## 同范围的引用声明提前筛选

后来进一步定位到一个无需改变事实核验范围的独立重复：`_expected_rows` 仅生成 entity 引用，却让 `references_from_data` 对所有声明（含 business source）逐条遍历、造字典后再丢弃。`payroll`、`payroll_bounded`、`annual_bonus` 各有 1 条 entity、3 条 business 声明；`opening_payroll_payable` 为 2 条 entity、1 条 business。固定 r27 主 12／48 月私有完整员工页 ABBA 响应逐字节规范摘要一致，引用 `_at` 调用分别 5,793→1,487、20,057→5,063。48 月 `_expected_rows` 两次请求内 CPU 约 125→109 毫秒；整页 CPU 的 Windows 计时粒度约 15.6 毫秒，未把微小差值写成正式时延收益。纯引用提取在主 48 月实际 2,429 个工资输入上约 10.6–11.8→3.7–5.7 毫秒；把这些合成输入重复到 6,000 个的 120 月**等效纯函数规模**为约 26.3–28.7→9.1–10.5 毫秒，不是 120 月已核验公司页面。原始记录：`.tmp/stage9-entity-only-r27-ab-r2.json`、`.tmp/stage9-entity-reference-walk-r27.json`。

据此只在对象引用模块的内部 `_expected_rows` 与期初纠错回放中先按注册声明的 `reference_type=entity` 过滤，再遍历字段，直接形成 `(path, entity_id, role)`；原始事实 `fact_data_many`、`decode_fields`、摘要、身份纠错及目录全集比较均保留。公共 `references_from_data/references_for` 仍返回 entity 与 business 全集；固定 v1 仍调用原独立解释器，未改 descriptor。新增工作量测试用真实 SQLite 主／子 typed 表及实际 `Store.fact_data_many`，业务来源明细从 1 条增至 1,000 条时，实体输出与其引用遍历量不变，公共解释器仍返回全部业务来源，篡改子项原文仍拒绝。身份纠错查完匹配链后如无变化，直接返回原已核 list，省去为所有头建立未消费的 owners 映射；有纠错仍走完整旧分支。新增工作量／混合引用／嵌套和空值／坏摘要／空纠错 4 项通过；既有闭期工资身份、伪造角色及工资身份纠错 4 项通过。此项只省无关声明解释和空纠错的对象分配，不替代前述逐个事实原文证明，也不表示 120 月员工页已达 500 毫秒。

固定 r28 的 40 文件受影响组 JUnit 共 454 项通过，源码前后校验一致；[组回执与压缩 JUnit](grouped-open-fact-range-r28.md)同时覆盖本修改。相同主 12／48 月默认五页诊断的员工页 SQL VM、返回行与字节在 r27→r28 均不变，符合只减少 Python 引用遍历／空纠错分配的修改范围；响应业务字段相同。该诊断没有单独证明员工页时延收益，48／120 月纯浏览器目标仍待验证。
