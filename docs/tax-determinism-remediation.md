# 税务事实与期间结算确定性整改基线

> 状态：已完成并通过独立验收（2026-08-10）
> 优先级：无形资产与借款利息阶段的强制前置门禁
> 目标：关闭税务事实推断默认、税期幂等与重叠、结算快照漂移、税则有效期歧义和 MCP 任意对象契约
> 边界：仍是单企业私有模拟试用内核，不是增值税申报、税务意见或法定结账系统

## 1. 裁决背景

工资和固定资产第一期已经完成独立验收，路线图的下一业务目标仍为“无形资产与借款利息”。开始扩展前的跨模块审计确认，通用事件和税务期间仍存在会直接改变会计处理的确定性缺口：

- 空的 `tax_facts` 会被补成“应税、1%、无发票、不放弃免税、当期计税”并正式过账；
- 费用事件未提交费用角色时会被补成普通费用；
- 税期过账使用相同幂等键但改变载荷时会回放旧事件，不报告冲突；
- 不同幂等键可形成重叠正式税期，结算后还可补录或冲正来源事件；
- 通用税则允许重叠有效期，并在歧义时静默选择起始日较新的规则；
- `finance_calculate_tax_period` 的公开 MCP 参数仍是任意对象。

这些问题违反“缺少业务事实必须返回 `needs_information`”和“税则必须有效期化”的仓库不变量。本基线不改变下一业务目标，只定义其开始实现前必须完成的前置整改。

## 2. 不可破坏的公共原则

- Agent 只能提交业务事实，不能提交科目代码、借贷方向或自由分录行。
- 金额继续只接受严格整数分；税率和税额计算只使用 `Decimal`，不使用二进制浮点。
- 缺少会改变计税、免税、发票或费用归类的事实时返回 `needs_information`，不得从默认值推断。
- 正式税期、税则、来源事件、凭证和计算轨迹不得原地重解释；更正必须使用关联冲正。
- 所有公共请求 `additionalProperties: false`，错误只暴露稳定错误码，不回显 SQL、路径、连接串或长输入。

## 3. TAXC-001：税务与费用事实必须显式

`TaxFacts` 的下列事实不得有业务默认：

- `taxable`
- `rate_percent`
- `invoice_type`
- `waive_exemption`
- `tax_due_on_event`

服务现销、服务赊销、预收和履约等适用事件必须逐项提交。任一缺失时返回 `needs_information` 并列出精确字段；不得生成凭证、开放项、银行匹配或正式税务派生事实。

`AmountFacts.expense_account_role` 也不得默认为普通费用。仅费用现付、费用挂账和员工报销需要该字段，有限值仍为 `general_expense` 或 `finance_expense`；其他事件不因该字段为空而失败，也不得使用它选择科目。

本项不得改变既有稳定税率集合、发票类型和 CNY 边界。严格布尔字段只接受 JSON `true`/`false`。

## 4. TAXC-002：税期预览与确认必须分离

公共工具稳定为：

- `finance_calculate_tax_period`：只读预览；接收类型化 `TaxPeriodPreviewRequest`，返回计算结果、来源事件集合和 `calculation_hash`；
- `finance_confirm_tax_period`：写入确认；接收类型化 `TaxPeriodConfirmRequest`，必须携带同一企业、期间、哈希和幂等键。

企业按月申报时，只接受完整自然月；按季申报时，只接受一、二、三、四季度的完整自然季度。期间不得跨越增值税或附加税规则版本。预览和确认均重新选择规则并计算；来源事件、组织配置、规则或金额变化导致哈希变化时，稳定拒绝 `TAX_PERIOD_CALCULATION_STALE`。

计算哈希至少覆盖：企业、自然期间、申报周期、城建税率、增值税规则 ID/版本/有效期/官方来源/参数、附加税规则 ID/版本/有效期/官方来源/参数、按稳定顺序排列的来源事件 ID 及其税务派生金额、全部计算结果。JSON 规范化后使用 SHA-256。

## 5. TAXC-003：幂等、唯一期间与并发

确认请求哈希包含专用命令名、企业、期间和计算哈希：

- 同一幂等键、同一载荷回放原结果；
- 同一幂等键、不同载荷拒绝 `TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH`；
- 同一企业已有相同或重叠的有效正式税期时拒绝 `TAX_PERIOD_ALREADY_POSTED` 或 `TAX_PERIOD_OVERLAP`；
- 并发确认相同或重叠税期最多一个成功。

