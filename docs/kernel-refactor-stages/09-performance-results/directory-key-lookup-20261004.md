# 目录key查找与冻结余额重复哈希组（2026-10-04）

本组两文件修改已收敛，固定源码为 `8473af5ed77f9c079bdf02912bca9c5070735a12a555df9601d22fd9e7fcb582`，770文件，源码manifest SHA为 `286b2058c8cf2bfed25b28aaec00531074b33a0e26a14ae11b28ee504795783a`。20模块统一回归356项全部通过，0失败／错误／跳过。目录遍历与类别哈希重复计算已减少；成对纯原生资产仍有3个≥500ms样本，报表新增1个，未证明稳定时延改善。新源码尚无新鲜完整资格、实际预览或浏览器验收；真实浏览器仍绑定c3c3的资产19个慢样本，第9阶段未完成。目录库和公司库保持draft／0，现有5173、资料根和身份原样，正式冻结、交付与运行入口切换延期。

## 必要完整读取与解析器边界

accounting family包含已提交目录descriptor及过滤依据，实际读取119个月完整原文属于必要读取，不是无用结果body，也不是已证明的重复解码。当前全shape和entry语义校验不能由SQL只提取过滤字段代替；部分SQL提取没有实施。

既有`.tmp/stage9-authenticated-descriptor-decoder-probe-20261004.{py,json,log}`绑定bfb，使用已安装pydantic_core2.41.5，与stdlib比较完整raw-SHA认证合成descriptor的值、类型、顺序及float bits，三组均相同。计时只含内存解析，不含读取、守卫、SHA、后续校验或整页：

| 内容 | blobs／原文字节 | stdlib→pydantic_core median ms |
| --- | --- | --- |
| accounting family | 119／4725674 | 15.9272→12.08265 |
| close headers | 119／443971 | 2.61775→1.85685 |
| settlement roots | 1／1649987 | 13.06745→9.6243 |

settlement替代解析器max61.1414ms保留，不能删慢样本或换算成500ms整页收益。既有内存兼容审查还显示未配对Unicode surrogate、深层嵌套及整数位数限制会改变成功接受或错误行为；有限实际descriptor相等不证明一般语义等价。因此本组不改生产解析器、unique-source解析或fixed-v1规则。先前`.tmp/stage9-directory-decode-cpu-audit-20261004.{md,json}`及同组`stage9_directory_decode_cpu_audit_20261004.py`保留原证据边界，不把旧推测时间当当前实测。

## 实际CPU诊断与两文件修改

`.tmp/stage9-assets-cpu-no-work-observer-20261004.json`绑定bfb：资产3次暖机后1次cProfile，不含工作量观察器。它是单次CPU诊断，仍有cProfile开销，不是纯计时或验收。记录中`_buckets_rows_many`7次调用、self约54ms，`setdefault`124797次；`read_frozen_balances`目标类别哈希12119次。累计函数时间存在嵌套，不能相加为整页分摊。

`close_storage.py`在既有descriptor遍历时保存各bucket首次出现值，新请求bucket直接查找，避免再次遍历整段descriptor；只在owned snapshot的精确token、root及descriptor list绑定下复用。目录索引与读取证明一并暂存，整组物理batch成功后才发布；晚项失败不留下前缀成功。unowned、fixed-v1与direct读取路径不扩展复用，原文SHA、完整shape、条目语义及下游校验保留。

`period_balance_freeze.py`按唯一key只计算一次目标类别哈希，再供所需类别使用。明确指定但没有记录的类别仍核验其桶，不能因空结果跳过stray rows拒绝；无筛选与有筛选职责保持原边界。这是CPU重复计算收敛，不引入跨请求业务缓存或改变冻结格式。

## 同类调用与当前验证边界

同类源码排查发现material_watch／duplicate freeze已按唯一key计算一次，settlement range没有相同类别×key笛卡尔重复；这些是调用链审查，不表示所有路径都实际测量过。公共读取供五页、reference和通用CLI使用，完整core、维修、备份及独立v1核验仍保留各自职责，不用当前目录查找缓存替代它们。

