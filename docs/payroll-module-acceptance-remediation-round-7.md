# 工资模块第七轮验收整改任务书

> 状态：独立验收通过
> 复验日期：2026-08-10
> 适用仓库：`ai-accounting-core`
> 上位设计：[工资、社保、公积金与个税模块开发基线](./payroll-module-development-plan.md)
> 前轮记录：[工资模块第六轮验收整改任务书](./payroll-module-acceptance-remediation-round-6.md)
> 完成定义：本文件全部编号关闭，前六轮通过项无回归，并由总设计与验收 Agent 在独立 PostgreSQL 17 上复验通过

> 验收结论：R7-001 至 R7-008 已全部关闭；工资模块第一阶段核心实现可进入后续集成准备，但仍属于单企业私有模拟试用，不是法定工资、个税申报或税务意见系统

## 1. 第六轮独立验收结论

执行 Agent 与验收 Agent 已分别取得以下绿色门禁：

- 非 PostgreSQL：102 passed，76 deselected。
- PostgreSQL：76 passed，102 deselected。
- 全量：178 passed，6 warnings。
- 覆盖率：84%。
- `ruff check .`、`pip check`、`compileall`、`git diff --check` 通过。
- 真实 `0006 → 0007` 预检、空库迁移往返和 STDIO 工资生命周期通过。

但绿色测试没有证明 R6-001 要求的完整累计下游闭包。服务层会阻止不完整更正，数据库提交点却仍只验证直接受影响批次，因此第六轮结论为 **不通过**。

## 2. 开发纪律

- 新增线性迁移 `0008`，不得回写 `0001` 至 `0007`。
- 默认 `alembic.ini` 指向的现有 finance 数据库禁止写入、升级或降级；所有迁移和 PostgreSQL 验证必须使用临时 PostgreSQL 17 容器。
- 服务预检和测试通过不能代替数据库提交点约束。
- 迁移测试必须使用目标 revision 的真实 Schema，不得临时添加 head 字段。
- 不暂存、不提交，不修改或清理用户及其他 Agent 的无关改动。
- 本轮其余并行审计结论将追加到本文件；执行 Agent 不得因先完成 R7-001 而结束会话。

## 3. 已关闭阻断项（历史问题与修复验收）

本节保留 R7-001 至 R7-008 的原始问题、实施要求和最低测试，作为审计轨迹；这些项目均已在本文件第七节的最终验收中关闭，不是当前待办。

### R7-001：数据库版本更正屏障必须覆盖累计下游闭包

`FinanceService._tax_downstream_closure` 已将同员工、同支付税年、从最早直接受影响工资起的后续累计工资纳入服务层阻断集合。`0007` 的 profile/policy 数据库断言却只查找 successor 生效期内直接引用 ancestor 的正式批次；tax-state slot 的反向触发仍调用同一直接断言。

因此可能出现以下不一致状态：

1. 九月和十月累计工资均已正式确认。
2. 只规范冲正九月，十月累计工资仍保持正式。
3. 插入覆盖九月的 profile 或 policy successor。
4. 数据库只看到九月直接批次已冲正，未把仍依赖九月累计状态的十月批次纳入阻断。

独立 PostgreSQL 17 复现输出：

```text
EARLY_CANONICAL_REVERSAL_WRITE posted []
DOWNSTREAM_CLOSURE_COMMIT ACCEPTED
OCT_FINAL_STATUS posted
```

必须实现：

- 数据库先识别 profile/policy successor 的直接受影响批次，再计算同员工、同支付税年的累计下游闭包。
- 普通工资和 `combined` 奖金属于累计链；`separate` 奖金仅在直接使用被更正版本且适用日期相交时纳入。
- 只有直接批次和全部累计下游均完成规范冲正后，successor 才可提交。
- version、batch、line、tax-state slot 的 INSERT、UPDATE、DELETE 必须从 OLD/NEW 两侧重验受影响维度。
- 与现有持久 guard 使用同一锁域和固定锁序，不能产生新并发不一致或死锁。
- `0007 → 0008` 必须在 DDL 前识别已经存在的部分冲正污染；拒绝时 revision 与存量逐字段不变。

