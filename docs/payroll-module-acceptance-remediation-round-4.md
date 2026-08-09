# 工资模块第四轮验收整改任务书

> 状态：开发已交付；第四轮独立复验不通过，后续以第五轮任务书为准
> 复验日期：2026-08-10
> 适用仓库：`ai-accounting-core`
> 上位设计：[工资、社保、公积金与个税模块开发基线](./payroll-module-development-plan.md)
> 前轮记录：[工资模块第三轮验收整改任务书](./payroll-module-acceptance-remediation-round-3.md)
> 完成定义：本文 R4-001 至 R4-011 全部关闭、前轮通过项无回归，并由总设计与验收 Agent 独立复验通过

> 第四轮复验结果：自动门禁 139 passed、PostgreSQL 40 passed、覆盖率 84%，但主动直接 SQL、双连接和真实 STDIO 审计仍发现正式来源链、证据内容、版本并发与幂等边界缺口，故未达到完成定义。整改要求转入 [工资模块第五轮验收整改任务书](./payroll-module-acceptance-remediation-round-5.md)。

## 1. 第四轮结论与边界

第三轮交付的自动化结果为 119 passed，其中非 PostgreSQL 93 passed、PostgreSQL 26 passed，覆盖率 84%；`ruff`、`pip check`、`compileall`、隔离 PostgreSQL 17 迁移往返和 `alembic check` 均通过。年度个税 guard、正常银行重记账、严格 MCP Schema 和跨年政策快照等正向路径已有明显改善。

但独立直接 SQL、双连接和真实 STDIO 攻击仍能搬走正式关系、伪造来源和证据、带病升级、破坏银行镜像或泄漏未知异常内容。因此第三轮验收结论为 **不通过**。全部 P0、P1 关闭前，不得标记工资模块完成或进入下一阶段。

必须保持的通过项：

- R3-001：`(org_id, employee_id, tax_year)` 持久 guard；跨税月确认和确认/冲正通过同一顺序域串行化，多员工按固定顺序锁定。
- R3-002 已存在状态槽时的同员工、同支付税月和 final batch 形状验证。
- R3-004：事件必须先 draft 再 posted；退款不再把被退款原事件当作 reversed；正常退款冲正可提交。
- R3-005：三类公开 successor 基本写入、叶节点选择和历史草稿旧引用。
- R3-008：同一原事件、同键同载荷的并发冲正可重放。
- R3-009：服务正常路径支持付款 → 冲正 → 同一流水重记账，并保留两条匹配历史、一个 current。
- R3-010：跨所属期/支付年时，社保与个税快照使用各自实际规则；确认会拒绝被篡改草稿快照。
- R3-011：开放项金额与状态污染会在 `0003 → 0004` 前拒绝，且 head 主动重验旧开放项。
- R3-012：真实 STDIO 的 Pydantic 预校验不再回显 `input_value`；Schema 严格、金额严格整数且无任意借贷/科目输入。
- 最终凭证平衡、期间关闭、企业隔离、整数“分”、`Decimal` 税率和关联冲正原则。

开发纪律：

- 新增线性迁移 `0005`；不得回写 `0001` 至 `0004` 来掩盖升级路径缺陷。
- 触发器涉及父键 UPDATE 时必须分别检查 `OLD` 和 `NEW`，并分别重验旧父对象和新父对象；不得用 `COALESCE(NEW.parent_id, OLD.parent_id)` 代替。
- 所有 PostgreSQL 不变量测试必须在 `commit` 点断言；只在 `flush` 失败不代表延期约束完整。
- 迁移测试必须从真实 `0003`、`0004` 污染数据开始；空库 head 测试不能替代存量升级。
- 并发测试使用两个独立连接与同步屏障；顺序调用两个连接不算并发。
- 不得增加自由分录、自由科目或 Agent 自选会计处理能力。
- 不得删除、放宽或跳过现有测试；不得修改、清理、暂存或提交用户及其他 Agent 的无关改动。

## 2. P0 阻断整改

### R4-001：正式合并奖金必须存在且唯一占用税务状态槽

对应前轮：R3-002。

当前数据库只在状态槽存在时校验槽形状。直接写入跨员工 `combined` 奖金并省略状态槽即可正式提交。

必须实现：

- 对正式、非冲正 `annual_bonus/combined` 批次建立从批次或工资行反向触发的延期断言；每个工资行必须恰有一个同企业、同员工、同支付税月状态槽，且 `final_batch_id` 为该奖金批次。
- `regular_payroll_batch_id` 必须指向同员工、同支付税月、已正式普通工资行；普通工资槽的 `regular_batch_id` 必须一致。
- 对正式普通工资同样验证每名员工恰有一个槽；单独计税奖金必须没有综合所得槽。
- 批次、工资行或槽任一 INSERT/UPDATE/DELETE 均重验所有受影响对象，不能通过省略关系绕过。

