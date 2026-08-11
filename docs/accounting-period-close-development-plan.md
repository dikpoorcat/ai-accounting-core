# 会计期间生成与月结第一期开发基线

> 状态：第一期已完成并通过最终全量门禁（2026-08-11）
> 阶段目标：完成自然月会计期间生成、结账前检查、预览哈希确认、单向月结、关闭期间写入保护和后续开放月关联更正闭环
> 前置基线：[无形资产与借款利息第一期验收记录](./intangible-assets-and-borrowing-acceptance.md)
> 定位：单企业私有模拟试用内核的内部期间控制，不是法定账簿、财务会计报告、纳税申报、审计意见或授权审批系统

## 1. 总设计裁决

一期只支持公历自然月，状态机固定为 `generated_open -> closed`。不提供反结账、期间重开、关闭状态回退、关闭月补录或历史凭证重排。

关闭后的错误只能在后续**已生成且开放**的自然月，通过现有的关联反向凭证和原专用业务工作流更正。原业务事实、原凭证、原关闭快照和审计链保持不变。请求把更正凭证记回关闭月时稳定拒绝，不得把“后续月更正”描述成反结账。

月结不生成会计分录，不自动补提折旧、摊销、利息、工资或税额，不执行损益结转、年结或期初余额迁移。Agent 不能提交科目、借贷方向、自由分录、账面余额或“检查已通过”等派生结论。

## 2. 官方依据与工程政策

官方依据：

