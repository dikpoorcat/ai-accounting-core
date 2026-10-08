"""Typed local entry points shared by CLI and MCP. No prepared-result input route."""

from __future__ import annotations

import base64
import secrets
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from .asset_batches import AssetBatches
from .backup import run_backup_jobs
from .business_queries import BusinessQueries
from .catalog import Catalog
from .close_review import CloseReview
from .contracts import KernelError, Registry
from .discovery import Discovery
from .display import Display
from .duplicates import Duplicates
from .engine import PROGRAM_VERSION, Engine
from .entities import Entities
from .exports import Exports, run_export_jobs
from .identity_corrections import IdentityCorrections
from .materials import Materials
from .payroll_preparation import PayrollPreparation
from .periods import Periods
from .replay_close import ReplayClose, ReplayCloseScope, ReplayScopeConfig
from .reports import Reports, run_report_jobs
from .security import SecurityService, consume_close_approval
from .tax_import import TaxImport
from .workflow import Workflow

_HTTP_DASHBOARD_COMMANDS = frozenset(
    {
        "dashboard_context",
        "dashboard_brief",
        "dashboard_funds",
        "dashboard_employees",
        "dashboard_assets",
        "dashboard_business_status",
        "dashboard_quarterly_report",
        "dashboard_period_preparation",
        "dashboard_close_review",
    }
)