最低测试：跨员工/月普通工资依赖但不建槽、漏建一名员工槽、重复槽、错误 final、separate 误建槽、合法 combined，以及批次正式后直接删除槽。

### R4-002：同时保护正式关系的旧父对象与新父对象

对应前轮：R3-003、R3-006、R3-007。

三个触发器在 UPDATE 时只检查 `NEW` 父键，已实测成功：

```text
entitlement：posted 工资行 → draft 工资行，正式行权益 1 → 0
PayrollEventLink：posted 事件 → draft 事件，正式来源边 2 → 1
PayrollBatchEvidence：sealed 批次 → draft 批次，封存证据 1 → 0
```

必须实现：

- 权益 UPDATE 分别检查 `OLD.payroll_line_id` 和 `NEW.payroll_line_id`；任一属于正式、冲正或已取代批次即禁止改变父键，并分别延期重验旧/新批次权益集合。
- 来源边 UPDATE 分别检查旧/新事件；任一最终事件即禁止改事件 ID、批次、来源事件、开放项或种类。
- 批次证据 UPDATE 分别检查旧/新批次；封存后整条边不可改，证据变化只能创建新 superseding 草稿。
- 为三个关系补齐“移出正式”“移入正式”“草稿间移动”“同父普通字段修改”矩阵；正式对象 INSERT/DELETE 仍必须拒绝。
- 审核 `0005` 所有不可变触发器，禁止再次出现只验证 `COALESCE(NEW.parent_id, OLD.parent_id)` 的父键迁移漏洞。

### R4-003：升级时全量验证正式工资权益与来源关系

对应前轮：R3-003、R3-006。

已复现两类带病升级：

- `0003` 正式工资行社保为 0，但养老 entitlement 为 100，升级到 head 后污染被冻结。
- `0003` 的正式 `salary_payment` 边没有 `source_open_item_id`，升级后显式调用 head 断言才失败。

必须实现：

- `0005` 安装新约束前，对全部正式/冲正/已取代工资批次执行权益集合与工资行快照一致性断言。
- 对全部正式 PayrollEventLink 执行当前 head 的完整形状断言；旧结构信息足以确定时进行确定性回填，不能唯一确定时以稳定诊断拒绝升级。
- 同时扫描 R4-001 的普通工资/合并奖金状态槽存在性和唯一性。
- 所有预检在 DDL 前或同一事务内完成；失败后 revision 与数据保持原样，不得把不满足 head 不变量的数据升级为 head。

最低迁移测试：`0003` 污染权益、缺来源开放项、缺状态槽、错误奖金依赖分别失败；可唯一回填旧边成功；正常 `0003/0004 → head` 后逐行显式断言通过。

### R4-004：正式事件证据必须同企业、不可变并与批次集合一致

对应前轮：R3-007。

通用 `event_evidence` 没有 `org_id`，已实测对正式工资事件删除本企业证据、插入外企业证据并提交；批次与计提事件证据集合也没有数据库一致性约束。

必须实现：

- `event_evidence` 增加 `org_id`，用复合外键绑定事件和证据；迁移扫描跨企业污染并拒绝，不得猜测归属。
- 最终事件的证据边禁止 INSERT、UPDATE、DELETE；draft 阶段可构建，finalize 后冻结。
- 正式工资计提事件的证据集合必须与其 PayrollBatchEvidence 完全一致；确认只能复用同一 Evidence 对象。
- 冲正事件/冲正批次明确继承原证据集合，并保存自己的冲正原因证据时采用可区分但不可变的关系类型；不得静默覆盖原集合。
- 批次与事件任一状态变化、证据边变化均在提交点重验集合。

最低 PostgreSQL 测试：正式事件跨企业插入、删除、替换、移动到 draft、集合少一项/多一项、正常确认与冲正继承、存量跨企业迁移拒绝。

### R4-005：冲正事件必须绑定真实原事件和精确反向凭证

对应前轮：R3-004。

当前最终事件断言允许 `payroll_accrual` 充当 reversal，但没有要求工资冲正批次存在或原事件为工资计提。已成功用孤立 `payroll_accrual` 冲正普通现销。现有测试中的 reversal voucher 也可不设置 `reversal_of_voucher_id`，数据库未验证分录为原凭证的精确反向。

必须实现：

