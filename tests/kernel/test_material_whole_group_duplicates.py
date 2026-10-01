"""Several originals jointly duplicate one complete pool, never partial allocations."""

import json

import pytest
from test_material_group_duplicates import setup_pool
from test_materials import Company, codes, csv_spec

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import YearMonth, canonical, digest


def resolve_copy(company, source, location, links=(), *, include_original=True, **changes):
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(
            connection, source["subject_id"]
        ).fact.evidence_digest
    data = dict(
        period="2026-01", source_id=source["subject_id"], source_fact_id=source["fact_id"],
        location=location, recognition_period="2026-01", links=list(links),
    ) | changes
    subject = data.pop("subject", "copy-resolution-" + location)
    return company.materials.resolve(
        subject, data, evidence=(evidence, company.proof) if include_original else (company.proof,),
        expected_revision=0, request_id=company.request(),
    )


def copies(company, *, resolutions=True, changes=None, direction="outflow"):
    spec = csv_spec()
    spec["columns"][1]["funds_direction"] = direction
    source, proof = company.source(
        b"name,amount,period\na,10,2026-01\nb,1,2026-01\n"
        b"c,20,2026-01\nd,30,2026-01\n", subject="joint-copy", spec=spec,
    )
    amounts = (1000, 100, 2000, 3000)
    if resolutions:
        for row, amount in enumerate(amounts, 2):
            extra = (changes or {}).get(row, {})
            resolve_copy(company,
                source, f"CSV!B{row}", treatment="duplicate", amount_fen=amount,
                duplicate_source_id="original", duplicate_location="CSV!B2",
                reason="All four original rows jointly restate the whole confirmed primary pool.",
                **extra,
            )
    return source, proof, amounts


def group(company, source, proof, amounts, links, *, group_subject="whole-copy-group", **changes):
    data = dict(
        period="2026-01", source_id=source["subject_id"], source_fact_id=source["fact_id"],
        members=[dict(location=f"CSV!B{row}", amount_fen=amount)
                 for row, amount in enumerate(amounts, 2)],
        group_amount_fen=sum(amounts), links=links, joint_basis_confirmed=True,
        basis_evidence_digest=company.proof, basis_location="L1",
        reason="Explicit synthetic joint original basis for the complete primary allocation set.",
    ) | changes
    return company.materials.resolve_group(
        group_subject, data, evidence=(proof, company.proof),
        expected_revision=0, request_id=company.request(),
    )


def test_complete_four_row_copy_reuses_entire_pool_and_retains_all_proof_refs(tmp_path):
    company = Company(tmp_path)
    primary, links = setup_pool(company)
    source, proof, amounts = copies(company)
    # Fragments remain ordinary mismatches until the complete joint group exists.
    assert "material_duplicate_amount" in codes(company.materials.check("2026-01"))
    saved = group(company, source, proof, amounts, list(reversed(links)))
    result = company.materials.check("2026-01")
    assert result["status"] == "complete", result["issues"]
    assert saved["fact_id"] in result["group_versions"]
    assert len(result["group_versions"]) == 2
    assert len(result["resolution_versions"]) == 4
    assert len(result["source_versions"]) == len(result["allocation_versions"]) == 2
    assert primary["source_fact_id"] in result["source_versions"]


@pytest.mark.parametrize("bad", [
    "missing_member_copy", "mixed_treatment", "two_targets", "selector_links",
    "wrong_amount", "wrong_period", "wrong_source_revision", "missing_reason",
    "partial_links", "repeated_links", "changed_link_field", "unconfirmed_basis",
    "unknown_member", "outdated_group_source",
    "missing_source_evidence",
    "self_target",
])
def test_only_exact_complete_joint_duplicate_structure_is_allowed(tmp_path, bad):
    company = Company(tmp_path)
    _, links = setup_pool(company)
    source, proof, amounts = copies(company, resolutions=False)
    for row, amount in enumerate(amounts, 2):
        if bad == "missing_member_copy" and row == 5:
            continue
        data = dict(treatment="duplicate", amount_fen=amount,
                    duplicate_source_id="original", duplicate_location="CSV!B2",
                    reason="Confirmed complete joint original copy.")
        if bad == "self_target":
            data["duplicate_source_id"] = source["subject_id"]
        selected_links = []
        if row == 2:
            if bad == "mixed_treatment":
                data["treatment"] = "no_accounting"
                data["non_accounting_reason"] = "not_company_business"
            elif bad == "two_targets":
                data["duplicate_location"] = "CSV!B3"
            elif bad == "selector_links":
                selected_links = links
            elif bad == "wrong_amount":
                data["amount_fen"] += 1
            elif bad == "wrong_period":
                data["recognition_period"] = "2026-02"
            elif bad == "wrong_source_revision":
                data["source_fact_id"] = "missing-revision"
            elif bad == "missing_reason":
                data["reason"] = ""
        resolve_copy(company, source, f"CSV!B{row}", selected_links,
                     include_original=bad != "missing_source_evidence" or row != 2, **data)
    change = {}
    if bad == "partial_links":
        # An exact existing part still cannot duplicate an entire primary pool.
        selected = [links[1]]
        amounts = (1000, 1000, 1000, 2000)
    elif bad == "repeated_links":
        selected = [links[0], links[0]]
        amounts = (100, 100, 1000, 1000)
    else:
        selected = [dict(link) for link in links]
    if bad == "changed_link_field":
        selected[0]["amount_field"] = "result.amount_fen"
    elif bad == "unconfirmed_basis":
        change["joint_basis_confirmed"] = False
    elif bad == "unknown_member":
        change["members"] = [dict(location=f"CSV!B{row+20}", amount_fen=amount)
                             for row, amount in enumerate(amounts, 2)]
    elif bad == "outdated_group_source":
        change["source_fact_id"] = "missing-source"
    with pytest.raises(KernelError):
        group(company, source, proof, amounts, selected, **change)
    assert company.materials.check("2026-01")["status"] != "complete"


