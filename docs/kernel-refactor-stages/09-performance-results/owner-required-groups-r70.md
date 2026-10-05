<!-- @format -->

# r70：必要读取收敛后的固定原生诊断

固定 r70 主 48 月六入口各三次原生样本均低于 500 毫秒，浏览器整页验收仍失败：150 次中 37 次达到或超过 500 毫秒。该候选的主 12、主 48 和独立 12 月注册完整核验均已完成，四项 coverage 为 verified，limitations 为空；详见 [内容与边界核验](owner-r70-content-and-boundaries.md) 和 [实际整页结果](owner-main48-browser-r70.md)。原始诊断 JSON 的 `current_source_full_integrity=not_run` 表示采样当时尚未运行，不回写原件。仓库仍为 draft/0，未正式冻结或交付第 9 阶段。

## 固定来源与调用范围

隔离 released/1 候选源清单 SHA 为 `402024802800938685bfb59983e6d717ae32fa55c555bad0a3ced3f3d8a8e404`；源目录 `.tmp/stage9-build-source-owner-r70-release`，使用同结构主48合成根 `.tmp/stage9-owner-main48-r63`。candidate/source 合同指纹保持原值：catalog `b484d315272bb64de3856b458f04d2d4881da05db7552250864260916c69a4ee`、company `c9f9f7051bca67f1241ee5c89676fb9476bc819dc8f92c1a0c0bd1e940f459cb`、content `ac6d042b8478d87add37376fab5e5c9b4ae99b6ccab89c1e2f52678b779fe5f8`。实际文件 SHA 另见 provenance；隔离候选转换不等于正式仓库冻结。

| 位置／调用方 | 根因与修复决定 | 验证及边界 |
| --- | --- | --- |
| `dashboard_reads._payroll_roster_candidates/_payroll_roster_sql/_adopted_heads_sql`，员工名单 | 将整个人员发现与本月／开放月／撤去／更正的必要来源候选分开。角色行只定位每人每kind的一份事实；独立缺current publication与当月冻结零行结果定位继续存在。未解析候选和覆盖缺口返回None，进入原完整 `payroll_head_metadata` 回退。 | 完整来源、采用、身份与本月金额核验不取消；相同姓名不合并，未来未采用／撤去不抹去旧人员见证。需当前全核验及浏览器确认。 |
| `settlement_freeze._scope`，简报、工资未付、资产付款、报表及清单清偿范围 | 完整开放尾部先核验；同快照相等scope提前复用已核汇总，之后才构造新groups／计数／金额，未来发生额仍分离。 | 已有3项与剩余14项独立回执，见专属归档；未声明一次17项重跑。资金／上下文不据此宣称受益。 |
| `ResidentReadPool`，常驻五页与上下文 | 必要数据库页反复驱逐；每连接物理页预算64→256MiB，最多4连接，借出读回预算，不正确连接失效并替换。 | 既有64/128/256/256/128/64正反对照及11项回归；四连接1GiB是按需页预算上限，不是实测RSS峰值或服务树内存证明。 |
| report flow／party／anchored来源及Row转换 | 4932月flow与411当前头无交集；party938/report1002交937，不能削減最终1004解析；anchored交50已跳过；1005×13列纯转换净差约0.68ms，不扩大源码修改。 | 必要读取保持；本样本 `_report` 一次，静态fullyclosed双调用本轮未核验，不能声称所有问题已排除。 |
| 独立 owner-read-review | 对读取职责、缺源和回退的静态审阅未报告新的证据缺陷，已停止。 | 这是静态判断，没有新增测试或计时，不替代当前registered full verify。 |

## 员工精确工作量与分批回归

`.tmp/stage9-r69-employee-scope-implemented.py/.json/.log` 名称保留r69，实际读取修正后的production query builder，`YearMonth('2019-12').ordinal=24227`，50对象／50候选subject，两侧全字段multiset及顺序相同。定位SQL原1,370,800VM；修正各locator阶段86,200+130,000+300+26,700=243,200VM。它是只读SQL计划／工作量诊断，不是原生时间或业务成功证明。旧错误24239实验仍撤回，原归档保留，本轮不重复搬迁。

