# r70 公共 HTTP 额外成本：成组决策

固定后端来源 SHA 402024802800938685bfb59983e6d717ae32fa55c555bad0a3ced3f3d8a8e404，正式仓库仍为 draft／0。本组保存模拟 TCP 控制、真实主48三次浏览器插桩和真实主12及主48公共阶段小样本，不修改生产实现。原[主48浏览器30次失败组](owner-main48-browser-r70.md)的37／150超线结论保持，本组不是新的500ms验收。

## 按问题类型处理

| 类型／位置及调用范围 | 必要读取与已有依据 | 处理／未验证范围 |
| --- | --- | --- |
| 重复响应转换：固定service.py:450–475、http.py:51–83及dashboard GET | 五页及context公共dispatch做一次response校验、一次dump_json；HTTP直接reply已序列化bytes。原生CLI/MCP与独立HTTP合同入口各自核验信任边界。 | 当前静态确认，主12小样本尾部成本很小；不称九路全部分支动态计数验证，不删除结构校验。 |
| 重复默认请求／串行：五个View.refresh、useDashboardContext、api/client、BriefView、CloseReviewPanel | 同公司同月context/main并行，未知选择先取context；FE主响应一次Ajv并核URL身份／期间；简报一次close-review预取交给组件。 | 保存当前checkout静态定位，它不是固定r70缺失的frontend源文件，也不是全部分支耗时证明。真实主48轨迹含setup／cold／warm／measured等，总请求数不是验收样本数。 |
| 授权及公司绑定：SecurityService._transaction／_check_session、Catalog.bind、ResidentReadPool.borrow | 核撤销、凭据版本、绝对／闲置TTL并更新last_seen；连接、结构、文件身份及借用首尾签名分别保护不同边界。 | 主12阶段有公共授权／目录成本，不能共享不同事务证明或删除授权。TTL的时间精度不等于整个授权耗时为微秒。 |
| TCP／Nagle：实际create_server、Handler.reply、真实Edge fetch | 模拟已序列化JSON，无库／Vue；socket实际NODELAY0/1及完整响应字节核对成功。 | 无明显收益，不采用disable_nagle_algorithm改法；class属性或20ms帧阶梯不能证明网络根因。 |
| extra落点：真实公共Handler与dispatch | 主12reply短，主要耗时在dispatch，含必要授权、连接及业务体；各嵌套阶段不能相加。 | 不以另一轮native与HTTP差值当同请求可省成本，不声称network原因。主48两条连接六路已各预热，public／bound完整字段等价，未显示_take容量等待；仍是单样本诊断，不能全归GIL／network。 |
| typed ID重建／FIFO轮转／snapshot首尾证明 | 固定types、QueryReads.snapshot与resident_reads定位副本保留。epoch／repair保护快照变化，池轮转仍保留文件与结构核验。 | 已校验ID重建重叠与FIFO轮转净成本未测。旧静态可疑项不转为根因；本组未有对应原始分段，不把交付摘要0.20–0.43ms算新验收。 |

## 模拟 TCP 正反控制

同一个真实Edge 154.0.4258.48，服务HTTP/1.0；off/on/on/off四组，后两组payload顺序反向。四payload各warm3＋measured11，共176测量及48预热。每次保存完整arrayBuffer时长、resource.responseEnd、transfer／encoded／decoded字节、reply发送调用耗时、实际TCP_NODELAY和全字节／SHA核验；字节核对在fetch计时结束之后。

| 组／NODELAY | bytes | fetch median ms | max ms | reply max ms |
| --- | ---: | ---: | ---: | ---: |
| 1／off | 136 | 3.30 | 6.50 | 0.174 |
| 1／off | 4096 | 3.40 | 5.20 | 0.183 |
| 1／off | 32768 | 3.50 | 4.20 | 0.290 |
| 1／off | 131072 | 3.80 | 4.50 | 0.406 |
| 2／on | 136 | 3.30 | 3.90 | 0.235 |
| 2／on | 4096 | 3.30 | 5.00 | 0.229 |
| 2／on | 32768 | 3.50 | 4.40 | 0.166 |
| 2／on | 131072 | 3.30 | 4.50 | 0.558 |
| 3／on | 136 | 3.40 | 5.10 | 0.166 |
| 3／on | 4096 | 2.80 | 4.00 | 0.189 |
| 3／on | 32768 | 3.20 | 4.50 | 0.179 |
| 3／on | 131072 | 3.80 | 4.40 | 0.283 |
| 4／off | 136 | 2.80 | 4.30 | 0.194 |
| 4／off | 4096 | 2.70 | 3.70 | 0.188 |
| 4／off | 32768 | 3.30 | 4.20 | 0.211 |
| 4／off | 131072 | 3.50 | 4.60 | 0.294 |

fetch中位数约2.7–3.8ms，最大6.5ms，没有40–100ms量级的NODELAY收益。Handler.reply仅计send调用，不计客户端接收或TCP acknowledgement。模拟响应不含业务核验／Ajv／Vue，不能外推所有真实payload与环境。

控制执行与主48诊断的真实preview构造重叠；root回执报告控制进程约7.6秒，原log只有完成JSON，不补造elapsed原件。主48browser测量晚于控制结束，但全过程非纯独占，不能把并行准备时间作为性能验收。

## 主48真实浏览器三次插桩

session64860 exit1，over_target，warm3／repeat3、instrumented_diagnostic，默认20项；真实preview先执行，browser测量在控制结束后，合成凭据已撤销。完整JSON保存149条HTTP及81条dispatch（包含setup和多类浏览器请求），不以此数量宣称重复主页面。