def test_current_primary_whole_group_all_parts_must_remain_valid(tmp_path):
    company = Company(tmp_path)
    _, links = setup_pool(company)
    source, proof, amounts = copies(company)
    group(company, source, proof, amounts, links)
    company.expense("first", 1200, revision=1)
    result = company.materials.check("2026-01")
    assert "material_result_stale" in codes(result)
    assert result["status"] == "needs_information"


def test_target_source_revision_change_invalidates_complete_duplicate(tmp_path):
    company = Company(tmp_path)
    _, links = setup_pool(company)
    source, proof, amounts = copies(company)
    group(company, source, proof, amounts, links)
    with company.engine.store.connection(read_only=True) as connection:
        old = company.engine.store.current_fact(connection, "original")
    data = old.fact.model_dump(mode="json")
    data["specification"]["columns"][0]["label"] = "reviewed original context"
    company.materials.receive(
        "original", data, evidence=old.evidence, expected_revision=1,
        request_id=company.request(),
    )
    assert "material_source_changed" in codes(company.materials.check("2026-01"))


def test_original_copy_direction_is_checked_independently_of_primary(tmp_path):
    company = Company(tmp_path)
    _, links = setup_pool(company, reserve=True)
    source, proof, amounts = copies(company, direction="inflow")
    with pytest.raises(KernelError) as error:
        group(company, source, proof, amounts, links)
    assert error.value.code == "material_funds_direction_mismatch"


def test_copy_cannot_be_primary_target_of_another_whole_copy(tmp_path):
    company = Company(tmp_path)
    _, links = setup_pool(company)
    source, proof, amounts = copies(company)
    group(company, source, proof, amounts, links)
    # The existing four-member copy cannot be a sole-primary endpoint.
    assert len(amounts) == 4
    next_source, ev = company.source(
        b"name,amount,period\na,61,2026-01\n", subject="next-copy"
    )
    resolve_copy(company, next_source, "CSV!B2", treatment="duplicate", amount_fen=6100,
                    subject="next-copy-resolution",
                    duplicate_source_id="joint-copy", duplicate_location="CSV!B2",
                    reason="Synthetic attempted copy chain.")
    with pytest.raises(KernelError):
        group(company, next_source, ev, (6100,), links, group_subject="next-copy-group")


