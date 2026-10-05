# 第9阶段：head直接采用节点的精确lookup（2026-10-04）

第二组head lookup实现与专项验证已收敛，现已纳入762文件固定候选源2e474，source SHA `2e474b71d7871f27a8146286508756c033e823017db02a1bf8ab8fc69e986c91`，manifest SHA `48437917589a2b7453cc5b644a8bc8c9c2444d318774e9cefd62350ee84aa274`。root固定head query的集中消费者6文件123 PASS／263.36秒，与下文专项及第一组166有重叠；同process的report为初版，不能移作最终report通过。第三组最终classification candidate已有30项封存源消费者结果，流式candidate已保留，pure顺序窗口完成但不证明稳定因果或browser500；共同来源、profile及各自绑定见[有界读取组](bounded-read-groups-20261004.md)。第一组69838及归档仍只覆盖第一组，不声明所有优化最终交付。

目录与公司仍draft／0，现有5173、资料根及身份原样；不改正式版本、运行入口或fixed-v1。2e474已有两组共同pure顺序窗口，但没有新完整资格／actual preview、HTTP／render、开发包、实际CLI／MCP或浏览器500ms结果；主120原browser失败与阶段9未完成状态保留。

## 根因、实现与必要范围

已选择的head具有不可变calculation ID与posting_period。旧`_verify_head_close_locators`按ID读取截至页面cutoff的全部月份reference，其他月份readiness或voucher对旧CID的提及不会成为该head的直接采用，却扩大索引范围。新`_head_close_locator_sql`仅在owned current snapshot且所有ID为非空str、posting_period为严格int并满足`0 <= posting_period <= snapshot.month`时，按`(head ID, posting_period)`读取直接采用节点。

reference type仍枚举真实索引前缀，不硬编码合法类型；选中节点仍调用原`verify_close_references`，坏type／position／related／source及选中路径见证继续拒绝。无效ID／period、unowned和registry-v1保留原宽范围query。`_adopted_heads_sql`原双ID候选发现完全保留，包括mutable source丢失或redirect的独立来源；本组不实施直接冻结leaf发现候选。

缺失locator本身不成为helper的absence proof，选中source与采用仍由后续独立证明检查。其他月份多余目录行属于完整verifier的完整多重集范围，不能以精确helper成功替代fullverify。必要discovery、min-close、absence、fact-seen、report classification range及完整multi-scope不缩窄；BusinessQueries与Funds已有pair范围，保留其职责。

全局同类排查还发现voucher known精确node可研究，但f105资产旧宽range仅两次调用／356返回行／7,400 VM，未见跨月readiness噪声；pair JSON开销可能抵消收益。该候选未验证、未施工，不能写为已解决或永久排除。

## 专项验证

| 验证 | 实际结果与来源 | 边界 |
| --- | --- | --- |
| head locator专项 | `tests/kernel/test_head_close_locator_scope.py`：31 PASS／83.04秒；XML `.tmp-head-close-locator-scope.xml` | closed／open／no-impact／late及zero-line公开金额／roster；选中节点损坏；无效输入fallback；unowned／registry-v1；缺失locator与其他月extra的proof边界。 |
| 强化实际growth | 同一growth node更新后1 PASS／7.57秒；XML `.tmp-head-close-locator-work.xml` | 与31项重叠，不累计成32个不同项。 |
| 员工既有损坏边界 | `test_employee_roster_scope.py`按`zero_line_frozen_witness_cannot_disappear_with_missing_mutable_source`或`selected_frozen_wage_rejects_a_damaged_reference_type`筛选：22 PASS／37 deselected／75.42秒 | 来源为agent工具终端，无XML，不编造持久log。 |
| fixed-v1／非owned完整scope | `test_employee_adopted_period_scopes.py::test_fixed_v1_period_scope_retains_union_complete_proof`及`test_full_accounting_period_scopes.py::test_nonowned_scope_and_fixed_v1_keep_their_proof_boundaries`：2 PASS／6.49秒 | 两个明确node，不代表完整历史套件。 |
| 静态检查 | Ruff PASS | 不替代业务回归或性能。 |

首轮25例为14 PASS／11 FAIL，失败涉及fixture月份、空月missing wage及path证明边界；后续read_version比较及100-step采样断言也曾失败并修正。均为测试构造或断言问题，agent工具记录保留，不称生产失败，也不删首次结果。

## 实际增长与主120读取诊断

growth使用computer的实际CID，再增加三个实际后月close及readiness提及；完整verify为verified，资产公开data不变。精确lookup VM为268→268，旧宽range为254→269；真实返回始终1行／129字节。这里是合成增长的实际计数，不推算整页毫秒。

主120只读诊断 `.tmp/stage9-head-locator-pair-review-20261004.json`取得资产120个heads：旧44,300→精确10,200采样VM，119个references完整多重集相同。员工50 heads为2,500→2,500 VM／0 references，empty为200→200；brief没有调用该helper。guards equal与read pool closed为true。诊断未执行完整verifier、preview或pure timing，不能据VM下降宣称五页性能改善或500ms通过。

## 后续绑定与未验范围

第二／第三组已固定到2e474候选源，源码差异与最终消费者按[有界读取组](bounded-read-groups-20261004.md)各自范围记录；当前pure顺序窗口与独立18件归档完成，不把69838改标为第二／第三组源码。不重跑或继承旧pure窗口生成新性能结论。第三组report classification仍完整核验4,932个独立header，以SQL流式decision减少Python返回行；VM增加约34%，流式candidate已按严格等价、传输工作与有限顺序观察保留；共同pure及归档完成，本页不代第三组宣布稳定因果性能收益。

阶段9、新源prepared browser、独立三规模、压力、按需详情、公司切换及最终开发包仍待实际验收；正式冻结、正式交付和切版延期。未验证候选不写成已解决，不修改第一组归档，不提交。
