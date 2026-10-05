# 当前 c063 开发包正常同根接续

当前独立开发包在桥接验证已创建的隔离合成资料根完成正常停服、真实进程退出、同根重启与 MCP 只读接续。v3 实际工具终态 exit0，10 次 MCP 调用；两次 owned daemon 的 process.wait 均实际返回0，两次 MCP SDK 上下文正常退出，两次新认证均撤销。目录及公司保持 draft／0，真实5173、资料和身份不动。

源 SHA `c063fdbf18975e1a3eab102116014de30f3bb58c8ae99ded406e51afaefeaf99`，manifest SHA `0c8ac9a2744d8e41ac66a8b03c811b337831660591eead1155cb0eca331a17c0`，777文件；运行包 build `local-kernel-2:34dcf3f83f5d2425a7bb99a5c5d911c2ae82cb811bdca1fdd4e6e60fe334830d`。精确绑定当前包 runtime、manifest、原包与桥接回执、当前导入模块；未重新构建、复制资料根或登记新业务。

每次实际读取 company_context、已有审计关联的 request_result（committed）、2026-09 closed_report、jobs 和 verify_integrity。前后五项完整响应相等，公司数据库全部 SQLite SQL／表行摘要（包括冻结、历史、audit、request）不变，owner 身份与凭据保护列摘要不变。正常成功认证仅允许 password_failures=0、password_blocked_until=NULL、last_authenticated_at 为非倒退整数；其他 owner 列严格保留。

| 尝试 | 实际工具终态 | MCP实际调用 | 两daemon实际退出 | 结果 |
| --- | --- | --- | --- | --- |
| v2 | 1 | 10 | 0／0 | 最后owner整行摘要断言失败，原件保留 |
| v3 | 0 | 10 | 0／0 | 窄认证字段守卫修正后通过 |

v2 已完成10读取、前后响应和公司全SQL／行守卫，但不能改写为整体验收通过。当前认证源码正常登录更新三个认证状态字段，v2将其纳入整行摘要假设过严。v2未保存before行，因此不宣称该次已证实仅last_authenticated_at变化。v3明确排除这三个状态列并分别检查合法值，其余身份、密码／恢复资料、凭据版本及创建信息全部严格不变。两次尝试日志和实际终态原件SHA均保留；Pydantic lifespan warning未阻止实际读取。

此证据仅证明正常 stop/restart 和两个 owned daemon真实退出、两个 SDK上下文正常关闭，不证明 Windows完整子进程树、SDK Job异常关闭、强制kill、inflight中断或真人native密码窗口，也不代表性能计时。当前159 CLI工资付款／完整月关账及19 MCP备份恢复分别保留在[开发包档案](development-package-summary-lifecycle-c063-20261005.md)，本组不重复业务或扩大其范围。

脱敏小证据及原件SHA见[manifest](current-package-normal-restart-c063-20261005-evidence/manifest.json)。10个确定性gzip均mtime=0且roundtrip通过；私有helper只保存SHA，未公开口令、PID、本机绝对路径或数据库／WAL／ZIP。
