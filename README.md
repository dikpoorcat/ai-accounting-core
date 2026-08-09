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
- 16 个严格参数的本地 STDIO MCP 工具；无聊天 UI、REST API 或模型调用。

工资只能通过专用工具登记、试算和确认；通用事件入口不会接受 Agent 自行组织工资分录。固定资产、无形资产、借款利息和存货事件仍会明确返回 `MODULE_NOT_ENABLED`。

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
- [工资模块开发基线](docs/payroll-module-development-plan.md)
- [工资模块第七轮最终验收](docs/payroll-module-acceptance-remediation-round-7.md)
- [多 Agent 协作与本地质量验证手册](docs/agent-collaboration-and-local-verification.md)

## 当前税务规则边界

- 默认规则只覆盖 2026-01-01 至 2027-12-31 的中国小规模纳税人试点配置。
- 企业按月或按季、城建税率均为显式配置。
- 起征点采用“不含税销售额严格小于阈值”判断；达到阈值时不免税。
- 专用发票或明确放弃免税的销售不进入减免金额。
- 小规模纳税人的采购税额随价税合计进入费用，不形成进项抵扣。
- 每次政策更新必须新增有效期版本和官方来源，不能覆盖历史规则。

政策依据：

- [2026—2027 年小规模纳税人相关政策](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247426/content.html)
- [2023—2027 年“六税两费”减半政策](https://www.mof.gov.cn/jrttts/202308/t20230802_3899936.htm)
- [中华人民共和国城市维护建设税法](https://fgk.chinatax.gov.cn/zcfgk/c100009/c5193055/content.html)
- [增值税会计处理规定](https://www.mof.gov.cn/gkml/caizhengwengao/2017wg/wg201703/201707/t20170707_2641107.htm)
- [小企业会计准则附录](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)

## 许可证状态

当前项目元数据标记为 `Proprietary`，未授予开源许可证。公开仓库可见不等于获得复制、修改或再分发授权；如需开源，应另行选择许可证并增加 `LICENSE` 文件。
