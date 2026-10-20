"""Evidenced actual assessments override policy estimates item by item."""

from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import format_fen, parse_fen

from ._common import checked, missing, plan_for, referenced, reject, total, use_result

KINDS = ("social_insurance", "housing_fund")
FIELDS = (
    "employee_social_fen",
    "employee_housing_fen",
    "employer_social_fen",
    "employer_housing_fen",
)
_ROUNDING = {"half_up": ROUND_HALF_UP, "down": ROUND_DOWN, "up": ROUND_UP}


@dataclass(frozen=True)
class ContributionLine:
    kind: str
    employee: int
    employer: int


@dataclass(frozen=True)
class ContributionAmounts:
    employee_social: int = 0
    employee_housing: int = 0
    employer_social: int = 0
    employer_housing: int = 0

    def values(self):
        return dict(
            zip(
                FIELDS,
                map(
                    format_fen,
                    (
                        self.employee_social,
                        self.employee_housing,
                        self.employer_social,
                        self.employer_housing,
                    ),
                ),
                strict=True,
            )
        )


def _key(item):
    return item["base_kind"], item["code"]


def _ordered(items):
    """Deduct social insurance before housing, then by stable code within each kind."""
    return sorted(items, key=lambda item: (KINDS.index(item["base_kind"]), item["code"]))


def _unique(items):
    keys = [_key(item) for item in items]
    if len(keys) != len(set(keys)):
        reject("同一缴费类别及项目代码不能重复。", code="invalid_input")


def check_policy(fact):
    data = fact["data"]
    policy = data["policy"]
    if policy.get("effective_to", "9999-12-31") < policy["effective_from"]:
        reject("缴费政策止日不能早于起日。", code="invalid_input")
    _unique(data["items"])
    for item in data["items"]:
        if parse_fen(item["minimum_base_fen"]) > parse_fen(item["maximum_base_fen"]):
            reject("缴费基数下限不能大于上限。", code="invalid_input")


def check_actual(fact):
    _unique(fact["data"]["items"])


def _totals(lines):
    return ContributionAmounts(
        *(
            total(getattr(item, side) for item in lines if item.kind == kind)
            for side, kind in (
                ("employee", KINDS[0]),
                ("employee", KINDS[1]),
                ("employer", KINDS[0]),
                ("employer", KINDS[1]),
            )
        )
    )


def employment(fact, context):
    data = fact["data"]
    if data.get("effective_to", "9999-12") < data["effective_from"]:
        reject("用工适用止月不能早于起月。", code="invalid_input")
    values = {
        key: data[key]
        for key in (
            "employee_id",
            "effective_from",
            "social_insurance_participating",
            "housing_fund_participating",
        )
    }
    return plan_for(fact, context, values)


def policy(fact, context):
    check_policy(fact)
    identity = fact["data"]["policy"]
    values = {
        key: identity[key]
        for key in ("effective_from", "effective_to", "official_url")
        if key in identity
    }
    values["policy_version"] = identity["version"]
    parameters = deepcopy(fact["data"])
    parameters["items"] = _ordered(parameters["items"])
    plan = plan_for(fact, context, values, accounting_input=parameters)
    plan["rule_refs"][0]["official_urls"] = [identity["official_url"]]
    return plan


def actual(fact, context):
    check_actual(fact)
    parameters = deepcopy(fact["data"])
    parameters["items"] = _ordered(parameters["items"])
    amounts = _totals(
        [
            ContributionLine(
                item["base_kind"], parse_fen(item["employee_fen"]), parse_fen(item["employer_fen"])
            )
            for item in parameters["items"]
        ]
    )
    return plan_for(
        fact,
        context,
        {"employee_id": fact["data"]["employee_id"], **amounts.values()},
        accounting_input=parameters,
    )


def _policy_covers_month(policy, period):
    year, month = map(int, period.split("-"))
    return (
        policy["effective_from"] <= period + "-01"
        and policy.get("effective_to", "9999-12-31") >= f"{period}-{monthrange(year, month)[1]:02}"
    )


def _estimate(rule, employment_result):
    employment_data = employment_result.basis["input"]
    field = rule["base_kind"] + "_base_fen"
    if field not in employment_data:
        source_fact = {
            "business_id": employment_result.source["business_id"],
            "evidence_ids": list(
                dict.fromkeys(
                    item["source_id"] for item in employment_result.basis["source_locations"]
                )
            ),
        }
        missing(
            source_fact,
            "/data/" + field,
            f"所用档案缺少{field}，不能把毛工资当缴费基数。",
            precision=("fen",),
        )
    base = min(
        max(parse_fen(employment_data[field]), parse_fen(rule["minimum_base_fen"])),
        parse_fen(rule["maximum_base_fen"]),
    )
    with localcontext() as decimal_context:
        decimal_context.prec = 80
        employee, employer = (
            checked(
                int(
                    (Decimal(base) * Decimal(rule[side + "_rate"])).quantize(
                        Decimal("1"), rounding=_ROUNDING[rule["rounding"]]
                    )
                )
            )
            for side in ("employee", "employer")
        )
    return ContributionLine(rule["base_kind"], employee, employer)


def _actual_items(fact, context, plan):
    data = fact["data"]
    if "contribution_actual_id" not in data:
        return {}
    result = referenced(
        fact,
        context,
        data["contribution_actual_id"],
        "people.contribution_actual",
        field="/data/contribution_actual_id",
    )
    actual_data = result.basis["input"]
    if actual_data["employee_id"] != data["person_id"] or result.basis["period"] != fact["period"]:
        reject("实缴资料必须属于同员工同工资月。")
    check_actual({"data": actual_data})
    use_result(plan, result)
    return {_key(item): item for item in _ordered(actual_data["items"])}