OPERATING_PROTOCOL = {
    "version": 4,
    "schema_discovery": (
        "finance_local_schema无参读取overview概要。按当前事项用view=selected明确选择"
        "fact_kinds、commands和response_types取得所需详细合同；通用登记命令同时选择事实类型。"
        "完整合同仅用view=full显式获取，不重复把整套事实和响应合同载入会话。"
    ),
    "work_entry": {
        "generic_start": (
            "泛化开始先绑定公司、读取company_context，再调用workflow，as_of使用当前实际日期。"
            "用户没有指定月份时省略period，由内核选择最早真实待处理月；空状态才询问起始月份。"
            "开场说明公司、月份、当前事项，并用编号清单完整展示银行、工资、普通业务、税务、资产、融资。"
            "工资、社保、公积金、个税等具体结果和缺项放在对应事项中说明，默认按展示清单顺序逐项推进。"
            "同时分别说明关账、实际办理和文件交付，不以一个完成覆盖所有状态。"
        ),
        "specific_request": (
            "用户明确交办先处理某件业务时直接查证处理，不先插入无关完整清单。"
            "补充资料、回答当前项或泛化继续不表示改序；指定事项结束后回到清单最早未完成或待确认项。"
        ),
        "priority": (
            "默认从展示清单最早未完成或待确认的事项开始；完成并已有适用确认，或有依据的不适用时才进入下一项。"
            "当前项缺资料、缺依赖或待老板确认时停在本项，先查已有来源，再问仍缺什么；"
            "后项即使资料齐全、已到期或可独立办理也不自动先处理，到期仅提醒，不自动改序。"
            "同一事项所需的查证与依赖准备在本项内完成，不独立发布后项结果；不能凭技术准备把前项标为完成。"
            "在当前事项或明确交办范围内，报税和报社保可先登记真实办理，再核对账务；财报须等待相关期间核算并关账。"
            "不自动执行真实外部申报或银行付款。"
            "未知期限不推定逾期，普通未结余额不是付款指令。"
        ),
        "progress": (
            "事实保存、正式发布、关账、实际办理和文件交付分别以系统状态为准。"
            "每完成一个已展示的清单事项，重读workflow，说明本次结果、完整清单和当前待办。"
            "已有适用确认时展示进展并依次继续相邻事项，不能跨过前面的缺项；"
            "没有确认时标为已处理、待确认，等老板确认后才进入下一项。"
            "同一事项内的查证、登记、预览和发布连续完成，不为每笔入账或内部工具调用增加确认和整表刷新。"
            "清单用✅表示已完成或本月无业务已确认，🔄表示当前处理或已处理待确认，"
            "⬜表示尚未处理或缺资料，⏰仅标有明确期限的到期未办事项，➖仅标有依据的不适用；每项写明实际状态。"
            "ready不一律等于已完成，unestablished不一律等于缺资料；核算未建立但已有有效无业务确认且无冲突时说明已确认无业务。"
            "资料无业务不能推定外部事项不适用；缺资料写具体缺项，待确认写清需要老板确认的范围。"
            "没有推进不反复刷清单。"
            "pending或running任务不能说已经交付，文件产物核验通过后才能提供。"
        ),
    },
    "owner_answers": {
        "lookup_first": (
            "当前事项先用work_context按明确公司、月份和事项读取已有资料、事实、对象与正式采用。"
            "本页未出现或尚未展开的历史依据不表示资料缺失；沿游标或精确来源继续查询。"
            "需要原文细节时按原位置读取inspect_material；解析缓存只供阅读，不代表正式核验完成。"
            "查清已有来源和精确任务后，再问仍会改变处理的缺项；老板可陆续提供资料，无需一次交齐。"
            "已有唯一明确依据时直接复用；仍有歧义时说明具体范围和有依据的建议，不能补造同意。"
        ),
        "scope": (
            "老板回答只覆盖刚展示的公司、期间、对象和事项。没有变化须形成精确工资复用依据；"
            "都完成了只对应已列明的外部事项；没有业务只对应明确的资料类别和月份。"
            "保存原话证据并使用相应类型化入口，不用一个通用已确认标记替代正式事实。"
            "回答范围不唯一或切公司后，先重新说明范围再确认，不能扩大到未展示事项。"
        ),
        "progress_confirmation": (
            "先核对资料或既有记录中的老板明确确认；覆盖当前公司、期间、本项事实和范围且仍适用时直接复用，"
            "无需额外要求一句允许继续。普通原件、AI判断或已发布状态本身不等于老板确认。"
            "无适用确认的事项处理完后展示结果并等待；老板确认只接续刚展示的事项，不扩大到其余清单。"
            "关键事实或范围变化时对变化部分重新确认，普通系统重算不一律使确认失效。"
            "单纯结果无异议或继续的答复用于会话推进，不新建通用进度事实，不冒充资料完整、无业务或实际办理；"
            "答复含新增业务事实时才使用对应类型化入口。清单确认不替代工资发布前确认或关账密码批准。"
        ),
    },
    "recovery": {
        "errors": (
            "依status、code、fact_issues及resolution处理，不解析中文message猜下一步。"
            "needs_information先查reusable_sources；技术错误、能力限制和待重算不能直接追问老板。"
            "resolution只说明有依据的处理入口，不意味着缺少的事实已经成立。"
        ),
        "requests": (
            "响应丢失且原请求仍在时，重发原载荷与原request_id；不能生成新键重复写入。"
            "只知道请求编号时用request_result查询；unknown只表示未观察到提交，不能认定失败。"
            "载荷无法可靠恢复时先查真实业务状态，不猜原载荷。"
            "预览失效须重新读取、预览和核对，改变内容后使用新request_id。"
        ),
        "jobs": (
            "查询精确job_id；pending/running等待同一任务。自动尝试耗尽后按error_code处理原因，"
            "原因消除后才显式retry_job，不无限开始新的重试周期。目录创建恢复用operations。"
        ),
        "resume": (
            "中断后重新读取schema、公司上下文、workflow、请求回执和相关任务。"
            "从真实业务状态和可核查的确认来源恢复清单；已处理但找不到适用确认时如实显示待确认，不猜已确认。"
            "后项已有结果如实保留，当前回到最早未完成或待确认项，不因后项有结果或待确认就跳过前项，不自行撤回已入账结果。"
            "同目录服务重连保留原请求；目录身份变化不得自动重放。"
            "开放月关账预览在服务重启后重新准备；已经提交的关账从回执和冻结内容确认。"
        ),
        "company_switch": (
            "切换公司及切回都重新绑定company_id并读取company_context和workflow。"
            "不得沿用另一公司的对象、候选、游标、预览、批准或未提交载荷；只保留明确业务意图。"
        ),
    },
    "user_facing_language_policy": (
        "默认用直白、简短的简体中文说明公司、事项、结果、仍需处理什么。"
        "状态必须来自内核；内部编号、哈希和JSON只供技术详情，不用它们代替业务名称。"
    ),
    "company_binding": "先列出公司并明确当前company_id；公司切换不能沿用另一公司的业务身份或预览。",
    "evidence_first": "先核对已提供资料及既有事实；正式确认事实必须引用实际采用的不可变证据。",
    "typed_facts": "只提交类型化业务事实，不编造科目、借贷或缺失的核算事实。金额使用整数分。",
    "entities": (
        "登记前用find_entities按明确资料查找公司内对象；复用或用register_entity生成对象编号。"
        "人员、机构、资金账户、资产、项目、基金产品与单笔业务编号分开；同名不自动合并。"
        "payee收款账户属于付款指令，不能当作公司资金账户。"
    ),
    "fact_discovery": (
        "find_facts使用entity_id和可选role、identity_match=current|recorded及不透明cursor；"
        "返回原事实与匹配归属，不从聊天记忆判断状态；游标失效后重新查第一页。"
    ),
    "duplicate_review": (
        "save_fact/amend_fact/save_facts自动查重；明显疑点先查既有原件，仍不能判断才问负责人。"
        "可提前prepare_fact_registration，不要求无疑点业务额外预检。复用已有业务不另建；"
        "确认另笔必须引用区分依据，不能只写解释；弱线索仅供查找，不主动打断。"
    ),
    "identity_correction": (
        "身份指错用preview_identity_correction/confirm_identity_correction，明确事实范围与依据；"
        "名单、累计、清偿和资产影响一起处理，不能先合并显示再留下未处理账务。"
        "不能通过改名称掩盖实际付错人；已冻结内容保留，闭期归属更正在指定开放月承接。"
    ),
    "managed_reserve": (
        "备用金用managed_reserve_expense登记实际支出、managed_reserve_refund登记实际退回公司；"
        "支持银行、现金和公司平台账户，备用金本身不建账户，不询问余额、成本来源或可退额度。"
        "退款只凭实际收款及业务性质，不把退款权利、原债结清或备用金内部消费当成实际退款。"
        "工资混合付款用reserve_expense_fen明确备用金部分；全额银行流水、完整净薪和实际退入依据分别核对。"
        "备用金内部原件保留并作有依据的不入账处置，公司平台原行仍须完整且唯一处理。"
    ),
    "missing_information": "根据fact_issues核对可复用来源后再补充，不把错误码直接变成负责人追问。",
    "publication": (
        "保存事实与发布结果分开；预览逐项核对source_period、posting_period和mode，"
        "再用同一摘要和相关版本确认，重试沿用同一请求键。"
    ),
    "payroll_confirmation": (
        "普通和bounded工资正式发布前必须有本月明确方案或有效的全员无变化确认及原始依据。"
        "方案绑定完整工资输入、档案和政策修订及全部变更通知；无变化绑定完整人员范围和逐人上期输入。"
        "prepare_payroll也检查已有工资；confirm_payroll_preparation只登记事实，不代表已发布。"
        "累计或实际扣款重算不自动使工资输入确认失效；先处理明确的待重算来源，不补造负责人确认。"
        "payroll_first_wage_treatment.period是有源适用首月，standard_deduction_start_month是累计扣除起点；"
        "工资只采用当月及以前的处理，未来月份规则不能反向改变历史工资。"
        "历史无界依赖误标待复核时先用同subject公开预览核对；完整签名相等才可review_no_impact发布，"
        "保留原凭证和冻结，不直接改待复核记录或强制声称无影响。"
    ),
    "tax_import_mapping": (
        "工资准备和期间待办的tax_import_mapping只检查个税文件列对应。"
        "缺失或错误映射按明确资料补正；tax_import_format_unsupported是文件能力限制，"
        "不能变成负责人业务追问，也不阻止工资记账、真实付款、关账或有依据的外部完成。"
        "正式个税文件仍须重新核对映射、人员和扣除明细，不能把映射就绪当成文件已完成。"
    ),
    "asset_batches": (
        "资产启用通过prepare_asset_activation_batch/confirm_asset_activation_batch按确认批次处理；"
        "折旧摊销通过prepare_asset_consumption_month/confirm_asset_consumption_month由内核确定完整月度成员。"
        "不得直接登记或发布单卡启用、单卡折旧摊销，也不得提交自由科目、分录或月度成员排除清单。"
    ),
    "correction": (
        "录入错误用amend_fact并明确依据；新实际行为用新身份；开放所属月按所属月入账，"
        "所属月已关账的迟到业务或冻结结果更正必须用posting_period指定开放入账月。"
        "自动重算的开放依赖保留各自原入账月；显式业务与posting_period冲突时拒绝。"
    ),
    "management": "管理说明、归集资料可后补，不把管理缺项当核算门禁，不以月末冒充实际日期。",
    "dashboard_management": (
        "对象名称、人员入离职资料与用途通过update_entity_profile按明确来源追加管理版本；"
        "save_display_profile只保存单笔业务说明；"
        "登记业务时同步保存原件已明确的人员/往来方/账户/资产名称及业务用途说明，"
        "复用稳定业务身份，记录证据文件名和具体来源位置；不只保留内部编号。"
        "资料后补保留实际来源，不因展示资料缺项阻断核算。"
        "不从工资生效月推断入职日。月度经营结论先preview_period_commentary读取核算上下文，"
        "再用同一context_digest调用update_period_commentary；闭期补充显示为后补说明，"
        "context_digest只用于提交并发校验；已存说明按独立content_digest及精确来源判断有效性，"
        "内容相同不能绕过过期提交。历史展示field_sources分别说明冻结与当前后补来源，"
        "recorded_at仅是系统确认时间，不能替代实际发生日或冒充负责人最早知悉日。"
        "不更改冻结凭证，不要求负责人补写AI应完成的经营说明。"
    ),
    "closing": (
        "普通记账按公司逐月preview_close，使用返回的核对定位让负责人查看经营简报的同版月度核对。"
        "页面只读；实际采用的政策、工资确认、原始资料和金额由内核提供，不用AI自写摘要替代。"
        "核对后请求approve_period_close原生密码窗口，携带同一preview_digest及版本；"
        "取得approval_id后执行close。密码只在原生窗口输入，批准窗口不执行关账。"
        "预览替换或版本变化须重新核对；响应丢失先查询原状态并沿用幂等键，不重复关账。"
        "连续月份逐月完成；空数据库或零待匹配项不能证明无业务。"
    ),
    "reconstruction_replay": (
        "按重放文档重建默认采用重放专用批量关账，不逐月请求密码批准。"
        "先由本机daemon --replay-scope加载明确私有范围文件，"
        "核对schema的replay_close_contract.enabled。"
        "范围限定目录、开发测试公司库、数据库身份、起止月份及已登记负责人原确认；普通服务明确拒绝。"
        "有效登录后调用preview_replay_close_range，再携带同一first_period、last_period、owner_confirmation、"
        "preview_digest、epochs及稳定request_id调用confirm_replay_close_range。"
        "内核按实际前序关账逐月重新预览、检查、冻结和生成备份；范围不代替逐月资料或业务事实。"
        "部分完成停于首个阻断月；原样重试校验持久子请求与冻结链，不能跳月或重复已关账。"
        "来源版本、读取修复、程序、会话或凭据变化停止旧批量并重新核对剩余范围。"
        "冻结approval为null，审计标明replay_scope，不冒充密码批准。"
        "开发交付本身不授权真实重放；已有明确继续授权时保留当前库、核对同版状态并沿原范围接续，"
        "不重复请求同一授权，不自行新建空目标或负责人身份。"
    ),
    "material_allocation": (
        "跨月原件先核对逐项归属，再分别处理各月业务；归属不等于入账完成，未知归属不能默认接收月。"
        "闭期资料新问题由后续开放月持续承接，直至真实处置完成；不能确认已知后忽略或挪到远期。"
        "同一文件内未来行仅在所属月阻断，file_status仅供诊断，不作为所有月份的门禁。"
    ),
    "material_duplicates": (
        "duplicate明确重复原件位置和依据；普通逐项links为空时须同额同期间引用完整原行。"
        "重复资料仅对应联合组中的部分完整分配时，duplicate.links精确选择目标组已有的完整MaterialLink，"
        "全部身份、事实、计算、字段、金额及核算月必须相同；选择金额合计等于重复原行。"
        "不得裁切分配、猜逐行对应或放宽原始收支方向；目标完整组及所有结果仍须有效。"
        "这些链接只表达重复覆盖，不新增业务或再次占用原业务金额；变化仍参与资料依赖和冻结核验。"
        "多个原行共同重复一个完整联合组时，用resolve_material_group明确完整成员、同额总池、"
        "joint_basis_confirmed及原件依据；每个成员同时保存唯一duplicate处置，links为空且指向"
        "同一目标原位置，成员处置版本也须引用完整原件。目标只能是唯一原行构成的完整主联合组，双方完整MaterialLink集合相同。"
        "所有成员同期间、方向和精确原额，不能部分声明、混合认账、多个目标或重复链；"
        "这些逐项处置只声明整池重复，不建立成员到个人结果的分配，整池不二次消耗业务金额。"
    ),
    "external_actions": (
        "申报、扣税、缴款和文件交付是不同事实。external_completion保存真实办理与原采用依据；"
        "external_basis_review保存其与正式账务的精确核对。已申报可以仍待核对或存在差异，"
        "后续账务变化不能抹掉实际办理。季度税务和季度财报分开，复核不能冒充再次申报。"
    ),
}


