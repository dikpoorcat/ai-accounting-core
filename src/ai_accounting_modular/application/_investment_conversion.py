"""Explicit offline investment-contract conversion into a new isolated target."""

from collections import defaultdict
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    converted_investment_fact,
    fact_source_ids,
    is_legacy_investment,
    loads_json,
    validate_contract,
)
from ai_accounting_modular.files.public import reset_for_restore as reset_files
from ai_accounting_modular.reading.public import reset_for_restore as reset_reading
from ai_accounting_modular.records.public import (
    open_investment_conversion_bundle,
    open_standard_bundle,
    write_investment_conversion_bundle,
)
from ai_accounting_modular.storage.public import (
    restore_database_copy,
    verify_database,
)

from . import _descriptions, _execution, _isolated, _relations
from ._backup_scope import DATABASE_PATH
from ._changes import fact_digest
from ._context import SubmissionContext
from ._submission import Submission
from ._target_paths import beneath, checked, copy_file


def _plan(bundle, basis_facts):
    """The caller supplies evidence-supported ranges, never an inferred start date."""
    if not isinstance(basis_facts, (list, tuple)):
        raise ContractError("invalid_input", "转换依据须为完整事实列表。")
    old = {fact["business_id"]: fact for fact in bundle.facts if is_legacy_investment(fact)}
    if not old:
        raise ContractError("invalid_operation", "选定资料中没有待拆分的旧投资条款。")
    supplied, covered = [], set()
    for value in basis_facts:
        fact = deepcopy(value)
        validate_contract("business-facts.schema.json", "Fact", fact)
        if fact["kind"] != "taxes.investment_basis":
            raise ContractError("invalid_input", "转换采用只能包含完整的投资税务依据。")
        data = fact["data"]
        before = old.get(data["terms_id"])
        if before is None or any(
            data.get(key) != before["data"].get(key)
            for key in ("tax_facts", "financial_instrument_reference")
        ):
            raise ContractError("invalid_input", "新依据必须完整保留对应旧条款的原税务事实及来源。")
        covered.add(data["terms_id"])
        supplied.append(fact)
    if covered != old.keys():
        raise ContractError(
            "invalid_input",
            "转换缺少有来源的适用区间和登记月；保留原输入，请为这些旧条款明确新税务依据："
            + "、".join(sorted(old.keys() - covered)),
        )
    existing = {fact["business_id"] for fact in bundle.facts}
    ids = [fact["business_id"] for fact in supplied]
    if len(ids) != len(set(ids)) or existing.intersection(ids):
        raise ContractError("invalid_input", "转换新增税务依据须使用不同且未占用的业务身份。")
    for issue in bundle.issues:
        if is_legacy_investment(issue.get("known_fact", {})):
            raise ContractError(
                "invalid_input",
                "待办 " + issue["issue_id"] + " 仍含未完成的旧投资条款；须先按真实缺口整理该待办，"
                "转换不会丢弃税务资料或猜造条款身份。",
            )
    converted = {identity: converted_investment_fact(fact) for identity, fact in old.items()}
    facts = tuple(converted.get(fact["business_id"], fact) for fact in bundle.facts)
    return old, converted, facts + tuple(supplied), tuple(supplied)


def _results(application, ids, read):
    rows = []
    for offset in range(0, len(ids), 500):
        rows.extend(application.ledger.get_results(ids[offset : offset + 500], read))
    return canonical_json(sorted(rows, key=lambda row: row["reference"]["result_id"]))


def _refresh_dependencies(application, unit):
    """Rebuild declarations only, without replacing any historical adoption."""
    repository = unit.repository("application")
    heads = repository.snapshot_heads()
    facts = application.registry.export_heads(heads, unit)
    versions = {row["business_id"]: row["seq"] for row in heads}
    grouped = defaultdict(list)
    for fact in facts:
        grouped[application.registry.owner(fact["kind"])].append(
            {
                "op": "recalculate",
                "business_id": fact["business_id"],
                "expected_seq": versions[fact["business_id"]],
                "fact": fact,
            }
        )
    context = SubmissionContext(
        unit,
        application.registry,
        application.identity,
        application.ledger,
        application.records,
        money=application.money,
        evaluation_period_to=repository.working_period_to() or unit.start_period,
    )
    context.set_recalculation_roots(set(), set())
    relations = _relations.declare(grouped, context)
    descriptions = _descriptions.describe(grouped, context)
    responsibilities = {
        item["business_id"]: item
        for _, description in descriptions
        for item in description["businesses"]
    }
    ids = sorted(responsibilities)
    repository.replace_relations(ids, relations, search_revision=unit.next_head)
    repository.replace_dependencies(
        ids, [edge for _, value in descriptions for edge in value["dependencies"]]
    )
    repository.replace_publications(
        ids, [item for _, value in descriptions for item in value["publications"]]
    )
    # A representation-only conversion cannot silently introduce new result slots.
    for head in heads:
        if head["state"] == "active" and set(
            responsibilities[head["business_id"]]["complete_result_slots"]
        ) != set(loads_json(head["result_slots_json"])):
            raise ContractError(
                "dependency_conflict", "转换后的结果责任范围发生变化，须先明确实际业务接续。"
            )


