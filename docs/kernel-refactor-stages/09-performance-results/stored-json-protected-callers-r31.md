# 保存 JSON：有前置核验的内部计算路径

本次按 G 类根因检查 `accounting.py`、`projections.py`、`period_balance_freeze.py` 和 `period_balances.py`。普通 `json.loads` 是排查线索，不能单凭它判定公开入口漏验。下表结论限于当前列出的生产调用链；内部函数不因此变成可单独调用的完整内容核验入口。

| 位置 | 生产调用与核验边界 | 处理决定 |
| --- | --- | --- |
| `AccountingBook.load` | 先解码比较结果，但进入记录前必须执行 `Store.calculation(row)`。后者使用本组严格解码，重复键被拒绝；比较层保留既有 `accounting_compatibility_required` 错误，不转成业务追问。该路径没有在严格解码之前使用 JSON1 排除候选。 | 保留原比较流程，不重复增加一次完整来源核验。 |
| `projections.expected_projections`、`_subject_contributions` | 完整核验先检查权威来源；维修在同一写事务先执行 `verify_integrity(include_projections=False, include_indexes=False)`。正式发布先 `verify_prepared_sources`，再准备投影检查，写入后检查发布内容。这里是 Python 对精确来源的派生计算，没有另一套 JSON1 解释。 | 保留完整来源与派生投影分工。不能把该内部重建函数单独当作来源认证。 |
| `period_balance_freeze._source_rows_from_segments` | 关账构造先完成同事务来源检查；完整核验先取得已验证来源。只按明确发布段取计算及比较基准，再导出余额贡献。 | 不为相同输入重复增加独立全库扫描。 |
| `period_balances.expected_period_balances` | 完整核验可直接复用 `verified_calculations`；维修先核权威源，发布在已有来源与发布核验边界内更新。未复用时读取的是明确发布段的计算，未用 JSON1 决定负向候选。 | 保留已有调用约束与金额计算。 |

另外增加公开入口回归 `tests/kernel/test_stored_json_repair_boundary.py`：在新建合成公司已发布资金结果中加入重复 `balances` 键，保留原逻辑摘要，使旧普通解码仍得到原值。两类维修和便携备份必须拒绝，且权威计算、发布、余额投影、请求及审计状态不变，不能交付 ZIP。三项定向通过，原始 JUnit 位于 `.tmp/stage9-g-repair-boundary.xml`；Ruff 通过。它验证真实入口的拒绝与无副作用，不代表本组回归或整页性能已通过。

其他独立读取入口（资金负向筛选、清偿候选、员工档案、付款导出、老板核对及身份纠错）的确存在不同前置条件，分别由 G 组修复和测试。完整核验、维修、备份不能借页面的局部路径守卫代替权威内容核验。