最低 PostgreSQL 测试：

- 九月、十月普通工资均正式；仅冲正九月后，九月 profile successor 和 policy successor 均在 commit 拒绝；再冲正十月后允许。
- 同月 `combined` 奖金与后续月份累计链。
- `separate` 奖金直接受影响和仅时间上位于其后的两种语义。
- profile 所属期跨年、支付税年边界。
- 跨员工共享 policy 的闭包和固定锁序。
- batch、line、tax-state slot 的 OLD/NEW 反向变化。
- 真实 `0007 → 0008` 污染预检、空库往返、`alembic check` 和安全降级。

### R7-002：法定缴款期间与控制政策兼容键必须按类别确定

`0007` 的迁移预检、正式事件延期断言和服务兼容键均使用 `batch.payroll_period` 作为全部法定缴款的期间键。个税纳税月份应由实际 `payment_date` 决定；工资所属期相同不代表个税税月相同。

独立 PostgreSQL 17 已确认：普通工资与 separate 奖金均属于 `2026-09`，但分别于 `2026-09-05` 和 `2026-10-05` 支付；两笔个税来源仍被合并为一笔正式缴款并成功提交。

此外，个税控制政策目前取 `policy_snapshot.income_tax_policy.id`。规范关系应使用带复合外键的 `batch.policy_version_id`；当前没有数据库约束证明 JSON 快照 ID 必定等于该列。社保、公积金则必须继续使用按所属期冻结的 `policy_snapshot.contribution_policy.id`。

政策字段独立提交点复现：

```text
POLICY_COLUMNS_DIFFER True
SNAPSHOT_IIT_IDS_EQUAL True
IIT_COLLECTION_COMMIT ACCEPTED
```

必须实现统一的类别化兼容键：

- 个税：`policy_version_id` + `payment_date` 的 `YYYY-MM` 税月。
- 社保、公积金：`contribution_policy.id` + `payroll_period` 缴费所属期。
- 两类均继续包含企业、法定类别、机构 ID、机构代码和 CNY 币种。
- 服务预检、数据库延期断言与 `0007 → 0008` 历史预检必须使用完全相同的语义。

最低 PostgreSQL 测试：

- 相同 `payroll_period`、不同支付税月的个税来源在 commit 拒绝。
- 不同 `payroll_period`、相同支付税月的个税来源按税月兼容规则处理。
- `policy_version_id` 不同但 JSON `income_tax_policy.id` 相同的个税来源在 commit 拒绝。
- 社保、公积金继续按 contribution policy 与所属期区分。
- 兼容的 regular + separate bonus 正常路径、所有反向依赖和历史污染预检保持通过。

### R7-003：MCP 公开契约与真实 STDIO 验收证据必须一致

真实 STDIO 主流程已经通过，但 `finance_register_evidence` 和 `finance_import_bank_statement` 仍以 `dict[str, Any]` 发布，MCP 参数 Schema 是任意对象。处理器内部的 Pydantic 校验不能替代 Agent 可见的准确契约。

现有 STDIO 测试也存在聚合断言过宽的问题：若某个预期来源关系缺失而另一个关系重复，或者一条银行流水缺历史而另一条多出历史，部分断言仍可能通过。

必须实现：

- 两个工具直接使用类型化请求，公开 Schema 准确描述字段并设置 `additionalProperties: false`；未知字段在工具边界稳定拒绝且信息脱敏。
- 法定缴款、工资计提、发薪和冲正按事件逐条核对规范 PayrollEventLink，包括批次、来源开放项、来源支付事件和类别。
- 批次证据精确核对 `evidence_id`；代扣权益和支付分配逐条核对 ID、金额、支付事件和冲正事件。
- 五条导入银行流水逐条核对返回 ID、CSV 字段、初始 current/history；每次付款与冲正后逐条核对 history、active/current、`invalidated_by_event_id` 和失效时间。
- 工资流水冲正后复用的“两条历史、一个 active/current”完整断言保持通过。
- 员工、profile、policy、试算和确认节点核对影响计算与审计的关键持久字段、计算哈希、政策/证据关系和 trace。
- STDIO 子进程负责写入，父进程使用新 Session 读取验证；不得在同一事务中自证。

