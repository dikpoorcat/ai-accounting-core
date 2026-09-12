"""Each selected version exposes its own correction group and its direct successor."""

from test_engine import close, evidence, publish, save
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel.periods import MATERIAL_CATEGORIES, Periods


def test_two_closed_period_corrections_are_navigable_without_changing_selected_amounts(engine):
    save(engine, amount=100)
    publish(engine)
    close(engine)
    save(engine, amount=125, revision=1, request="amend-first")
    publish(engine, request="publish-first", correction_period="2026-02")

    periods = Periods(engine)
    proof = evidence(engine)
    for category in MATERIAL_CATEGORIES:
        periods.inventory(
            "2026-02",
            category,
            evidence=[],
            expected=0,
            no_business=True,
            confirmation_evidence=proof,
            request_id="february-inventory-" + category,
        )
    preview = periods.preview_close("2026-02", owner_confirmation=proof)
    periods.close(
        "2026-02",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="february-close",
    )
    save(engine, amount=150, revision=2, request="amend-second")
    publish(engine, request="publish-second", correction_period="2026-03")

    versions = {
        row["number"]: row
        for period in ("2026-01", "2026-02", "2026-03")
        for row in engine.ledger(period)
    }
    before = {period: engine.ledger(period) for period in ("2026-01", "2026-02", "2026-03")}
    expected = {
        1: {(2, "reversal", 1, "current"), (3, "replacement", 1, "current")},
        2: {(1, "original", 1, "current"), (3, "replacement", 1, "current")},
        3: {
            (1, "original", 1, "current"),
            (2, "reversal", 1, "current"),
            (4, "reversal", 3, "next"),
            (5, "replacement", 3, "next"),
        },
        4: {(3, "original", 3, "current"), (5, "replacement", 3, "current")},
        5: {(3, "original", 3, "current"), (4, "reversal", 3, "current")},
    }
    amounts = {1: 100, 2: 100, 3: 125, 4: 125, 5: 150}
    edges = {}
    for number, version in versions.items():
        result = engine.trace(voucher_version_id=version["id"])
        assert result["voucher"]["id"] == version["id"]
        assert result["voucher"]["total"] == amounts[number]
        assert result["calculation"]["outcome"]["values"]["amount"] == amounts[number]
        for direction in ("debit", "credit"):
            assert sum(line[direction] for line in result["voucher"]["lines"]) == amounts[number]
        assert {
            (row["number"], row["role"], row["correction_of_number"], row["correction_group"])
            for row in result["related_vouchers"]
        } == expected[number]
        edges[number] = {row["number"] for row in result["related_vouchers"]}
        assert number not in edges[number]
        assert len(edges[number]) == len(result["related_vouchers"])
        for row in result["related_vouchers"]:
            anchor = row["correction_of_number"]
            assert row["correction_of_voucher_id"] == versions[anchor]["id"]
            role_name = {"original": "原凭证", "reversal": "冲正凭证", "replacement": "替换凭证"}[
                row["role"]
            ]
            assert row["label"].startswith(f"{role_name} {row['number']} · {row['period']}")
            assert f"原凭证 {anchor}" in row["label"]
            assert ("后续更正" if row["correction_group"] == "next" else "本次更正") in row["label"]
            assert row["correction_of_voucher_id"] not in row["label"]
    reached, pending = set(), [1]
    while pending:
        number = pending.pop()
        if number not in reached:
            reached.add(number)
            pending.extend(edges[number] - reached)
    assert reached == set(versions) == {1, 2, 3, 4, 5}
    assert {period: engine.ledger(period) for period in before} == before
