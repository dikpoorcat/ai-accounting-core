# r24 真实报表来源重复度诊断

本诊断只读固定 `.tmp/stage9-build-source-r24-release` 与已完整核验的 48 月主合成书。`.tmp/stage9-r24-report-party-overlap.py` 分别调用真实 `ThreadingHTTPServer` 的 2019-Q4 deferred 报告，以及常驻三进程 complete 简报中的真实 reports worker。正式代码／样本未改。两次报告响应除动态 `checked_at`／`generated_at` 与未插桩基线相同；完整简报除 `generated_at` 相同。原始逐调用 ID、SQL 返回字节、每 100 步采样的 VM 与 `_decode` 记录在 `.tmp/stage9-r24-report-party-overlap-r2.json`。它是有插桩、且运行环境可能有其他负载的工作量诊断，不是纯浏览器时延。

两个入口分别在自己的只读事务里，观测到**完全相同**的调用形状：

| 步骤 | 精确输入／缓存 | 物理读取（调用自身及子调用，嵌套不能相加） |
| --- | --- | --- |
| 前一年期末 party cutoff 24215（2018-12） | `source=closed`，第一次 uncached | 9 SQL／418 行／376,860 值字节／约 15.6–15.7k VM；为期初比较基线。 |
| 本期 party cutoff 24227（2019-12） | `source=open`，第一次 uncached；本月 `_party_delta` 输入 2,292 条已选账面行 | 整个 cutoff 146 SQL／21,396 行／8,918,348 值字节／约 805.5k VM；内含下列开放贡献与 11 个后续闭月。 |
| `_party_delta` 的第一次 `read_open_contributions` | 938 个精确 calculation ID，缓存命中 0，实际缺 938 | 15 SQL／5,193 行／4,663,611 值字节／约 136.7k VM。 |
| `_report` 后续 cash/profit 行的第二次 `read_open_contributions` | 1,003 个 ID，**与前批精确交集 937**；缓存命中 937，仅新查 66 | 21 SQL／656 行／740,016 值字节／约 16.0–16.1k VM。第二批物理查询还需查新源与其依赖，不能把 21 SQL 全称为重复。 |
| 投影正文 `_decode` | 合计 **1,004 次**，恰好两批 ID 并集 `938+1003−937`；UTF-8 输入合计 3,059,132 字节 | 没有把共同的 937 份投影正文再次解码。 |
| `read_report_flow` | 23 次调用覆盖 12 个实际关账月；第一个月 1 次、后 11 月各 2 次 | **12 次物理读取／11 次成功缓存命中**。物理总约 2,436,331 值字节／188.1–188.3k VM；缓存调用返回 0 SQL／0 字节／0 VM。首月 checkpoint 约 376.8 KB，随后每月约 187.2 KB。 |
| 简报 reports worker 的第三次 party balance | 同一 cutoff 24227，已缓存 | 0 SQL／0 返回字节／0 VM。季度 HTTP 不触发这第三次。 |

结论：当前同一 `QueryReads` 快照**没有**把 937 个共同开放贡献重新拉正文／重解码，也没有为重复读取同月 flow 重新查库。`party_balance_rows` 的两次未缓存调用是前一年末和本季末两个不同 cutoff；不能视为相同结果重复。r24 48 月 worker cProfile 中 `party_balance_rows` 约 581 ms、`_party_delta` 约 303 ms、开放贡献两次约 168 ms 属嵌套累计，不能叠加为潜在节省。这一证据否决“给现有共同来源再加同快照缓存即可省 100 ms”的假设。

尚可测的窄问题是**真实非重复工作**：本月 `_party_delta` 为 938 个账面往来来源消费 4.66 MB 的实际投影／来源／依赖值，两个 cutoff 还需读取 12 个关账月 2.44 MB 的认证 flow；后续 cash/profit 使用另 66 个新 source。若要继续定位，应按每个源的已验证原文、`source_bindings`、凭证行／后补分类逐项计字节及 CPU，提出不减少命中内容核验的具体消除重复证据；不要重试已撤回的另建逐行表、仅用清偿金额聚合 party、或跨请求／跨进程证明缓存。异常 flow 无法使用时的 fallback 仍须单独测，当前主样本皆走成功根。

同类消费者边界：当前 `Dashboard._position`、报告页与报告准备在共享 `snap.reads` 时使用同一 `party_balance_rows`／`report_open_contributions` 缓存；简报父进程与报告 worker 各持独立事务，不能共享这些 proof。固定 `report_projection_v1.party_balance_rows` 没有当前模块的 `reads` 成功缓存参数，`_party_delta_v1` 每次新建 `V1Reads` 并用固定 `query_relations_v1` 全 resolver；它服务历史规则／完整 comparer，不能根据 r24 当前普通页的 937 命中推断 v1 同样无重复或改用当前 read_open 快路。本轮没 profile v1；只有 v1 完整核验相位确认为热点时才对其独立事务和实际 period/cutoff 做同型测量，保持固定语义及独立权威重建。
