# AI Accounting Core

面向中国小规模纳税人服务型企业的确定性记账内核。它让 Codex 等 Agent 提交“发生了什么”，但不允许 Agent 自由编造借贷分录。只有资料完整且规则唯一的业务事件才会原子入账；信息不足时返回 `needs_information`。

> 当前版本是单企业私有试用内核，不是法定账簿、财务报表、纳税申报或税务意见。投入实际使用前，应由有资质的中国会计和税务专业人士复核企业配置及政策规则。

## 已实现

- 服务现销/赊销/履约、客户回款/预收/退款。
- 费用现付/挂账、供应商付款、员工报销。
- 股东借款/投入/归还、银行手续费、内部转账、税款支付。
- 应收应付开放项及严格核销，禁止超额核销制造负数往来。
- 小规模纳税人价税分离、期间起征点、增值税减免和附加税试算。
- SHA-256 内容寻址证据库、CSV/XLSX 银行流水导入与去重。
- 幂等入账、期间关闭校验、关联冲正、凭证规则轨迹和审计日志。
- PostgreSQL 延迟借贷平衡约束及已入账凭证不可改删触发器。
- 工资、社保、公积金、累计个税和全年一次性奖金的登记、试算、确认、支付与冲正闭环。
- 固定资产外购、启用、逐月直线折旧、出售/报废、资产卡片和逆序冲正闭环。
- 外购无形资产的取得、可供使用当月起直线摊销、月末零收入报废和逆序冲正闭环。
- 持牌金融机构人民币固定利率借款的放款、合同单利计提、付息、到期还本和逆序冲正闭环。
- 税期试算与计算哈希确认、来源快照封闭和税务事实锁定。
- 自然月会计期间逐月生成、结账前累计完整性检查、计算哈希确认、不可变关闭快照和关闭月写保护。
- 38 个严格参数的本地 STDIO MCP 工具；无聊天 UI、REST API 或模型调用。

工资、固定资产、无形资产和借款只能通过各自的专用工具登记、试算和确认；通用事件入口不会接受 Agent 自行组织这些分录。存货事件仍会明确返回 `MODULE_NOT_ENABLED`。

## 本地启动

