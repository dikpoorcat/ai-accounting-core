# r62 老板页面历史对象身份读取审查

本记录为 Stage 9 窄范围静态审查，读取了 `AGENTS.md`、`FRONTEND.md` 和相关实现、测试。未修改实现，未运行测试、性能测量或真实资料/服务操作。它不是修复结果，也没有排除损坏风险。当前体系仍为 draft / 0，正式 v1 尚未冻结；隔离合成验收不构成真实库操作授权。

## 根因与当前调用

当前固定 profile 为 `.tmp/stage9-resident-r62-profile.json`。已有观察为员工请求传输 650 个结果正文、约 3.59 MB，仅解码 100 个；本月 50 名员工含工资及奖金，历史覆盖 12 月。本次没有重测该观察。代码显示，少解码不等于少读取正文，历史身份检查仍传输并散列旧 outcome。

`Dashboard._employees` 调用 `payroll_head_metadata(..., line_count_period=snap.period)`；后者委托 `adopted_head_metadata`。它按冻结高水位及开放期当前头选取每个 subject 最新采用，只有本月头调用 `verify_sql_outcomes` 并读取 `line_count`，历史返回 `None`。本月行数用于区分无凭证工资，属于实际消费的结果内容。

随后 `_employees` 把全部 `wage_heads` 交给 `verified_payroll_heads`。该 helper 先读无状态 metadata、验证冻结 adopted leaf，再把尚未验证的闭月 `c.outcome` 传输到 Python 并做 SHA256；非规范 JSON 进入严格解码回退，开放期头调用 `verify_sql_outcomes`。默认员工名单只消费返回的 subject/id/fact 身份对照以及付款来源身份检查，不用旧 outcome 生成历史卡或本月金额。这是“采用身份核验”承担了“未消费正文核验”的读取职责问题，不能仅靠减少 JSON decode 解决。

所有历史头都会影响名单 `known`，包括无行工资及身份纠错后合并的人员。不得只核验最后一个代表头。开月通过 `current_role_matches → verify_hits → _expected_rows` 校验原事实内容、完整对象引用集合及 current bindings；闭月通过 `payroll_head_identities → verify_hits(recorded)` 校验原事实及 recorded 引用。已使用的 fact/person 身份不能被省略，也不能把纠错后的当前对象归属替换成原始 employee_id。

## 五页及核心范围

| 范围 | 当前用途与审查结论 |
| --- | --- |
| 员工默认页 | 本月金额、行数、代发和未清余额需要准确来源；历史名单身份需要全部精确 fact/person 与采用身份。完全未消费的历史 outcome 是可收窄候选。 |
| 命中员工详情 | `_employees` 只为明确打开的 employee 选择工资历史并分页；被展示或实际用于清偿结果的正文继续严格核验。不能把默认列表省正文称为完整历史核验。 |
| 资产默认页 | `_asset_card_sources → calculations_of_kind → Calculations.selected → _selected_accounting + metadata(state=True)` 在分页前解释历史状态/结果。对象筛选和数量主要消费已验证的 asset_id/type/cost、启用/处置事实与采用身份；该选择链有收窄候选，但不能整体省正文。 |
| 经营简报 | `_long_term_assets` 复用 `_asset_card_sources` 取得对象数量和状态，承担相同身份选择成本；账面汇总及项目成本有独立金额读取，必须保留证明。 |
| 资金 | 既有 `frozen_bank_account_identities` 可作职责参照：只避免未消费的历史 fact children，仍读取实际提供银行身份的结果并绑定独立采用。不能自动套成工资省 outcome。本次未重新审查整页。 |
| 财务报表 | 本次没有证明报表存在相同未消费正文；报表预览/导出及采用来源仍按现有严格路径处理。 |
| CLI/MCP 核心 | `business_queries._selected_accounting` 和 Store selection 继续承担业务状态、金额、详情及精确关系核验；建议仅改变老板身份投影职责，不改变核心完整读取。 |
| 完整核验、维修、backup | `integrity.verify_integrity → _check_sources/_check_closes`、`maintenance` 和 backup 的 `verify_file/schema_bundle` 保留完整源与历史检查。它们不能信任身份投影成功标记来跳过正文。 |
| fixed-v1 | `QueryReads.close_adopted_results_many` 在 historical-v1 context 回退该版本完整 `close_accounting`；不把当前身份投影职责套进已固定的历史证明路径。 |

资产中实际消费正文的路径包括 `_assets` 对取得 publications 的 `balances` 核验、`_project_cost_balances` 的余额效果、折旧金额及 `zero_reason`、命中处置卡的结果值、项目卡的 `capitalized_fen`。这些历史读取有业务用途，不能归为未使用正文。资产候选集合与现有核心 selector 的等价性尚未证明。

## 现有行为测试

`tests/kernel/test_employee_head_adoption_reads.py` 明确覆盖：晚复核后的冻结头与完整选择一致、冻结结果正文及 mutable digest 不能替换独立 adopted digest、重复 JSON 成员拒绝、非规范但等价 JSON 回退、默认页与完整头选择一致、规范旧结果不 decode，以及旧开放期工资的 calculation/fact 封签、fact、kind 损坏拒绝。

其中 `test_frozen_wage_head_bytes_require_authenticated_adoption` 保护现 helper 的旧正文拒绝语义。改成身份职责会有意改变“默认身份读取检查未用旧正文”的范围，需明确调整此行为测试；不能宣称行为完全不变。`test_payroll_head_line_scope.py` 保护本月行数范围，仍应保留。

## 推荐职责边界

工资身份读取可核对精确 calculation/fact/subject/kind、来源期和入账期、双方封签、发布记录与当前头关系；闭月继续绑定独立冻结 adopted leaf 的 publication、subject、fact、period、result digest。保留被使用的原事实及完整 person 引用核验，但不传输完全未消费的历史 outcome。该成功证明不得写入完整结果/内容证明集合，也不得用跨请求业务 cache 或版本号代替核验。

本月金额、无行判断、详情命中、清偿 fallback 实际使用的结果保留完整核验。缺 calculation/fact/封签、错误 kind/subject/period、缺冻结 leaf、坏 fact/person 引用仍由身份读取拒绝；仅未消费的历史正文损坏可留给详情、核心业务选择、完整核验及 backup 拒绝。这是局部读取职责，不是承诺默认页面发现全库或完整历史损坏。

## 未验证项

- 新身份职责的全部缺损负例，特别是开放期 publication/current-head 损坏和冻结 adopted leaf 缺失。
- 身份纠错后冻结工资映射与清偿 cohort 的一致性。
- settlement fallback 实际读取正文的范围及是否重新进入全部历史选择。
- 资产 metadata 候选与原 selector 在撤回、冲正、无影响复核及批次成员关系下的等价性。
- 页面金额和状态不变、核心/完整核验/维修/backup/fixed-v1 不退化的隔离验证。
- 实际传输量及性能收益；本次未运行测量。

本审查与文档记录在下一轮纯计时开始前结束，不把审查过程计入计时结果。
