# 个人劳务报酬的当前核算流程

先用 `finance_local_schema` 确认所选劳务事实的字段、来源、精度和专用入口。当前劳务相关类型包括 `labor`、`labor_accrual` 和 `labor_project_cost`；不同类型分别表达实际业务、已赚取但尚未收付的报酬以及项目归集，不可互相代填。公司内自然人可承担不同业务角色，身份登记与核算事实分开；不因姓名相同自动合并，也不以技术字段把劳务改成工资。

登记前查人员、服务依据和同月现有事实，留存原件，明确所属月、金额、费用归属及实际已知的扣缴情况。按类型化入口登记，`preview` 核对政策版本、金额、来源与凭证，`confirm` 才正式发布。真实发放、税款缴纳、外部申报和账务核对分别保存各自依据；未实际扣缴不得由理论税额推定为已扣，未付款不得因计提而标成已付。缺少会改变会计处理的事实时用结构化 `fact_issues` 指出缺项，先查可复用来源再询问负责人。

未关账修改或撤去遵守依赖和审计约束；已关账内容不原地改写，更正在开放月建立关联冲正。政策日期、税率、可用类型和明确的官方来源以当前运行时 Schema 与版本化政策为准。外部完成由通用办理事实记录，文件生成不代表申报或付款已完成。通用流程见[类型化业务事实](business-components.md)。

## 历史边界

旧 `finance_record_event` 劳务组件、`finance_register_labor_service_person`、`finance_end_labor_service_person`、`finance_confirm_labor_external_declaration`、`finance_get_event_schema` 及 PostgreSQL `0001_business_baseline_v4` 是退役设计，不是当前命令或升级步骤。原税务边界和组合发放方案由 Git 历史保留，不能据其旧字段向当前内核提交请求。当前仍为 `ai-accounting-kernel/2` 开发合同，正式首版尚未冻结。