正式税务事件必须保存非空 `request_payload_hash`。服务预检不能替代 PostgreSQL 提交点约束；直接 SQL 和双连接并发也不能制造重叠有效税期。

## 6. TAXC-004：正式税期是封闭快照

正式税期冻结预览中的完整计算、规则与来源事件集合。税期有效时：

- 禁止新增、改写或删除落入该期间的正式应税来源；
- 禁止直接冲正任一来源事件；
- 禁止修改、删除或重绑税期、调整事件、凭证、规则和来源集合；
- 需要更正时先使用 `finance_reverse_event` 冲正税期调整，再更正来源事实并重新预览、确认。

税期冲正只创建关联反向事件和反向凭证；原税期快照、原事件、原凭证和来源关系保持不变。`status` 只允许由规范冲正触发 `posted -> reversed`，不得直接改回。

## 7. TAXC-005：税则有效期和不可变性

同一 `code + jurisdiction` 的税则有效期不得重叠。规则选择必须唯一：无有效规则拒绝现有缺失规则错误；多条有效规则拒绝 `TAX_RULE_AMBIGUOUS`，不得按排序静默选取。

税则一经创建不得原地更新；需要更正时新增不重叠的后继版本。正式事件引用的税则不得删除。线性迁移必须在安装约束前检查历史重叠和已漂移税期，发现污染时拒绝升级，revision 和存量逐字段不变。

## 8. TAXC-006：最低稳定错误契约

- `TAX_PERIOD_INVALID_BOUNDARY`
- `TAX_PERIOD_SPANS_RULE_CHANGE`
- `TAX_PERIOD_CALCULATION_STALE`
- `TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH`
- `TAX_PERIOD_ALREADY_POSTED`
- `TAX_PERIOD_OVERLAP`
- `TAX_PERIOD_SOURCE_LOCKED`
- `TAX_PERIOD_SNAPSHOT_IMMUTABLE`
- `TAX_PERIOD_NO_ADJUSTMENT`
- `TAX_RULE_AMBIGUOUS`
- `TAX_RULE_EFFECTIVE_RANGE_OVERLAP`
- `TAX_RULE_IMMUTABLE`

已有业务冲突错误码保持稳定；不得让调用方根据迁移版本判断同一冲突。

## 9. 实现工作包

- A：严格税务/费用事实、自然期间、计算哈希、确认幂等、类型化 MCP 及 SQLite 单元/服务/MCP 测试。`schemas.py`、`tax.py`、`service.py` 和 `mcp_server.py` 由同一写入负责人串行修改。
- B：`models.py`、线性迁移、污染预检、税期/税则提交点保护、不可变与并发 PostgreSQL 测试。迁移文件和模型由单一负责人修改。
- C：独立负向验收、真实 MCP Schema/STDIO、跨模块回归和文档复核；不得把实现者报告直接视为验收结论。

## 10. 最低验收矩阵

- `tax_facts={}` 及逐字段缺失均返回 `needs_information`，数据库无正式副作用；费用角色缺失同理。
- 合法显式税务事实继续生成原有平衡模板，金额与税务轨迹不回归。
- 月/季边界正反例、规则版本边界、哈希陈旧、同键换载荷、相同/重叠税期串行与并发。
- 正式税期后新增来源、冲正来源、直接修改税期、直接更新/删除税则均在 PostgreSQL commit 拒绝；规范冲正后允许更正与重算。
- 迁移覆盖 `0009 -> head -> 0009 -> head`、空库往返、历史污染预检和 `alembic check`，只使用显式唯一命名的临时 PostgreSQL 17。
- 真实 FastMCP 参数均为类型化对象且 `additionalProperties: false`；真实 STDIO 由子进程写入、父进程新 Session 核对快照和冲正链。
- 全量执行非 PostgreSQL、PostgreSQL、覆盖率、Ruff、`pip check`、`compileall`、`alembic check` 和 `git diff --check`。

全部门禁通过并形成独立验收记录后，才进入无形资产与借款利息实现。

零调整预览可以正常返回计算结果，但确认稳定拒绝
`TAX_PERIOD_NO_ADJUSTMENT`，且不创建业务事件、凭证、`TaxPeriod` 或
`TaxPeriodSource`。因此它不是已封闭税期，也不是会计月结；零凭证税期封闭留给
“会计期间与月结”阶段，不得把本阶段能力描述为法定结账或申报。

本基线的实现与门禁证据见
[税务事实与期间结算确定性整改验收记录](./tax-determinism-acceptance.md)。
