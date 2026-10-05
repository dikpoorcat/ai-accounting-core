# 结果元数据类型筛选只读审查

2026-10-02，GPT-6.1 Sol／中等推理。只读核对生产调用链和已有合同；未运行测试、建库、性能、MCP 或服务。行号是审查时工作树定位，其他 agent 正在实施，不是固定 r60 源码的新验收。

## 已确认根因及公共修正边界

资金 agent 已实际证明：历史 bank_statement 的 calculation.kind 改为 expense 后，brief／funds 接受。`BusinessQueries._selected_accounting` 虽先按 subject.kind 定位业务主体（business_queries.py:428），冻结 state 路径却在读取所选 metadata 后先按 calc.kind 跳过（约 614），之后才检查 direct_adoption；已消费的历史银行身份因而退出验证。当前 state 路径又先按 calc.kind 排除 NON_ACCOUNTING_CALCULATIONS（约 666），也存在先筛后验证的同类结构；后者未单独动态证明。

`QueryReads.metadata`（query_reads.py:647）是合适的窄公共身份边界：所选 ID 本就关联 fact_revision，增加 f.subject_id／f.period／subject.kind 标量，校验 c.subject_id=f.subject_id、c.kind=s.kind、c.period=f.period，之后再把值交给消费者或缓存；不读取事实／结果正文，不枚举额外历史候选。资金 agent 已按 root 分工实施此公共修正，本审查不把实施写成已通过。

上述三字段约束没有发现合法例外：schema.py:305 的 calculation_fact_owner 对所有 calculation INSERT 强制同一规则；integrity.py:343 对所有来源再次核对。身份纠错／终止仍绑定自己的 Fact。资产 activation／consumption member 只允许缺独立 publication，现有 metadata 白名单与 asset_batch_member 采用证明应保留，不是主体／类型／期间例外。普通服务读取 released/1 库仍使用当前 QueryReads.metadata，可共享这一标量身份边界。固定内容核验另有独立 `history_reads_v1.V1Reads`，不是当前 QueryReads 的子类，也没有 metadata；position_v1／close_review_integrity_v1／report_projection_v1／report_semantics_v1／settlement_projection_v1 显式导入它。verify_v1_company 先以固定 registry 完整核所有来源身份，再进入这些 stored proof。不能为公共 metadata 改动替换 V1Reads、v1 decoder、冻结采用或固定版本语义。

## 实际路径与范围

| 路径 | 类型权威／实际验证顺序 | 结论与边界 |
| --- | --- | --- |
| brief、funds 默认银行身份 | FundsRead → _selected_accounting(kinds=银行／期初集合) → 冻结 state metadata → calc.kind continue。dashboard_funds.py:83／365。 | 银行类型被移出集合的接受缺口已由资金 agent 实证。公共 metadata 身份认证可在 continue 前拒绝，原则上可替代新增 adopted_head 双定位；保留冻结 bank identity 证明，不能只检查元数据而删存储锚。 |
| 其他 state 消费、明确业务详情及 core business_status | Calculations.selected（dashboard_reads.py:953）、opening_selection（dashboard.py:345）、业务状态（business_queries.py:1944）复用同一 selector。 | 公共修正覆盖已选 ID 的先筛风险，不要求完整页面扫描无关历史。当前头 NON_ACCOUNTING 排除也会先过 metadata。具体其他业务损坏接受未动态证明，不能按银行结果宣称全域实证。 |
| Journal 按类型子范围 | Journal.sql → selected_voucher_sql；query_reads.py:267 按 sc.kind 驱动，326 又按 vc.kind 过滤。员工 payroll／labor／project-cost、资产取得／折旧细目实际调用这种选择。 | 候选可能在 metadata 前已被漏掉，公共 metadata 修正不会自动覆盖负面选择。root 已分配另一 agent 实际入口验证与公共修正。本审查未证明每一页面接受；不以 SQL 相似替代结果证据。 |
| 员工默认工资／劳务 head | adopted_head_metadata 使用 publication.subject_id 对应 subject.kind 定位（dashboard_reads.py:159–167），再有来源身份／封签／冻结采用核验（约 249–323）；dashboard.py:3451／3891。 | 已有权威发现路径，不能因为其后 metric 子 SQL 有 c.kind 就宣称默认员工页存在接受缺口。身份纠错回退、已登记历史采用仍必要。 |
| 资产 batch/member 与金额快路 | _selected_asset_members 会按 calc.kind 筛成员（business_queries.py:740）；前面 frozen_members_many → _validate_members 已核固定成员 kind/subject/fact/period（asset_batches.py:113–151）。资产快路 c.kind 折旧过滤（dashboard.py:4266）之前有 verify_selected_balances。 | 成员单改 kind 已有精确采用身份拒绝，不能列作同一个已证漏读。快路是否对每个源正文／身份均提前认证需结合实际损坏验证；其他 agent 正在处理，不在本审查改写通过结论。 |
| 资金投资清偿 | dashboard_funds.py:1299 仍按 c.kind 筛投资来源；此前 1274 左右先提取付款直接引用的完整 source_ids，再 verify_saved_input_identity／verify_sql_outcomes。 | 先认证后解释类型已有同类修复，不能把尾 SQL 的 c.kind 自动认定成漏核；其原实际投资负测仍按原回执解释。 |
| context | Dashboard.context 只读期间、公司身份和当前上下文（dashboard.py:1643），不构造 FundsRead 或承诺完整业务内容 verified。 | 不要求 context 验证未消费历史 bank_statement；context 成功不构成完整内容通过，也不构成本次已证接受漏洞。 |
| 季度／core report | Reports 的分类／税务事实候选多按 subject.kind；opening/current sources 仍经过 _selected_accounting（reports.py:2173）。 | 已选 state 的类型身份应从公共 metadata 获益。季度仍有独立来源／映射／冻结证据，不因 owner 来源列表退出而删。固定 v1 report_projection_v1.py:850／939 的 kind-only selector 同样有先筛结构，但公开历史 verifier 先全源认证；未证明它能绕过该前置认证，不能据此改写原 stored-proof 规则。 |
| CLI／MCP／HTTP | service.py:702–729 使用同一 Dashboard／BusinessQueries；command_schema.py 也路由 core report／业务／维护入口。 | 没有另一套自动避开 selector 的适配层。实际负测须区别 owner 默认页与核心详情；不能把其中一个成功写成全部入口已验证。 |
| 完整核验、repair、backup 与 released v1 内容 verifier | integrity._check_sources 在全核验 calculation_ids=None 时枚举全部 calculation（272），_check_facts 按 subject.kind 读取事实（152–172），343 检查 calculation↔fact 身份。完整核验调用它（1290）；maintenance._repair 先完整验证源、仅忽略待修投影／目录（46）；backup.py:166 按结构版本选已登记 company verifier；content_v1.py:559 固定 historical_content(1) 与 v1_registry。 | 这些全源入口不靠同一个 c.kind 负筛发现 calculation，类型篡改应被既有身份核验拒绝，不能把页面漏洞推广成 full verifier／repair／backup 已接受。此结论是静态调用链，不是本轮重新运行。 |

