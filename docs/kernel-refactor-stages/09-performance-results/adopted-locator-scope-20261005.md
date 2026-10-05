<!-- @format -->

# adopted locator 剩余成本与 path-first 负实验（2026-10-05）

本组固定来源为 `ed96dfd296cd96d4279745eb0ac26a9422db450c10d3bb9f8e7def11e2f9d8c8`，771 文件，源码清单 SHA `58067bd14dcb1374b944cd891842c69ad4385ca020f37437464d39f7907a63cf`。只记录既有原生插桩 profile、真实读取参数的 locator SQL 比较及 memory-only 续测；没有新完整核验、实际 close preview、HTTP/浏览器或 500ms 验收。生产源码、schema、索引及业务资料未因本实验改变；另外进行的临时 shadow 索引诊断不在本组。

## 剩余成本 profile

原件先准备选定 funds/context，另有五页各一次无 profile 的原生预热，再对五页各一次 profile。五页观察均为成功，保存 SQL execute、GC、累计/自身耗时表及原始 pstats SHA。execute 时间只包括准备程序和首个 VM 行，不包括 lazy fetch 或完整 SQL 时长；SQL observer 不含直接 cursor.execute，pstats 则观察其原生工作。

资产 adopted-head 查询的 SQL execute 为 111.579ms，`adopted_head_metadata` 累计为 146.567ms，二者不是可相加的独立阶段。funds 两次对应 recursive locator execute 为 17.566ms、13.344ms，employees 为 27.936ms。资产的一个 voucher selector execute 仅 1.6857ms，不是此处主要剩余成本。完整 SQL 和 profile 表保留，不把单个首行 execute 当作全部查询耗时。

各页捕获 GC 事件的耗时合计约 3.41–16.28ms，包括 profile 开销下的观察；不支持将本组剩余成本主要归因于 GC，更不能解释旧资格 host 的全部慢样本。member、publication 和历史采用边界仍须按实际引用核验，不能以跳过这些边界的捷径替代当前语义。

## path-first 与 thin 负实验

首次诊断已完成五次真实读取参数的 SQL 对照，三种查询返回 multiset 均相等。VM 每 100 条指令采样，以下为保存的采样步数，不是纯性能计时。

| 调用 | baseline 返回行 | baseline VM | path-first VM | thin IDs VM |
| --- | ---: | ---: | ---: | ---: |
| assets | 240 | 239400 | 4387000 | 2477000 |
| employees（非空） | 100 | 27900 | 4575100 | 2421600 |
| employees（空） | 0 | 400 | 200 | 100 |
| funds（空） | 0 | 56200 | 4453000 | 2431000 |
| funds（非空） | 241 | 112000 | 4508800 | 2486800 |

除极小的空 employees 调用外，两种替代拓扑明显增加 VM 工作量，不能因返回相同或列更少宣称优化。path-first 与 thin IDs 均不采用，不再重复尝试这两种拓扑；不存在生产落地或门槛通过结论。

首次整体状态为 `diagnostic_failed`，日志记录 `sqlite3.OperationalError: no such table: fact_current`。失败发生在 memory fixture 的 DDL，不是已完成五次真实调用的业务失败。该 JSON、日志和首次 helper SHA 原样保全，不改成 complete。后续仅进行 memory-only 续测，补齐 fixture 后 15 个场景的三种查询 multiset 全相等，包括缺事实/计算/owner/publication、成员版本差异、错误及重复引用类型、身份重新归属、withdrawal、期间或命名空间越界等。

这些 memory 场景只比较 locator SQL 返回 multiset，没有执行 publication digest、membership、body 或完整业务检查器。相等不证明损坏数据被业务接受或拒绝，也不证明可以放松成员及 publication 的完整性边界。续测不访问现有数据库、服务或会话，不能与首次记录拼成一次全业务通过。

## 保护与保全

profile 与首次真实调用 receipt 的业务/身份/状态/历史守卫相等、源码未变、原会话不变、新会话正常 logout、read pool 已关闭，cleanup errors 为空；memory-only receipt 声明源码未变。本次归档仅核对原件、helper、pstats 与清单 SHA，依据 receipt 记录保护事实，没有再次独立核验数据库、重新哈希数据库或执行测试/服务/性能测量。

独立 [manifest.json](adopted-locator-scope-20261005-evidence/manifest.json) SHA 为 `40c997ce2fef2441c687154acb3ba2a0f8967288aaadddea93d8eb40fd8857aa`。7 件分析版 gzip 包含三份 JSON、三份日志及源码清单，保存全部真实调用、plan、VM、返回 multiset、首次失败和续测场景。三件 helper 仅保留私有路径/SHA，不发布合成密码；五件二进制 pstats 仅保存 descriptor、字节数与原始 SHA，公开表使用 JSON 原件已经保存的 profile 条目，不发布含私有路径的 pstats 二进制。参数、内部 ID、guard、资料根、PID、内存地址和秘密经脱敏或哈希；清单记录原件/脱敏/gzip SHA，gzip `mtime=0`，全部解压回验相等。未复制数据库、ZIP、凭据或真实资料，未修改主阶段、路线图、AGENTS、生产及既有档案。

| 原件 | 原始 SHA256 |
| --- | --- |
| remaining-cost profile JSON | `4b61281de2a0011e5cd26062524f9d10ac43092f7f0b6743a97283fabbad1c3f` |
| 首次失败 JSON | `b15f0942c2d2282f7ae18e2b7fbe5e5c7d0b390c7de40269e187cba4ad36a233` |
| 首次 helper（已核对） | `a2a848bfab1c3dfe2034b029fea8f2737fafbe936f1fcd332029ccf564d4dac9` |
| memory-only 续测 JSON | `3c0204728c4c7f39e1ac8641e7862be5961437f7dce98b01dbfdaa52966345a4` |