- 普通冲正统一使用 `event_type='reversal'`；如工资计提冲正继续使用 `payroll_accrual`，必须有同企业正式 reversal PayrollBatch，且原事件和原批次均为工资计提。
- posted reversal 本身必须引用一个同企业 posted 原事件；原事件必须同步变为 reversed 且 `reversed_by_event_id` 指向该 reversal，禁止孤立 reversal。
- reversal voucher 必须设置 `reversal_of_voucher_id` 指向原事件正式凭证；数据库验证逐行科目、往来、借贷金额完全反向且总额一致。
- 禁止跨企业、自引用、循环、重复冲正，以及 payroll reversal 反转非工资事件。
- 银行匹配、代扣分配和来源边所谓“正式失效/冲正”必须引用这一同一规范冲正关系。

最低测试：孤立 reversal、无 voucher link、错误金额/科目/往来、工资冲正销售、普通 reversal 工资计提、跨企业/循环/重复，以及服务正常收入、费用、工资、退款冲正。

### R4-006：工资来源边必须证明批次与开放项语义

对应前轮：R3-006。

当前法定缴款边可使用真实工资支付和真实核销，却把 `payroll_batch_id` 指向无关同企业批次；也未验证社保、公积金、个税事件与开放项类别对应。

必须实现：

- 法定缴款边的 claimed batch 必须等于来源 salary payment 规范边的 batch，并且该工资支付真实核销该来源开放项。
- 社保事件只接受 employer/withheld social 类别，公积金只接受 housing 类别，个税只接受 individual income tax 类别；机构、员工和险种维度必须与开放项及权益一致。
- 一笔缴款的每个直接来源支付/开放项各有独立边；禁止 `NULL` 代表多来源，禁止挂无关批次。
- 正式边不可移动或篡改，并保留完整冲正链。
- `finance_get_payroll_batch` 和相关查询必须实际读取并返回规范 PayrollEventLink，包括 batch、来源支付、来源开放项、政策/资料和 reversal；不得只从开放项递归或 JSON 重建。

最低测试：无关同企业 batch、错误法定类别、来源支付没有真实核销、两次部分发薪、多批次兼容/不兼容、边移动与完整查询结果。

## 3. P1 必须整改

### R4-007：所有并发幂等冲突都必须回读并比较载荷

对应前轮：R3-008。

两个不同原事件并发使用同一冲正幂等键时，一个 posted，另一个抛裸 `IntegrityError/uq_event_org_idempotency`，而不是稳定 `IDEMPOTENCY_PAYLOAD_MISMATCH`。

必须实现：

- reverse_event 捕获唯一冲突，回滚到 savepoint 后按企业和键回读，比较完整请求哈希并返回重放或 mismatch。
- 不得只依赖锁同一原事件；不同原事件、同一键也必须稳定收口。
- 工资确认、工资支付、三类法定缴款及普通业务事件使用相同处理模式。

最低双连接矩阵：同键同载荷、同键异金额/日期/银行/分配/原事件、不同键争同一对象、失败事务回滚后重试；任何路径不得向 MCP 泄露 IntegrityError。

### R4-008：银行 current 指针与匹配历史必须双向强一致

对应前轮：R3-009。

只把 BankTransactionMatch 标为正式失效、不清 `bank_transactions.matched_event_id` 可以提交，形成 pointer 指原事件但 active edge 为 0；还可创建指向已 reversed 事件的 current edge。

必须实现：

- match INSERT/UPDATE/失效也必须触发对应 BankTransaction 的 current 镜像断言；bank row 更新也验证匹配历史，形成双向触发。
- current edge 的 event 必须是 posted；reversed 只允许作为已有且已失效的历史边。
- 失效要求 matched event 已由同一规范 reversal 冲正；必须同步清理/替换 current pointer。
- 保留每流水最多一个 active edge的唯一约束及不可变历史。

最低测试：仅失效 edge、仅改 pointer、active 指 reversed、错误 reversal、同事务合法失效、并发争用、付款冲正后同流水重记账。

### R4-009：successor 仍须拒绝与非祖先版本重叠

对应前轮：R3-005。

服务在提供 predecessor 时跳过全部重叠校验。已复现资料 A 为 1–6 月、独立资料 B 为 7–12 月，A 的 successor 扩为 1–12 月并成功登记，8 月生效查询随后返回 `AMBIGUOUS_EMPLOYEE_PROFILE`。

必须实现：

- 计算 predecessor 的完整祖先集合；只允许新版本与该集合重叠，与任何非祖先当前链节点重叠均拒绝。
- 资料、政策和期初三类采用一致规则；数据库约束与服务校验均覆盖分叉、自环、环、跨维度和非祖先重叠。
- 并发创建同一 predecessor successor 时，同载荷重放、异载荷稳定拒绝，不得抛裸唯一冲突。
- 明确已有正式下游工资时 correction 的登记与重新使用规则，禁止旧下游未冲正时静默混用新版本。

