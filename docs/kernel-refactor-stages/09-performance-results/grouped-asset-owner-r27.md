# 资产 owner 采用范围与同页复用取舍

本组只分析固定 r26 源码、已完整核验的主 48 月合成库（2019-12 默认资产页）。私有脚本及未压缩原件保留在 `.tmp/stage9-r26-asset-*`；本目录的同名 `.json.gz` 是原始 JSON 字节的压缩副本。浏览器纯计时、固定 v1 核验、关账、维修和正式发布均不使用这些原型结果作验收。

| 实际调用范围 | 选择结果及证明职责 |
| --- | --- |
| `dashboard.py:4170` `_asset_card_sources` → `dashboard_reads.py:530` `Calculations.selected` | 取得 48 个计算；报销批 0；独立 activation/disposal 0；经成员目录选中的窄 owner 48；项目成本 0。零结果也保留原选集与不存在核对 |
| `dashboard.py:4265` `_assets` → `dashboard.py:4409` → `business_queries.py:817` `_selected_asset_member_heads` | 有 48 个 `fast_assets` 时选完整 owner 96；窄 owner 的 48 个主体和计算全包含其中。还要核对完整批成员目录、结果摘要、依赖、发布及卡片身份，不能只把宽结果按资产筛掉后跳过证明 |
| `business_queries.py:399` `_selected_accounting` → `query_reads.py:1183` `close_accounting` | 取得、窄 owner、完整 owner 各读 47 闭月，共 141 个精确 `(period, subjects)` slice；完整 owner 包含窄 owner，但 slice key 不相同。简报的银行、取得、窄 owner 三组彼此不交，资金仅银行；它们与资产页重叠发生在不同 HTTP 请求和事务，不能跨请求共享 |
| `business_queries.py:696` `_selected_asset_members`、`dashboard.py:378` `asset_member_events` | 同一快照按完全相同的 `kinds/subjects/asset_ids` 条件复用成员结果；本例窄与完整范围不同，未命中此 cache。关账按具体 key 反查、固定 v1 读者、完整核验和维修均不走本页的 47×3 slice，不能拿本页的重复选择削弱其范围 |

`close_storage.read_accounting` 已在同一读取快照把通过验证的 family、filter、bucket 放入 `_verified_parts`，每个 slice 完成后才提交新块。资产页三个范围分别新增 114、106、104 个块，合计 **324 个唯一 `(period,field,bucket)`、683,247 字节 block content**，没有重复物理块读取；简报和资金分别是 450／232 唯一块。重复工作主要是上层再次按较大主体集合核对预期发布、当前凭证、组装采用和 owner 结果，不是把同一 683 KB 解码两次。

| 私有对照（同一完整资产响应） | SQL 次数 | SQLite VM | 返回行／值字节 | adoption slice／行 | 决定 |
| --- | ---: | ---: | ---: | ---: | --- |
| 原始 ABBA | 1,487 | 791.7k／796.7k | 10,846／8,315,112 | 141／188 | 基线 |
| 先完整 owner 后供窄消费者筛选 | 1,152 | 约 724.7k | 约 10,656／8,278,702 | 94／141 | **不采用**：在没有 `fast_assets` 时仍提前读完整 owner，扩大无须读取的来源；原型还依赖隐藏调用次序 |
| 窄 owner 已认证结果加宽阶段剩余主体 | 1,487 | 710.5k／710.7k | 10,608／8,270,110 | 141／141 | **暂不采用**：证明范围保持，但需要跨多层显式传递精确 proof，当前净收益不足以承担接口复杂度 |

两种私有原型的四次完整响应（除时间戳）各自哈希全等；物理块数量、标准 JSON loads 2,740 次、计算结果 JSON 解码 288 次、typed fact 解码 96 次在先宽原型中均未下降。先宽原型的原生 ABBA 在并行建库下 CPU 中位数 328.1→304.7 ms、wall 328.1→301.7 ms，仅作诊断。合成 12 月库副本损坏 owner 采用块与成员摘要时，原始和先宽原型分别同码拒绝 `content_integrity_failed(close)`、`asset_batch_digest`；**窄加剩余原型尚无损坏 AB**，不能把前一个原型的拒绝证明迁移给它。

若以后确需实施“窄加剩余”，必须由资产页明确携带同一快照、期间、窄 owner **候选主体全集（包括未发布主体）**及已成功核验的 voucher/state 结果；仅在 `fast_assets` 非空时，对完整 owner 集合的差集执行原 `_selected_accounting`，按原顺序合并，后续成员目录及采用证明全保留。普通 business query、简报、小／零 owner 集合、无资产或无 `fast_assets` 的资产页不应先读宽范围；无窄 proof 时走原完整选择。当前 `_asset_card_sources` 经 `Calculations.selected`、`asset_member_events` 才进入嵌套 owner 选择，要显式传 proof 需改多层职责接口，私有 monkeypatch 的实例临时属性不能进入生产。本阶段保留现状。

同页清偿封签与开放尾部的两份表示另有[同快照私有负面对照](grouped-settlement-tail-handoff-r27.md)：SQLite 工作量虽降，Python 大 JSON 解码增加且资产／简报 CPU 未改善，因此也不进入生产。

完整历史成员目录是否可由已认证 owner 结果加 SQL 全量比对代替，也已做[完整函数私有 ABBA](grouped-asset-member-r27.md)：传输与 JSON 解码下降，但 SQLite VM 上升，原生整页 CPU 未有稳定收益，暂不落地。

证据压缩原件：`stage9-r26-asset-selection-summary-r2.json.gz`（集合与包含关系）、`stage9-r26-asset-owner-prefetch-ab-r2.json.gz`（SQL／VM／解码）、`stage9-r26-asset-owner-complement-ab.json.gz`（差集 AB）、`stage9-r26-asset-owner-prefetch-native.json.gz`（诊断墙钟）、`stage9-r26-asset-owner-negative-r2.json.gz`（损坏拒绝）。完整逐 ID 插桩 `.tmp/stage9-r26-asset-selection-scope-r2.json` 保留原位，没有复制进交付目录。
