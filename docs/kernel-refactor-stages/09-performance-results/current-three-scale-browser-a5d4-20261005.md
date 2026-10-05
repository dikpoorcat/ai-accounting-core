<!-- @format -->

# a5d4三主规模实际浏览器验收（2026-10-05）

固定a5d4优化组的12／48／120月主样本已各完成150个真实整页热刷新样本，共450个，全部成功且低于500ms。每页3次预热、30次正式取样，15个页面组均无超线。恢复控制器终态为`measured_all_passed`，三个runner退出码均为0，实际native worker和venv launcher退出已观察；本代理仅核对小文件和归档，没有运行浏览器、数据库、资格或测试。

结果只属于a5d4，**不代表最新525f来源已测、阶段9全部完成或正式交付**。本轮仍draft／0，现有真实资料、身份及5173未切换。

## 来源、资格与串行释放

固定源码SHA `a5d4e1ac7d41814bb7a74d30410e0dbe45e6a691c85bd69f71deff10f57bd1d1`，776文件；source manifest SHA `a95fce65d9fc6d5381f043d591caf2ab6562f1b08bba1ce24090c833e3639a0b`。实际backend build为`local-kernel-2:5c689d17374baad801e4d73778715f9f5c9a71642a990d682ec7dba99575d3e2`。归档核对manifest与runner绑定，不重新扫描全部源码或核验数据库；源码执行前后清单未变来自真实runner守卫。

三个规模均为50员工、每月1000笔业务、mixed_cumulative分布，默认20条，保留完整汇总。各自重新执行注册完整核验，sources／historical_adoption／projections／read_indexes四项均verified、limitations=[]；随后实际开放月preview均由a5来源执行。包装回执`fresh_qualification_and_actual_preview_before_timing=true`、`old_qualification_inherited=false`。构造来源索引和声明迁移来源不冒充资格来源；12／48真实迁移是a5，120真实迁移是ce54，详见[12／48迁移](synthetic-index-transition-a5d4-20261005.md)与[120迁移](synthetic-index-transition-main120-ce54-20261005.md)。

| 月数 | facts / calculations / vouchers / closes / evidence | 完整核验 ms | 实际preview ms |
| --- | --- | ---: | ---: |
| 12 | 30225 / 12413 / 12309 / 11 / 85 | 64204.0449 | 55309.4764 |
| 48 | 121593 / 50519 / 49245 / 47 / 337 | 614718.2298 | 524933.1129 |
| 120 | 308217 / 130619 / 123117 / 119 / 841 | 2889562.6869 | 2487611.7026 |

资格及preview是释放前的准备过程，有并行窗口，不属于整页500ms取样或纯准备耗时比较。全部host ready后，根建立自有负载停止声明；控制器逐次核对实际native worker与launcher命令身份、其他自有负载及3秒空闲CPU增量，没有暂停或强杀进程。纯计时按12→48→120释放：2026-10-05 00:50:45.553599、00:52:01.797710、00:53:32.085124 UTC；整体于00:55:19.916838 UTC结束。控制器不是这三个runner的父进程，使用真实runner退出回执并观察两个进程退出，未伪造Popen wait。

测量沿用release静态构建及生产浏览器harness，点击至既有两个requestAnimationFrame后的完成点，包含请求、JSON及响应式DOM更新，不是独立paint或CPU计时。每条样本对应的ResourceTiming完整保留。page_timing模式的http_requests／dispatch_calls为空列表，不冒充另一次插桩计量。

## 实际热刷新结果

下表数值由原始page.median_ms／p95_ms／max_ms直接格式化到0.1ms；全部450原始samples及450组resource_samples仍存档，没有剔除慢值或选择子集。

| 月数 | 页面 | 中位 ms | p95 ms | 最大 ms | ≥500ms / 样本 |
| --- | --- | ---: | ---: | ---: | --- |
| 12 | brief | 291.2 | 330.1 | 330.9 | 0 / 30 |
| 12 | funds | 228.6 | 229.3 | 249.9 | 0 / 30 |
| 12 | employees | 186.3 | 187.2 | 187.2 | 0 / 30 |
| 12 | assets | 145.2 | 166.0 | 166.1 | 0 / 30 |
| 12 | reports | 312.3 | 331.8 | 331.9 | 0 / 30 |
| 48 | brief | 328.3 | 332.7 | 350.4 | 0 / 30 |
| 48 | funds | 246.9 | 248.6 | 266.9 | 0 / 30 |
| 48 | employees | 206.3 | 226.9 | 228.0 | 0 / 30 |
| 48 | assets | 246.7 | 248.2 | 248.9 | 0 / 30 |
| 48 | reports | 352.7 | 373.8 | 374.0 | 0 / 30 |
| 120 | brief | 355.0 | 375.7 | 375.8 | 0 / 30 |
| 120 | funds | 270.3 | 290.9 | 291.6 | 0 / 30 |
| 120 | employees | 308.2 | 330.8 | 331.5 | 0 / 30 |
| 120 | assets | 437.8 | 456.0 | 456.7 | 0 / 30 |
| 120 | reports | 396.7 | 432.0 | 454.2 | 0 / 30 |

## 主120与原9ac窗口对照