最低测试：真实 FastMCP parameters 契约、额外字段拒绝、完整 STDIO 生命周期及每个步骤的独立数据库断言。

### R7-004：把已确认的数据保护行为固化为持久回归

独立 PostgreSQL 17 已确认以下运行时行为正确：

- 正式工资来源 Settlement 在同一 UPDATE 中同时修改 `id` 与 `amount_fen`，commit 被拒绝且原行保持。
- 正常服务冲正成功，Settlement 恰好只变化 `reversed` 与 `reversed_by_event_id`。
- 正式 Evidence 的全部十个现有字段逐列不可修改；未引用草稿可按契约编辑。

这些证明目前主要存在于独立验收过程，仓库自动化没有完整覆盖组合变化和逐字段结果。必须补充：

- Settlement `id + amount_fen` 组合 UPDATE 的 commit 拒绝、原 ID 全字段不变、新 ID 不存在。
- 正常服务冲正前后逐字段比较，并验证反转指针精确指向本次冲正事件。
- `0007 → 0008` 污染预检失败时，不仅 revision 不推进，相关存量也逐字段不变。
- 使用真实历史 Schema 的 `0005 → head` 与 `0007 → head` 专项；不得使用 head DDL 预处理夹具。

### R7-005：银行导入行级错误必须脱敏，并完善条件式公开 Schema

虽然 MCP 外层未知字段已经稳定返回脱敏错误，银行导入内部仍将逐行解析异常的 `str(exc)` 直接写入工具结果。真实 STDIO 输入无效日期后返回了包含原始单元格值的异常文本：

```text
Invalid isoformat string: 'TOP_SECRET_DATE_VALUE'
```

这说明行级结果绕过了统一异常信息脱敏边界。

必须实现：

- 银行导入逐行错误只返回稳定错误码、字段名和行号，不返回异常文本、原始单元格、文件路径、SQL 或长输入。
- 日期、金额、缺列及文件解析错误均有真实 STDIO 哨兵测试，确认工具结果与服务日志接口不回显输入值。
- Evidence 公开 JSON Schema 表达 `file_path` 与 `content_base64` 恰好一个。
- 银行导入公开 Schema 表达允许的 canonical mapping key，以及 `booking_date + (amount | debit + credit)` 必要条件；运行时 Pydantic 与公开 Schema 语义一致。
- 法定来源实际类别集合精确等于预期并绑定本批次/来源计提；profile/policy 返回 ID、政策来源 URL 和关键持久字段逐项核对。
- PayrollLine 的 gross/net、个税、社保、公积金关键计算字段进入 STDIO 持久化断言。
- 非复用的四条银行流水按 bank ID 逐条验证 history、active/current 与失效关系，不再只做总数聚合。
- 新增测试必须通过 Ruff；不得以关闭 E501 规则代替格式修正。

### R7-006：迁移合并残留与当前 head 断言必须收口

独立迁移矩阵已通过 revision 长度、线性 head、`0005/0007 → head`、空库往返、元数据一致性、污染预检回滚及降级保护，但发现：

- `test_payroll_postgres_invariants.py` 的降级保护测试仍把拒绝后的当前版本硬编码为 `0007`；正确结果应为 `0008_payroll_r7_tax_closure`。
- `0008` 中存在两段字节级重复的过渡函数 DDL，且随后又被正式 R7 定义覆盖。

必须实现：

- 更新旧 head 断言并复跑完整迁移测试。
- 删除重复 DDL，只保留必要的迁移前置定义和最终定义，不改变行为。
- 重跑空库 `upgrade head`、`alembic check`、`0007 → head`、downgrade/re-upgrade 与全量 PostgreSQL 门禁。
- 所有 Alembic 命令显式绑定临时容器 URL；不得依赖默认 `alembic.ini` 连接。