| 页面 | median ms | max ms | ≥500ms |
| --- | ---: | ---: | ---: |
| brief | 516.40 | 574.20 | 2／3 |
| funds | 296.60 | 356.20 | 0／3 |
| employees | 275.70 | 296.40 | 0／3 |
| assets | 416.50 | 476.40 | 0／3 |
| reports | 496.30 | 496.90 | 0／3 |

这些15条诊断样本不覆写原150条纯浏览器验收，也不与异轮native样本相减宣称净收益。HTTP处理／dispatch／客户端接收与render边界不同。cold导航及single_eligible_company导致switch unavailable独立保留在原件，不混入热刷新。

## 主12真实公共阶段的小样本

真实LocalService、原read pool、loopback HTTP／urllib；六路warm3，六次stages、六次handler-thread cProfile、五对context/main并发、一次unprepared close-review，共23 measured请求成功。前后业务state／identity相等，自己的session已logout。准备可能与主48任务重叠；原件声明measured阶段root已确认heavy任务结束，但仍不是纯浏览器30次验收。

| 路由 | Handler ms | authorize ms | dispatch body ms | validate＋dump ms | reply ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| context | 32.85 | 9.37 | 22.96 | 0.11 | 0.304 |
| brief | 347.15 | 9.52 | 336.79 | 0.50 | 0.129 |
| funds | 243.15 | 9.19 | 232.57 | 0.73 | 0.452 |
| employees | 146.28 | 9.51 | 135.62 | 0.61 | 0.362 |
| assets | 143.79 | 7.95 | 134.82 | 0.59 | 0.260 |
| quarterly-report | 326.78 | 8.23 | 317.23 | 0.58 | 0.520 |

实际六路reply最大0.520ms，主要成本在dispatch。connect／commit嵌套于authorize，Catalog.bind及pool enter包含于dispatch body，不能将列相加；pool enter也是direct Dashboard必要成本，不全算HTTP额外。

并发使用两条实际物理连接，但首个新connection_2无独立warm3，且未独立计_take等待，不能声称容量饥饿。Windows thread_time为15.625ms量化；cProfile、诊断PRAGMA及Python JSON解析的限制保留，不替代浏览器Ajv／Vue测量。unprepared close-review不替代主48prepared browser路径。详细阶段、路径与结构caller说明在归档原.md中保留；没有证明可以合并首次连接与事务内、borrow首尾的不同身份检查。

## 主48真实公共路径与bound对照

固定同一r70主48合成库，repair1 complete：23 measured公共请求、36 warm；两条实际物理连接对六路径均各warm3，再记录一次stages、一次handler-thread cProfile、一次bound同范围对照及五对context/main并发。state／identity不变，自己的session已logout，preview_close调用0。原件声明本轮无root heavy并行，但单样本仍不是浏览器或30次纯计时验收。

public与bound的完整响应字段及金额相同，只精确归一context.generated_at、brief.data.generated_at、quarterly-report.checked_at这三个展示时间；其他字段全部保留。初轮把展示generated_at逐字比较，准备期失败，measured0；原失败JSON／log及脚本保留，不能称业务不一致或CPU验收失败。repair1只修诊断等价检查，不修改生产。

| 路由 | public Handler ms | bound read ms | context/main并发主Handler ms | 顺序_take ms | reply ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| context | 36.76 | 13.69 | — | 0.0106 | 0.361 |
| brief | 404.21 | 403.38 | 451.66 | 0.0097 | 0.381 |
| funds | 251.75 | 326.71 | 319.08 | 0.0089 | 0.373 |
| employees | 196.54 | 188.44 | 213.07 | 0.0096 | 0.370 |
| assets | 371.67 | 337.85 | 383.6 | 0.0099 | 0.332 |
| quarterly-report | 379.03 | 375.40 | 450.47 | 0.0090 | 0.416 |

已预热的实际_take调用为极短定位，没有显示池容量等待；这不证明各种公司切换／满池都不存在等待。public与bound分别包含不同授权／绑定边界，先后单样本差值不稳定，资金public甚至短于bound；不把差值泛化为固定公共税、GIL或网络根因。简报public404.2／bound403.4，资产371.7／337.9，报表379.0／375.4；并发主Handler分别451.7、383.6、450.5ms，不能据此宣称浏览器通过或并发净收益。

context.prof调用图混入不合理urllib／client记录，不能拿其归因或与其他profile累计相加；原profile如实保全并在manifest标为归因不可用。其他五profile及独立stages正常，仍有profiler开销／Windows thread_time量化。没有为清理这一观察重新测量。详细调用阶段与限制按作者完整原.md归档；单次pool_take不等于整个borrow，文件／结构检查仍是必要读取。

## 历史静态审查与证据边界

grouped-reuse文档明确2026-09-27／固定r21；grouped-transport含旧默认100条、完整准备及专用三worker等历史范围。它们保存原文与文件mtime，mtime不冒充作者生成时间，旧假说不写成当前结论。当前checkout前端副本仅作静态定位；固定后端副本经r70清单hash核对。

第一批原件与静态定位见[首批manifest](owner-http-extra-cost-r70-manifest.json)；主48初失败及repair1完整原件见[主48manifest](owner-http-extra-cost-r70-main48-manifest.json)。两份清单分别保存raw及gzip双SHA／bytes／mtime与逐字节解压确认，第二批引用第一批而不重复制旧档；本集中记录直接更新，首批清单中的document_sha256保留当时版本。本次归档没有新增测量、测试、SQL、服务或生产修改。
