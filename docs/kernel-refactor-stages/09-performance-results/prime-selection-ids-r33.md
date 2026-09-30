# r33 完整简报父进程 Read 选择 ID 归因

一次性私有 hook `.tmp/stage9-r33-prime-selection-ids.py` 从固定 `.tmp/stage9-build-source-r33-release` 启动生产三 worker `LocalService._dashboard_brief(preparation=complete,limit=100)`，分别读取 r32 已 `complete/integrity=verified/limitations=[]` 的主 12、48 月合成书。只在**父进程**包住原 `Periods.collect_current_readiness`、`Store.select_many` 与 `Store.calculation` 并原样调用；原事实、outcome 严格解析、来源摘要与完整简报结果仍走原生产代码。spawn worker 未被插桩。两档各仅运行一轮，与其他回归并行，**不输出或解释页面时延**。输出仅 Read 范围、ID、构造次数与 outcome 原文字节数，不保存正文。

| 父 readiness 范围 | 12 月 | 48 月 |
| --- | ---: | ---: |
| 声明 Read slot | 60 | 60 |
| 真正非空的 `Store.select_many` 调用 | 1 | 1 |
| 随后空请求的 `select_many` 调用 | 60 | 60 |
| 选中 fact ID／calculation ID | 607／186 | 859／1,428 |
| 同 source 的跨 slot 重复 ID | 0 | 0 |
| `Store.calculation` 构造／独立 ID | 186／186 | 1,428／1,428 |
| 同 ID 重复构造 | 0 | 0 |
| 已构造 calculation 的 outcome 原文字节 | 352,950 | 998,524 |
| 历史 `asset_consumption` calculation ID／outcome 字节 | 66／32,874 | 1,128／562,848 |
| 本月 `report_classification` fact ID | 411 | 411 |
| 本月 `report_income_tax_confirmation` fact ID | 1 | 1 |

48 月历史消费计算占全部本次计算对象构造的 79.0%、已构造计算 outcome 字节的 56.4%；12 月分别为 35.5%、9.3%。当月分类事实 411 个保持不变，在 48 月为本次选中事实的 47.8%。另外 48 月有 50 个工资计算，约 276,670 outcome 字节；12 月相同。父进程 collector 之外另有 9 次 `select_many` 调用，只有 2 个请求 slot、1 个选中事实、0 次计算对象构造。因此 `r33` cProfile 所见 1,428 次 `Store.calculation` **不是**同 ID 跨 slot 或再次选择导致；一对一的真实历史候选数增长是该对象构造量的直接来源。

这证明了对象构造的实际工作量归属，不证明 1,128 个历史消费结果的每个 outcome 字段都被规则使用，也不证明可在不改变完整来源/损坏拒绝与 close trace 的前提下只取标量。事实载入内部 SQL/typed 子表耗时未按每个 kind 插桩；411 个分类 ID 不能直接等同 411 次物理原文读取。此前 31→27 资产声明已去掉无用的消费**事实**，仍要由 `asset_consumption` **计算结果**判断每张卡连续确认。业务范围结论与 `.tmp/stage9-readiness-prime-scope-r33.md` 一致；本轮否定其“可能跨 slot 重复计算构造”的未验证候选。

原始逐 Read ID、逐次构造计数、摘要与完整返回 SHA 分别在 `.tmp/stage9-r33-main12-prime-selection-ids.json` 和 `.tmp/stage9-r33-main48-prime-selection-ids.json`。返回 SHA 含实时字段，只证本次普通完整返回已产生，不作跨版本响应等价断言。固定书来源为 r32，当前读取器为 r33；均未修改或重建。

原始量测和临时 hook 按字节压缩保存，见 [文件摘要清单](prime-selection-ids-r33-manifest.json)。