### R4-010：MCP 未知异常也必须统一脱敏

对应前轮：R3-012。

当前包装只改写 ValidationError；工具内部 `RuntimeError('SECRET-UNKNOWN-987654')` 会被 FastMCP 原样返回。

必须实现：

- 在最外层 Tool 调用边界捕获并分类所有异常；ValidationError 返回字段路径，数据库返回稳定数据库码，未知异常只返回 `INTERNAL_ERROR`。
- 响应不得包含异常消息、`repr`、请求片段、SQL、连接串、堆栈或输入哨兵；服务端日志也按敏感信息策略处理。
- 不得通过关闭严格校验、吞掉业务拒绝结果或返回模糊成功来脱敏。

最低真实 STDIO 测试：预校验、数据库异常、ValueError/OSError、未知 RuntimeError、超长字符串、SQL/连接串、身份证和银行卡哨兵。

### R4-011：降级不得静默丢弃 R3 关系数据

当前 0004 降级只检查 tax-year guard 和 bank match。含 `source_open_item_id` 的正式来源边可降到 0003，列被删除且来源信息静默丢失；代扣分配 `reversed_by_event_id` 同样未纳入保护。

必须实现：

- `0005` 降级预检覆盖所有仅由 0004/0005 表达的列、表、索引和语义关系。
- 无法无损映射到旧 revision 时以稳定诊断拒绝，不得清空或截断审计关系。
- 可安全降级时证明所有新列为空或能确定性恢复；DDL 失败必须事务回滚，revision 和数据原样保留。

最低测试：含 source_open_item、分配 reversal、证据 org、bank history 和税年 guard 的降级拒绝；空数据安全降级；失败后列、数据和 revision 均保持 head。

## 4. 工作包与顺序

### 工作包 A：奖金状态、权益与迁移

范围：R4-001、R4-002 的 entitlement、R4-003、R4-011。独占 `models.py` 与 `0005` 迁移，先定义延期断言和存量预检。

### 工作包 B：事件、来源和证据

范围：R4-002 的 event link/evidence、R4-004、R4-005、R4-006。与 A 共同评审 `0005`，随后独占相关 `service.py` 查询、确认和冲正段落。

### 工作包 C：事务、银行、版本与 MCP

范围：R4-007、R4-008、R4-009、R4-010。在 A、B 的关系契约落地后实施，避免同时编辑共享事务路径。

### 集成工作包

真实 STDIO PostgreSQL 17 流程必须在每一步之后立即从独立数据库连接核对，而非流程末尾集中检查。重记账用例必须冲正工资付款本身并复用它的银行流水；来源边逐条核对事件、批次、开放项和类别。

推荐顺序：A/B 先形成数据库契约并串行合并 `0005`；B 接服务；C 收口并发和协议；最后集成。共享文件由调度者显式交接，禁止多 Agent 同时编辑。

## 5. 第四轮独立验收门禁

```powershell
pytest -ra
pytest -m postgres -ra
pytest --cov=ai_accounting --cov-report=term-missing
ruff check .
python -m pip check
python -m compileall -q src tests
alembic upgrade head
alembic check
git diff --check
```

额外必须提供：

1. R4-001 至 R4-011 的修复前失败、修复后通过映射。
2. 三种 final → draft 父键迁移攻击全部在 commit 点失败，旧正式对象事实保持不变。
3. 从污染 `0003`、污染 `0004` 到 head 的升级分别拒绝或确定性回填，升级后全表显式断言通过。
4. 正式工资事件跨企业证据、集合不一致、来源边错批次/错类别攻击失败。
5. 冲正事件和凭证精确反向；孤立 payroll_accrual 不能冲正普通业务。
6. 双连接幂等、银行 current 镜像和版本 successor 争用返回稳定业务结果，无裸数据库异常。
7. 真实 STDIO 未知异常与所有哨兵脱敏，每一步 MCP/数据库双向核对。
8. 隔离 PostgreSQL 17 上 `0003 → head`、`0004 → head`、空库往返、安全降级和 `alembic check`。

任何 P0 或 P1 未关闭时，第四轮仍为“不通过”。现有 119 个测试继续通过只是必要条件，不是充分条件。

## 6. 开发 Agent 交付格式

```text
完成编号：R4-...
修改文件：...
新增迁移：...
编号到测试映射：R4-... → tests/...::test_...
修复前主动反例：...
修复后结果：...
并发/直接 SQL/迁移/STDIO 证据：...
全量门禁：...
未完成项与风险：...
是否暂存/提交：...
```

实现选择与本文裁决不同，必须在编码前向总设计与验收 Agent 报告并取得书面裁决。
