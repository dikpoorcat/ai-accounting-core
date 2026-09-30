# 员工身份读取窄修（r24 诊断）

- 默认员工名单仍从全部已采用工资头枚举对象，并对开放期所有相关 fact 执行 `current_role_matches` / `verify_hits` 的真实 typed 内容、角色摘要和索引核验。列表只消费核验成功的 `employee` 角色；合法自定义 registry 不声明该角色时，批量读取相应工资标量作为回退。坏角色行或错摘要直接拒绝，不按缺角色回退。
- 普通工资付款汇总从已核身份取得员工；仅期初未付工资的 `component` 需要补读标量。显式员工详情仍读取该员工完整工资来源。闭期路径维持已认证 `payroll_head_identities` 和命中来源核验。
- r24 固定 48 月书 `2019-12`，原默认名单 `scalar_facts` 水合 2,400 条工资头，约 31.25 ms CPU；隔离 overlay 改动后此调用为 0。`current_role_matches` 的 2,450 条、约 140.6 ms CPU 保留。原始诊断在 `stage9-employee-proof-overlap-r24.json` 与 `stage9-employee-proof-overlap-r24-overlay.json`；插桩 wall 625→549 ms 仅供定位，非整页验收。
- 固定 r24 与隔离 overlay 对同一合成书的默认名单及显式一人 `payroll_sources` 响应逐字段相同，排除当日 `read_context.as_of` 与构建摘要变化。原始 JSON 在 `stage9-employee-r24-fixed-response-seed0.json`、`stage9-employee-r24-overlay-response-final-seed0.json`。真实 LocalService/HTTP 员工响应同值测试通过。
- 定向 10 项通过：默认列表工作量、合法缺角色回退、坏/缺角色与源正文拒绝、期初、闭期来源、无影响复核缺发布拒绝、开/闭身份纠错、HTTP/native 同值。Ruff 和 diff 检查通过。最终批量回退修改后全新大样本纯计时未做，交统一组验收。
- 同类静态搜索：`dashboard_reads.py` 的 `payroll_head_identities` 服务闭期头身份；`dashboard.py` 的 `_brief_workforce_cost` 只取本月工资头，`_employees` 是本次重复水合位置。`_labor_sources` 与资产标量加载同用 `scalar_facts`/`verified_scalar_facts`，但其输出需组件/金额或对象状态；未见同一个已认证角色结果可直接替换的证据，本轮保持。资金页未调用工资角色枚举。CLI/MCP 员工调用最终进入同一 `Dashboard.employees`；固定 v1 阅读规则未改。
- 仍有约 2,450 条的开放期角色正文与实体引用重建成本，属此次未缩的必要核验范围；48→120 月实际整页时延与 GC 抖动待统一纯计时，不据单次插桩宣称达标。
