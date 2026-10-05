# 冻结activation的current member guard组（2026-10-04）

本组固定源80735c1e2832458cacb1e13097b9efd3e357cd5bd09fe6a67ad56716eef08613，manifest SHA `fd92ae66b32a466f4f1f7526668094098c6fc5b1b05f3e6d2248cd0092a85d95`，766文件，位置`.tmp/stage9-build-source-frozen-member-guard-20261004`。相对c3c3只改`business_queries.py`／`dashboard.py`并新增`test_asset_activation_current_guard.py`，763文件不变，合同、33个历史模块及前端字节保留。旧c3c3完整资格、开发包／MCP和[主120浏览器FAIL19](owner-proof-main120-browser-c3c3-20261004.md)继续按旧来源绑定，不继承为807当前资格或通过。

## 职责与验证边界

同一owned snapshot已完成冻结activation的精确身份、源与目录证明后，原current member guard仍再装owner＋members全部结果体。现在caller先构造身份证明、后读heads；原generic dual-ID SQL以及当前来源、owner／publication／voucher核对不变。仅全组精确匹配owner、入账月及member subject／calc／fact／kind／month／digest，才省重复body读取。其它版本／范围回原路径，不发布body成功缓存；显式body读取和完整core仍独立拒绝坏内容，不把身份／目录证明扩大为未读body已经成功。

新文件14节点覆盖2／8对象业务与工作量、同subject未采用current、owner／publication／voucher损坏、无影响review、fixed-v1／unowned／posting／include回退、不发布body缓存、显式body及core拒绝。最终集中专项29通过／99.49s（新14＋owner activation7＋owner kind8），不与先前迭代相加。XML suite时间99.336s与pytest端到端时间分别保留。

两轮准备失败保留：run1为9通过／2 fixture失败／27.86s，run2为1通过／3 fixture或断言失败／11.84s；run3为4通过／10.73s，不能描述成第三次失败。各轮log／XML原件分别保存，未改写为生产失败。首次Ruff E501只有工具stdout，没有私有文件，不制造归档原件。专项来自运行时共享源码，源码封存SHA独立绑定，不冒充封存源完整qualification。

## 已完成的插桩工作量

保存profile状态diagnostic_complete，前后业务守卫、source inventory未变和read pool关闭均true；五页严格业务响应SHA与独立c3c3基线一致。主120资产结果体行／解码485→247、结果体字节527,255→364,225，返回行14,112→13,755、值字节12,248,702→11,991,251，JSON2,596→2,239／输入字节9,090,289→8,907,624；SQL376不变，采样VM1,228,200→1,215,500。

简报、员工、报表工作量全部不变。资金仅共同candidate新增fact_id／digest标量，返回值字节3,576,374→3,576,630（+256B），body、JSON、行、SQL与VM不变；不把这项增加隐藏为“所有其它counter完全相同”。基线profile明确属于c3c3，当前profile只属于807。

插桩五页与23模块回归并行，instrumented_ms含观察器开销和并行扰动，不是纯时延，也不证明耗时改善。23个受影响模块回归已完成314通过／462.00s／3条record_property警告、exit0（XML suite461.422s单列），与focused及迭代有重叠，不累计为不同通过项。插桩仍不用于时延结论。当前807未新完整资格／实际预览或浏览器验收；不能宣称资产大样本已达标。

## 成对原生纯计时

baseline c3c3／current807各入口3次暖机＋30个成功样本，各150成功、0错误。保存的前后守卫、源码清单未变、读取池关闭为true，五页业务SHA与双方profile对应相等；两来源company／catalog／非manifest文件守卫相同，source manifest按各自来源独立绑定。全部暖机和正式样本保留；两来源资产各有1个原生慢样本。单位ms，median／p95／max：

| 入口 | baseline c3c3 | current807 |
| --- | --- | --- |
| assets | 455.74695／473.2881／507.1526 | 443.53155／479.6888／508.3899 |
| brief | 338.2399／388.6029／426.2616 | 335.3355／349.9297／354.3620 |
| reports | 395.4228／454.9348／480.3444 | 389.48105／442.6456／445.6291 |
| funds | 233.9574／287.1438／290.1138 | 251.73705／302.7011／338.1966 |
| employees | 259.48105／297.6838／303.9403 | 243.9461／301.0169／303.6610 |

资产median下降但p95／max上升，其他页波动亦保留；有限顺序窗口不证明稳定时延改善或达标。原生没有HTTP／render／owner TODO。c3c3新鲜完整资格只作为diagnostic输入来源，不继承为807资格；c3c3真实浏览器19／150失败保持原样，807尚未新资格或浏览器。暂缓约90分钟的整库资格重跑，先收敛剩余必要读取成本。

## 热资产必要读取诊断

补充profile仅3次暖机＋1次插桩，守卫、源码清单与pool关闭通过，业务SHA同807资产基线。119个月冻结accounting约4.7MB在该调用中只读取／解码一次，属于必要完整读取，不能称重复。开放owner两份body重复仍只是待核候选，未实施。插桩含cProfile／SQL／EXPLAIN开销，不是纯计时，不用于500ms或稳定收益结论。后续voucher头绑定缺口正在修，记录见[同类型组](voucher-header-binding-20261004.md)；代码未固定，不给最终新来源性能结论。

## 原件与当前剩余结果

[26件同组清单](frozen-member-current-guard-20261004-evidence/manifest.json) SHA `7c39740c1bebaa57b861faf5e612ed947be09daa2f56cf2e77d970a794b98b3c`。包含源差异、最终专项及三轮log／XML、当前插桩py／JSON／log、明确标注的c3c3基线JSON、314回归log／XML、两来源native py／JSON／log、初始及最终汇总，以及3暖机＋1次插桩的hot asset profile py／JSON／log。初始待验摘要按当时范围保留，最终摘要另存，原专项／失败原件不覆盖。原件SHA／长度与公开representation分开；私有根路径、guard与read_context按分析脱敏／SHA引用保存，不冒充raw。按本轮明确要求保留插桩observer脚本，仅源码路径脱敏，不运行它、不复制凭据、DB、ZIP或完整inventory。gzip mtime=0、SHA／解压回环核对一致，所有旧清单及失败不改。

本组受影响回归、工作量与原生窗口已完成，各自范围不扩大为整页或807完整资格；后续仍沿同组补实际结果，不新增总体计划或全量门禁。正式合同／交付／入口切换延期，阶段9未完成。