def _adopt_employment(plan, result, estimated_kinds):
    parameters = deepcopy(result.basis["input"])
    for kind in KINDS:
        if kind not in estimated_kinds:
            parameters.pop(kind + "_base_fen", None)
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "employment",
            "source": {"business_id": result.source["business_id"], "slot": "main"},
            "parameters": parameters,
        }
    )


def _adopt_actuals(plan, fact, items, participating):
    consumed = [
        deepcopy(item) for item in _ordered(items.values()) if item["base_kind"] in participating
    ]
    if consumed:
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "contribution_actual",
                "source": {"business_id": fact["data"]["contribution_actual_id"], "slot": "main"},
                "parameters": {"employee_id": fact["data"]["person_id"], "items": consumed},
            }
        )


def wage_contributions(fact, context, plan, gross, tax):
    data = fact["data"]
    employment_result = referenced(
        fact, context, data["employment_id"], "people.employment", field="/data/employment_id"
    )
    employment_data = employment_result.basis["input"]
    if employment_data["employee_id"] != data["person_id"]:
        reject("工资与用工档案必须属于同一人员。")
    if (
        not employment_data["effective_from"]
        <= fact["period"]
        <= employment_data.get("effective_to", "9999-12")
    ):
        reject("用工档案不覆盖工资所属月。")
    use_result(plan, employment_result)
    participating = {kind for kind in KINDS if employment_data[kind + "_participating"]}
    actual_items = _actual_items(fact, context, plan)
    if any(
        item["base_kind"] not in participating
        and (parse_fen(item["employee_fen"]) or parse_fen(item["employer_fen"]))
        for item in actual_items.values()
    ):
        reject("未参与类别不能采用非零个人或单位实缴额。")
    if not participating:
        _adopt_employment(plan, employment_result, set())
        return ContributionAmounts()
    if "contribution_basis" not in data:
        missing(fact, "/data/contribution_basis", "参与缴费时必须明确采用实际额还是允许政策估算。")

    policy_data, policy_result = None, None
    if "contribution_policy_id" in data:
        policy_result = referenced(
            fact,
            context,
            data["contribution_policy_id"],
            "people.contribution_policy",
            field="/data/contribution_policy_id",
        )
        policy_data = deepcopy(policy_result.basis["input"])
        policy_data["items"] = _ordered(policy_data["items"])
        check_policy({"data": policy_data})

    rules = policy_data["items"] if policy_data is not None else []
    policy_keys = {_key(item) for item in rules}
    if policy_data is not None and set(actual_items) - policy_keys:
        reject("实缴项目与所引用政策的项目不对应。")
    for kind in participating:
        if not any(item["base_kind"] == kind for item in (*rules, *actual_items.values())):
            missing(
                fact,
                "/data/contribution_actual_id",
                "参与类别缺少完整实缴项目或有效政策，不能把缺项当零。",
            )

    lines, applied_rules, estimated_kinds = [], [], set()
    inputs = rules if policy_data is not None else list(actual_items.values())
    for item in inputs:
        kind = item["base_kind"]
        if kind not in participating:
            continue
        assessment = actual_items.get(_key(item))
        if assessment is not None:
            lines.append(
                ContributionLine(
                    kind,
                    parse_fen(assessment["employee_fen"]),
                    parse_fen(assessment["employer_fen"]),
                )
            )
        else:
            if item["enabled"] and data["contribution_basis"] == "actual_required":
                missing(
                    fact,
                    "/data/contribution_actual_id",
                    "要求实际缴费额的参与项目仍缺实际申报或明确未申报资料。",
                )
            if not _policy_covers_month(policy_data["policy"], fact["period"]):
                missing(
                    fact,
                    "/data/contribution_policy_id",
                    "估算政策须覆盖工资所属整月；月中变更不能只凭管理日期选择。",
                    precision=("month",),
                )
            applied_rules.append(deepcopy(item))
            if item["enabled"]:
                estimated_kinds.add(kind)
            lines.append(
                _estimate(item, employment_result)
                if item["enabled"]
                else ContributionLine(kind, 0, 0)
            )
    if applied_rules:
        policy_id = data["contribution_policy_id"]
        use_result(plan, policy_result)
        parameters = deepcopy(policy_data)
        parameters["items"] = applied_rules
        plan["basis"]["policies"].append(
            {
                "policy_business_id": policy_id,
                "policy_kind": "people.contribution_policy",
                "parameters": parameters,
            }
        )
        plan["rule_refs"][0]["policy_business_ids"].append(policy_id)
        plan["rule_refs"][0]["official_urls"].append(policy_data["policy"]["official_url"])
    _adopt_employment(plan, employment_result, estimated_kinds)
    _adopt_actuals(plan, fact, actual_items, participating)
    personal = total(item.employee for item in lines)
    available = gross - tax
    if personal > available and employment_data["contribution_shortfall"] == "reject":
        reject("个人缴费及已明确个税超过毛工资，需明确处理差额。")
    budget = min(personal, available)
    adjusted = []
    for item in lines:
        employee = min(item.employee, budget)
        budget -= employee
        adjusted.append(
            ContributionLine(item.kind, employee, checked(item.employer + item.employee - employee))
        )
    return _totals(adjusted)
