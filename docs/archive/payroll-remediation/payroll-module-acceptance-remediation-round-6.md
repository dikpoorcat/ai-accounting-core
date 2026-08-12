# 工资模块第六轮验收整改任务书

> 状态：可选旧整改参考。本文件不构成审计保留义务，也不是当前规范、待办、验证要求或执行指令。
> 状态：开发已交付；独立验收不通过，转入第七轮整改
> 复验日期：2026-08-10
> 适用仓库：`ai-accounting-core`
> 上位设计：[工资、社保、公积金与个税模块开发基线](../../payroll-module-development-plan.md)
> 前轮记录：[工资模块第五轮验收整改任务书](./payroll-module-acceptance-remediation-round-5.md)
> 完成定义：R6-001 至 R6-005 全部关闭，前五轮通过项无回归，并由总设计与验收 Agent 在独立 PostgreSQL 17 上复验通过

> 后续整改：[工资模块第七轮验收整改任务书](./payroll-module-acceptance-remediation-round-7.md)

## 1. 第五轮独立验收结论

执行 Agent 和验收 Agent 均复跑通过以下门禁：

- 非 PostgreSQL：102 passed。
- PostgreSQL：58 passed。
- 全量：160 passed，6 warnings。
- 覆盖率：84%。
- `ruff check .`、`pip check`、`compileall`、`git diff --check` 通过。

R5 的 Settlement 冲正审计字段、持久版本 guard、通用冲正证据继承、工资 reversal 来源边、幂等信封、兼容多批次服务路径和真实 STDIO 工资流水复用均已有实质改善。

但独立直接 SQL 和受控双连接仍成功制造正式事实污染，因此第五轮结论为 **不通过**：

```text
SEALED_EVIDENCE_CREATED_AT_UPDATE=ACCEPTED True
DIRECT_SUCCESSOR_OVER_FINAL_PAYROLL=ACCEPTED
PREVIEW_AFTER_DIRECT_CORRECTION calculated
USES_NEW_PROFILE_WITH_OLD_TAX_CHAIN True
register correction => registered
concurrent confirm => posted
FINAL_SETTLEMENT_PRIMARY_KEY_UPDATE=ACCEPTED True
NON_HEX_SHA256_FINAL_EVIDENCE calculated posted zzz...(64)
DIRECT_INCOMPATIBLE_MULTI_PERIOD_STATUTORY_PAYMENT=ACCEPTED 2026-09 2026-10
```

## 2. 开发纪律

- 新增线性迁移 `0007`，不得回写 `0001` 至 `0006`。
- 数据库约束必须同时保护目标事实和所有反向依赖；服务预检不能代替提交点约束。
- 并发测试必须使用两个独立连接和确定性同步点，证明双方都越过旧预检后仍不可能双双提交。
- 历史迁移测试必须使用目标 revision 的真实 Schema，不得临时增加 head 列后再删除。
- 失败必须返回稳定领域错误；不得用裸数据库异常或笼统 `INTERNAL_ERROR` 代替可分类冲突。
- 不得削弱现有触发器、测试、整数分、精确冲正、证据继承和来源链约束。
- 不暂存、不提交，不修改或清理其他 Agent 与用户的无关改动。

## 3. P0 阻断项

### R6-001：correction 屏障必须在数据库和并发两个方向闭包

第五轮仅在三个公开登记方法中查询已存在的正式工资。直接插入合法 lineage successor 可以绕过屏障；后续预览会选择新 profile，却继承旧正式工资的累计税态。

公开服务也存在 TOCTOU：correction 事务先查到无正式批次后暂停，confirm 事务用旧版本完成重算后暂停，correction 提交 successor，confirm 随后仍可提交。最终 correction 与旧版本正式工资同时存在。

必须实现：

- profile、policy、opening 三个版本维度的 INSERT、UPDATE、DELETE 在数据库提交点计算正式下游闭包；直接 successor 也必须拒绝。
- successor 写入和 payroll finalization 必须取得同一持久 guard 域；不能只让版本写之间互斥。
- payroll batch、line、tax slot、policy/profile/opening 关系任一 OLD/NEW 变化均反向重验。
- profile 的适用日期是 `payroll_period` 月末，不是 `payment_date`；两者跨月或跨年时不得漏掉 correction。个税/期初累计状态仍按实际支付税月判断，禁止用一个日期代替两种业务事实。
- profile guard 至少按企业和员工；policy guard 按企业和政策维度，并与批次内所有员工 guard 按固定顺序组合；opening 按企业、员工、税年和状态月份。
- 事务中完成全部规范冲正后允许 successor；只冲正部分累计下游仍拒绝。
- `0006 → 0007` 全量扫描已存在的 successor 与 posted payroll 混用污染，失败不得推进 revision。

最低测试：

- profile、policy、opening 三类直接 INSERT successor 与 UPDATE 移入阻断维度。
- correction 与 confirm 两连接同步写偏斜，两种提交顺序。
- 同月 combined、跨月累计税态、separate 奖金及跨员工共享 policy。
- 所属期月末与支付日跨月、跨年的 profile successor，顺序调用和并发调用均覆盖。
- 全部依赖冲正后登记成功并可重建；部分冲正仍拒绝。
- 多员工锁序无死锁，连接池复用无会话锁遗留。

### R6-005：法定缴款兼容性必须成为数据库事件级集合不变量

