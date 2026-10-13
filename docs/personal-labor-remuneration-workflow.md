# 个人劳务报酬的当前核算流程

先用 `finance_local_schema` 确认所选劳务事实的字段、来源、精度和专用入口。当前劳务相关类型包括 `labor`、`labor_accrual` 和 `labor_project_cost`；不同类型分别表达实际业务、已赚取但尚未收付的报酬以及项目归集，不可互相代填。公司内自然人可承担不同业务角色，身份登记与核算事实分开；不因姓名相同自动合并，也不以技术字段把劳务改成工资。

登记前查人员、服务依据和同月现有事实，留存原件，明确所属月、报酬金额和费用归属。三种劳务来源均可通过 `remuneration_tax_basis` 采用有源最终报酬表应扣额或实际扣税额，明确税期、整数分金额、依据类型和确认；零税也须有依据，未列入申报表不能证明零税。内核不按税率计算或推定个税。`labor` 缺税时可保存报酬事实，正式预览和发布等待税依据；`labor_accrual` 和 `labor_project_cost` 无税依据时仍可发布毛额计提及实际清偿，其未付毛额不能作为已确认净额生成代发文件。后补税依据绑定原计提的精确版本，使原业务及下游进入复核；重新预览、发布原 `source_id` 后形成净额和应付个税，毛额成本不重复计提，开放期凭证保留原编号，再接续净额代发。不要另建一笔 `labor` 替代已计提业务。`preview` 核对金额、来源与凭证，`confirm` 才正式发布。真实发放、税款缴纳、外部申报和账务核对分别保存各自依据，未付款不得因计提而标成已付。缺少会改变会计处理的事实时用结构化 `fact_issues` 指出缺项，先查可复用来源再询问负责人。

未关账修改或撤去遵守依赖和审计约束；已关账内容不原地改写，更正在开放月建立关联冲正。可用类型、字段、精度及来源以当前运行时 Schema 为准；历史计税格式只供读取，不继续用于新核算。外部完成由通用办理事实记录，文件生成不代表申报或付款已完成。通用流程见[类型化业务事实](business-components.md)。

## 历史边界

旧 `finance_record_event` 劳务组件、`finance_register_labor_service_person`、`finance_end_labor_service_person`、`finance_confirm_labor_external_declaration`、`finance_get_event_schema` 及 PostgreSQL `0001_business_baseline_v4` 是退役设计，不是当前命令或升级步骤。原税务边界和组合发放方案由 Git 历史保留，不能据其旧字段向当前内核提交请求。当前仍为 `ai-accounting-kernel/2` 开发合同，正式首版尚未冻结。