## 外部办理真实入口、Store 消费边界与最终修正

- `BusinessQueries` 外部办理关联：约 1471 用当前 c.kind 选择义务定义，1505 用 c.kind='external_completion' 发现 related completion 后才 prime_calculations。r61 独立合成诊断已证明实际缺口：使用现有 workflow／payroll fixture 保存工资、义务、真实采用提交证据和完成事实，正常发布后只把 completion 的 calculation.kind 改为 expense（恢复表触发器）。`business_status('january','2026-01',as_of='2026-02-28')` 的 external.completions 从 1 条变为 0 条，external.status 仍为 completed，obligations 内 recorded_completions 仍为 1 条；没有身份错误。这个发现应改为按不可变 subject.kind 定位相关候选，再进行现有身份／正文证明，不扩大为全历史枚举。当前 c.kind 义务定义筛选是否能在另一实际业务链漏掉整个义务，仍未单独动态验证。
- 实际 `Workflow.query` 同一损坏后也返回 completed。它的 `_external_obligations`（workflow.py:1088–1151）通过命名 completion scope 仍选到 ID，但 `QueryReads.prime_select`（query_reads.py:619–630）直接调用 Store.select_many，将 Calculation 缓存到 _typed_calculations，并不经过 metadata／verify_selected_content。所以新公共 metadata 修复只覆盖调用该边界的消费者，不能自动宣称覆盖此入口。首个方案对 prime_select 实际命中的 ID 做标量 metadata 认证；最终改为 Store 共同 guard 并删除 prime_select 重复认证，详见下文。必要正文／封签／采用证明仍是另外的职责，不能把标量认证称作完整证明。本轮只改 kind，未把结果正文损坏也写成已复现。
- `Store.select_many` 的 calculation '#'／'*' 分支在 storage.py:659–660 用 c.kind 负筛；普通命名 scope 分支使用 calculation_scope 的保存 kind。同一合成诊断证明：typed external_completion '*' 和 typed '#ID' 从 1 条变为空；kind='*' 的 '#ID' 与命名 completion scope 仍选中 1 条。随后 opening basis 实际 preview 已证明误报 needs_information／members，最终按 subject.kind 发现并在 Store 核身份，详见下文。当前源码唯一明确使用 typed calculation '*' 的外部 `workflow.required_reads`／`required_work` 没有注册到 registry.readiness；它在诊断中产生假的 external_declaration issue 只算 helper 证据，不算实际关账入口。资产 accounting member／支付比较的 typed '#ID' 有其他采用或 AccountingBook 认证边界，未用 SQL 相似推断其都接受损坏。
- Subject.kind 是业务发现权威，不是可以替代所有证明的万能字段。发现所选 ID 后仍需认证 calculation↔fact、封签、实际正文、发布和冻结采用；修 discovery 不能放宽版本化内容核验。