第五轮只在 `FinanceService._statutory_payment_compatibility_key` 比较来源。直接构造一张平衡的正式个税缴款、两条真实 Settlement 和两条各自合法的 statutory PEL，即可把 2026-09 与 2026-10 两个不兼容税期合并并成功提交。

必须实现：

- 对 posted 社保、公积金、个税缴款的完整 statutory PEL 集合建立延期断言。
- 集合兼容键至少包含企业、法定类别、机构 ID 与机构代码、政策版本、缴费所属期/税期和币种。
- 每条来源边继续逐一证明 batch、来源支付、开放项及真实 settlement；集合断言不能替代单边断言。
- business event、PEL、source batch、source open item、counterparty 的 OLD/NEW 变化均反向重验受影响正式事件。
- 正式事件只有一条来源时也验证完整兼容键；不得通过删除到单边或替换支撑对象绕过。
- `0006 → 0007` 扫描全部正式法定缴款集合并拒绝污染。

最低测试：直接构造跨期、跨政策、跨机构 ID/代码、跨类别和跨企业集合均在 commit 拒绝；同期间兼容 regular + separate bonus 成功；服务不兼容请求保持原子拒绝；多来源冲正集合精确复制。

## 4. P1 必须整改项

### R6-002：正式 Evidence 必须冻结登记时间

`finance_block_sealed_evidence_mutation` 比较内容、所有者和存储字段，但漏掉 `created_at`。已引用正式工资证据的登记时间可被任意改写。

必须实现：

- sealed/final 引用存在时，Evidence 除非明确列入追加式扩展表，否则整行不可 UPDATE 或 DELETE。
- 至少补齐 `created_at`，并审查 ORM 与数据库的全部列，避免未来新列默认落入可变区。
- 草稿 Evidence 仍可按明确契约编辑；一旦引用对象 sealed/final 即冻结。
- 迁移不得重写既有 `created_at`。

最低测试：直接修改 `created_at`、同时修改时间和其他字段、主键/所有者攻击均拒绝；正常登记与草稿编辑通过。

### R6-003：正式 Settlement 必须冻结核销身份

第五轮拒绝 DELETE+INSERT 替换，却允许 `UPDATE settlements SET id = new_uuid`。PayrollEventLink 没有 settlement ID 外键，因此主键替换成功且规范边表面仍能找到同一付款/开放项关系，审计身份已变化。

必须实现：

- 支撑 posted/reversed payroll PEL 的 Settlement 冻结 `id`。
- 审查 Settlement 全部列；正式支撑关系只允许 `reversed=false/reversed_by=NULL → reversed=true/reversed_by=规范冲正事件` 的原子单向转换。
- 任何主键、企业、开放项、付款事件、金额或其他身份字段变化均拒绝。
- 合法 service reversal、迁移回填和查询历史保持通过。

最低测试：主键 UPDATE、组合主键/金额变化、DELETE+INSERT、伪冲正拒绝；正常付款冲正与银行流水复用通过。

### R6-004：数据库必须验证 SHA-256 的规范编码

`length(sha256)=64` 会接受 64 个 `z`、空白或其他非十六进制内容。该对象可以被正式工资引用并成功入账，无法声称保存的是 SHA-256。

必须实现：

- 选择唯一规范编码；推荐 lowercase 十六进制 `[0-9a-f]{64}`，与服务实际输出一致。
- 数据库 check、服务 Schema、登记幂等和升级预检使用同一规范。
- `0006 → 0007` 遇到非 hex、非规范大小写、空白或 Unicode 污染时拒绝，不得猜测或自动改写历史摘要。
- 正式对象和草稿 Evidence 均不得保存伪 SHA-256。

最低测试：非 hex、uppercase（若采用 lowercase）、前后空白、Unicode、63/65 位均拒绝；合法摘要、同企业重复摘要幂等与跨企业合法重复通过。

## 5. 必须保持的第五轮通过项

- Settlement 付款事件、开放项、企业、金额和规范 reversal 审计保护。
- Evidence 内容、所有者、存储路径和 metadata 冻结。
- 三类版本之间的并发非祖先重叠 guard。
- 通用冲正证据继承与工资五类 reversal PEL 精确集合。
- 工资试算、确认、支付、法定缴款和冲正的并发幂等信封。
- 兼容多批次法定缴款的服务正常路径及逐边查询。
- 真实 STDIO：工资付款 → 冲正 → 复用同一工资银行流水；两条历史、一个 active/current。
- 前四轮所有数据库、迁移、税务、MCP 和会计不变量。

## 6. 第六轮验收门禁

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

1. R6-001 至 R6-005 的修复前输出、修复后 commit 输出和测试映射。
2. correction 与 confirm 的确定性同步双连接证明，不能依靠概率循环。
3. `0006` 五类污染分别拒绝升级且 revision/数据不变。
4. 空库 `base → head → base → head`、真实 `0005/0006 → head`、安全降级和两次 `alembic check`。
5. 全量真实 STDIO 工资生命周期保持通过。

任何一项未关闭时，第六轮仍为“不通过”。

## 7. 开发 Agent 交付格式

```text
完成编号：R6-...
修改文件：...
新增迁移：0007...
编号到测试映射：...
修复前主动反例：...
修复后 commit 结果：...
并发/直接 SQL/迁移/STDIO 证据：...
全量门禁：...
未完成项与风险：...
是否暂存/提交：...
```