def test_old_v2_close_and_new_whole_copy_freeze_watch_followup(tmp_path, monkeypatch):
    from stage9_book import MixedBook
    from test_material_watch_future_links import _classify_expense, _expense, _frozen, _link

    from ai_accounting.kernel import frozen_material, material_watch, materials
    from ai_accounting.kernel.backup import create_portable, verify_portable
    from ai_accounting.kernel.close_storage import decode_close
    from ai_accounting.kernel.integrity import verify_integrity

    book = MixedBook(tmp_path / "whole-copy", employees=1, businesses=26)
    book.add_month(0, close=False)
    specification = csv_spec()
    primary_proof = book.evidence(b"name,amount,period\npool,30,2016-01\n", "pool.csv")
    primary = book.materials.receive(
        "primary", dict(period="2016-01", evidence_digest=primary_proof,
                        category="transactions", purpose="business", specification=specification),
        evidence=(primary_proof, book.proof), expected_revision=0,
        request_id=book.request("primary"),
    )
    for name, amount in (("pool-first", 1000), ("pool-second", 2000)):
        _expense(book, name, "2016-01", amount)
        _classify_expense(book, name, "2016-01")
    links = [_link(book, name, "2016-01", amount)
             for name, amount in (("pool-first", 1000), ("pool-second", 2000))]
    primary_data = dict(
        period="2016-01", source_id="primary", source_fact_id=primary["fact_id"],
        members=[dict(location="CSV!B2", amount_fen=3000)], group_amount_fen=3000,
        links=links, joint_basis_confirmed=True, basis_evidence_digest=book.proof,
        basis_location="L1", reason="Explicit entire primary pool.",
    )
    primary_group = book.materials.resolve_group(
        "primary-group", primary_data, evidence=(primary_proof, book.proof),
        expected_revision=0, request_id=book.request("primary-group"),
    )

    def inventory(period, extra):
        with book.engine.store.connection(read_only=True) as connection:
            ident = connection.execute(
                "SELECT id FROM material_revision WHERE period=? AND category='transactions' "
                "ORDER BY id DESC LIMIT 1", (YearMonth(period).ordinal,),
            ).fetchone()[0]
            existing = [row[0].hex() for row in connection.execute(
                "SELECT evidence_digest FROM material_item WHERE inventory_id=?", (ident,),
            )]
        evidence = sorted(set(existing + extra))
        book.periods.inventory(
            period, "transactions", evidence=evidence, expected=len(evidence), no_business=False,
            confirmation_evidence=book.proof, request_id=book.request("inventory"),
        )

    inventory("2016-01", [primary_proof])
    old_rule = digest({"contract": "ai-accounting-kernel/2/material-coverage", "version": 2})
    with monkeypatch.context() as old:
        old.setattr(frozen_material, "MATERIAL_COVERAGE_RULE_DIGEST", old_rule)
        book.close_last_month()
    original_close = _frozen(book)
    book.add_month(1, close=False)
    copy_proof = book.evidence(
        b"name,amount,period\na,4,2016-01\nb,6,2016-01\n"
        b"c,8,2016-01\nd,12,2016-01\n", "copy.csv",
    )
    source = book.materials.receive(
        "copy", dict(period="2016-02", evidence_digest=copy_proof,
                     category="transactions", purpose="business", specification=specification),
        evidence=(copy_proof, book.proof), expected_revision=0, request_id=book.request("copy"),
    )
    copy_resolutions = []
    amounts = (400, 600, 800, 1200)
    for row, amount in enumerate(amounts, 2):
        copy_resolutions.append(book.materials.resolve(
            f"copy-row-{row}", dict(
                period="2016-02", source_id="copy", source_fact_id=source["fact_id"],
                location=f"CSV!B{row}", treatment="duplicate", recognition_period="2016-01",
                amount_fen=amount, links=[], duplicate_source_id="primary",
                duplicate_location="CSV!B2", reason="Jointly duplicate the entire primary pool.",
            ), evidence=(copy_proof, book.proof), expected_revision=0,
            request_id=book.request("copy-row"),
        )["fact_id"])
    duplicate_group = book.materials.resolve_group(
        "copy-group", dict(
            period="2016-02", source_id="copy", source_fact_id=source["fact_id"],
            members=[dict(location=f"CSV!B{row}", amount_fen=amount)
                     for row, amount in enumerate(amounts, 2)],
            group_amount_fen=3000, links=links, joint_basis_confirmed=True,
            basis_evidence_digest=book.proof, basis_location="L1",
            reason="All four original amounts collectively confirm the whole primary allocation.",
        ), evidence=(copy_proof, book.proof), expected_revision=0,
        request_id=book.request("copy-group"),
    )
    inventory("2016-02", [copy_proof])
    book.close_last_month()
    assert _frozen(book) == original_close
    february = YearMonth("2016-02").ordinal
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT * FROM period_close WHERE period=?", (february,)
        ).fetchone()
        manifest = decode_close(connection, row)
        proof = manifest["material_coverage"]
        assert {primary_group["fact_id"], duplicate_group["fact_id"]} <= set(
            proof["group_versions"]
        )
        assert set(copy_resolutions) <= set(proof["resolution_versions"])
        heads = material_watch.heads_at(
            connection, material_watch._stored_root(connection, february)["highwater"]
        )
        _, reverse = material_watch._directory(
            connection, february, manifest, historic_heads=heads
        )
        assert {"primary", "copy"} <= set(reverse["business:pool-first"])
        assert "primary" in reverse["business:copy"]
        assert "copy" in reverse["business:primary"]
        assert verify_integrity(book.engine, connection)["status"] == "verified"
    archive = create_portable(
        book.engine.store.path, tmp_path / "portable", _bundle=book.engine.store.bundle
    )
    assert verify_portable(
        archive["path"], _bundle=book.engine.store.bundle
    )["latest_closed_period"] == "2016-02"
    frozen = _frozen(book)
    changed = json.loads(json.dumps(specification))
    changed["columns"][1]["funds_direction"] = "outflow"
    book.materials.receive(
        "primary", dict(period="2016-01", evidence_digest=primary_proof,
                        category="transactions", purpose="business", specification=changed),
        evidence=(primary_proof, book.proof), expected_revision=1,
        request_id=book.request("primary-revision"),
    )
    with book.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        full = materials.check_completeness(
            connection, YearMonth("2016-03").ordinal, book.engine.store.registry
        )
        fast = materials.check_completeness(
            connection, YearMonth("2016-03").ordinal, book.engine.store.registry,
            _allow_frozen_reuse=True,
        )
        assert canonical(full) == canonical(fast)
        assert {"primary", "copy"} <= set(
            material_watch.changed_material_sources(connection, february)[1]
        )
    assert "material_source_changed" in codes(full)
    assert _frozen(book) == frozen
