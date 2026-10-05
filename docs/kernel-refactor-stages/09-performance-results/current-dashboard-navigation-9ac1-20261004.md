# 9ac看板布局与切换竞态正确性（2026-10-04）

本组完成限定GUI正确性核对：61条布局记录取自attempt5，八个主竞态分支取自attempt6，员工月份的逐卡工资所属期强化取自attempt7。它们保留各自provenance，不拼成一次九分支全通过，也不累加后续重复布局。执行与主120资格／资源观察并行，所有time只作diagnostic，不证明导航性能、500ms、完整主规模详情或完整GUI会计操作。

固定源码 `9ac1ddcc2ee3b4d5d4758aff091d5b33c12e1f54a482c4f6ab83ecd6853bcd2d`，770文件，源码manifest SHA `5398ce88ec9b8bc1878cc425046f1d9764d42383720e46706a8aca4dfeea2a1b`。主公司2个月、每月40笔业务、2员工；次公司1个月、40笔业务、2员工。正常类型化入口与单次native负责人批准形成合成业务，注册完整核验sources／historical_adoption／projections／read_indexes四项verified、limitations为空。小样本不代替主120规模或真实企业验收。

## 布局与按需交互

attempt5的`gui-progress.json`保存61条成功布局记录，视口320／375／768／1440。范围包括五页默认内容、可用业务按需详情、凭证详情、凭证下一页及全部模式的完整唯一条数、资金银行／账簿切换及账户筛选、员工payroll／all、资产fixed／all、三类报表及税务模板切换、按实际preview digest展开关账复核。

attempt5后续竞态失败使其`browser-raw.json`整体status仍为failed；不能把61条布局成功改写成整attempt通过。attempt6／7原件中的重复布局和竞态观察都保留，但本记录只采用上述61条布局及明确选定的8＋1竞态证据，不合并重复计数。

## 暂扣真实合法响应后的选择切换

已采用九个分支如下；“公司”覆盖真实公司上下文回落，再用现有快捷月份到有业务的2016-01，不假定切公司会保留旧月份。

| 页面 | 切公司 | 切月份 | 采用证据 |
| --- | --- | --- | --- |
| brief | 已核对 | 已核对 | attempt6 |
| funds | 已核对 | 已核对 | attempt6 |
| employees | 已核对 | 逐卡工资所属期强化 | 公司取attempt6，月份取attempt7 |
| assets | 已核对 | 已核对 | attempt6 |
| reports | 已核对 | 跨季度未覆盖 | 公司取attempt6 |

每个采用分支先得到原请求的真实合法payload并暂扣；切换后暂扣目标合法响应时，旧main DOM为0，原旧请求各观察到一次`ERR_ABORTED`。释放新合法响应，再fulfill旧合法响应，旧结果没有覆盖新页面。实际旧／新URL、对象名称、金额和业务月份分别核对，原件保留完整payload，不用人为错公司响应证明拒绝。

员工切月前后金额同为1520100分，仅金额相同不能证明读到新月份。attempt7强化逐卡`payroll_periods`：两张员工卡均明确2016-01，而不是旧2016-02；姓名、URL、请求取消及晚响应不覆盖一并保存。attempt6该分支的弱观察与attempt7重复的其他分支仍按原件保留，不把强化补跑写成同一次完整九分支证据。

本组证明正常abort启用时的组合行为。没有强制禁用abort单独验证generation分支，不能声称该分支在传输取消失效时已经独立通过。中间默认月份请求的预期取消与原旧公司请求取消分开记录，不混算为旧请求的多次abort。

## 数据守卫与清理

before／after核对两公司完整logical摘要、identity、state、冻结BLOB的SHA／长度相同，固定源码清单不变。catalog认证ticket／session行有正常预期变化，不宣称catalog完整物理不变。按实际批准产生的native确认原件保留，布局／读取竞态不写入新的业务。

清理结果为owned Node／Chrome进程0、service线程退出、合成token logout／删除。凭据使用`InMemoryCredentialStore`，没有OS凭据变动；未操作真实资料、真实负责人身份或现有5173服务。该清理说明只绑定本组，不扩大其他运行流程的结论。

## 首次失败与未覆盖范围

早期UTF-8辅助编码失败、BLOB直接JSON化失败、凭证切换定位误用role、切公司默认月份假定失败的输出、failure与原result全部保留。后续helper使用实际`role=switch`，公司logical改为完整dump哈希并额外保存冻结BLOB SHA／长度，切公司遵循真实上下文行为；修正辅助工具不抹去失败或改写旧状态。私有helper含合成密码，不公开内容。

尚未覆盖报表跨季度inflight、多次快切、按需详情响应本身inflight、非空导入银行流水与分页、export生成／下载及强制禁用abort的generation分支。不是完整AI对话／GUI会计操作、完整主规模详情或500ms验收。当前主120资格尚未ready，纯计时未开始，第9阶段未完成。

## 独立保全清单

[证据manifest](current-dashboard-navigation-9ac1-20261004-evidence/manifest.json) SHA `6c3b37ec95412d09418905c3c2b51a6d682d0a8034cbcfcd5baaaa580c9edfc1`，绑定导航目录64件文本原件和9ac源码manifest，共65件输入：61件公开脱敏gzip分析、4件私有脚本仅保存相对路径／SHA／长度。summary原SHA `590e950f774f0dfd8cdbf685c536fbd8346770001b97b55bba9593b694b78c52`。每项含原文件SHA、脱敏SHA、gzip SHA及解压一致性，gzip mtime为0；PID、私有根路径和认证信息脱敏，不冒充未脱敏raw。

保留全部早期失败、attempt5布局、attempt6／7payload／URL／竞态、native确认、公司守卫、构造与注册核验回执、清理及固定源证据。synthetic数据库根、ZIP、凭据及含密码helper没有复制；只从保存原件归档，没有运行数据库、测试、浏览器、构建或性能流程。既有封存档案不改，主阶段与路线图也不修改。
