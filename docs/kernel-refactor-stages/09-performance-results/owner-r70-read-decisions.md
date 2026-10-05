<!-- @format -->

# r70：清偿范围复用与报表必要读取决策

本轮只归纳已存在的实现、定向回执和只读诊断，不追加测试、性能测量或验收。r69 固定主 48 月六入口仍未完成浏览器整页刷新或当前源码完整核验，也没有全部达到 500 毫秒；r70 的清偿构造调整尚无统一候选的整页收益证据。仓库仍为 draft/0，不据此完成第 9 阶段。

## 同一快照的相等清偿范围

根因是先历史后当前时，`settlement_freeze._scope` 在判断可复用前再次构造冻结根的 groups、期间计数和目标月金额，随后丢弃新对象。当前实现将这些构造移到相等范围提前返回之后。提前返回仍位于完整开放尾部核验之后，只复用同一快照、相同 base_period 且无截止期之后发生额的已核汇总；未来真实付款、更正或未知清偿不能以相等范围处理。两种读取顺序均检查实际金额及必要输入处理量，不增加跨请求缓存，不调整结构、接口或业务处理。

此公共范围被简报待收待付、员工工资未付、资产付款范围、报表清偿位置和工作清单清偿汇总等调用；不能由公共函数构造减少推定每页净收益。资金与上下文当前不直接使用此范围，完整冻结投影核验和固定 v1 历史读取仍保留原职责。具体修改范围见 [相等范围决策](owner-equal-snapshot-scope-r70.md)。

| 已存在的回执 | 实际结果与边界 |
| --- | --- |
| `.tmp/stage9-settlement-equal-scope-r70.log/.xml` | 第一批 3 passed，日志 12.92 秒；XML tests=3、failures=0、errors=0，suite time=12.651 秒。覆盖两种顺序的相等范围处理量、未来无关发布损坏、历史／当前金额与完整 SQL 比较。 |
| `.tmp/stage9-settlement-equal-scope-r70-remaining.log/.xml` | 第二批 14 passed、3 deselected，日志 36.13 秒；XML tests=14、failures=0、errors=0，suite time=35.890 秒。包含同快照发布证明、来源损坏、冻结页及目录、完整核验与维修等剩余已有回归。 |
| `.tmp/stage9-settlement-equal-scope-r70-ruff.log` | `All checks passed!`，只记录已有静态检查回执。 |

上述是先 3 项、再排除该 3 项后的 14 项，不能写成整组 17 项重新运行一次，也不能合成不存在的总耗时。XML suite time 与 pytest 日志墙钟时间各按原记录保存。本次整理没有另跑回归，当前源的归档副本是整理时版本，不能自动等同于测试启动时不可变源码。

## 报表读取的必要范围与负面判断

以下精确 ID 诊断引用 fixed r69 源，声明的候选 source SHA 为 `600fc82ccf6437c28c81c03ff56d04ddb0abaf07446782535ffb0dc29baa8af8`。来源是 `.tmp/stage9-report-r69-exact-scope-probe.json/.log`；JSON 标注 `readonly_exact_params_scope_probe_no_timing_no_tests`，其范围证据不能当作计时或业务验收。

| 疑似重复工作 | 原始证据 | 决策 |
| --- | --- | --- |
| 月度分类 flow 与当前标量 header | 12 次月度 flow 共 4,932 行、4,932 个不同 ID，跨月重复为 0；当前 header 411 个不同 ID，与 flow 交集为 0。4 个 closed-root ID 与 flow 的成员交集为 0。 | 这是不同核算输入，不能用当前头或其他月份代替，也不能因为字段相似就删除月度读取。本诊断没有显示这批 ID 上的重复。 |
| party 范围相对 full report 是否可缩窄 | party 实际消费 938 个 ID，report 1,002 个，交集 937；party-only 1、report-only 65。 | 即使 party 单独可缩窄，后续 report 仍需其完整范围；当前最终 1,004 次贡献解析不能据此削减。不扩大生产修改。消费 ID、来源证明 ID 和解析调用次数是不同指标，不能直接相加或相等替代。 |
| anchored 来源的两批证明是否重复 | party 请求 939、report 请求 116，二者 union 1,005、交集 50；第二批进入证明前已有 anchored 50，实际 SQL 仅需剩余 66。第一批实际 SQL 939。另有 report 的单个 decoded-source 请求。 | 同一快照已正确跳过交集 50，实际证明读取 939+66=1,005，没有将两批请求量 1,055 当作实际重复读取。必要来源、身份、采用关系与内容核验继续保留。 |
| `dict(row)` 宽行转换 | `.tmp/stage9-r69-row-conversion-diagnostic.json/.log` 记录一次 fetch 后的纯转换；1,005 行×13 列 selected header 的两个纯转换中位数为 1.6014／0.9221 毫秒，净差约 0.68 毫秒。 | 差值很小，不值得为此扩大修改。不是 SQLite 执行、fetch、I/O 或整页计时；18／15／24 列对照也不能转成当前路径净收益，24 列 identity 已使用 zip。 |
| 季报 opened／closed 两次 `_report` | fixed r69 主 48 月 profile 已记录 `_report` calls=1；该样本季度末未关账，dashboard 在 absent quarter-end close 分支跳过 closed plan。 | 不能将静态代码中的 fully closed 两次调用当成本样本重复成本。fully closed 双调用路径本轮未验证，仍是独立待查范围，不能声称所有报表重复问题已排除。 |

纯转换诊断保留默认 GC、每项 11 个样本，JSON 明示 `no_DB_execute_fetch_or_IO_timing=true`。这些已停止的诊断没有改动报表源码。本轮整理不继续诊断，不把负面结果写成全局不存在优化机会。

## 证据保存与未验证范围

[独立归档清单](owner-r70-read-decisions-manifest.json) 保存上述原始计划、日志、XML、fixed r69 原生/profile 记录与相关实现/测试源副本。每个条目记录原始相对路径、SHA256、字节数、mtime 和 gzip 路径、SHA256、字节数、mtime；新文件均用排他创建，解压后逐字节与原始快照比较。旧 r69 manifest 及其归档仅作为原始来源保存，不回写旧档。fixed r69 源与当前 r70 源分别标识，当前源副本不冒充 fixed r69 测量源码。

r70 尚无统一候选五页测量或浏览器整页净收益；fixed r69 的 current_source_full_integrity 明确为 `not_run`，旧 r63 完整核验不能认证 r69/r70。主 120 月、正式独立运行包与实际 AI MCP 最终验收也未完成。既有回归证明所覆盖的业务边界，不能代替以上性能与交付核验。本页没有改主阶段文档，没有操作真实资料或服务，没有执行 Git 提交。
