# 组合式记账重构交付记录

验证日期：2026-09-08。协议说明见 [组合式记账](business-components.md)，
实际重录步骤见 [空库回放手册](empty-database-replay.md)。

## 实现范围

- 单项及多项业务统一使用 `finance_record_event` 的 `components`、`funds` 协议。
  同类明细科目通过受控 `business_class` 配置；新的核算机制需要对应的组件规则。
- 正式凭证统一由 `ledger.commit_posting_plan` 提交，银行匹配统一由
  `bank_matching.commit_bank_matches` 执行。原混合场景接口、封装表和运行时兼容路径已移除。
- 组件保存来源、证据、分录及现金流归属；同笔依赖按整笔计划核验。
  工资、劳务、资产、借款和税务保留确定性计算与必要确认。
- 整笔修改保留原凭证编号和审计；删除、冲正处理全部组件及派生记录，
  并继续检查真实外部依赖、期间状态、来源额度和税务时点。
- 组件修改在事务内重新预览替换事实，再统一提交。补记购置或启用与首个应计月份的
  折旧可以同笔完成；来源投影参与确认哈希，修改保留资产及组件身份。
- 社保完成状态合并检查当月所有有效正常工资批次，核验员工、政策、实际缴费来源和金额。
- 业务库仅从 `0001_business_baseline_v3` 空库初始化；目录库保留独立的
  `0001_catalog_baseline_v2`。本次基线完成后，后续结构变化使用前向迁移。

## 验收覆盖

| 要求 | 主要测试 |
| --- | --- |
| 多费用、应付核销及手续费；应收、预收及多债权人代收 | `test_composition_acceptance.py` |
| 配置新增明细科目及来源科目继承 | `test_business_components.py`、`test_component_ledger.py`、`test_composition_acceptance.py` |
| 资金守恒、真实银行匹配、幂等和失败整笔回滚 | `test_component_guards_postgres.py`、`test_component_ledger.py`、`test_pass_through_postgres.py` |
| 工资、劳务及代扣税同笔处理，重复同类组件 | `test_confirmed_accrual_components.py`、`test_repeated_payroll_components.py`、`test_repeated_labor_components.py` |
| 正常工资与合并计税奖金、多来源批次的同笔依赖 | `test_combined_payroll_components.py`、`test_combined_payroll_lifecycle_postgres.py` |
| 利息计提、本息同付，按真实来源检查结清 | `test_component_guards_postgres.py` |
| 同笔购置或启用与首月折旧，成本修改后重算并保留身份 | `test_local_asset_components.py`、`test_local_asset_lifecycle_postgres.py` |
| 多批次正常工资的完整社保核验及缺员拒绝 | `test_owner_workflow_payroll_aggregation.py` |
| 跨类别税费、同笔税源及税期确认、所得税差额更正 | `test_tax_account_components_postgres.py`、`test_tax_confirmation_components_postgres.py` |
| 整笔修改、删除、闭期冲正、外部依赖和审计 | `test_component_lifecycle_postgres.py`、`test_event_amendments_postgres.py`、`test_round6_integrity_postgres.py`、`test_round7_integrity_postgres.py` |
| 现金流、报表和子账一致 | `test_component_reporting.py`、`test_financial_statements_postgres.py` |
| 新协议导出、重新预览、稳定来源重映射及空库重录 | `test_component_replay_postgres.py`、`test_replay_cli.py`、`test_backup_restore_postgres.py` |

最终非 PostgreSQL 回归执行 `python -m pytest -m 'not postgres' -q`：
**690 项通过，1 项跳过**。跳过项只验证非 Windows 行为。
另有一条既有测试构造触发的 Pydantic 序列化警告，不影响测试结果。

PostgreSQL 验证使用可丢弃的独立 PostgreSQL 17.10 集群及随机测试数据库，
未对试用库执行迁移或写入。受影响的内核、身份归属、MCP、报表、资产借款、工资劳务、
税务和备份回放套件分别通过。收尾验证包括：

- 基线、税务并发、完整性及整笔更正组合回归：32 项通过。
- 最终基线及旧迁移拒绝验证：4 项通过。
- 多来源正常工资与奖金：SQLite 4 项、PostgreSQL 3 项通过。
- 上述组合的整笔修改、删除、冲正及伪造来源拒绝：PostgreSQL 3 项通过。
- 本地资产来源、成本修改、整体生命周期及伪造来源拒绝：PostgreSQL 3 项通过。
- 共享修改流程收尾验证：PostgreSQL 4 项通过；相关组件、领域及修改回归 44 项通过。
- 资产及借款数据库约束、组件来源和生命周期回归：PostgreSQL 9 项通过。
- 多工资批次的社保查询与完成证明：3 项通过。
- 新协议实际回放：PostgreSQL 5 项通过；回放 CLI 14 项通过。

这些分组存在重叠，不相加作为独立用例总数。
前端 `npm --prefix frontend run build` 包含 `vue-tsc --noEmit` 与 Vite 构建，均通过。
Ruff 与 `git diff --check` 通过；静态检索确认凭证及银行匹配各只有一处正式构造路径。

## 重录资料状态

Git 忽略的 `outputs/composition-reentry-20260908/` 保存 9 月 8 日最新只读快照、
与 9 月 3 日旧包的差异及一次性整理产物。可执行新包位于其 `composition-replay-v2/`：

- 最新源快照时间：2026-09-08 11:26:17（北京时间）。
- 2 家公司分别为 330、81 条有效业务，共 411 条；包含 290 条组件入账，
  资料登记、计算及期间处理等合计 867 个操作。证据 90 份，共 116 个文件。
- 证据总计 26,467,272 字节，离线格式、哈希、依赖和组件结构验证通过。
- 包清单 SHA-256：`7f150f19ad8b01b936c36f8c539e35f567e44abb6e54881a0dcc6a682b413b75`。

私有 [重录交付索引](../outputs/composition-reentry-20260908/reentry-delivery.json) 可定位逐公司资料。
各公司的 `reentry-checklist.md` 和 `reentry-gaps.json` 记录操作顺序、依据、组件、
稳定来源引用、金额与核对点。原始资料、旧包和旧数据库继续保留。
当前资料缺口清单为空，未生成额外会计处理更正；这不替代实际重录后的终态核验。

本次交付状态为 **资料已整理并离线验证，隔离测试库回放已验证**。
**试用公司业务尚未实际重录，也未切换试用数据库。**
实际重录后的公司余额、未结往来、银行、工资税务、资产借款及报表终态，
仍须按手册执行后逐项核对，不能用资料包验证代替。
