<!-- @format -->

# 安装、运行与会计 AI 接入

本文说明重构后的 3.0 体系，运行标识为 `ai-accounting-modular/1`。本项目当前正式目录是 `data/accounting/catalog.sqlite3`，项目 MCP 和记账 Skill 已指向这里；每家公司在该目录下独立保存运行库、原件、标准业务文档及备份。当前账套状态见[交付结果](10-delivery-status.md)。

旧 `finance-local` 及其资料仍独立保留；5173 开发页面默认连接当前 V3 后台。本节后续安装路径和公司资料是合成示例；普通启动不会自动搬移资料、替换已有数据库或重放。

## 安装与启动

源码开发使用仓库 `.tmp-kernel-venv` 的 Python 3.12。先安装前端依赖，分别构建既存 `finance-local` 和新体系 `finance-modular` 的随包静态页面，再构建 Python wheel；两套页面使用独立输出目录：

```powershell
npm --prefix frontend ci
npm --prefix frontend run build:release
npm --prefix frontend run build:modular
& '.tmp-kernel-venv/Scripts/python.exe' -m pip wheel . --no-deps --wheel-dir dist
```

安装到独立的 Python 3.12 虚拟环境；下面的 Python 安装位置和 wheel 文件名需替换为实际交付位置。`pip install` 安装 wheel 及其声明的依赖，运行不需要源码目录、Node 或工作区设计文档。

```powershell
& 'C:\Python312\python.exe' -m venv 'D:\合成会计程序\.venv'
& 'D:\合成会计程序\.venv\Scripts\python.exe' -m pip install 'D:\交付\ai_accounting_core-0.1.0-py3-none-any.whl'
$program = 'D:\合成会计程序\.venv\Scripts\finance-modular.exe'
$catalog = 'D:\合成会计资料\catalog.sqlite3'
& $program --help
& $program --catalog $catalog init
& $program --catalog $catalog serve
```

`init` 只在明确的新位置建立目录和负责人，密码及恢复码在本机原生窗口处理，不放入聊天、JSON 或命令行。`serve` 启动指定目录的后台并打开同源看板；已有后台时复用它。`daemon` 在前台运行，`service-info` 和 `stop` 只访问指定后台，不另起服务。全局 `--catalog` 必须放在子命令之前，并使用绝对路径。源码运行时可将 `$program` 调用替换为仓库虚拟环境的 `python -m ai_accounting_modular.host.runtime.cli`。

原生窗口打开后自动聚焦输入，支持回车提交、取消按钮及 Esc 关闭。处理时显示等待提示并防止重复提交；输入有误可以重试，办理结果不明时先核对，避免重复更换密码或恢复码。恢复码可一键复制，但只有明确选择“我已保存恢复码”才完成保存确认；密码或恢复码已更换后，关闭窗口不会撤回更换。请求取消或过期后窗口同步关闭，临时断线则保留窗口中的恢复码，便于保存和重试确认。

## 看板热更新

正式使用的 V3 后台启动后，在仓库根目录执行：

```powershell
npm --prefix frontend run dev
```

保持命令运行，打开 `http://127.0.0.1:5173/`。默认目录库为仓库中的 `data/accounting/catalog.sqlite3`，与当前 CLI、MCP 及普通看板共用同一个后台、负责人身份和公司库。需要指定其他 V3 目录库时，在启动前设置 `FINANCE_MODULAR_CATALOG`；该变量接收目录库文件路径，不接收旧体系资料根。

Vite 仅代理看板 API，通过仓库虚拟环境执行公开命令 `python -I -X utf8 -m ai_accounting_modular.host.runtime.cli --catalog <绝对目录库路径> dev-proxy-info`。命令只发现已运行的后台，不自动启动服务、创建或升级数据库，也不打开身份窗口；`service-info` 继续用于不输出连接能力的普通状态查询。

开发桥接成功时以退出码0输出单个 UTF-8 JSON，字段为 `protocol`、`catalog_id`、`catalog_path`、`port`、`capability`、`state_path`、`state_fingerprint`；失败沿用 CLI 的拒绝响应和非零退出码。运行模块负责路径、目录身份、连接资料解密及实际服务核验，并确认探测前后状态文件的 SHA-256 一致；文件消失或变化时拒绝本次发现。`state_path` 是运行模块提供的绝对变更探针位置，Node 不解析其内容或推导命名，只比较原始字节摘要；同一目录重连时该路径可以改变。

私有连接能力只经过子进程管道和 Node 内存，不发送到浏览器或日志。页面通过一次性票据和 HttpOnly Cookie 登录；没有有效负责人会话时，仍由本人在原生窗口登录。后台重启后，代理会重新验证同一目录并更新端口，失败请求不会自动重放。普通 API 的流式响应、Cookie 及取消语义保持。

