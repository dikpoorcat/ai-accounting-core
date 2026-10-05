<!-- @format -->

# 资金历史账户身份的必要读取范围

`FundsRead.__init__` 原先选择截止月内全部 `bank_statement`／`bank_reconciliation` 状态。`account_summary` 对旧冻结状态只消费银行账户 ID，以保留零余额、停用和本月无流水账户；金额来自既有完整 period balance 贡献，本月流水和对账仍消费实质来源。全部账户必须发现，并不等于每账户所有历史状态正文都必须装载。

## 实际样本及修改边界

对 `.tmp/stage9-owner-main48-r63/91310000123456789S/company.sqlite` 只读查询显示：登记银行账户 1 个，流水及对账各 48 份，分别覆盖 48 个 posting 月／48 个 subject；96 个 recorded 银行角色均指向该账户；身份纠错收据为 0。`bank_opening` 只有首月 1 条，当前没有证据需要缩它的历史范围，故全部保留。这个样本能满足账户正覆盖 guard，不据旧 profile 的 init 102 毫秒承诺实际节省。

生产修改仅为 `dashboard_funds.py` 的私有资金账户范围 helper、原 selector 的 subject 参数及实际 fact ID 驱动的角色 locator。继续使用 `_selected_accounting` 对选中主体核精确计算头、完整结果、来源身份和独立冻结采用，继续使用 `frozen_bank_account_identities` 对旧见证核实际 scalar、注册类型及与冻结摘要绑定的结果。没有改 `query_reads.py`、金额聚合、voucher lines、公共查询语义、DDL 或冻结根。

## 正覆盖及失败边界

实际 publication／冻结 reference／current 指针先定位全部 posting tranche，LEFT JOIN 保留缺行候选；消费所有 `heads`，不使用其按 subject 的最新排名丢弃旧 tranche。现有 `verified_adopted_head_identities` 的独立开放 terminal 检查仍核所有银行种类，防止坏 current 指针因账户已有冻结见证而隐去开放状态。

完整候选的真实 calculation／fact／publication 头、封存、主体／种类／source period／posting period先核对。银行角色必须与实际事实 scalar、实际期间和事实摘要一致，银行实体必须是实际登记的 `fund_account / bank`。目录和这些头只定位范围，不形成正文成功标记。

登记银行全集是正覆盖 guard，绝不是正式账面账户输出。只有每个登记银行均由实际候选正覆盖才缩范围；登记草稿账户无正见证会返回原完整 selector，不把草稿加成正式账户。角色缺失、格式或身份冲突、登记覆盖不全均完整回退，不能把派生缺行当权威不存在。缺 publication／calculation／seal 的候选在缩范围前拒绝。

每账户选择最早实际 posting 的精确见证，同期优先流水。原 selector 随后核该主体的实际采用；全部开放状态、posting 为本月或 source 为本月的流水／对账主体均加入范围。所有 `opening_package / opening_bank / opening_cash / bank_opening` 主体完整保留，unknown opening 及其账户传播、opening binding 和已退役 opening 的原处理仍在原路径。

任一 subject 存在多 posting tranche、任何身份纠错收据或撤去候选时，当前实现完整回退。这个保守纠错 guard 也会使无关身份纠错回退，尚未实现只核银行纠错范围。固定 registry v1、`historical_content(1)` 的实际 reader、非 owned、disabled 或非当前 reader 使用完整路径。没有跨请求缓存和新的成功证明集合。

## 同职责排查与未消费正文

`FundsRead` 只由页面资金投影调用，资金页及简报共享该职责；显式 statements 明细仍消费本月真实来源。现金、平台、投资、期初和金额贡献没有按银行身份缩小。CLI／MCP 的业务历史、完整 verify、关账、修复和备份仍使用原公共完整路径，未改它们的证明范围。

未选中的旧冻结 outcome／原始 children 不再是默认账户名单的消费正文，损坏须由 core／fullverify／备份拒绝，其自身月份的资金页仍实质消费并拒绝。已有历史 children 负例保留；新增未消费 outcome 例明确检查默认业务响应不变、fullverify 拒绝及自身月份拒绝。坏 scalar／角色使候选 A 映成 B 时，独立 A 正见证不能消失；不允许对照 mutable body 与它自己重算的摘要证明合法。真正消费的 witness 的自洽正文＋digest 改写仍由独立冻结叶拒绝。

## r68 实际扫描遗漏及同根因修复

前 scope 组只覆盖小样本 VM，没有检查真实主 48 月 locator 的访问路径，这是同类排查遗漏。r68 固定候选实际资金页 600–675 毫秒、简报 809–896 毫秒，均明显回归；原件 `.tmp/stage9-resident-r68-main48-profile.json/.log`，不能以 scope 测试通过替代真实性能验收。

实际角色 SQL 用 `r.fact_id=c.fact_id AND r.role='bank_account'`，SQLite 选择 `entity_recorded_role(role=?)`，对每个候选扫描整个公司该角色集合。主 48 月有 24,097 条银行角色、79,476 条全部引用，96 个必要候选只返回 26,448B，却执行 13,884,500VM；profile 中范围 helper 达 324–356 毫秒。保留全部必要字段，不能把返回行少当作访问范围有界。

修复在原 LEFT JOIN 指定既有 `(fact_id,path)` 主键索引，按实际 fact ID 的前缀查询所有匹配银行角色；没有限定为一个正确 path，仍能发现错 path 或重复角色并完整回退。role、实际 scalar、角色摘要与事实摘要、封存／发布、正覆盖等保护全部保留。固定 v1／非 owned 等原完整回退不变，没有新 DDL、索引、根或缓存。

