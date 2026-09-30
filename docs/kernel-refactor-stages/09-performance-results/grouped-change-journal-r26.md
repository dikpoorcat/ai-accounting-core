# r26 季度 `source_change` 范围诊断结论

只读对象：固定 r26 源、已完成独立核验的 main48，末月 2019-12。私有脚本与原始结果：`stage9-change-journal-scope-r26.py`、`stage9-change-journal-scope-r26-result-r2.json`；同事务字典共享原型：`stage9-change-journal-shared-ab-r26.py`、`stage9-change-journal-shared-ab-r26-result-r2.json`。首份 scope 结果因诊断脚本集合类型错误失败，保留原始失败 JSON；修正仅在 `.tmp`。无生产源码、固定候选或业务库修改。后台建库已恢复，所有时延只标诊断。

`Dashboard.quarterly_report(preparation='complete')` 三个月各自新建 `_CompletenessInspectionCache`，但同处一个 `QueryReads.snapshot`。六次逻辑 `source_changes_since` 调用中，四次真正读取 journal：10 月资料 `(361888,369678,369678)` 7,790 行，10 月查重 `(354102,369678,369678)` 15,576 行，11、12 月资料再次读取前一个 7,790 行范围；后两月查重则在各自月内命中资料的成功缓存。因此重复的只有两次 7,790 行完整相同成功键，合计 15,580 行，依据原 grouped SQL 约 1.30 MB 和 140,200 VM。10 月查重较早起点有必要，不能按“重复四次”删除。各月资料、查重的事实期间、截止和结论仍分别计算。

私有 AB 仅在活跃的同一 `QueryReads.snapshot`、同一连接下共享成功的 `source_changes` 字典，不共享资料结论、首建对象或其他月份结果。complete ABBA 的物理 journal 行从 38,946 降到 23,366，完整响应去除唯一会随调用时间变化的根字段 `checked_at` 后 SHA-256 全相同 `5c37c15aa126dbdf38b0e4f049bc8ced2dc9a2bf7d1755813df70a04296b300e`；其他来源、版本、金额、问题和字段均参与哈希。`Reports.report` 的 open/closed 业务入口在单独诊断中均为 0 次 completeness cache/journal 调用。

净 CPU 不支持落地：complete 原路两次 3078/2984 ms，私有共享两次 3016/3047 ms，均值约 3031 ms；wall 3105/3076 对 3024/3065 ms。背景建库使 wall 不能作为正式时延。更关键的是，真实 `ReportsView` 经 `fetchDeferredQuarterlyReport` 请求 `preparation='deferred'`；该分支 ABBA 全部 0 次 journal 读取，响应逐字段同义，故本候选无法解释或降低默认浏览器报表约 800 ms 的热态成本。完整准备和 CLI 能力仍保留，不能将 complete 诊断外推到默认页面。

异常边界仅作静态审查：`changes_since` 在返回前核对 journal 头、区间、序列连续性、来源／类型／引用字段；cache 只在调用成功后写入，每次命中仍调用 `head`。私有候选要求同连接、活跃事务与精确 `(after,bound,latest)`，不同起点不会命中。坏 journal、事务重开和修复后再次检查未做动态故障注入；由于没有可见净收益，本轮停止原型，不将其变成生产机制或新增跨请求缓存。