def default_registry():
    from . import (
        materials,
        payroll_preparation,
        payroll_tax_declarations,
        reports,
        tax_import,
        workflow,
    )
    from .domains import (
        accounting,
        adjustments,
        assets,
        banking,
        cash,
        investments,
        labor_assets,
        managed_reserve,
        opening,
        payroll,
        payroll_reserve_payment,
        platforms,
        taxes,
        transactions,
    )

    registry = Registry()
    for module, category in (
        (payroll, "payroll"),
        (taxes, "tax"),
        (transactions, "transactions"),
        (assets, "assets"),
        (labor_assets, "assets"),
        (banking, "bank"),
        (adjustments, "transactions"),
        (cash, "transactions"),
        (platforms, "transactions"),
        (managed_reserve, "transactions"),
        (payroll_reserve_payment, "bank"),
        (investments, "transactions"),
    ):
        before = set(registry.models)
        module.register(registry)
        for kind in set(registry.models) - before:
            model = registry.models[kind]
            model.material_category = (
                "bank"
                if kind
                in {
                    "payment",
                    "cash_bank_transfer",
                    "bank_platform_transfer",
                    "payroll_reserve_payment",
                }
                else ("financing" if kind.startswith(("loan", "borrowing")) else category)
            )
    reports.register(registry)
    workflow.register(registry)
    opening.register(registry)
    materials.register(registry)
    payroll_preparation.register(registry)
    tax_import.register(registry)
    payroll_tax_declarations.register(registry)
    accounting.register(registry)
    from .identity_corrections import register as register_identity_corrections

    register_identity_corrections(registry)
    return registry