Vue、TypeScript 和样式修改由 Vite 热更新。Python 常驻后台源码变更需另行重启 V3 后台；前端热更新不代替后端重启。普通看板也会直接读取构建后的静态文件，因此源码构建可能让新页面先于后台生效。涉及接口或数据库结构的功能，先在隔离环境验证，核验当前备份后停止对应服务，完成明确结构接续，再构建页面并启动同版后台，最后验证实际读取；构建成功和Git提交不代表正式环境已接通。

后台重启会结束旧登录会话，需要在本机密码窗口重新登录并刷新页面。原生密码窗口每次独立启动，表单修改在下次打开窗口时生效。独立运行包和普通看板继续使用各自随包的静态页面。

## 新建公司和登记独立目标

登录后，将以下完整 JSON 保存为 UTF-8 的 `company-create.json`。`company_root` 必须是尚不存在的明确新目录；`backup_directory` 是可选的独立备份目录，不是备份文件名。公司和数据库身份由程序建立，不需要 AI 生成。

```json
{
  "credit_code": "913100000000000001",
  "legal_name": "合成运行公司",
  "start_period": "2026-01",
  "company_root": "D:\\合成会计资料\\company-a",
  "backup_directory": "D:\\合成会计备份\\company-a"
}
```

```powershell
$company = & $program --catalog $catalog company-create --input '.\company-create.json' | ConvertFrom-Json
$company.company_id
$company.database_id
```

`company-create` 新建空公司，不导入旧体系资料。已由恢复或重建交付的独立目标使用 `company-register`；将以下 JSON 保存为 `company-register.json`，目标须是已经完整交付的新体系公司根。登记保留目标的公司及数据库身份，不替换目录中的原公司。`backup_directory` 同样可省略。

```json
{
  "company_root": "D:\\合成接续资料\\restored-company",
  "backup_directory": "D:\\合成会计备份\\restored-company"
}
```

```powershell
& $program --catalog $catalog company-register --input '.\company-register.json'
```

这两个命令属于安装接入，不是日常 `accounting_*` 业务命令，也不在 `accounting://contracts` 的业务请求目录中。

## CLI 办理一次完整业务

业务入口使用 `call <accounting命令> --input <JSON文件>`，不直接把 `accounting-apply` 当子命令。命令名接受下划线或连字符。省略 `--input` 时从标准输入读取一个 JSON 对象。

先用空请求列实际支持种类和可选公司，再选择明确公司。以下调用不写账：

```powershell
'{}' | & $program --catalog $catalog call accounting-schema
'{}' | & $program --catalog $catalog call accounting-context
@{ company_id = $company.company_id } | ConvertTo-Json | & $program --catalog $catalog call accounting-context
'{"kinds":["operations.expense"]}' | & $program --catalog $catalog call accounting-schema
```

业务事实、对象、来源和未解事项按[公开命令合同](03-contracts.md)一次提交完整单。新业务、对象和请求的 UUID 由下列无状态工具分配；更正、重试和提交回复不明时复用原业务身份。生成编号不创建或预留业务，调用方在当前完整请求上下文保存它们。

```powershell
& $program ids --count 3
& $program --catalog $catalog call accounting-apply --input '.\完整业务请求.json'
```

`accounting=saved`、`documents=pending` 表示账已保存、归档待接续；不能再次创建业务。后台接续标准文档、读取和文件任务，实际状态分别通过 `accounting-documents`、页面及 `accounting-jobs` 查询。失败先区分现实事实缺项、合同错误和临时故障；提交结果不明先按原业务 ID 查询，不盲目重放或更换身份。

单笔核算和资金核对失败时，现有问题字段会指明已知的业务身份；已有更具体的字段、来源和错误原因继续保留。问题字段也可用于定位程序或文档冲突，不能仅因返回了字段就向老板追问，应先核对已有资料与错误性质。

