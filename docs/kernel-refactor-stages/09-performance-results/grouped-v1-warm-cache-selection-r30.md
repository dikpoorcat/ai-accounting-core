# 正式 v1 历史核算的暖缓存差集

固定 r29 主 12 月合成库已完成独立完整核验；本次只读探针在该来源上调用正式 v1 的 `require_settlement_projection`，实际比较了 12 月、14,950 行清偿投影。`V1Reads._load_calculations` 调用 8,139 次，累计请求 ID 67,914 个；大量 `relations_many` 祖先解析会单 ID 回读已缓存核算。原 `requested - cache.keys()` 即使请求已命中，也要遍历整个暖缓存，累计键遍历下界 98,241,936，其中“小请求（≤8）且缓存≥100”占 98,193,648。相同调用中 `prime_parents` 仅两次；`fact_versions`、`vouchers`、`voucher_lines` 没有触发。原始计数见 [v1-require-cache-r30.json](v1-require-cache-r30.json)。

只把 `_load_calculations` 的差集改为逐请求 ID 查缓存。固定 r29 来源的私有六个月 ABBA 采用同一生产投影函数，7,476 行的 `repr` SHA-256 四轮完全一致；旧两轮 CPU 1.52/1.58 秒，新两轮 1.31/1.36 秒。原始结果见 [v1-cache-diff-ab-r30.json](v1-cache-diff-ab-r30.json)。耗时受并行任务影响，只证明这条历史核验调用链的工作量缩减，不是浏览器五页 500 毫秒验收证据，也不能解释主页面时延。

生产修复只改 `history_reads_v1.py::_load_calculations`，既有缺失 ID 查询与内容认证继续执行。新增合成非空库回归把缓存填入 2,000 个无关项，直接让暖缓存禁止键遍历，再检查命中与缺失结果和正式 v1 清偿投影；相关 3 项测试通过，Ruff 通过。其他 `requested - cache.keys()` 位置只有出现“有界请求 × 大暖缓存 × 反复调用”的实际证据时再改；本次代表路径没有这样的证据。