固定整页员工工作量则从r69的1,800,300VM降至r70的672,300VM，不能将整页与单locator数字混用。整页返回5,431→5,531行、3,473,865→3,479,645B；独立异常定位带来正常必要读取，不能声称所有返回量下降。

| 独立组与原始文件stem | 实际回执 | 最终版本说明 |
| --- | --- | --- |
| `stage9-r69-employee-scope-repair1` | 12 passed，43 deselected，95.44秒（XML suite95.223秒） | 最小新增业务／角色／pointer／真实增长回归。 |
| `stage9-r69-employee-scope-directed` | 34 passed，120.03秒（XML suite119.729秒） | 同类必要分支组合；发生在最后fallback修正之前，不改写成最后源码整组结果。 |
| `stage9-r69-employee-scope-fallback` | 6 passed，20.80秒（XML suite20.612秒） | 最后未解析／覆盖缺口回退修正后，仅重验六个受影响分支。 |

三组有各自log/XML/command，不能相加成一次最终52项全过；owned.diff与SHA保留最终改动版本。清偿日志3 passed/12.92秒及14 passed、3 deselected/36.13秒引用 [读取归档](owner-r70-read-decisions-manifest.json)；物理页预算11 passed/13.30秒与Ruff引用 [页预算归档](owner-r70-page-budget-manifest.json)。这些是独立事项，未合并为新的统一全组回执。

## 固定六入口原生／profile／工作量

实际wrapper只读base `stage9-resident-r64-main48-profile.py` 并替换轮次r64→r70。每个入口先预热3次、原生采样3次，再各执行一次cProfile和一次工作量插桩；三种观察分开，不把profile或插桩耗时当原生。下表为三次原生min–max，不是p95、median或浏览器计时。

| 入口 | 原生min–max ms | SQLite VM | 返回行 | 返回值字节 |
| --- | --- | ---: | ---: | ---: |
| 上下文 | 11.33–12.13 | 11,000 | 96 | 850 |
| 简报 | 386.99–400.47 | 1,471,100 | 19,042 | 9,498,221 |
| 资金 | 268.01–281.94 | 1,122,300 | 8,574 | 4,296,677 |
| 员工 | 217.83–249.67 | 672,300 | 5,531 | 3,479,645 |
| 资产 | 366.63–411.48 | 703,700 | 11,501 | 8,448,529 |
| 报表 | 339.47–349.29 | 888,000 | 23,049 | 10,401,285 |

正常慢样本全部保留。本轮记录说明固定候选这18次原生读取均低于500ms；不由此宣称五页刷新达标、净收益单独归因或完整发行包通过。报告读取仍解析1004份贡献，必要内容并未靠去掉核验而减少。

## 证据保存与剩余范围

[固定诊断归档](owner-r70-fixed-diagnostic-manifest.json) 排他创建原始base/wrapper/log/JSON/六份prof、source inventory/provenance/candidate log、员工三组回执及typed query probe、owned diff/SHA和固定实现/测试副本；每条记录raw和gzip的SHA256、bytes、mtime与解压逐字节相等。已有清偿／页预算专属manifest只引用，不重复压缩。注册完整核验和浏览器结果另有独立归档，不把原生、插桩或内容核验当作整页计时。

主 48 月五页已按规定各测 30 次，简报、资产及报表仍有慢样本，不能宣布性能收口。冷开单列；本轮只有一家可切换公司，切公司标为 unavailable，不能算通过。主／独立 120 月和独立 48 月承接、两档原件规模及内存、最终正式合同／独立包／实际 AI MCP 交付仍需对应证据。不能将 prior source full verify 或隔离 source 合同冻结作为当前完整验收。本页更新实际状态，不新增测试或测量，不操作真实资料或现有服务，尚未提交 Git。