资料的整理和退出条件见[资料工作说明](08-ai-work-and-materials.md#资料如何进入系统)。确已选定无用来源并接续好当前引用后，可提交 `{"company_id":"实际公司UUID","action":"discard","source_ids":["已选来源UUID"]}` 给 `call accounting-sources --input <请求.json>`；MCP 使用同一内容调用 `accounting_command(command="accounting_sources", payload=请求)`。只按返回的file_cleanup.state判断文件是否清完；pending由后台沿旧清理标识接续，重新导入的登记及原件受到保护。不能重复discard来催清理，也不通过主机命令自行删原件。

## MCP 接入与按需操作说明

向 MCP 宿主提供以下 stdio 启动命令和参数；配置外层格式以宿主为准，不把此示例写入已有真实配置。MCP、CLI 和看板使用同一指定目录后台，密码仍只在原生窗口输入。

```json
{
  "command": "D:\\合成会计程序\\.venv\\Scripts\\finance-modular.exe",
  "args": ["--catalog", "D:\\合成会计资料\\catalog.sqlite3", "mcp"]
}
```

开始或恢复记账先读 `accounting://guides/work`，按月度办事清单引导老板并展示实际进度；其他说明从 `accounting://guides` 小目录按需读取：

| 资源 | 内容 |
| --- | --- |
| `accounting://guides/work` | 月度清单、各阶段完成条件与推进、资料引导、当前待办、更正和完成状态。 |
| `accounting://guides/commands` | 公开请求、身份、防重、错误接续及标准业务文档。 |
| `accounting://guides/business/{kind}` | 指定事实种类的实际业务规则；例如 `accounting://guides/business/operations.expense`。 |
| `accounting://guides/reporting` | 共享报表的期间、实际采用、未知和取数含义。 |
| `accounting://guides/runtime` | 本安装及运行说明。 |

这些是由唯一设计正文生成、随包提供的只读说明，不读取工作区文档。文档中的种类目录不代表当前实例已装配该能力；先调用 `accounting_schema` 取得实际支持种类，选择后再读取该 kind 的说明和输入 Schema。`accounting://contracts` 提供当前可调用命令的自包含请求 Schema，不含任意分录入口。

实际 MCP 工具只有下列四项；`accounting_apply/get/find/sources/...` 通过 `accounting_command` 选择，不是另一个同名 MCP 工具。

| 工具名 | 一次调用的完整参数对象 |
| --- | --- |
| `accounting_schema` | `{"kinds":["operations.expense"]}` |
| `accounting_new_ids` | `{"count":3}` |
| `accounting_command` | `{"command":"accounting_context","payload":{}}` |
| `accounting_security` | `{"operation":"session_status","payload":{}}` |

工具调用的外层形式以 MCP 宿主为准，逐项选用上表参数。业务请求中的 `company_id` 使用当前选择结果，不能把另一家公司的身份套入。

## 离线恢复和重建

没有目录参数的 `mcp` 只提供 `accounting_restore`、`accounting_rebuild`、它们的请求合同和本运行说明，不提供在线记账工具，也不连接原目录。CLI 的 `restore`、`rebuild` 同样不需要目录或源服务。

将下面的完整 JSON 保存为 `restore-check.json`。`check` 完整核验选定包但不创建目标；`run` 才创建尚不存在的新目标，不覆盖已有文件。备份恢复保留原账状态，标准文档重建按最终事实重新核算，两者不能互称。

```json
{
  "action": "restore",
  "source_path": "D:\\合成会计备份\\company-a\\913100000000000001.finance-company.zip",
  "new_target_directory": "D:\\合成接续资料\\restored-company",
  "mode": "check"
}
```

```powershell
& $program restore --input '.\restore-check.json'
& $program mcp
```

重建使用同一形状，`action` 改为 `rebuild`，`source_path` 指向已交付的完整标准文档 `current.json`。成功交付新目标后再显式 `company-register`，不会自动切换现有服务。

## 明确的公司结构升级

当前公司结构为3，目录结构仍为1。公司结构1、2普通打开会明确返回需要升级；不能重建公司或只改指纹。结构1至2承接公司说明格式，2至3接入银行代发表文件任务。原内核数据库及其备份不适用此入口。

先在原可用环境生成并完整验证每家公司当前版本的ZIP，再停止使用该公司库的服务。离线命令不需要目录库，不启动服务；`--company-directory`是原公司资料根，`--backup`必须是同公司、同数据库、同账务版本的起始结构完整包。起始指纹来自安装包封存的`storage/schema/previous/company-v1.json`或`company-v2.json`，不得用当前结构指纹代替。

```powershell
& $program database-upgrade --company-directory 'D:\合成会计资料\company-a' --backup 'D:\合成会计备份\company-a\913100000000000001.finance-company.zip' --expected-structure-fingerprint '<实际起始结构的fingerprint>'
```

命令完整核验备份，在写锁内重新核对起始结构、身份和账务版本，再按明确增量原子接续到当前结构。DDL失败整链回滚；成功保留身份、业务、凭证、冻结依据、标准文档、原件、历史任务和有效读取结果，不增加账务版本。完成重试不重复改写；若提交后最终资料核验受临时故障影响，可沿同一备份重试，核验成功后再恢复服务。两家公司分别处理，不用一家的包接另一家。

结构1、2的ZIP可先用`restore`准确恢复，再执行上述升级，最后显式登记；恢复不会自动升级或重新核算。验证旧包时只升级临时数据库副本；恢复目标保持原结构与正式数据，只清理不适用于新位置的文件任务和读取派生状态。当前结构包沿正常恢复路径办理。
