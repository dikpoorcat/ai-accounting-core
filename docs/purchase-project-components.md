# 预付款与项目成本的类型化来源

当前内核以类型化事实保存有依据的预付款、履约、退款、项目成本及项目释放；相关种类包括 `advance`、`advance_fulfillment`、`advance_refund`、`project_cost` 和 `project_release`。每项事实应有明确业务身份、金额、期间、证据和适用的来源引用，具体字段和精度以 `finance_local_schema` 为准。管理项目名称或合同标签不能替代实际权利义务或核算确认条件。

先查已有对象、事实和来源，再以 `save_fact` 或 Schema 指定的专用入口登记；`preview` 检查计算、累计容量、期间与依赖，核对后用 `confirm` 正式发布。预付发生、供应商履约、尾款支付、退款、成本形成和资产达到可用状态分别按真实事实处理。冲抵与资产结转不凭空产生第二次现金流；真实付款、收款必须有实际日期和对应资金来源。不得通过改项目标签、科目或自由分录绕过来源余额和累计消耗约束。

项目成本的可用容量在消耗来源时由内核核验，`find_facts` 不提供旧 `project_cost_balance` 字段。开放期修改或删除遵守下游依赖，闭期更正创建开放月关联冲正，冻结历史不回写。完整的登记、发布和追问边界见[类型化业务事实](business-components.md)。

## 历史边界

旧 `finance_preview_event`／`finance_record_event` 组件、`supplier_advance_application`、`project_cost_expense`、`finance_get_event.project_cost_balance`、`finance_configure_account` 和公司 v13／目录 v3 属于退役设计；旧请求示例不能作为当前命令、升级或恢复来源。原项目组件方案由 Git 历史保留。当前系统采用 `ai-accounting-kernel/2` 开发合同，正式首版仍待冻结。