首次两模块专项51通过／1失败，失败是精确owned对象失配的合成fixture缺少目录表；修正fixture后的14项边界检查通过。首次失败与修正原件均保留，不与统一回归相加。固定8473的20模块统一回归356项覆盖受影响读取、五页投影、current／fixed-v1历史、完整核验、读取修复及备份，见`.tmp/stage9-directory-key-lookup-regression-20261004.json`。旧bfb的832项仍只绑定bfb。

## 工作量、纯原生与插桩诊断

8473五页profile与bfb逐页业务摘要相同，SQL、返回行和值字节、SQLite采样VM、事实／结果装载和JSON解码工作量保持原值。资产仍为SQL381、14232行、11950624值字节、1241100采样VM、245结果／253244字节；本组没有裁减必要内容。单独work计数的资产descriptor条目遍历119186→59847、冻结类别目标哈希12119→239；work及cProfile均为插桩诊断，不能将其计时写成纯原生收益。资料、身份、状态、读取修复版本、固定源码守卫及逐页业务摘要不变，读取池正常关闭。

回归与插桩结束后的bfb／8473顺序纯原生窗口各150成功／0错误，每页3次暖机后保留30次样本。计时不含HTTP、渲染及负责人待办，未重新获得当前源码的完整浏览器资格；窗口顺序和未测OS干扰限制因果结论。

| 原生入口 | bfb median／p95／max ms | 8473 median／p95／max ms | ≥500ms |
| --- | --- | --- | --- |
| assets | 467.952／512.368／544.468 | 444.857／544.342／572.566 | 3→3 |
| brief | 337.400／349.708／358.311 | 338.082／389.938／395.034 | 0→0 |
| quarterly_report | 393.984／463.760／470.204 | 393.997／467.764／520.397 | 0→1 |
| funds | 228.688／282.120／285.861 | 228.701／292.406／303.562 | 0→0 |
| employees | 276.806／302.023／311.372 | 278.102／330.205／377.071 | 0→0 |

tail首次helper因把模块函数`_asset_card_sources`当作Dashboard方法而失败，原件保留；修正的`-r2`两源诊断记录分段wall／thread CPU及GC回调，属于插桩，不替代以上纯窗口。当前资产diagnostic median443.70045ms、GC总wall中位70.173ms；报表max552.815ms。不可变descriptor原型仅在helper内临时替换表示，资产median463.62595ms，比当前诊断更慢；GC总wall中位68.779ms，仅减少约1.394ms，报表max567.5897ms。两页摘要及守卫相同，但这不足以证明普遍语义或生命周期安全；本轮不采用生产原型，不关闭GC，不建立跨请求业务缓存。

## 证据保全与收口

[归档manifest](directory-key-lookup-20261004-evidence/manifest.json)保存42件runner、首次专项失败、边界修正、回归、源码差异、profile、解析器／CPU审查、两源work／纯原生、tail首次失败及两源r2、不可变descriptor原型的原文件SHA、脱敏SHA、gzip SHA与表示。manifest SHA为 `d22b31f305e88606eeeed511f686345453a955dc53614157b367abb791df63f2`。既有39件gzip写成后，原归档在WindowsPath与字符串相加时失败、未生成manifest；恢复helper保留39件全部原字节，按相同脱敏算法逐件重构一致，再加入原型三件。JSON前后私有守卫及read_context以SHA归纳，文本移除workspace根路径，gzip mtime为0；这些公开脱敏分析不冒充未脱敏原件。

本组受影响回归与诊断已收口，慢样本、首次失败及负面原型结果均保留。500ms失败标准保持，新源码完整资格、实际开放预览及真实浏览器门槛尚未验收；c3c3浏览器结果不由后续原生或插桩覆盖，阶段9未完成。
