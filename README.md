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
- 9 个本地 STDIO MCP 工具；无聊天 UI、REST API 或模型调用。

工资、固定资产、无形资产、借款利息和存货事件会明确返回 `MODULE_NOT_ENABLED`。

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

项目包含 `.codex/config.toml`。在 Codex 中信任并打开本仓库、确认 PostgreSQL 已启动和迁移完成后，重启 Codex 即可加载 `ai_accounting` MCP。Codex 官方配置说明见 [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

## MCP 工作流

1. `finance_get_profile`：读取企业、科目和税务政策。
2. `finance_get_event_schema`：取得业务事件 JSON Schema。
3. 可选调用 `finance_register_evidence`、`finance_import_bank_statement`。
4. `finance_query_context`：查询开放项和未匹配流水。
5. `finance_record_event`：提交业务事实。
6. `finance_get_event`：审阅事实、凭证、证据和轨迹。
7. 需要更正时使用 `finance_reverse_event`，不要修改旧凭证。

所有金额均为整数“分”，日期均为 ISO `YYYY-MM-DD`。

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
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

默认单元测试使用内存 SQLite 以提高反馈速度；带 Docker 的 PostgreSQL 集成测试验证迁移、延迟平衡约束与不可变触发器。

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
