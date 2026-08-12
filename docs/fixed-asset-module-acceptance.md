# 历史：固定资产模块第一期最终验收记录

> 文档性质：过去的实现结果。旧迁移编号只代表当时时点，当前数据库仅使用 `0001_baseline`。本文列出的独立验收方式、测试环境、测试矩阵和数量不是当前统一门禁或后续任务要求。
> 状态：独立验收通过
> 复验日期：2026-08-10
> 适用仓库：`ai-accounting-core`
> 上位设计：[固定资产模块开发基线](./fixed-asset-module-development-plan.md)
> 历史前置验收：[已归档的工资模块第七轮验收整改任务书](./archive/payroll-remediation/payroll-module-acceptance-remediation-round-7.md)
> 完成定义：固定资产购置、启用、月折旧、出售/报废、查询和关联冲正形成确定性闭环，且专属、跨模块、迁移、PostgreSQL、MCP、真实 STDIO 与全量质量门禁全部通过

> 验收结论：固定资产模块第一期实现通过独立验收，可以作为后续模块的稳定基线；它仍是单企业私有模拟试用内核，不是法定资产卡片、税务申报、企业所得税汇算清缴或税务意见系统

## 1. 已交付范围

本阶段交付六个专用 MCP 工具：

- `finance_acquire_fixed_asset`
- `finance_activate_fixed_asset`
- `finance_preview_fixed_asset_depreciation`
- `finance_confirm_fixed_asset_depreciation`
- `finance_dispose_fixed_asset`
- `finance_get_fixed_asset`

四类内部业务事件均由固定模板生成凭证：

- 购置：借记待启用固定资产，贷记银行存款或应付账款；
- 启用：借记固定资产原值，贷记待启用固定资产；
- 折旧：按冻结的受益区域借记有限费用角色，贷记累计折旧；
- 处置：自动读取原值、累计折旧、账面价值、收入、增值税和清理费用，唯一计算清理损益。

通用 `finance_record_event` 不能接收固定资产事件，所有公共 Schema 均不接受科目代码、借贷方向或自由分录行。金额只接受严格整数分；税率与含税换算只使用 `Decimal`。

## 2. 规则与边界复核

会计处理采用有效期化的 `small_enterprise_fixed_asset_straight_line_2013.1`，依据[财政部《小企业会计准则》](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf)及其[会计科目和主要账务处理附录](https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf)。折旧从启用月份次月开始，处置月份仍须先计提当月折旧；整分直线法在最后一个计划月闭合到预计净残值。

