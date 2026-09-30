# 全局缺 typed 分类探针的成组决定（固定 r25 私有 AB）

`Dashboard._position` 普通 brief 使用全局缺 typed 探针，任何被关账准备资料采纳过的 `report_classification` 原始头失去 typed 行，即使不是当前版本、没有 party child、当前本页目录找不到 voucher key，也必须拒绝。原计划从 `subject_kind→fact_revision` 遍历候选，但优化器在 `LEFT JOIN c ... IS NULL` 前对每个候选执行 `EXISTS close_reference`。48 月无损样本返回 0 行仍消耗 488,700 VM。独立 `MATERIALIZED missing` 先找缺 typed，随后对这些 ID 执行**同一** `reference_type/reference_id/path/close_period` 条件，返回 ID 集合完全一致，主48为276,200 VM；主12为118,800→69,000 VM。以 `close_reference` 类型全局起步是负方案：主48 9,525,400 VM。

固定 v1 的 `position_v1._position_classification_ids` 不使用当前目录。它原先让 `close_reference` 类型全局扫描，再 join fact/subject，最后与开放当前事实 `UNION`。主48 11,034,000 VM，选择 0 个 party 分类；独立 v1 方案以 `subject_kind→fact_revision→同一 close_reference EXISTS` 取得 frozen IDs，仍与原开放候选 `UNION`，同一 duplicate voucher/缺 typed/party child 判定，主48 2,247,500 VM，选择集合相同。v1 源文件被 `content_v1` descriptor 哈希纳入；本次只允许在开发版正式 v1 冻结前独立修规则执行计划，随后由 root 重生实验 descriptor 并独立核验历史样本。

落地版 v1 保留 `CROSS JOIN` 候选驱动，但不强制 `INDEXED BY`：自定义旧能力包可合法缺少这些性能索引；有索引的新库由规划器选择它们。上面 2,247,500 VM 属带索引私有原型，未冒充新 v1 文件在真实主48上的验收数。新增生产函数工作量测试以 12/48/120 月来源及大量其他业务测试两套最终函数，未减少权限/来源范围。

私有内存源构造覆盖：已采用缺 typed、未采用缺 typed、错误 readiness path、晚于截止 close、已替换/撤去但旧冻结仍采用、非分类类型、开放当前缺 typed、party child、同 voucher 双分类、重复 close reference。原/current候选以及原/v1候选分别精确相同。原始计划、VM、CPU 诊断和结果在 `.tmp/stage9-r25-main48-position-missing-typed-ab-v1r3.json` 及主12对应文件；计时不是页面验收。

相邻调用：`reports._validated_report_classification_headers` 从显式 reference IDs 直接精确 join typed，再检选中凭证行/冲突；`reports._rooted_classification_headers` 的开放候选按实际 `fact_period` seek，旧键按认证目录；`report_flow` 已把精确 voucher/月候选 MATERIALIZED 后才做采用 EXISTS。它们无「对所有历史分类先 adoption EXISTS，再做缺 typed anti-join」这一同形计划。完整核验、维修、备份依独立全源/关账重建，不应改用页面缺项探针作为完整性证明；公开完整报告仍保留全ID和命中来源正文。

组内定向：`test_position_classification_candidates.py`、`test_v1_position_classification_selector.py`、`test_report_classification_directory.py` 合计 27 passed；相关五文件 Ruff passed。涵盖开放/闭期、晚注册、旧头重复引用与分类冲突、无 typed 的自定义 v1 能力包、坏 party child/目录、全局缺 typed 拒绝及维修、无关来源增长。尚未在改后代码对主12/48运行完整核验或五页整页计时；这两项由 r26 固定副本统一执行。