- [《中华人民共和国会计法（2024年）》](https://kjs.mof.gov.cn/zt/kjfxcgc/kjfqw/202408/t20240814_3941788.htm)第十一、十四至十七条：会计年度采用公历年度，会计凭证、账簿和更正须符合统一制度；[修改决定](https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202406/t20240629_3938355.htm)自 2024-07-01 起施行。
- [《企业财务会计报告条例》](https://xzfg.moj.gov.cn/front/law/detail?LawID=722)第十九条：年度结账日为公历年度最后一日，月度、季度结账日为公历期间最后一日。
- [《会计基础工作规范》现行文本](https://www.mof.gov.cn/gp/xxgkml/tfs/201903/t20190318_3195239.htm)第六十至六十六、七十一条：登记、对账、结账和错误更正的基本要求。2025 年公开材料仍是[修订征求意见](https://kjs.mof.gov.cn/gongzuotongzhi/202509/t20250905_3971696.htm)，不能当作已生效依据。
- [《会计信息化工作规范》财会〔2024〕11号](https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805628932632907.pdf)：必要程序、规则可查询校验追溯和日志完整性要求。
- [《会计软件基本功能和服务规范》财会〔2024〕12号](https://kjs.mof.gov.cn/zhengcefabu/202408/P020240805635126967297.pdf)第二十一至二十五条：已记账凭证关键字段不可直接修改，账簿按审核凭证生成，结账前已输入凭证必须登记入账；[发布通知](https://www.mof.gov.cn/jrttts/202408/t20240809_3941453.htm)明确自 2025-01-01 起施行。

一期内部规则版本为 `cn_accounting_period_close_2026.1`，有效起始日为 `2026-08-11`。自然月预生成、连续关闭、单向关闭、后续开放月关联冲正、哈希快照和数据库锁均为本内核的确定性工程政策，不宣称是法规原文唯一实现。

## 3. 启用与兼容边界

产品创建的新企业默认 `accounting_period_control_enabled=true`。迁移时发现的既有企业只为兼容而回填为未启用；将来如真实存在此类企业，必须通过独立历史迁移流程显式启用，不能静默改变既有正式入账行为。

期间按自然月逐月显式生成。首次生成必须提交企业、`YYYY-MM` 月份、确认人、确认说明、证据和幂等键。服务：

1. 首月可以是企业开始记账的任意过去自然月，例如企业 3 月开始经营时以 3 月为控制起始月，不要求从 1 月开始；
2. 将企业控制起始日冻结为首月第一日，只生成该一个完整自然月；
3. 若企业已经存在正式凭证、业务事件或旧期间行，稳定拒绝 `ACCOUNTING_PERIOD_LEGACY_DATA_REQUIRES_MIGRATION`，不静默包裹历史数据；
4. 后续每次只能生成紧接上一个已生成月份的下一个自然月，跨年度时同样连续，不允许跳月、回填或改变起始日；
5. 期控一旦启用不得关闭或原地修改起始日。

启用后，任何正式凭证的入账日都必须命中唯一已生成开放期间；早于控制起始日或落在未生成月份均返回 `ACCOUNTING_PERIOD_NOT_GENERATED`，已关闭返回 `ACCOUNTING_PERIOD_CLOSED`。所有企业的正式入账日都不得晚于 Asia/Shanghai 的当前日期。检查同时存在于服务与 PostgreSQL 提交点；不能依赖调用方选择正确的服务子类。

线性迁移 `0012` 在首个 DDL 前检查既有 `accounting_periods`。任何旧期间行都缺少自然月身份、生成动作和关闭快照，必须以 `ACCOUNTING_PERIOD_LEGACY_PERIOD_PRECHECK_FAILED` 拒绝升级并保持 revision 与存量不变，不能猜测回填。

## 4. 公共工具与严格请求

专用 MCP 工具：

- `finance_generate_accounting_period`
- `finance_preview_accounting_period_close`
- `finance_confirm_accounting_period_close`
- `finance_get_accounting_periods`

所有请求均为类型化 Pydantic 对象且 `additionalProperties: false`。公共接口不接受科目、借贷行、余额、检查结果覆盖值或任意对象。

生成请求必须明确：`org_id`、严格 `YYYY-MM` 的 `period_month`、`idempotency_key`、`confirmed_by`、`confirmation_note` 和至少一份 `evidence_references`。年份只接受 `0001..9999`，月份只接受 `01..12`；按 DEC-010/012，只允许生成 Asia/Shanghai 当前月或过去月份。

预览请求只接受 `org_id`、`period_id` 和 `closing_date`。`closing_date` 必须等于该月最后一日，期间末日不得晚于 Asia/Shanghai 当前日期。预览纯读，不写动作、期间或快照。

确认请求在预览字段外必须携带 `calculation_hash`、`idempotency_key`、严格人工复核事实、`confirmed_by`、`confirmation_note` 和至少一份证据。缺失复核事实返回 `needs_information`；显式为 `false` 返回 `ACCOUNTING_PERIOD_REVIEW_INCOMPLETE`，不关闭期间。

人工复核事实固定为严格布尔字段：

- `voucher_completeness_reviewed`
- `bank_reconciliation_reviewed`
- `open_items_reviewed`
- `payroll_and_statutory_items_reviewed`
- `tax_items_reviewed`
- `asset_and_borrowing_schedules_reviewed`

这些字段只冻结用户声明和证据，不代表内核已经实施真实身份、RBAC、四眼审批、电子签名或专业复核。

## 5. 结账前系统检查

系统阻断项：

1. 企业已启用期控，期间是唯一已生成的完整自然月，状态为 `open`；
2. 期间末日不晚于当前日期，请求结账日精确等于期间末日；
3. 只能关闭企业最早的开放月份，所有此前已生成月份均已关闭；
4. 期间内不存在 `draft` 凭证或 `draft` 业务事件；
5. 每个正式业务事件恰好关联一张正式凭证，凭证与事件同企业、同入账日，分录至少两行、借贷相等、总额为正；
6. 每项有效固定资产启用记录从应折旧首月至本月、寿命终点或有效处置月三者最早月份的全部连续折旧叶均已确认；已处置资产不能把处置前漏提整棵排除；
7. 每项有效无形资产从可供使用月到本月、寿命终点或有效报废月三者最早月份的全部连续摊销叶均已确认；已报废资产不能把报废前漏摊整棵排除；
8. 借款合同中落在本月末及以前的下一应付息日均已连续计息；
9. 已创建的本月工资批次不得停留在草稿、预览或其他未完成状态。

检查器只检查已存在且具有明确合同/卡片/批次事实的义务，不从“没有工资批次”等情况推断无义务，也不自动生成业务事件。未结清应收应付、未匹配银行流水、未确认税期和未提交工资事实可以跨期，本阶段只作为人工复核计数与警告，不自动当作系统阻断。月结后补导入属于已关闭月份的外部银行流水不属于本一期闭环；用户已选择 `DEC-013 B`，后续契约和当前暂停点见[迟到银行外部证据开发基线](./late-bank-evidence-development-plan.md)。

税期确认、增值税申报周期和会计自然月是独立状态机。零调整税期仍不产生 `TaxPeriod`，会计月结也不创建、替代或证明税务申报。

## 6. 预览、哈希与确认

预览输出规范计算对象，至少包含：

- 规则版本、有效期和全部官方来源；
- 企业、期间 ID、年度、月份、自然起止日和结账日；
- 前一期关闭哈希；
- 按稳定顺序排列的系统检查结果、阻断项和人工复核计数；
- 本期全部正式凭证快照：凭证 ID/号码/日期、来源事件 ID/类型、借贷合计和稳定分录摘要；
- 本期按账户稳定排序的借方、贷方和净发生额汇总；
- 固定资产、无形资产、借款、工资待办检查结果；
- 银行未匹配、开放项和税务待复核计数。

规范 JSON 使用 UTF-8、键排序和紧凑分隔符，计算 SHA-256。确认在取得企业月份共同锁和期间行锁后重新读取组织、期间、凭证、事件及各模块事实并重算；任何变化返回 `ACCOUNTING_PERIOD_CALCULATION_STALE`。确认哈希不得依赖来源事件以后可能发生的 `posted -> reversed` 状态或 `reversed_by_event_id` 当前值，而要保存关闭时点状态快照；后续开放月合法冲正不能使旧关闭哈希漂移。

同企业同幂等键同载荷回放同一动作；换载荷返回 `ACCOUNTING_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH`。不同键重复关闭返回 `ACCOUNTING_PERIOD_ALREADY_CLOSED`。确认成功不生成凭证，只创建不可变关闭动作和快照并把期间从 `open` 单向改为 `closed`。

## 7. 持久模型与不可变审计

`AccountingPeriod` 强化为自然月规范行：企业、年度、月份、起止日、生成动作、状态、关闭时间、当前关闭快照 ID，并有企业内自然月唯一、日期恒等、连续/无重叠和 `open -> closed` 单向约束。

新增：

- `AccountingPeriodCalendar`：企业与公历年度的规则容器；同一年度逐月生成的期间复用该容器；
- `AccountingPeriodAction`：`period_generation|period_close`，保存幂等键、请求哈希、`posted|needs_information|rejected`、输入事实、缺失项/错误、确认人、说明和时间；
- `AccountingPeriodClose`：期间、动作、规范计算 JSON 原文、计算哈希、规则/来源、前期关闭哈希、检查器版本、确认时间和统计；
- `AccountingPeriodCloseSource`：关闭时全部凭证及来源事件的稳定快照；
- `accounting_period_action_evidence`：生成/关闭动作与企业内证据的复合外键关系；
- `BusinessEventDependency`：通用预收、履约和退款的规范父子关系。

成功动作必须有非空证据。正式动作、关闭快照、来源集合和依赖关系一经形成禁止更新、删除或重绑。失败动作只保存请求哈希、稳定错误码、字段路径、时间和可用的幂等/调用标识，不长期保存完整无效输入或证据内容；并发不得泄漏裸数据库异常。

## 8. PostgreSQL 提交点与锁序

月结和所有最终凭证写入使用同一 `(org_id, month_start)` PostgreSQL transaction advisory lock。涉及正式业务事实时先取税期企业锁，再取月锁并重读期间；关闭同样先取税期企业锁、再取月锁和期间行，然后以普通 MVCC 快照重算来源全集，不锁来源事件或凭证行。这样避免与冲正链形成锁序反转，close-vs-post 并发最多一方按先后线性化成功，另一方得到稳定领域错误，不得把延迟触发器异常泄漏到外层 `commit`。

期间生成使用独立的企业生成锁，再取目标月锁；服务和直接 SQL 触发器使用相同顺序，防止并发首次生成不同月份造成跳月。涉及税期与会计期间的写操作固定锁序：税期企业锁在前，会计月份锁在后，再锁期间行，最后锁凭证序号并生成凭证。数据库触发器必须自行取得相同锁，不能只依赖 Python 预检。

PostgreSQL 延迟提交点至少复核：

- 启用后期间存在且开放、自然月身份与连续性；
- 关闭来源全集、逐凭证快照、账户汇总、规范 JSON 和 SHA-256；
- 关闭动作、证据、规则、前期哈希和 `closed_at` 一致；
- 不可变、跨企业复合外键、直接 SQL 篡改和删除；
- 同月 post-vs-close、close-vs-close 双连接并发；
- 迁移污染预检和 downgrade 安全边界。

## 9. 关闭后更正与跨模块前置修复

关闭期间原凭证永不修改。后续开放月的冲正凭证必须继续满足税期来源锁、工资累计链、固定资产、无形资产、借款和开放项的既有逆序依赖。

通用 `customer_advance`、`customer_receipt`、`service_fulfillment`、`customer_refund` 的依赖不能只保存在 JSON。`BusinessEventDependency` 固定保存 `advance_fulfillment|advance_refund|sale_return` 父子边；服务和 PostgreSQL 验证同企业、允许的事件类型、金额守恒和最终状态。冲正父事件前按稳定顺序锁有效子事件；仍有有效下游时返回 `REVERSE_DEPENDENT_EVENTS_FIRST`。迁移只能对可唯一证明的历史链回填；跨企业、类型错误、缺失或累计超额均在首个 DDL 前拒绝。

历史税期更正也必须能在后续开放月闭合。`TaxPeriodPreviewRequest` 和 `TaxPeriodConfirmRequest` 同时增加无默认的 `adjustment_posting_date`，否则预览哈希不能冻结该事实：税期 `start_date/end_date` 继续表示税务事实范围，调整事件的税务义务日仍为税期末日，但调整凭证使用显式入账日。该入账日不得早于税期末日，并必须命中已生成开放会计期间；税期计算哈希、确认请求哈希、正式事实和 PostgreSQL 快照均冻结此日期。既有未启用期控企业保持兼容，但不能把调整凭证写入已关闭期间。

## 10. 稳定错误契约

- `ACCOUNTING_PERIOD_REQUIRES_SPECIALIZED_WORKFLOW`
- `ACCOUNTING_PERIOD_LEGACY_PERIOD_PRECHECK_FAILED`
- `ACCOUNTING_PERIOD_LEGACY_DATA_REQUIRES_MIGRATION`
- `ACCOUNTING_PERIOD_CONTROL_ALREADY_ENABLED`
- `ACCOUNTING_PERIOD_INVALID_CALENDAR`
- `ACCOUNTING_PERIOD_GENERATION_OUT_OF_SEQUENCE`
- `ACCOUNTING_PERIOD_FUTURE_GENERATION_NOT_ALLOWED`
- `ACCOUNTING_PERIOD_NOT_GENERATED`
- `ACCOUNTING_PERIOD_CLOSED`
- `ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED`
- `ACCOUNTING_PERIOD_REOPEN_NOT_SUPPORTED`
- `ACCOUNTING_PERIOD_INVALID_CLOSE_DATE`
- `ACCOUNTING_PERIOD_FUTURE_CLOSE_NOT_ALLOWED`
- `ACCOUNTING_PERIOD_CLOSE_OUT_OF_SEQUENCE`
- `ACCOUNTING_PERIOD_CLOSE_BLOCKED`
- `ACCOUNTING_PERIOD_REVIEW_INCOMPLETE`
- `ACCOUNTING_PERIOD_CALCULATION_STALE`
- `ACCOUNTING_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH`
- `ACCOUNTING_PERIOD_ALREADY_CLOSED`
- `ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE`
- `REVERSE_DEPENDENT_EVENTS_FIRST`

错误只返回稳定码和必要字段路径，不回显 SQL、连接、文件路径、证据内容或长请求值。

## 11. 验收门禁

- 自然月十二期生成、连续下一年、首次启用污染拒绝、同键回放、换载荷冲突和并发生成；
- preview 纯读，完整阻断/警告矩阵，哈希逐叶篡改与依赖变化 stale；
- 人工复核逐字段缺失/false、证据和确认人缺失均无正式关闭副作用；
- 关闭顺序、未来期、重复关闭、关闭后所有通用和专用写入口拒绝；纯预览继续可读；
- 空月份仅可在六项人工复核全真且有确认说明与证据时显式关闭，不自动跳过；
- 关闭月错误只能在后续开放月关联冲正，且专用依赖和税期锁不能绕过；
- 通用预收/履约/退款依赖规范行、父事件冲正顺序、直接 SQL 和历史迁移污染；
- 历史税期调整在后续开放月冲正及重确认，税务范围和会计入账日不混淆；
- PostgreSQL 直接 SQL、不可变、跨企业、哈希重算、close-vs-post、close-vs-close 与外层提交无裸错；
- `0011 -> head -> 0011 -> head`、`base -> head -> base -> head`、首 DDL 污染失败、扩展/函数/触发器零残留和 `alembic check`；
- FastMCP Schema 严格，真实 STDIO 由父进程新 Session 验证生成、预览、关闭、拒绝和更正审计链；
- 全量非 PostgreSQL、PostgreSQL、覆盖率、Ruff、`pip check`、`compileall`、迁移检查和 `git diff --check` 全绿，并形成独立验收记录。

## 12. 明确排除与免责声明

一期不支持反结账、年结、损益结转、期初余额、法定账簿、财务报表、对外报送、纳税申报、自动补提、真实 RBAC、电子签名、审批流、备份恢复或专业审计判断。

本功能仅提供单企业私有试用内核的期间控制与内部审阅轨迹，不构成法定账簿、财务会计报告、纳税申报、审计意见或会计/税务法律意见；实际使用前须由具备资质的专业人员复核企业会计政策、授权、证据及结账结果。