准确主 48 月 96 个 ID 的参数为 6,625B，记录 hash 和完整访问计划在 `.tmp/stage9-bank-role-plan-r69.json/.log`。生产常量 SQL 与 r68 原 SQL 的请求参数、96 行、26,448B 和完整结果 digest 相同，访问计划变为 `sqlite_autoindex_entity_reference_recorded_1(fact_id=?)`，实际 VM 降为 **5,200**。这只证明 locator 工作量修正，不承诺整页时间。

| 同类 consumer | 必要范围及实际证据 | 决定 |
| --- | --- | --- |
| 资金候选角色 locator | 每个实际候选的全部匹配角色；原角色集扫描已在真实参数复现。 | 改 fact ID 前缀索引，保留全部字段及失败保护。 |
| 员工 heads→role JOIN | 同 fact ID＋role 形态；员工范围由其负责智能体核实际计划，未用相似代码宣布确认或排除。 | 交根节点／员工组处理，不修改该文件。 |
| `entity_references.verify_hits` | recorded/current 两种真实 96 事实计划都按 fact ID 主键；recorded 为 1,800VM。 | 保留；没有同扫描证据。 |
| 业务详情引用列表 | 真实主体参数按 subject→fact 主键→recorded fact ID→current fact/path 主键。 | 保留必要完整详情。 |
| 资金期初退役见证 | 真实账户参数按 `entity_current_lookup(entity_id)` 查最新实际来源。 | 保留。 |
| 员工登记 membership | 真实截止月计划按 `(entity_id,role,period)` 组合索引；不是每 fact 扫角色全集。 | 保留；员工 heads 的另一访问点单独核查。 |
| Discovery／fullverify／backup／repair | Discovery 按实际查询实体／角色范围选对应索引；完整引用核验本来消费全部记录。 | 不按 locator 职责缩小。 |

独立计划原件为 `.tmp/stage9-bank-role-consumer-plans-r69.json`、`.tmp/stage9-bank-role-current-hits-plan-r69.json` 和 `.tmp/stage9-bank-role-entity-plan-r69.json`。除资金修复外未修改上述生产文件。

## 验证记录和实际工作量

首轮新范围及两组直接受影响历史资金测试为 **48 passed，211.95 秒**，完整 command/stdout/JUnit 在 `.tmp/stage9-bank-identity-witness-first.log/.xml`。旧 local carry 的工作量断言随实际消费范围由四个历史状态改为两个银行正见证；业务金额、三 tranche、缺 publication、children、core／备份拒绝及 v1 的原负例未删除。

静态审查后加入独立开放 terminal guard 和实际角色摘要对照。最小补组为 **3 passed／1 failed，30.43 秒**，原件 `.tmp/stage9-bank-identity-witness-followup.log/.xml`。失败属于 redirected pointer fixture：指向另一 subject 仍占用的 current ID，先触发 UNIQUE，没有到达业务断言。随后改成真实开放期第二版本留下的旧 ID，仅补跑两个 pointer 负例，**2 passed，13.81 秒**，原件 `.tmp/stage9-bank-identity-witness-pointer-repair.log/.xml`。真实银行身份纠错回退和最终 guard 下的工作量单例已在补组通过；不将局部补跑改称最终整组通过。

开始扩大的 final 组在根节点明确要求最小补跑后停止，仅四个进度点，没有完成结果，不计通过；中止原因保存在 `.tmp/stage9-bank-identity-witness-final.log`。

| r68 初稿两银行、两冻结月的 funds＋brief 实际工作量 | 完整 scope | r68 guard 的见证 scope |
| --- | --- | --- |
| `FundsRead.states` | 10 | 4（2 个完整 bank starts＋2 个见证） |
| saved outcome 实际传输／decode 次数 | 24 | 12 |
| saved outcome 字节 | 4,556 | 2,072 |
| accounting slice reads／采用行 | 10／40 | 8／16 |
| 总返回行 | 295 | 255 |
| 总返回值字节 | 79,418 | 66,940 |
| SQLite VM | 18,400 | 28,600 |

这个小样本增加 10,200VM，曾没有触发对真实角色索引驱动的核查；它不足以覆盖访问范围随公司历史增长的问题。该历史工作量 property 保留，不重写成修复后结果。

角色 locator 修复的最小新例及受影响组合首轮为 **11 passed／1 failed，63.40 秒**，原件 `.tmp/stage9-bank-role-locator-r69.log/.xml`。唯一失败为增长 fixture：第 13 份同月同金额草稿 funding 触发真实重复业务审核，尚未到工作量断言。改成 600 份不同金额的实际 typed funding，覆盖 600 个无关业务对象和 12 个未来月份，只补跑增长单例，**1 passed，212.64 秒**，原件 `.tmp/stage9-bank-role-locator-r69-growth-repair.log/.xml`。不将该局部补跑改称整组重过。错 path／重复角色仍可见、正账户响应等价、六种回退、两个坏开放 pointer 和 witness 自洽正文＋digest 损坏均在首轮通过；最终两个修改文件 Ruff 通过。

增长回归直接运行生产 locator 常量及准确实际 ID 参数，没有镜像它的保护逻辑。600 个无关来源增长前后，新路径均为 **1 SQL／4 行／850B／200VM**，真实计划按 fact ID 主键；同结果的原 SQL 从 **300→14,700VM**。实际计数、参数和计划保存在修复单例 JUnit property，证明工作量没有因无关角色历史增长。

主 48 月实际 init、资金页／简报及下一次统一 native 验收由根节点固定共同源码后运行。本组未跑原生六页、浏览器、规模重建、构包或全套回归，不宣称已达 500 毫秒。