### R7-007：把 R7-001/002 的完整类别矩阵固化为仓库测试

独立一次性 PostgreSQL 复验已确认当前 `0008` 的以下行为正确，但仓库 R7 测试仅保留两个主反例，不能防止后续回归。

必须固化：

- 全部累计链规范冲正后，profile 与 policy successor 均可提交。
- 同月 `combined` 奖金进入累计下游；时间上位于其后的 `separate` 奖金不进入累计链；直接使用被更正版本的 `separate` 仍阻断。
- 十二月到次年一月不跨支付税年扩展累计闭包。
- 双员工共享 policy 时，任一员工仍有同税年累计下游就阻断；全部相关链冲正后放行，并验证固定锁序。
- 个税 `policy_version_id` 不同但 JSON snapshot ID 相同的正式来源集合在 commit 拒绝。
- 社保、公积金按 `contribution_policy.id + payroll_period` 的允许与拒绝正反例。

所有夹具必须保持正式事实符合现有唯一约束、批次状态和来源关系，不得删除约束或把非规范 batch kind 的结果当作验收证据。

### R7-008：强化约束不得更名既有稳定错误码

独立 PostgreSQL 全组为 `82 passed, 2 failed`。两项失败均因 `0008` 将既有 `R6_*` 数据库错误码改成新的 `R7_*` 名称，而非数据保护行为失效。

必须实现：

- profile、policy、opening correction 继续使用 `0007` 已建立的稳定 `R6_*_CORRECTION_BLOCKED` 错误码。
- 法定缴款兼容集合继续使用 `R6_FINAL_STATUTORY_PAYMENT_INCOMPATIBLE_SOURCES`。
- 服务层领域错误映射、旧测试与新测试使用同一稳定契约；不得让调用方通过迁移版本判断同一业务冲突。
- 先复跑两个旧失败用例，再复跑完整 PostgreSQL 组。

## 4. 并行复核项目（已关闭）

以下项目在最终验收前均已完成独立复核并固化为回归：

- R6-003 Settlement 组合身份字段变化的提交点拒绝、正常冲正允许字段及反转指针均已验证。
- R6-005 的政策、期间、机构、类别、企业和反向依赖边界已由法定缴款兼容矩阵覆盖。
- 真实 STDIO 工资生命周期已按写入步骤使用独立 Session 核对持久状态与最终关系。
- `0007 → 0008`、真实 `0005 → head`、空库往返、元数据一致性和安全降级均已通过临时 PostgreSQL 17 验证。

## 5. 当前验收门禁

```powershell
pytest -ra
pytest -m postgres -ra
pytest --cov=ai_accounting --cov-report=term-missing
ruff check .
python -m pip check
python -m compileall -q src alembic tests
alembic check
git diff --check
```

迁移命令只允许对临时 PostgreSQL 17 执行。最终验收前，任何编号未关闭时，第七轮均应判定为“不通过”。

## 6. 最终独立验收记录

2026-08-10 在稳定工作区和临时 PostgreSQL 17 上完成最终复验：

- 非 PostgreSQL：`103 passed, 96 deselected`。
- PostgreSQL：`96 passed, 103 deselected`。
- 完整覆盖率运行：`199 passed, 6 warnings`，总覆盖率 `84%`。
- R7 PostgreSQL 持久矩阵：`13 passed`。
- Ruff、`pip check`、`compileall`、`git diff --check`：通过。
- 空库迁移、真实 `0005/0007 → head`、`base → head → base → head`、污染预检回滚、降级保护与 `alembic check`：通过。
- 真实 STDIO 覆盖证据、银行导入、工资确认/支付、三类法定缴款、冲正及同一工资流水复用；输入契约和行级错误脱敏通过。
- 默认 compose finance 数据库只读确认仍为 `0001_initial`，未被验收迁移推进。

已保留 6 条既有 datetime/Pydantic 警告；它们不影响本轮结果，但应在后续依赖升级工作中清理。验收执行当时工作区保持未暂存、未提交，随后由独立收尾步骤审阅并提交。
