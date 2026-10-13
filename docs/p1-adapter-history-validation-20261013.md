<!-- @format -->

# P1 适配器与固定历史补验

2026-10-13 新增 `tests/kernel/test_p1_adapter_history.py`。仅运行新增三项，最终 **3 passed / 95.68s**；Ruff 检查通过。此结果验证功能和信任边界，不是刷新性能成绩。

实际产品来源为固定 S9 candidate，源码清单 SHA `df585bea798f510b8d3c6bfb57b57190678c6459de365959501e5bca0101bf53`，build 为 `local-kernel-2:8200c138e06e870d2cfe321a999fdf34196e11c03e134ae41b161496e3c1d856`。新测试从工作区加载，既有构造辅助和 182 个实际运行模块均核对固定树文件；CLI 和 stdio MCP 子进程显式绑定同一 `PYTHONPATH`，直接编译声明源码并记录实际模块路径和 SHA。没有修改固定树、产品源码或三档主样本。

| 新增项 | 实际验证 |
| --- | --- |
| CLI / stdio MCP 五页 | 正常临时负责人和公司；真实 `_produce` 到 ready；五页正式 delivery envelope 与原生响应一致，实际消费已发布模型；季度三报表顶层结构；大于 JavaScript 安全整数范围的金额仍是原生整数；整数月份被实际 schema 拒绝；读取前后公司原文一致 |
| 原生续页与坏源 | v6 凭证续页真实走原生算法；两个适配器内容一致且只读；损坏实际当前资金事实后，适配器、完整核验、明确修复、便携包创建独立拒绝；拒绝后无 queue/reset/audit/repair 版本变化 |
| current041 闭账 / 备份 / 恢复 | 只读一致性副本先通过当前完整核验；保存工资4、计划4、材料父体81/链接79、利息4；一月 format4 的全部冻结存储原文保持；二月真实 preview/close 生成 v5；ZIP 完整验证和真实恢复后，两月逻辑冻结内容、旧正文及原财务行保全，源 ZIP 不变 |

外部历史输入为 `.tmp/stage9-original-history-small-20261013/stage9-builder.json`，属于此前从 SOURCE24 实际构造并迁移通过的健康两人、两月微型样本，不是故意损坏的负例。本次复制后重新取得当前完整核验，不借原成绩跳过核验。

| 来源 | SHA-256 |
| --- | --- |
| 构造回执 | `2496367293d141fac219505ffea945f9acdbd5d88c75d35cff98c6349900f534` |
| 原 `stage9-original-history-small-20261013-adjustment-r4.json` | `d0e9d642290be1eaf0ce02e1290b13ec3a76ebb09111dd618a85b0f8761b4a2a` |
| 原 `stage9-original-history-small-current-functional-20261013.json` | `b8e1868544e29d177c37fdae1fa21834437b9319226a03b0e8a879c8309b84ec` |
| 健康公司 SQLite，本次前后相同 | `cbb071c780e8a0e064c1a57eb0091cf3af26a18e59916f29c4e15ec6b76f2bbd` |

本次实际日志、XML、模块来源回执保存于 `.tmp/stage9-p1-adapter-history-fixed-s9-20261013-r3.{log,xml,json}`；JSON SHA 为 `725d86a4b1019420511f60335ef19beefe75ada0fd67625128d24771a3a57f39`。新测试 SHA 为 `f22ffa8c3f46bd6e1572eca7568ff7e640d3fff5473324611a328cf03551ca46`。r1/r2 失败原件均保留。

常规 Windows CI 可直接运行前两项。历史联动项默认 skip；本轮显式设置 `P1_ADAPTER_HISTORY_INPUT` 到上述保全回执实际执行。复验使用仓库 `.tmp-kernel-venv/Scripts/python.exe`，只关闭/恢复 pytest 临时副本，不提交数据库。该测试不重新安装旧计算器，不制造确认，不改写或重新签名旧冻结。

## 未解决的独立接口缺陷

r2 真实 CLI 和 stdio MCP 调用 `dashboard_brief`，传入字符串月份 `"invalid"`，返回 preparing，并分别留下 `dashboard_projection_request` 审计及 `pending / priority100` 请求。只读观察保存在 `.tmp/stage9-p1-adapter-history-fixed-s9-invalid-period-r2-observation.json`。此缺陷未修复，既有测试断言未删除。

代码定位为 `LocalService.dispatch` → `dashboard_delivery.deliver` → `dashboard_async.delivery_state`：schema 从 `period: str | None` 签名生成；默认 delivery 在原生 `YearMonth` 校验之前排队。同一公共入口用于四个月度默认页面及原生 HTTP、浏览器 HTTP、CLI、MCP；本次动态复现仅 brief 的 CLI/stdio，其他入口是静态确认的适用范围。带 section/expected_version 的原生读取和直接 Dashboard 不经过这段提前排队；季度另用 year/quarter 参数，未据相似性声明其有同一缺陷。此项不属于默认刷新性能或 P1 同快照复用，本轮未修改产品，也未据此重跑规模准备。
