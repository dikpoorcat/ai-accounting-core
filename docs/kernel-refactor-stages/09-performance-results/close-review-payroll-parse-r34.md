# 关账核对工资确认的重复结果解码

| 调用方 | 首次解析与证明 | 重复解析 | 应保留的职责 |
| --- | --- | --- | --- |
| 当前 `business_adopted_basis` | 精确计算 ID 的保存内容由同一 `QueryReads` 快照或独立 `verify_sources` 核验；调用方以当前 `stored_json.load_outcome` 解析工资确认 | `_adopted_payroll_confirmation` 对同一行 `outcome` 再调用 `load_outcome` | kind、确认来源缺失、采用事实 ID 与已读事实集合一致、来源引用摘要 |
| 当前 `_payroll_cards` | 精确卡片行先由 `verify_outcome_bytes` 严格解析并核对摘要 | 同一私有适配函数再次解析同一行 | 卡片来源和确认事实完整性，错误仍拒绝 |
| 固定 v1 `business_adopted_basis` | 使用 v1 的 `load_outcome` 解释精确保存行 | v1 私有适配函数再次按 v1 解释同一行 | 固定历史规则、来源 ID 与事实集合一致 |
| 固定 v1 `_payroll_cards` | 先用 v1 `load_outcome` 解释精确卡片行 | v1 私有适配函数再次解释同一行 | 固定历史规则、卡片来源及错误拒绝 |

本组只把调用方已严格解出的 `payroll_confirmation` 传给各自版本的私有适配函数；适配函数继续验证 kind、缺源、确认事实集合并构造相同来源引用。没有跨请求或跨快照缓存，也不把 v1 解码转给当前规则。金额、业务事实和摘要的原核验入口不变。已解析但未采用的其他 kind 不会因本组改变选择。

`test_close_review_payroll_parse_reuse.py` 使用隔离的 released/1 结构合同、登记 v1 完整核验器和真实发布的工资计算。当前 `business_adopted_basis` 对目标结果调用 `load_outcome` 一次；当前 `_payroll_cards` 在 `verify_outcome_bytes` 严格解析后不再另调 `load_outcome`。固定 v1 两个入口各调用 v1 `load_outcome` 一次。两版返回的计算摘要、精确确认事实引用及卡片相同；实际业务详情的税前工资金额与发布结果相同。把结果改为重复 `values` 键并同步改同行摘要后，四个入口仍以 `content_integrity_failed` 拒绝。

定向命令：`.tmp-kernel-venv/Scripts/python.exe -m pytest -q tests/kernel/test_close_review_payroll_parse_reuse.py tests/kernel/test_brief_adopted_basis_reads.py::test_brief_adopted_basis_keeps_exact_payroll_confirmation tests/kernel/test_payroll_confirmation_presentation.py::test_dashboard_current_and_frozen_wage_details_retain_monthly_plan_evidence`，结果 4 passed；上述三个文件中仅新增测试文件属于本组。三个相关源码／测试文件 Ruff 检查通过。这里测的是局部真实调用，不能代替固定 v1 整库完整核验；正式内容合同须在最终冻结时包含此 v1 源码版本。