[9ac主120原报告](current-main120-browser-9ac1-20261004.md)仍保留150成功、17次≥500ms的失败结果。读取实际旧raw及两份source manifest确认：139个frontend和27个static文件SHA集合相同，两条browser／实际preview脚本SHA相同，实际release assets清单也相同，默认20条相同。原始业务样本与迁移后的副本来源分开保存；不同日期、执行窗口、数据库合同和优化集合不构成单一partial index的受控因果证据，也不证明稳定尾延迟改善。12／48没有在本档案构造不同来源的before。

| 页面 | 9ac 中位 / p95 / max ms | a5 中位 / p95 / max ms | 9ac→a5 ≥500ms |
| --- | --- | --- | --- |
| brief | 377.7 / 416.6 / 518.2 | 355.0 / 375.7 / 375.8 | 1→0 |
| funds | 298.3 / 358.3 / 394.7 | 270.3 / 290.9 / 291.6 | 0→0 |
| employees | 316.7 / 357.3 / 416.7 | 308.2 / 330.8 / 331.5 | 0→0 |
| assets | 497.8 / 577.4 / 618.0 | 437.8 / 456.0 / 456.7 | 10→0 |
| reports | 437.9 / 576.1 / 677.4 | 396.7 / 432.0 / 454.2 | 6→0 |

工作量解释另见[真实pool五页对照](five-page-pool-work-v2-20261005.md)：ed96→a5业务相同，SQL次数、返回行／字节及保存结果解码未降；资产与报表近似VM下降。该并行插桩诊断与本组纯浏览器结果分别记录，不合并成单项优化收益。

## 原失败、冷态与保持边界

首次私有controller真实exit1／controller_failed，是observer把Store.connection误当专用读取池，先在holder enter／exit计数断言失败；old诊断observations=[]、七守卫全true，未释放纯计时、不是500ms失败。首次controller、日志、helper SHA与raw均保留。v2接真实pool后的诊断及根审查通过，有限恢复接续已有12／48等待host和120新Q，不重复迁移或12／48Q。准备回执的prepared_not_executed／v2_work_binding_pending是当时状态，最终执行由独立恢复回执证明，未回写历史。

各runner的business_identity_state_history_guards_equal、source_inventory_unchanged及正常凭据撤销回执均true；catalog main物理SHA相等、catalog WAL物理SHA不等，负责人认证时间及会话变化属于正常预期，SHM字节未宣称不变。不能把业务守卫写成catalog全部物理文件原样。没有OS凭据写入或真实服务切换。

冷态、首次及切公司不纳入450热样本门槛；全部另存，冷打开确有超过500ms的结果：

| 月数 | 首次加载 ms | 冷 brief / funds / employees / assets / reports ms | 公司切换 |
| --- | ---: | --- | --- |
| 12 | 1936.3 | 389.4 / 515.7 / 373.0 / 368.1 / 789.6 | unavailable：single_eligible_company |
| 48 | 3443.8 | 489.2 / 755.0 / 720.0 / 908.8 / 1284.9 | unavailable：single_eligible_company |
| 120 | 1031.2 | 503.9 / 949.7 / 710.6 / 1395.2 / 791.2 | unavailable：single_eligible_company |

本组不覆盖完整主规模详情、跨公司切换性能、报表跨季度inflight、连续快切、详情自身inflight、完整AI GUI会计、独立／压力分布、大原件前台影响或正式v1发行。资格过程中约1230MB当前RSS及CPU仅局部进程观察，未归为全生命周期、全树或绝对峰；本档案没有新的完整内存采样证明。525f快照修正组的包与小型内容桥已有独立证据，但其主规模资格／计时不能从a5继承。

## 原件与公开封存

| 原件 | SHA256 |
| --- | --- |
| main12 browser JSON | `bd7291dff99814651479e03b39226e461db1c5868a6b714c3216d76e3b966324` |
| main48 browser JSON | `70e1985452ce8bca67f6fe0fa2214077686e444b39c46e5b843966fbedc8da33` |
| main120 browser JSON | `16a55979fd74753d3f870ac7b4ab9def11ed4514c777629cd08684e5d718dc10` |
| 最终恢复controller JSON | `9138043a9a81bad8256e11cb80cedee5b95b58e8df863a4670e4950cbaa23235` |
| ed96／a5工作量及首次observer失败链 | 通过恢复preparation／review逐项绑定，原件及SHA保持；见manifest |

[证据manifest](current-three-scale-browser-a5d4-20261005-evidence/manifest.json) SHA `3c978626da1c83e2efddb4c195e7a3b88a09830b8be862d5ec77e004d0f715c8`。36份公开gzip全部mtime=0并解压回验，通过原件SHA、脱敏SHA及gzipSHA逐件绑定；另10份runner／collector／observer／辅助脚本仅保留私有路径、SHA和长度，含合成密码风险的helper不公开。纯恢复控制器保存脱敏文本，其他敏感helper不因任务已完成而发布。

公开的是脱敏分析版，不能宣称与原件字节相同：私有root、PID（含进程映射）、token／capability／密码移除，before／after等私有守卫结构以规范SHA保全。保留全部热／冷数值、ResourceTiming、错误状态与原失败，未归档DB、ZIP、凭据或真实资料。原件、旧公开档案及既有manifest未改；归档不是独立重跑完整核验。