要求 Python 3.12、PostgreSQL 17；推荐使用 Docker Desktop。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
docker compose up -d
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\finance-bootstrap.exe --name "测试服务公司" --filing-cycle quarterly
```

`finance-bootstrap` 会输出企业 `org_id`。后续所有工具调用都必须携带该 ID。

项目包含 `.codex/config.toml`，其命令、工作目录和证据目录按本仓库固定路径 `D:\GitHub\ai-accounting-core` 配置；如果仓库位于其他路径，需要同步修改这三个值。在 Codex 中信任并打开本仓库、确认 PostgreSQL 已启动和迁移完成后，重启 Codex 即可加载 `ai_accounting` MCP。Codex 官方配置说明见 [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

Docker Compose 中的数据库账号仅用于本机开发，不得复用于共享或生产环境。数据库端口只绑定到 `127.0.0.1`。

## MCP 工作流

1. `finance_get_profile`：读取企业、科目和税务政策。
2. `finance_get_event_schema`：取得业务事件 JSON Schema。
3. 可选调用 `finance_register_evidence`、`finance_import_bank_statement`。
4. `finance_query_context`：查询开放项和未匹配流水。
5. `finance_record_event`：提交业务事实。
6. `finance_get_event`：审阅事实、凭证、证据和轨迹。
7. 需要更正时使用 `finance_reverse_event`，不要修改旧凭证。

所有金额均为整数“分”，日期均为 ISO `YYYY-MM-DD`。

### 会计期间与月结专用工作流

产品创建的新企业默认启用期间控制；正式业务必须先生成对应的开放自然月：

1. `finance_generate_accounting_period` 从企业开始记账的任意过去月份起逐月连续生成；不能跳月，也不能生成 Asia/Shanghai 当前月之后的月份。
2. `finance_preview_accounting_period_close` 只读重算本月凭证、账户发生额、固定资产折旧、无形资产摊销、借款计息、工资批次及人工复核计数，并返回 SHA-256 计算哈希。
3. `finance_confirm_accounting_period_close` 在税期企业锁和月份锁内复算同一哈希；系统阻断为零、六项人工复核全真且有确认说明和证据时，保存完整不可变快照并单向关闭期间。
4. `finance_get_accounting_periods` 查询生成、关闭动作和期间状态。空月份不会自动跳过，但允许经过同样的显式复核后关闭。
5. 一期不支持反结账。关闭月原事实和凭证不改；错误只能在后续已生成开放月通过关联冲正及原专用工作流重记。

所有企业的正式入账日都不得晚于 Asia/Shanghai 当前日期。历史税期更正保留原税务归属期，但调整凭证必须显式记入后续开放会计月。完整边界见[会计期间与月结开发基线](docs/accounting-period-close-development-plan.md)和[产品决策记录](docs/accounting-period-close-decisions.md)。

### 工资专用工作流

工资不经过 `finance_record_event` 的自由事件路径，按以下顺序调用：

1. `finance_register_employee` 登记员工。
2. `finance_register_employee_profile_version` 和 `finance_register_payroll_policy_version` 登记有效期版本。
3. 仅在系统年中启用或迁移历史累计状态时调用 `finance_register_payroll_opening_state`。
4. `finance_preview_payroll` 试算并取得计算哈希。
5. 用户核对事实后，由 `finance_confirm_payroll` 使用同一哈希确认并入账。
6. 工资发放及社保、公积金、个税缴纳通过受支持的业务事件核销正式开放项。
7. `finance_get_payroll_batch` 查询完整计算、政策、凭证、支付和冲正链；更正仍使用 `finance_reverse_event`。

资料缺失、政策无有效版本、累计状态断层或支付无法唯一归属时，内核返回 `needs_information` 或稳定拒绝原因，不推测会改变会计处理的事实。

### 固定资产专用工作流

固定资产同样不经过 `finance_record_event`，只接受有限业务事实：

1. `finance_acquire_fixed_asset` 登记外购待启用资产；银行现付必须精确匹配流水，供应商挂账会生成受控应付开放项。
2. `finance_activate_fixed_asset` 启用资产并冻结直线法、使用寿命月数、预计净残值、受益区域和官方规则来源。
3. `finance_preview_fixed_asset_depreciation` 按启用月份次月起算单月折旧并返回计算哈希。
4. `finance_confirm_fixed_asset_depreciation` 复算同一哈希后生成固定模板凭证；月份必须连续且入账日必须属于该折旧月份。
5. `finance_dispose_fixed_asset` 处理单项非不动产资产出售或零收入报废，自动读取原值和累计折旧并计算清理损益；出售按有效的旧固定资产专项增值税规则计算。
6. `finance_get_fixed_asset` 查询资产卡片、政策版本、全部历史规范事实、凭证、证据和冲正链。
7. 更正仍使用 `finance_reverse_event`，顺序为处置、最新折旧、启用、购置；原凭证和规范事实不修改。

房屋建筑物、土地、自建/改建、融资租赁、减值、加速折旧、所得税折旧及税会差异仍不在本阶段范围。完整契约见[固定资产模块开发基线](docs/fixed-asset-module-development-plan.md)。

### 无形资产专用工作流

无形资产一期只支持外购、已可供使用、可单独识别且不含土地权利的单项资产：

1. `finance_acquire_intangible_asset` 登记取得事实、成本组成、供应商、使用寿命、受益区域和证据；银行现付精确匹配流水，挂账生成受控应付开放项。
2. `finance_preview_intangible_asset_amortization` 从可供使用当月开始试算下一个连续自然月，并返回计算哈希。
3. `finance_confirm_intangible_asset_amortization` 在锁内复算同一哈希并生成固定摊销模板；最后一个月按整分余数闭合。
4. `finance_retire_intangible_asset` 只处理自然月末、收入/赔偿/税费/残料均明确为零的报废；当月必须已摊销或此前已经摊足。
5. `finance_get_intangible_asset` 查询资产、规范事实、凭证、证据、累计摊销和冲正链。
6. 更正顺序为报废、最新摊销、取得；原资产编号不复用。

研究开发、土地、商誉、后续资本化、减值、出售和税务摊销不在一期范围。完整契约见[无形资产与借款利息开发基线](docs/intangible-assets-and-borrowing-development-plan.md)。

### 借款专用工作流

借款一期只支持中国持牌金融机构、人民币、单次全额放款、固定利率、合同单利、到期一次还本且无需资本化的合同：

1. `finance_draw_borrowing` 冻结合同、贷款人、应付息日、利率、日计数基础、支持边界事实、银行流水和证据。
2. `finance_preview_borrowing_interest` 按合同下一应付息日及 `[period_start, period_end)` 实际天数试算利息并返回计算哈希。
3. `finance_confirm_borrowing_interest` 在锁内复算合同和有效计息链后生成财务费用与应付利息凭证。
4. `finance_pay_borrowing_interest` 只清偿唯一关联的有效计息事件，付款日不得早于计息期末或晚于合同到期日。
5. `finance_repay_borrowing_principal` 只在到期日、连续计息至到期且全部有效利息已支付后一次归还全部本金。
6. `finance_get_borrowing` 查询合同、有效余额、规范事实、凭证、证据和冲正链；更正顺序为还本、付息、最新计息、放款。

循环额度、多次提款、浮动利率、复利、罚息、提前或分期还本、展期、外币、非金融企业贷款和借款费用资本化不在一期范围。

### 10,100 元服务现销示例

```json
{
  "org_id": "替换为-bootstrap-输出的-UUID",
  "idempotency_key": "bank-20260808-001",
  "event_type": "service_cash_sale",
  "business_dates": {
    "business_date": "2026-08-08",
    "fulfillment_date": "2026-08-08",
    "payment_date": "2026-08-08",
    "tax_obligation_date": "2026-08-08",
    "posting_date": "2026-08-08"
  },
  "amounts": {"gross_amount_fen": 1010000, "currency": "CNY"},
  "tax_facts": {
    "taxable": true,
    "rate_percent": "1",
    "invoice_type": "ordinary",
    "waive_exemption": false,
    "tax_due_on_event": true
  },
  "description": "已完成咨询服务并收到款项"
}
```

该事件生成：借银行存款 10,100 元，贷主营业务收入 10,000 元、应交增值税 100 元。收到款项但无法确定是回款、预收还是收入时，应提交 `customer_receipt`；没有核销或预收分类时，内核会返回 `needs_information`。

## 测试

```powershell
.\.venv\Scripts\pytest.exe -m "not postgres"
.\.venv\Scripts\pytest.exe -m postgres
.\.venv\Scripts\pytest.exe --cov=ai_accounting --cov-report=term-missing
.\.venv\Scripts\ruff.exe check .
```

第一条是快速反馈门禁，主要使用内存 SQLite；第二条需要 Docker，并使用隔离的临时 PostgreSQL 17 验证迁移、并发、延迟约束与不可变触发器；第三条运行完整测试并生成覆盖率。迁移往返和污染数据验证只允许连接临时测试库，默认 Compose `finance` 数据库视为用户状态，未经明确授权不得用于破坏性验收。

## 文档

- [文档索引](docs/README.md)
- [固定资产模块第一期最终验收](docs/fixed-asset-module-acceptance.md)
- [固定资产模块开发基线](docs/fixed-asset-module-development-plan.md)
- [无形资产与借款利息第一期最终验收](docs/intangible-assets-and-borrowing-acceptance.md)
- [无形资产与借款利息第一期开发基线](docs/intangible-assets-and-borrowing-development-plan.md)
- [会计期间与月结第一期开发基线](docs/accounting-period-close-development-plan.md)
- [会计期间与月结第一期最终验收](docs/accounting-period-close-acceptance.md)
- [会计期间与月结产品决策记录](docs/accounting-period-close-decisions.md)
- [工资模块开发基线](docs/payroll-module-development-plan.md)
- [工资模块第七轮最终验收](docs/payroll-module-acceptance-remediation-round-7.md)
- [多 Agent 协作与本地质量验证手册](docs/agent-collaboration-and-local-verification.md)

## 当前税务规则边界

- 默认规则只覆盖 2026-01-01 至 2027-12-31 的中国小规模纳税人试点配置。
- 企业按月或按季、城建税率均为显式配置。
- 起征点采用“不含税销售额严格小于阈值”判断；达到阈值时不免税。
- 专用发票或明确放弃免税的销售不进入减免金额。
- 小规模纳税人的采购税额随价税合计进入费用或相关资产成本，不形成进项抵扣。
- 2026-01-01 起出售自己使用过的非不动产固定资产，按 3% 含税基数换算并减按 2% 计算增值税；是否进入期间起征点减免仍取决于发票与放弃免税事实。
- 每次政策更新必须新增有效期版本和官方来源，不能覆盖历史规则。

政策依据：

- [2026—2027 年小规模纳税人相关政策](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247426/content.html)
- [2023—2027 年“六税两费”减半政策](https://www.mof.gov.cn/jrttts/202308/t20230802_3899936.htm)
- [中华人民共和国城市维护建设税法](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193055/content.html)
- [增值税会计处理规定](https://www.mof.gov.cn/gkml/caizhengwengao/2017wg/wg201703/201707/t20170707_2641107.htm)
- [2026 年小规模纳税人出售自己使用过固定资产专项规则](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html)
- [小企业会计准则附录](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)

## 许可证状态

当前项目元数据标记为 `Proprietary`，未授予开源许可证。公开仓库可见不等于获得复制、修改或再分发授权；如需开源，应另行选择许可证并增加 `LICENSE` 文件。