出售自己使用过的非不动产固定资产采用有效期自 `2026-01-01` 起的 `small_scale_used_fixed_asset_vat_2026.1`，依据[财政部、税务总局公告 2026 年第 10 号](https://fgk.chinatax.gov.cn/zcfgk/c102416/c5247434/content.html)。税则按纳税义务日选择，官方来源、版本和参数随处置事实冻结；期间税务试算读取同一组派生事实。

本阶段只支持单项外购、需要启用的可移动有形资产、银行现付或供应商挂账、直线折旧以及出售或零收入报废。资料缺失返回具体的 `needs_information`；房屋建筑物、土地、自建/改建、融资租赁、减值、加速折旧、税务折旧和税会差异等未支持处理通过严格 Schema 或服务边界稳定拒绝，不会被推断为已支持业务。

## 3. 数据库与不可变性结论

当时的线性迁移 head 为 `0009_fixed_assets`，新增四张规范事实表：

- `fixed_assets`
- `fixed_asset_activations`
- `fixed_asset_depreciations`
- `fixed_asset_disposals`

折旧和处置通过非空 `activation_id` 绑定当时的启用事实。组织复合外键、资产编号唯一性、每资产每月最多一笔有效折旧、连续月份、累计上限、处置月折旧、入账日期顺序和正式事实不可变均由数据库提交点约束复核；被冲正的历史事实继续保留。

PostgreSQL 延迟触发闭包覆盖业务事件、四张资产事实表、凭证及分录、开放项、银行流水及匹配、科目和税则。资产行与新资产编号使用统一锁域；并发处置最多一笔成功。正式凭证和规范事实不能原地修改或删除，更正只能生成关联反向事件，并按处置、最新折旧、启用、购置的依赖逆序执行。

`0009 → 0008 → 0009`、历史 revision 直升 head、空库升级和污染预检均已复验。降级先删除本版本安装在既有表上的触发器和函数，再恢复 `0008` 的最终事件守卫；迁移所有权台账只回滚本版本创建或绑定的科目和税则，不删除兼容的既有数据。

## 4. 独立审计关闭项

实现和终审过程中发现的高风险项均已关闭并固化为回归：

1. 固定资产来源事件存在后，禁止越过处置、折旧或启用依赖直接冲正前序事件。
2. 出售固定资产的应税销售额、销售净额、增值税和免税事实进入统一期间税务试算。
3. 折旧和处置保留原启用事实血缘，冲正后重新启用不会重解释旧历史。
4. `rule_trace` 使用新 JSON 值写回，真实 STDIO 子进程退出后由全新 Session 仍能读取完整轨迹。
5. 折旧入账日必须属于声明月份；资产类别不提供可混入不动产的模糊 `other`；改变处理的布尔事实使用严格布尔类型。
6. PostgreSQL 降级完整移除 `0009` 的触发器和函数并恢复 `0008` 语义。
7. SQLite 历史库降级时，迁移创建的税则使用带 UUID 类型处理器的删除表达式清理，覆盖 `0001 → head → 0001` 和“已有企业 `0008 → head → 0008`”两条路径。

独立只读终审未发现未关闭的 P0 或 P1；其提出的 P2 文档状态和验收索引问题已由本记录关闭。

## 5. 最终门禁记录

2026-08-10 在稳定工作区和隔离的临时 PostgreSQL 17/SQLite 数据库上完成最终复验：

- 标准全量：`pytest -ra`，`240 passed, 8 warnings`。
- PostgreSQL：`pytest -m postgres -ra`，`99 passed, 141 deselected`。
- 完整覆盖率：`pytest --cov=ai_accounting --cov-report=term-missing`，`240 passed, 8 warnings`，总覆盖率 `86%`；固定资产服务 `88%`，固定资产纯计算器 `91%`。
- 固定资产 PostgreSQL 专项：`3 passed`，覆盖正向链、数据库直写负向、双连接并发处置和迁移往返。
- 真实 MCP STDIO：隔离子进程完整执行购置、启用、折旧预览、错误哈希拒绝、确认、查询、出售、事件查询和冲正；父进程用全新 Session 验证事实、平衡凭证、证据、轨迹、税则来源和银行匹配失效。
- 当时的迁移一致性：临时数据库 `upgrade head` 后执行 `alembic check`，结果为 `No new upgrade operations detected`；`alembic heads` 唯一输出 `0009_fixed_assets (head)`。
- `ruff check .`、`python -m pip check`、`python -m compileall -q src alembic tests` 和 `git diff --check` 全部通过。

8 条告警中，7 条来自 Python 3.12 的 SQLite 默认 datetime 适配器弃用提示，1 条来自既有工资政策测试的 Pydantic 序列化提示；均不改变会计结果或数据库约束，记录为后续依赖升级清理项。

当次迁移测试使用了显式临时数据库，默认 Compose `finance` 数据库未升级、降级或写入。测试创建的临时数据库和 Testcontainers 容器均已清理。

## 6. 阶段结论与后续路线

固定资产第一期目标已经达成。下一开发目标按路线图为“无形资产与借款利息”，尚未启动，也不属于本次验收范围。

开始真实数据试用前仍必须补齐会计期间、期初余额、权限、加密、备份恢复和专业会计复核；本次验收不改变这些上线前置条件。