def _convert_database(application, stage, old, converted, supplied):
    with _isolated._unit(application, stage) as read:
        application.backup_verifier.verify_snapshot(read, investment_conversion=True)
        original_ids = sorted(
            row["business_id"] for row in read.repository("application").snapshot_heads()
        )
        original = _results(application, original_ids, read)
        closed = application.ledger.closed_through(read)
    if any(closed is not None and fact["period"] <= closed for fact in supplied):
        raise ContractError("invalid_input", "转换新增依据的登记月份须开放；适用范围另按来源声明。")
    with _isolated._unit(application, stage, "write") as unit:
        for identity, before in old.items():
            after = converted[identity]
            application.registry.owner(before["kind"]).convert_current_terms(before, after, unit)
            unit.repository("application").convert_fact_digest(
                identity, fact_digest(before), fact_digest(after)
            )
            application.records.replace_source_uses(
                "business", identity, fact_source_ids(after), unit
            )
            application.records.mark_document_targets([f"facts:investment:{after['period']}"], unit)
        _execution.execute(
            application,
            Submission(
                company_id=unit.company_id,
                command_id=str(uuid4()),
                mutations=tuple(
                    {"op": "create", "expected_seq": 0, "fact": fact} for fact in supplied
                ),
            ),
            unit,
        )
        _refresh_dependencies(application, unit)
    with _isolated._unit(application, stage, "maintenance") as unit:
        reset_files(unit)
        reset_reading(unit)
    _isolated._publish_documents(application, stage)
    with _isolated._unit(application, stage) as read:
        application.backup_verifier.verify_snapshot(read)
        if original != _results(
            application, original_ids, read
        ) or closed != application.ledger.closed_through(read):
            raise ContractError(
                "dependency_conflict", "转换改变了原正式结果、凭证或关账范围；目标未发布。"
            )


def run(application, source, target, basis_facts, *, source_kind):
    """No catalog access; publish only after full verification of the independent copy."""
    source = checked(Path(source).absolute())
    target = _isolated._target({"new_target_directory": str(Path(target).absolute())})
    _isolated._apart(source, target)
    result = {"state": "failed", "new_target_directory": str(target), "checks": []}
    with ExitStack() as stack:
        if source_kind == "backup":
            stream = stack.enter_context(source.open("rb"))
            extracted = stack.enter_context(application.backup_verifier.extract(stream))
            application.backup_verifier.verify_extracted(
                extracted.company_root, extracted.manifest, investment_conversion=True
            )
            root = extracted.company_root
        elif source_kind == "company":
            root = source
            verify_database(beneath(root, DATABASE_PATH))
        elif source_kind == "standard":
            if source.name != "current.json" or source.parent.name != "standard":
                raise ContractError("invalid_path", "请明确选择公司的 standard/current.json。")
            root = source.parent.parent
            _isolated._apart(root, target)
        else:
            raise ContractError("invalid_input", "明确选择 company、backup 或 standard 输入。")
        bundle = open_investment_conversion_bundle(root / "standard/current.json")
        old, converted, facts, supplied = _plan(bundle, basis_facts)
        with _isolated._stage(target, False) as stage:
            if source_kind == "standard":
                with _isolated._stage(target, True) as materials:
                    converted_bundle = write_investment_conversion_bundle(
                        bundle, materials, facts, bundle.issues
                    )
                    _isolated._rebuild(application, converted_bundle, stage)
            else:
                # Snapshot the database first; complete source verification below
                # rejects any original/doc change during copying.
                restore_database_copy(beneath(root, DATABASE_PATH), beneath(stage, DATABASE_PATH))
                with _isolated._unit(application, root) as read:
                    snapshot = application.backup_verifier.verify_snapshot(
                        read, investment_conversion=True
                    )
                for item in snapshot.files:
                    copy_file(
                        beneath(root, item.relative_path),
                        beneath(stage, item.relative_path),
                        item.sha256,
                        item.byte_size,
                    )
                _convert_database(application, stage, old, converted, supplied)
                # Current final documents must remain rebuildable without old
                # frozen tax qualifications concealing a missing adopted range.
                with _isolated._stage(target, True) as rebuilt:
                    final_bundle = open_standard_bundle(stage / "standard/current.json")
                    _isolated._rebuild(application, final_bundle, rebuilt)
                    with _isolated._unit(application, rebuilt) as read:
                        application.backup_verifier.verify_snapshot(read)
            _isolated._finish(application, stage, target, False, result)
    return result