原始诊断：`.tmp/stage9-completion-kind-r61-probe.py` 首次构造失败（ExternalCompletion 缺采用来源），其 JSON／log 保留；probe2 使用现有 save_completion 的真实提交证据，probe3 增加实际工资采用链。三次命令均为 `.tmp-kernel-venv/Scripts/python.exe -X utf8 .tmp/stage9-completion-kind-r61-probe[2|3].py`（首次文件名无数字）；工具耗时分别 3.93／5.15／6.47 秒，只是诊断执行耗时，不作性能结果。JSON 内保存读取时源码 SHA；workspace 当时公共 metadata 已修改，仍是未封存私有诊断，未作修复后验收。

typed '#ID' 的真实撤去 previous 路径另作了小诊断：复用 `test_identity_corrections.identity_engine` 与两笔已发布 expense，调用 `IdentityCorrections.preview_identity_correction` 撤去重复主体。正常结果 impact=accounting_changed；只改旧 calculation.kind 为 service_sale 后，结果改为 compatibility_required，带 `accounting_compatibility_required / frozen_fact_identity_mismatch`，没有把来源损坏误报为 needs_information。AccountingBook 精确 ID 核验在该路径发挥作用。首次误写字段 superseded_by_subject_id 被 IdentityChange 拒绝，原失败保留；改为现有 API 的 replacement_subject_id 后前后比较成立，`.tmp/stage9-typed-exact-kind-r61-probe{,2}.json/log` 保留，耗时 2.78／3.15 秒。

随后 worker 的实际 opening preview 已建立缺口：`stage9-store-kind-opening-probe-complete.log` 中 public identity／typed binding 正常通过，改 package calculation.kind 后为 opening_binding_source；public basis／typed basis 正常通过，但 typed basis 改 kind 后被漏选，误报 `needs_information`、fact_issues.field=members。所有 preview 的原数据库 dump 前后相同。最终 Store 修复后 `stage9-store-kind-opening-probe-fixed.log` 的三个损坏分支均为 `content_integrity_failed`，正常分支保留成功，且 no_preview_writes 均 true。首次不完整 probe 原件也保留，不以最终结果覆盖现场。

最终公共 guard 位于 `Store.select_many`，按事实所对应 subject.kind 发现 typed '#'／'*' 候选，并在解码与交付前 `_verify_calculation_identity` 检查结果 subject/kind/period 与原始事实身份。外部关联发现也改为 subject.kind。此前 prime_select 额外 metadata 认证属于暂时方案，12 passed／31.73s 是该方案当时证据；最终删除重复认证后单独 6 passed／14.13s，Store 收敛组 39 passed／1 warning／42.02s。标量 guard 不等于完整正文、封签、发布或冻结采用核验；这些职责及 fixed v1 原 stored-proof 规则保留。

支付 `domains/accounting.py:189 payment_references` 与 `asset_batch_models.py:69 accounting_reads` 的 typed '#ID' 是 AccountingBook 的精确比较引用，`AccountingBook.load` 按 ID 查找，不经 Store.select_many '#' 的 c.kind 过滤。Payment／Repayment 自身使用 '@source'；不能用它们的 preview 结果冒充 typed '#' 分支实证。固定 v1 的同名读取仍保留独立 decoder 与 stored proof，未因当前 metadata／selection 修复而替换。

已完成 r61 35 份 log／JSON／JUnit 原件归档见 [owner-required-r61-manifest.json](owner-required-r61-manifest.json)：独占新建 gzip，完整原件与解压 SHA 一致，包含首次失败及本次诊断，不覆盖历史、不包含未来候选或纯计时文件。各定向组与局部重跑不合并成一个通过数。

## 最小处理顺序

1. 公共 metadata 先认证所选 ID 的三字段身份；保留资产成员 publication 合法例外、错误结构化、固定 v1 原读取与采用证明。
2. 对 Journal kind-only 候选层，按权威业务主体发现所需范围，再认证和分类；由负责 agent 的实际默认／细目入口负测界定，不新增全历史正文读取。
3. 银行新增双定位若已被公共边界覆盖，收敛回同一 selector，仍保留它消费的冻结银行身份锚。外部办理关联漏选及 prime_select 未认证已取得真实业务状态证据，可交 root 集中决定修复；Store wildcard 的真实计算生命周期影响仍须按现有采用／比较边界区别，不能只凭 helper 结果扩大生产改动。

本文记录他人执行的上述定向结果及本审查真实小诊断，没有运行整套测试；不同组不累计。新封存候选完整核验、纯浏览器和阶段完成结论不在本文证据范围。
