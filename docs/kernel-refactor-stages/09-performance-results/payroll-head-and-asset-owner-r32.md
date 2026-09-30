# r32 员工采用头与资产 owner 重读

固定来源为 `.tmp/stage9-build-source-r32-release`，合成主 12／48 月库的验证报告均为 `complete / integrity=verified / limitations=[]`。全部 AB 是串行 native 诊断，受后台负载影响，未覆盖 HTTP、浏览器和简报 Windows workers，不作为 500 ms 验收。八份原始 JSON 已按原字节 gzip；[清单](stage9-g-head-r32-raw-archive.json)同时记录原文与压缩文件 SHA-256，可解压逐字节复验。

## 员工采用头

`Dashboard.employees → _employees → payroll_head_metadata → adopted_head_metadata` 要枚举历史全部工资采用候选。r32 的 `limits AS MATERIALIZED` 已避免每候选重解 JSON，但 `LEFT JOIN limits` 对每条候选扫描全部闭月。主 48 月 2,400 个头／47 个闭月时，查询 VM 为 746.1k。仅在私有 SQL 将已认证 close header 的 period 和 publication highwater 显式转为 INTEGER，SQLite 计划从 `SCAN l LEFT-JOIN` 改为 `BLOOM FILTER + SEARCH l USING AUTOMATIC COVERING INDEX(period=?)`；返回 2,400 行的顺序和 SHA 相同，VM 降至 311.8k。12 月 600 行的 VM 为 99.6k→77.2k。

完整默认100员工页的旧／新／新／旧 AB 也逐字段相同（只排除 `generated_at`）：48 月全页 VM 1.4423M→1.008M、响应 166,340B；wall 旧 419.9／389.9 ms、新 385.0／388.2 ms，CPU 旧 406／391 ms、新 375／391 ms。它证明工作量下降，净时间在并行诊断中仅小幅且不稳定。12 月全页 VM 503.4k→481k。简报只取本月工资头，48 月全页 VM 2.6388M→2.6389M；本改动不能解决简报超时。此前连接 UDF 私有 AB 虽也减 VM，但完整员工页没有稳定净 CPU 收益，且增加回调与连接生命周期，不采用。

当前工作树仅在 `dashboard_reads.adopted_head_metadata` 的两个已认证 `limits` 整数上加 CAST。进入 SQL 前要求闭月 period 和 highwater 为真正的 `int`，损坏字符串不会被 SQLite 转成 0 或误走开放期；原候选、最新修订、来源认证、默认行数和冻结 v1 不变。生产函数 12／48／120 月返回身份及实际进度步数增长测试、真实 Engine 闭期／开放期及更正金额、损坏 outcome 与事实拒绝的窄组共 **7 passed**；Ruff check 通过。完整固定候选回归仍由主线程执行。

同类 CTE 未按语法批量改：`Store.select_many.requests` 按多 slot 选事实/计算；`QueryReads.selected_voucher_sql`、独立 v1 `report_projection_v1` 的 `frozen_vouchers` 按精确凭证／闭月对；`QueryReads.OPEN_FACT_PERIODS_CTE` 枚举事实期间；`BusinessQueries._selected_accounting_references.wanted` 还须保留错 type/path 与重复输入多重集。这些消费者没有员工高水位的已证 per-candidate `SCAN limits` 问题。CLI/MCP 的员工页经相同 Dashboard，关账、完整核验、修复、备份和固定 v1 reader 走各自规则，未外推本页测量。

## 资产 owner：已证重读，暂不落地

同一完整 `Dashboard.assets` 快照中，`QueryReads.verify_sql_outcomes` 成功严格解析的计算 ID 为 12 月 48 个、48 月 192 个；随后 `_selected_asset_member_heads` 又从数据库取出并严格解析 24／96 个 owner，**这些 owner ID 全部与前面的成功集合重合**，48 月第二次正文共 1,099,169B。owner 解析后还须做 `digest(outcome)`、采用锚与成员顺序、身份、依赖、摘要和完整目录比较；成功 ID 不能替代这些检查。

私有上界 AB 暂存同一资产请求中首次严格解析的对象，owner 消费时弹出，并保留上述所有后续检查。48 月完整响应 SHA 和 188,966B 相同、VM 都为 799.1k；旧 CPU 469／500 ms，新复用解析约 438 ms，旧 wall 466／516 ms，新约 440 ms。再省第二次 owner 正文列，wall 约 448／457 ms，没有稳定额外收益。实验峰值捕获 192 对象、约 **4.36 MiB 额外 Python 对象**，owner 用后还有 96 个未消费对象须丢弃。首次只在 owner 方法内捕获的探针因守卫实际更早发生而失败，失败记录保留在 `.tmp/stage9-g-asset-owner-decode-r32-attempt1-error.txt`；r2 每行递归算缓存大小使时延严重增耗，也不纳入结论。r3 将内存估算移到计时外才用于上述诊断。

目前只证明上界有约数十毫秒潜力，尚未证明能以窄接口仅留命中 owner、维持成功/失败与同事务边界、控制峰内存并通过故意坏源回归。因此不新增解析对象缓存。资产页和简报的来源范围不同，不能把这项潜力计入简报收益。
