# 代收代付的类型化处理

当前内核用 `pass_through` 事实记录有依据的代收债权和转付义务。事实明确付款方 `payer_id`、受益方 `beneficiary_id`、整数分金额 `amount_fen` 和权利义务确认；未确认义务不得推断成收入、预收或可支付余额。登记前查已有对象、事实与证据，字段精度和所需来源以 `finance_local_schema` 为准。

用 `save_fact` 登记业务事实，`preview` 核对计算、期间和依赖后再 `confirm` 正式发布。实际收款、向受益人支付及其核销按各自发生日期和来源另行登记，资金方向、账户、银行原行、金额及未结义务逐项核对。资料接收与处置、事实登记、正式发布、真实资金收付和外部办理是不同状态，不能由其中一项推定其他项已完成。

开放期事实错误通过类型化更正或符合依赖条件的删除入口处理；闭期不改原凭证，在开放月使用关联冲正和替代。缺少会改变会计处理的付款方、受益方、金额、期间或证据时返回 `needs_information`，不把未知余款默认归为预收或收入。完整命令和当前能力见[类型化业务事实](business-components.md)及运行时 Schema。

## 历史边界

旧 `finance_record_event` 的 `components`／`funds` 请求、`finance_get_event`、`finance_amend_event` 和任意 `metadata` 写入接口均已退出运行层。旧公司 v13、目录 v3 和组件式请求示例只保留在 Git 历史，不是新系统的导入或运行来源。当前使用 `ai-accounting-kernel/2` 开发合同，正式首版仍待第 9 阶段冻结。
