<!-- @format -->

# 稳定 owned 读 guard：机制、验证与旧新对照（2026-10-05）

本组将 owned 读连接的 authorizer 固定安装在连接初始化阶段，保留受控原生事务入口，避免 `QueryReads` 每次快照重新安装非空 authorizer，使已有 SQL 准备程序过期。主要机制是非空 `set_authorizer` 的重复安装；不能把清除 `None` 本身写成主要原因。一般连接与 fixed-v1 继续使用原有通用路径，不改变业务、合同或前端实现。

固定新来源为 `ed96dfd296cd96d4279745eb0ac26a9422db450c10d3bb9f8e7def11e2f9d8c8`，771 文件，源码清单 SHA `58067bd14dcb1374b944cd891842c69ad4385ca020f37437464d39f7907a63cf`。与旧来源 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d` 的清单比较恰好变化五个路径：生产 `runtime.py`、`resident_reads.py`、`storage.py`、`query_reads.py` 及新增 `test_owned_read_snapshot_guard.py`；前端、合同和 33 个 fixed-v1 文件未变。四个生产文件及最终测试 SHA 均与最终 receipt、固定快照匹配。

## 验证与审查

机制 PoC 仅在内存数据库验证稳定 guard、准备程序复用、公开事务拒绝、原生 permit 重入及失败清理等边界，不等同生产验证。旧9ac compilation 观察仅统计公司 `Connection.execute`，直接 cursor 不在计数内。两者均以插桩原件保留，不作为纯性能验收。

模块组 XML 记录 91 PASS、0 failure/error/skip，实际范围为 `test_resident_reads`、`test_resident_read_guard_work`、`test_t7_snapshot_reuse`、`test_runtime_backup`、`test_owned_read_snapshot_guard`。其后四个生产文件未变，仅新增测试补充三个 native descriptor 边界及一个 progress reentry 边界，最终该测试模块单独 22 PASS。两次验证存在重叠，不能相加为 113 PASS；没有把最终测试版本描述成重新跑完五模块。

此前两次测试设置失败分别为 1 failed/13 passed 和 1 failed/21 passed：一次尝试更新不可变 identity 源，被正确阻断；一次在 idle validator PRAGMA 阶段调用合法 no-op 的 native commit，没有触发预期阻断。测试设置均已修正，失败事实保留在最终 receipt。两次只有控制台记录，未捕获独立 raw 文件，不伪造日志。

root 转述的独立生产审查已核对最终四文件 SHA，未发现新增缺陷；这不是全领域证明，也不是本归档者独立重跑的审查。helper 审查发现季度请求遗漏 `preparation="deferred"`，以及 logout 异常可能阻断后续 cleanup，执行前已修复。审查时的 helper SHA 只绑定当时版本；四份实际对照结果的执行 helper SHA 均与保全的执行版匹配。

## 准备程序与响应对照

work 模式每来源三轮、每轮 context 加五页，共 18 请求。下表为热轮观察到的 compiled SQL hash 数量；新来源首轮冷加载不同，完整冷/热记录保留。

| 页面 | 旧9ac热轮 | 新ed96热轮 |
| --- | ---: | ---: |
| brief | 145 | 6 |
| funds | 127 | 9 |
| employees | 73 | 6 |
| assets | 132 | 6 |
| reports | 123 | 6 |

六个 PRAGMA 仍正常重新编译，不能宣称编译全为零。work 计数含插桩，不与 HTTP 纯读耗时混用。

HTTP 模式每页三次预热、十次正式 context/main 并发刷新，每来源保留全部 65 对记录。funds 首次选择页面默认账户，热读携带明确账户筛选；季度请求携带 `preparation="deferred"`。`main_ms` 为主接口网络调用时间；`pair_ms` 还包含 stdlib opener 构造、Future 调度等，均不等同浏览器点击、渲染或整页完成。

| 页面 | 主接口中位数：旧 → 新 ms | 最大值：旧 → 新 ms |
| --- | ---: | ---: |
| brief | 322.68 → 324.76 | 383.84 → 339.29 |
| funds | 259.18 → 238.16 | 266.61 → 251.75 |
| employees | 266.26 → 263.75 | 309.64 → 303.03 |
| assets | 436.76 → 430.36 | 590.61 → 444.17 |
| reports | 387.77 → 387.87 | 401.21 → 401.57 |

不同来源在不同窗口运行，资产最大值差异不能称为稳定尾部改善。正式样本中旧/新主接口 ≥500ms 分别 1/0，pair ≥500ms 分别 9/7，所有慢样本原样保留；这些计数不是浏览器门槛结果。

响应时钟仅按精确路径归一化：context `/generated_at`、brief `/data/generated_at`、reports `/checked_at`，没有递归删去同名字段。直接核对两种模式保存的归一化响应，context 完全相同；五页仅 `/read_context/read_version` 因 build 来源改变，保存响应的派生 SHA 随之不同。清单记录这些原始差异路径，归档保留差异，不抹去 read version。

## 保护与证据范围

四份对照 receipt 均为 complete，业务/身份/状态/历史守卫相等、源码未变、原会话不变、新会话 logout、read pool 关闭及 server stopped 均为 true，cleanup errors 为空。合成目录认证变化不等于目录库物理完整不变。这些保护事实来自 receipt；本次仅归档与 SHA/文本比较，没有独立重新核验数据库或计算数据库 hash。

旧新对照均为新的隔离 host，没有新完整资格或实际 close preview；brief 缺当前活动 owner-review 定位器。原旧9ac官方浏览器结果仍独立保留为 150 次取样、17 次 ≥500ms，不由本组原生 HTTP 结果替代。本组不证明浏览器 500ms 通过，不宣称阶段 9 完成、正式冻结或交付。

独立 [manifest.json](stable-owned-read-guard-20261005-evidence/manifest.json) SHA 为 `afeb149d907ef40e153664d0dec5761044195fe104900fee831f29930d05cf1f`。最终 receipt SHA 为 `c40415a65eaed2d8c05aad45e6b75f190ac72c19e05e3068e5655b17b9f6ccdd`。16 件分析版 gzip 包含两份机制 JSON、最终 receipt、两份 XML 和最终控制台日志、源码清单、四份旧新结果及五个变化源码/测试文件；四件 helper 仅索引私有路径、SHA 和字节数，不发布含密码内容。原件、脱敏数据、gzip SHA 及解压回验结果全部记录，gzip `mtime=0`；内部 ID、guard、根路径、PID、内存地址和秘密脱敏或哈希化。没有复制数据库、ZIP、凭据或真实资料，没有运行测试、数据库、浏览器、服务或性能测量，也未修改主阶段、路线图、AGENTS 或已有封存档案。