class LocalService:
    def __init__(
        self,
        root: str | Path,
        *,
        enable_read_pool: bool = False,
        replay_scope: dict | None = None,
        _static_runtime=None,
    ):
        from .schema_bundle import production_bundle

        if _static_runtime is None:
            self.bundle = production_bundle()
            prepared_models = None
        else:
            self.bundle, prepared_models = _static_runtime
            if self.bundle is not production_bundle():
                raise ValueError("daemon runtime does not match the production bundle")
        self.registry = self.bundle.registry
        self.catalog = Catalog(root, self.bundle)
        from .inspection_cache import InspectionCache

        self.inspection_cache = InspectionCache(self.catalog.root, build_id=PROGRAM_VERSION)
        self.read_pool = None
        self._catalog_observer = None
        self._catalog_observer_connection = None
        try:
            if enable_read_pool:
                from .resident_reads import ResidentReadPool

                self.read_pool = ResidentReadPool(maximum=4)
            self.security = SecurityService(self.catalog.path)
            self.replay_close_scope = (
                ReplayCloseScope.from_config(replay_scope, self.security.catalog_instance_id)
                if replay_scope is not None
                else None
            )
            self.replay_close_previews = {}
            self.replay_close_lock = RLock()
            self.close_previews = {}
            self.active_close_previews = {}
            self._close_preview_key = secrets.token_bytes(32)
            if prepared_models is None:
                from .command_schema import command_models

                prepared_models = command_models(self.registry)
            self.command_models = prepared_models
            if enable_read_pool:
                # This connection has no read transaction and carries no business
                # result. It only keeps SQLite's catalog connection lifecycle
                # stable while each request opens and validates its own connection.
                observer = ExitStack()
                try:
                    connection = observer.enter_context(
                        self.catalog.connection(read_only=True, _cross_thread=True)
                    )
                    if connection.in_transaction:
                        raise RuntimeError("Catalog observer must not hold a transaction")
                except BaseException:
                    observer.close()
                    raise
                self._catalog_observer = observer
                self._catalog_observer_connection = connection
        except BaseException:
            self.close()
            raise

    def engine(self, company_id, *, dashboard_read=False):
        return Engine(
            self.catalog.bind(
                company_id,
                read_pool=self.read_pool if dashboard_read else None,
            )
        )

    def close(self):
        if hasattr(self, "close_previews"):
            with self.security.authorization_gate:
                self.close_previews.clear()
                self.active_close_previews.clear()
        with ExitStack() as cleanup:
            if self.read_pool is not None:
                cleanup.callback(self.read_pool.close)
            if self._catalog_observer is not None:
                observer, self._catalog_observer = self._catalog_observer, None
                self._catalog_observer_connection = None
                cleanup.callback(observer.close)

    def require_active_close_preview(self, company_id, database_id, period, preview_digest):
        """Read the exact active preview under the same gate used by close commits."""
        from .close_preview import ClosePreviewIntent

        with self.security.authorization_gate:
            preview = self.close_previews.get((company_id, database_id, preview_digest))
            if (
                self.active_close_previews.get((company_id, database_id, period)) != preview_digest
                or preview is None
            ):
                raise KernelError("preview_expired", "该关账预览已失效，请重新准备并核对")
            if not isinstance(preview, ClosePreviewIntent):
                raise KernelError("content_integrity_failed", "活动关账预览内容不完整")
            review = preview.require(
                self._close_preview_key,
                company_id=company_id,
                database_id=database_id,
                period=period,
                preview_digest=preview_digest,
            )
            return preview, review

    def _remember_close_preview(self, engine, result, owner_confirmation):
        from .close_preview import make_close_preview_intent
        from .read_state import repair_revision

        company_id, database_id = engine.store.company_id, engine.store.database_id
        period, preview_digest = result["manifest"]["period"], result["digest"]
        with self.security.authorization_gate:
            # A concurrent commit may have completed after the read snapshot.
            # Never reactivate that old preview after a successful close.
            with engine.store.connection(read_only=True) as connection:
                connection.execute("BEGIN")
                current = {
                    **engine.store.epochs(connection),
                    "read_repair_revision": repair_revision(connection),
                }
                if current != result["manifest"]["read_version"]:
                    raise KernelError("preview_expired", "关账预览已变化，请重新准备并核对")
            intent = make_close_preview_intent(
                self._close_preview_key,
                manifest=result["manifest"],
                preview_digest=preview_digest,
                epochs=result["epochs"],
                owner_confirmation=owner_confirmation,
            )
            # Check the store binding too; the signed preview must originate
            # from this company, not merely contain internally matching fields.
            intent.require(
                self._close_preview_key,
                company_id=company_id,
                database_id=database_id,
                period=period,
                preview_digest=preview_digest,
            )
            key = (company_id, database_id, preview_digest)
            active_key = (company_id, database_id, period)
            previous = self.active_close_previews.get(active_key)
            if previous is not None and previous != preview_digest:
                self.close_previews.pop((company_id, database_id, previous), None)
            self.close_previews[key] = intent
            self.active_close_previews[active_key] = preview_digest
            while len(self.close_previews) > 128:
                removed_key = next(iter(self.close_previews))
                removed = self.close_previews.pop(removed_key)
                active_key = (*removed_key[:2], removed.period)
                if self.active_close_previews.get(active_key) == removed_key[2]:
                    self.active_close_previews.pop(active_key)

    def dashboard_context(self, company_id: str | None = None):
        from .dashboard import Dashboard

        with self.catalog.connection(read_only=True) as connection:
            companies = [
                dict(row) for row in connection.execute("SELECT * FROM company ORDER BY name,id")
            ]
            if not companies:
                if company_id:
                    raise KernelError("unknown_company", "公司尚未登记")
                return {
                    "schema_version": 3,
                    "company": None,
                    "companies": [],
                    "current_company": None,
                    "periods": [],
                    "quarters": [],
                    "default_period": None,
                    "default_quarter": None,
                }
            company = next((item for item in companies if item["id"] == company_id), None)
            if company_id and company is None:
                raise KernelError("unknown_company", "公司尚未登记")
            company = company or companies[0]
            store = self.catalog._bound_store(company, read_pool=self.read_pool)
        return Dashboard(
            Engine(store),
            company_name=company["name"],
            companies=companies,
        ).context()

    def dispatch(
        self, command: str, payload: dict, *, session_token=None, response_format="native"
    ):
        """Closed set of business commands, with company binding on every request."""
        from .command_schema import validate_command
        from .response_contracts import RESPONSE_ADAPTERS, validate_response
        from .work_context_contract import WORK_CONTEXT_ADAPTER

        if response_format not in {"native", "http", "http_json"} or (
            response_format in {"http", "http_json"} and command not in _HTTP_DASHBOARD_COMMANDS
        ):
            raise KernelError("invalid_command", "响应格式不适用于此命令")

        payload = validate_command(self.command_models, command, payload, registry=self.registry)
        if command == "schema":
            return self._dispatch(command, payload)
        authority = self.security.authorize(session_token, request_id=payload.get("request_id"))
        if command in {"create_company", "restore_company", "configure_backup"}:
            with self.security.authorization_gate:
                self.security.validate_authority(authority)
                return self._dispatch(command, payload, authority=authority)
        result = self._dispatch(command, payload, authority=authority)
        if command == "work_context":
            try:
                result = WORK_CONTEXT_ADAPTER.validate_python(result)
            except ValidationError:
                raise KernelError(
                    "response_contract_mismatch", "读取结果不符合接口合同"
                ) from None
        else:
            result = validate_response(command, result)
        if response_format == "http_json":
            return RESPONSE_ADAPTERS[command].dump_json(result)
        if response_format == "http":
            return RESPONSE_ADAPTERS[command].dump_python(result, mode="json")
        return result

    @contextmanager
    def _commit_authority(self, authority):
        # Read snapshots and pure computation remain parallel. Revocation and
        # the final publication are ordered under the short shared identity gate.
        with self.security.authorization_gate:
            self.security.validate_authority(authority)
            yield

    def _dispatch(self, command, payload, *, authority=None):
        if command == "schema":
            from .close_contract import ADOPTION_ROLES, CLOSE_FORMAT, CLOSE_FORMAT_VERSION
            from .response_contracts import RESPONSE_ADAPTERS
            from .schema_discovery import discover_schema
            from .security.native import NativeRequest
            from .work_context_contract import WORK_CONTEXT_ADAPTER

            metadata = {
                "build_id": PROGRAM_VERSION,
                "error_handling": {
                    "version": 1,
                    "needs_information": {
                        "status": "needs_information",
                        "issues_field": "fact_issues",
                        "issue_semantics": "保留核算／管理语义、允许精度和可复用来源；先查资料再问",
                        "resolution": (
                            "只在已知下一步时返回 command、fact_kind 或 candidates 单一目标"
                        ),
                    },
                    "rejected": {
                        "status": "rejected",
                        "code_field": "code",
                        "rule": (
                            "预览失效、来源待发布、错误入口、能力限制和技术故障分别处理，"
                            "不直接追问老板"
                        ),
                    },
                    "lost_response": (
                        "先以原请求和原请求键重放，或读取 request_result；unknown 不表示失败"
                    ),
                    "changed_content": "重新读取和预览；载荷变化后使用新请求键",
                    "company_operations": "目录级创建、恢复查询 operations",
                    "jobs": "读取 error_code 和 error_message；只有核验成功的产物才算交付",
                },
                "publication_contract": {
                    "immutable": True,
                    "chain": "one_unforked_chain_per_subject",
                    "preview_item_fields": ["source_period", "posting_period", "mode"],
                    "withdraw_preview_fields": [
                        "source_period",
                        "posting_period",
                        "mode",
                    ],
                    "fields": [
                        "id",
                        "sequence",
                        "subject_id",
                        "previous_publication_id",
                        "calculation_id",
                        "mode",
                        "posting_period",
                        "baseline_calculation_id",
                        "voucher_id",
                    ],
                    "modes": [
                        "initial",
                        "open_replace",
                        "closed_correction",
                        "review_no_impact",
                        "withdrawn",
                    ],
                    "posting_period_parameter": (
                        "开放所属月按所属月入账；所属月已关账的迟到业务和冻结结果更正"
                        "必须明确指定开放入账月；自动重算的开放依赖保留原入账月，"
                        "显式业务与指定月冲突时拒绝；无影响复核不能移动入账月。"
                    ),
                },
                "period_close_contract": {
                    "format": CLOSE_FORMAT,
                    "format_version": CLOSE_FORMAT_VERSION,
                    "immutable": True,
                    "required_fields": [
                        "format",
                        "format_version",
                        "period",
                        "company_id",
                        "database_id",
                        "previous_close_period",
                        "previous_close_digest",
                        "publication_sequence",
                        "adopted_results",
                        "vouchers",
                        "opening_calculation_id",
                        "asset_batch_adoptions",
                        "asset_card_adoptions",
                        "inventories",
                        "owner_confirmation",
                        "readiness",
                        "management_snapshot",
                        "material_coverage",
                        "trial_balance",
                        "report_classification",
                        "read_version",
                        "approval",
                        "owner_review",
                    ],
                    "optional_fields": [],
                    "adopted_result_fields": [
                        "publication_id",
                        "calculation_id",
                        "result_digest",
                        "subject_id",
                        "fact_id",
                        "source_period",
                        "posting_period",
                        "role",
                    ],
                    "adoption_roles": sorted(ADOPTION_ROLES),
                    "voucher_fields": [
                        "id",
                        "voucher_id",
                        "calculation_id",
                        "total",
                        "number",
                        "adopted_calculation_id",
                        "result_digest",
                        "reverses_id",
                    ],
                    "read_version_fields": [
                        "accounting",
                        "material",
                        "management",
                        "read_repair_revision",
                    ],
                    "authority": "direct_adoptions_and_exact_voucher_versions",
                    "dependency_semantics": "transitive_basis_follows_immutable_dependencies",
                },
                "database_formats": {
                    kind: self.bundle.database_format(kind) for kind in ("catalog", "company")
                },
                "security_operations": ["request", "status", "cancel", "session_status"],
                "replay_close_contract": {
                    "format": "ai-accounting-kernel/2/replay-close-scope/1",
                    "enabled": self.replay_close_scope is not None,
                    "scope_digest": (
                        self.replay_close_scope.scope_digest if self.replay_close_scope else None
                    ),
                    "authorization_method": "replay_scope",
                    "startup": "daemon --replay-scope <private-scope.json>",
                    "maximum_months": 120,
                    "normal_close_password_required": True,
                },
                "agent_operating_protocol": OPERATING_PROTOCOL,
                "format": 1,
                "fact_semantics": "核算字段决定计算；管理说明单独版本化；实际收付日不得由月份代替",
                "commands": sorted(self.command_models),
            }
            if payload["view"] == "full":
                metadata["security_request_schema"] = NativeRequest.model_json_schema()
                metadata["replay_close_contract"]["scope_schema"] = (
                    ReplayScopeConfig.model_json_schema()
                )
            return discover_schema(
                metadata, self.registry, self.command_models, payload,
                response_adapters={**RESPONSE_ADAPTERS, "work_context": WORK_CONTEXT_ADAPTER},
            )
        if command == "companies":
            return self.catalog.companies()
        if command == "dashboard_context":
            return self.dashboard_context(**payload)
        if command == "operations":
            return self.catalog.operations()
        if command == "create_company":
            return self.catalog.create_company(**payload)
        if command == "restore_company":
            return self.catalog.restore_company(**payload)
        if command in {"company_settings", "configure_backup"}:
            self.catalog.bind(payload["company_id"])
            return getattr(self.catalog, command)(**payload)
        data = dict(payload)
        company_id = data.pop("company_id")
        engine = self.engine(company_id, dashboard_read=command in _HTTP_DASHBOARD_COMMANDS)
        engine.commit_guard = lambda: self._commit_authority(authority)
        engine.audit_actor = {
            "catalog_id": authority.catalog_instance_id,
            "owner_id": authority.owner_id,
            "session_id": authority.session_id,
            "credential_version": authority.credential_version,
        }
        if command in {"preview_replay_close_range", "confirm_replay_close_range"}:
            replay = ReplayClose(self, engine, authority)
            if command == "preview_replay_close_range":
                return replay.preview(**data)
            with self.replay_close_lock:
                return replay.confirm(**data)
        if command == "close" and data.get("backup_directory") is None:
            setting = self.catalog.company_settings(company_id)
            data["backup_directory"] = setting["backup_directory"] or str(
                self.catalog.root / "backups" / engine.store.path.parent.name
            )
        approval_id = data.pop("approval_id", None) if command == "close" else None

        def authorize_close(connection, period, preview_digest, epochs):
            self.security.validate_authority(authority)
            self.require_active_close_preview(
                company_id, engine.store.database_id, period, preview_digest
            )
            return consume_close_approval(
                connection,
                approval_id,
                authority=authority,
                company_id=engine.store.company_id,
                database_id=engine.store.database_id,
                period=period,
                preview_digest=preview_digest,
                epochs=epochs,
                now=self.security.now(),
            )

        periods = Periods(engine, authorize_close=authorize_close)
        exports = Exports(engine)
        reports = Reports(engine)
        workflow = Workflow(engine)
        materials = Materials(engine, inspection_cache=self.inspection_cache)
        discovery = Discovery(engine)
        duplicates = Duplicates(engine)
        entities = Entities(engine)
        identities = IdentityCorrections(engine)
        display = Display(engine)
        from .dashboard import Dashboard

        dashboard = Dashboard(
            engine, owner_review_request=CloseReview(self, engine).owner_review_request
        )
        business_queries = BusinessQueries(engine)
        payroll_preparation = PayrollPreparation(engine)
        asset_batches = AssetBatches(engine)
        tax_import = TaxImport(engine)
        from .maintenance import Maintenance
        from .work_context import WorkContext

        maintenance = Maintenance(engine)
        actions = {
            "prepare_fact_registration": duplicates.prepare_fact_registration,
            "find_entities": entities.find_entities,
            "register_entity": entities.register_entity,
            "update_entity_profile": entities.update_entity_profile,
            "preview_identity_correction": identities.preview_identity_correction,
            "confirm_identity_correction": identities.confirm_identity_correction,
            "save_fact": engine.save_fact,
            "amend_fact": engine.amend_fact,
            "save_facts": engine.save_facts,
            "preview": engine.preview,
            "confirm": engine.confirm,
            "overview": engine.overview,
            "jobs": engine.jobs,
            "request_result": engine.request_result,
            "retry_job": engine.retry_job,
            "ledger": engine.ledger,
            "trace": engine.trace,
            "business_status": business_queries.business_status,
            "period_readiness": business_queries.period_readiness,
            "management": periods.management,
            "inventory": periods.inventory,
            "preview_close": periods.preview_close,
            "close": periods.close,
            "closed_report": periods.closed_report,
            "rebuild": engine.rebuild_projections,
            "verify_integrity": maintenance.verify_integrity,
            "repair_read_indexes": maintenance.repair_read_indexes,
            "report": reports.report,
            "preview_report_export": reports.preview_export,
            "confirm_report_export": reports.confirm_export,
            "confirm_browser_report_export": reports.confirm_browser_export,
            "workflow": workflow.query,
            "obligation_basis": workflow.obligation_basis,
            "prepare_obligations": workflow.prepare_obligations,
            "confirm_obligations": workflow.confirm_obligations,
            "inspect_material": materials.inspect,
            "receive_material": materials.receive,
            "resolve_material": materials.resolve,
            "resolve_material_group": materials.resolve_group,
            "material_completeness": materials.check,
            "preview_material_allocation": materials.preview_period_allocation,
            "confirm_material_allocation": materials.confirm_period_allocation,
            "company_context": discovery.company_context,
            "update_company_note": discovery.update_company_note,
            "save_display_profile": display.save_display_profile,
            "display_profiles": display.display_profiles,
            "preview_period_commentary": display.preview_period_commentary,
            "update_period_commentary": display.update_period_commentary,
            "dashboard_brief": dashboard.brief,
            "dashboard_funds": dashboard.funds,
            "dashboard_employees": dashboard.employees,
            "dashboard_assets": dashboard.assets,
            "dashboard_business_status": dashboard.business_status,
            "dashboard_quarterly_report": dashboard.quarterly_report,
            "dashboard_period_preparation": dashboard.period_preparation,
            "dashboard_close_review": CloseReview(self, engine).read,
            "find_facts": discovery.find_facts,
            "work_context": WorkContext(engine).query,
            "payroll_reuse_basis": payroll_preparation.reuse_basis,
            "prepare_payroll": payroll_preparation.prepare,
            "confirm_payroll_preparation": payroll_preparation.confirm,
            "prepare_asset_activation_batch": asset_batches.prepare_activation_batch,
            "confirm_asset_activation_batch": asset_batches.confirm_activation_batch,
            "prepare_asset_consumption_month": asset_batches.prepare_consumption_month,
            "confirm_asset_consumption_month": asset_batches.confirm_consumption_month,
            "preview_tax_import": tax_import.preview,
            "confirm_tax_import": tax_import.confirm,
        }
        actions.update({"preview_delete": engine.preview_delete, "delete": engine.delete})
        actions.update(
            {
                "save_payee": exports.save_payee,
                "preview_export": exports.preview,
                "confirm_export": exports.confirm,
            }
        )
        if command == "evidence":
            content = base64.b64decode(data.pop("content_base64"), validate=True)
            return engine.register_evidence(content, **data)
        if command == "backup":
            return engine.queue_backup(**data)
        if command == "run_jobs":
            return {
                "backups": run_backup_jobs(engine.store.path, _bundle=engine.store.bundle, **data),
                "exports": run_export_jobs(engine, **data),
                "reports": run_report_jobs(engine, **data),
            }
        if command == "run_export_jobs":
            return run_export_jobs(engine, **data)
        if command == "run_report_jobs":
            return run_report_jobs(engine, **data)
        if command not in actions:
            raise KernelError("unknown_command", "不支持该业务命令")
        if command == "close":
            with self.security.authorization_gate:
                result = actions[command](**data)
                active_key = (company_id, engine.store.database_id, data["period"])
                if self.active_close_previews.get(active_key) == data["preview_digest"]:
                    self.active_close_previews.pop(active_key)
                self.close_previews.pop(
                    (company_id, engine.store.database_id, data["preview_digest"]), None
                )
                return result
        result = actions[command](**data)
        if command == "preview_close":
            self._remember_close_preview(engine, result, data["owner_confirmation"])
            result = {
                **result,
                "review_locator": {
                    "company_id": company_id,
                    "database_id": engine.store.database_id,
                    "period": data["period"],
                    "preview_digest": result["digest"],
                },
            }
        return result
