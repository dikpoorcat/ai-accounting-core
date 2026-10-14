"""Explicit line roles survive equal amounts, split payments and reversals."""

import sqlite3

import pytest
from opening_fixture import adopt_empty_opening
from test_opening_continuation import book as _opening_book

from ai_accounting.kernel import fund_line_details as details
from ai_accounting.kernel.contracts import KernelError

opening_book = _opening_book


@pytest.fixture
def connection(monkeypatch):
    from ai_accounting.kernel import fund_accounts

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE voucher_version(id TEXT PRIMARY KEY,calculation_id TEXT,
          period INTEGER,reverses_id TEXT);
        CREATE TABLE voucher_line(version_id TEXT,line_no INTEGER,account TEXT,
          debit INTEGER,credit INTEGER,cashflow TEXT,
          PRIMARY KEY(version_id,line_no));
        CREATE TABLE calculation(id TEXT PRIMARY KEY,period INTEGER);
    """
        + details.FUND_LINE_DETAILS_DDL
    )
    mapping = {
        "source": {"entity_id": "source", "parent_account": "1002", "detail_code": "1002.0001"},
        "destination": {
            "entity_id": "destination",
            "parent_account": "1002",
            "detail_code": "1002.0002",
        },
    }
    monkeypatch.setattr(
        fund_accounts, "require_fund_account", lambda conn, ident, account_type=None: mapping[ident]
    )
    yield connection
    connection.close()


def transfer_source(monkeypatch):
    lines = [
        {"account": "1002", "debit": 700, "credit": 0, "cashflow": None},
        {"account": "1002", "debit": 0, "credit": 700, "cashflow": None},
    ]
    fact = {"source_bank_account_id": "source", "destination_bank_account_id": "destination"}
    outcome = {
        "lines": lines,
        "balances": [
            {"category": "bank", "key": "source", "amount": -700},
            {"category": "bank", "key": "destination", "amount": 700},
        ],
    }
    monkeypatch.setattr(
        details,
        "_source",
        lambda conn, ident: ({"kind": "funds_transfer", "fact_id": "f-transfer"}, outcome, fact),
    )
    return lines


def test_equal_amount_transfer_and_closed_basis_reversal(connection, monkeypatch):
    lines = transfer_source(monkeypatch)
    connection.execute("INSERT INTO voucher_version VALUES('original','calc',100,NULL)")
    connection.execute("INSERT INTO voucher_version VALUES('reversal','newcalc',101,'original')")
    for number, line in enumerate(lines, 1):
        connection.execute(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)", ("original", number, *line.values())
        )
        connection.execute(
            "INSERT INTO voucher_line VALUES(?,?,?,?,?,?)",
            ("reversal", number, line["account"], line["credit"], line["debit"], None),
        )
    connection.execute("INSERT INTO fund_detail_activation VALUES(1,101)")
    # Historical original stays without attribution; the new reversal uses its exact source roles.
    details.store_voucher_fund_line_details(connection, "reversal")
    rows = details.verify_fund_line_details(
        connection, version_ids=["original", "reversal"], calculation_ids=[]
    )["lines"]
    assert [(row["line_no"], row["entity_id"], row["basis_calculation_id"]) for row in rows] == [
        (1, "destination", "calc"),
        (2, "source", "calc"),
    ]
    assert (
        connection.execute(
            "SELECT count(*) FROM fund_line_detail WHERE version_id='original'"
        ).fetchone()[0]
        == 0
    )
    details.store_voucher_fund_line_details(connection, "reversal")
    assert connection.execute("SELECT count(*) FROM fund_line_detail").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute("UPDATE fund_line_detail SET entity_id='source' WHERE line_no=1")
    connection.execute("DROP TRIGGER immutable_fund_line_detail_UPDATE")
    connection.execute("UPDATE fund_line_detail SET entity_id='source' WHERE line_no=1")
    with pytest.raises(KernelError, match="资金明细"):
        details.verify_fund_line_details(connection, version_ids=["reversal"], calculation_ids=[])


def test_split_payment_uses_one_actual_account_without_amount_matching():
    lines = [
        {"account": "221101", "debit": 500, "credit": 0},
        {"account": "1002", "debit": 0, "credit": 500},
        {"account": "224102", "debit": 100, "credit": 0},
        {"account": "1002", "debit": 0, "credit": 100},
    ]
    assert details.bindings("payment", {"bank_account_id": "actual-bank"}, lines) == {
        2: ("1002", "actual-bank"),
        4: ("1002", "actual-bank"),
    }
    with pytest.raises(KernelError):
        details.bindings("payment", {}, lines)


def test_source_effects_must_equal_attributed_movements(connection, monkeypatch):
    lines = transfer_source(monkeypatch)
    source = details._source(connection, "calc")
    source[1]["balances"][0]["amount"] = -699
    with pytest.raises(KernelError, match="资金明细"):
        details._expected(connection, "calc", lines)


def test_typed_publish_persists_details_and_exposes_preview(tmp_path):
    from entity_fixture import seed_entities
    from schema_fixture import test_bundle

    from ai_accounting.kernel.engine import Engine
    from ai_accounting.kernel.entities import Entities
    from ai_accounting.kernel.service import default_registry
    from ai_accounting.kernel.storage import Store

    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            test_bundle(default_registry()),
            "company",
            "911100000000000001",
            "database",
        )
    )
    adopt_empty_opening(engine, period="2026-09")
    proof = engine.register_evidence(
        b"Synthetic bank identity", "text/plain", "identity", request_id="proof"
    )["digest"]
    account = Entities(engine).register_entity(
        "fund_account",
        {
            "display_name": "合成银行账户",
            "bank_name": "合成银行",
            "bank_account_number": "1234567890123456",
            "account_holder_name": "合成公司",
        },
        account_type="bank",
        source="合成开户资料",
        evidence_digest=proof,
        request_id="account",
    )["entity_id"]
    seed_entities(engine, [("owner", "person", None)])
    engine.save_fact(
        "funding",
        "capital",
        {
            "period": "2026-09",
            "actual_date": "2026-09-01",
            "owner_id": "owner",
            "amount_fen": 700,
            "funding_kind": "capital",
            "bank_account_id": account,
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="capital",
    )
    preview = engine.preview(["capital"])
    assert preview["results"][0]["fund_line_details"]["lines"][0]["detail_code"] == "1002.0001"
    engine.confirm(
        ["capital"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish",
    )
    with engine.store.connection(read_only=True) as connection:
        version = connection.execute("SELECT version_id FROM voucher_current").fetchone()[0]
        row = details.verify_fund_line_details(
            connection, version_ids=[version], calculation_ids=[]
        )["lines"][0]
        assert row["entity_id"] == account
        assert (
            connection.execute(
                "SELECT account FROM voucher_line WHERE version_id=? AND line_no=1", (version,)
            ).fetchone()[0]
            == "1002"
        )
    engine.amend_fact(
        "funding",
        "capital",
        {
            "period": "2026-09",
            "actual_date": "2026-09-01",
            "owner_id": "owner",
            "amount_fen": 800,
            "funding_kind": "capital",
            "bank_account_id": account,
        },
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="correct-capital",
    )
    revised = engine.preview(["capital"])
    engine.confirm(
        ["capital"],
        preview_digest=revised["digest"],
        epochs=revised["epochs"],
        request_id="replace",
    )
    with engine.store.connection() as connection:
        # Simulate the exact newly installed empty attribution table at a forward upgrade.
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_fund_line_detail_DELETE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_fund_line_detail_DELETE")
        connection.execute("DELETE FROM fund_line_detail")
        connection.execute(trigger)
        details.initialize_open_details(engine, connection, "2026-09")
        assert (
            connection.execute(
                "SELECT count(DISTINCT version_id) FROM fund_line_detail"
            ).fetchone()[0]
            == 2
        )
        assert connection.execute("SELECT count(*) FROM voucher_current").fetchone()[0] == 1


def test_stage9_builders_register_explicit_synthetic_bank_identities(tmp_path, monkeypatch):
    import stage9_book
    from stage9_independent_book import IndependentBook

    monkeypatch.setattr(stage9_book, "__file__", str(tmp_path / "tests/kernel/stage9_book.py"))
    for name, builder in (
        ("mixed", lambda root: stage9_book.MixedBook(root, employees=1, businesses=26)),
        ("independent", lambda root: IndependentBook(root, objects=1, businesses=4)),
    ):
        book = builder(tmp_path / (".tmp/stage9-fund-" + name))
        with book.engine.store.connection(read_only=True) as connection:
            from ai_accounting.kernel.fund_accounts import require_fund_account

            account = require_fund_account(connection, book.bank, "bank")
            assert account["identity"]["account_holder_name"] == book.company["name"]
            assert account["evidence_digest"] == book.proof


@pytest.mark.parametrize("kind", ["opening_package", "opening_cash"])
def test_historical_fund_sources_do_not_apply_current_fact_models(opening_book, monkeypatch, kind):
    from ai_accounting.kernel.integrity import _check_sources
    from ai_accounting.kernel.verified_source_lease import (
        verified_calculation_source,
        verified_source_lease,
    )
    from ai_accounting.kernel.verified_source_reads import (
        VerifiedSourceReads,
        readonly_verification,
    )

    engine, _, _, package, _ = opening_book
    package(
        [
            ("opening_cash", "cash", {"cash_account_id": "cash", "balance_fen": 100}),
            (
                "opening_equity",
                "equity",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 100,
                    "holder_or_basis_id": "owner",
                },
            ),
        ]
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with readonly_verification(connection), verified_source_lease(connection):
            source = _check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source)
            )

            def forbidden(*args, **kwargs):
                pytest.fail("historical fund attribution applied the current model")

            monkeypatch.setattr(
                engine.store.registry.models[kind], "model_validate_json", forbidden
            )
            selected = details.verify_fund_line_details(connection, _source_reads=reads)
            assert len(selected["opening_lines"]) == 1
            assert selected["opening_lines"][0]["entity_id"] == "cash"
            with pytest.raises(KernelError, match="精确事实"):
                reads.raw_facts(("unknown-fact",))
            connection.execute("PRAGMA query_only=OFF")
            with pytest.raises(KernelError, match="只读事务"):
                reads.raw_facts((selected["opening_lines"][0]["source_fact_id"],))


@pytest.mark.parametrize(
    "account_type,parent,kind,account_field,identity,fallback",
    [
        (
            "bank", "1002", "funding", "bank_account_id",
            {"bank_name": "Synthetic bank", "bank_account_number": "1234567890123456",
             "account_holder_name": "Formal holder"},
            "Synthetic bank（尾号3456）",
        ),
        (
            "platform", "1012", "platform_funding", "platform_account_id",
            {"provider_name": "Synthetic platform", "account_identifier": "platform-123",
             "account_holder_name": "Formal holder"},
            "Synthetic platform—platform-123",
        ),
        ("cash", "1001", "cash_funding", "cash_account_id", {}, "现金账户"),
    ],
)
def test_fund_display_keeps_identity_separate_from_missing_management_name(
    opening_book, account_type, parent, kind, account_field, identity, fallback,
):
    from ai_accounting.kernel.account_definitions import ACCOUNT_NAMES
    from ai_accounting.kernel.entities import Entities
    from ai_accounting.kernel.fund_accounts import require_fund_account

    engine, save, publish, _, proof = opening_book
    adopt_empty_opening(engine, period="2026-09")
    entities = Entities(engine)
    account = entities.register_entity(
        "fund_account", {"display_name": "Managed account alias", **identity},
        account_type=account_type, source="Synthetic account document",
        evidence_digest=proof, request_id="display-account",
    )["entity_id"]
    funding = {
        "period": "2026-09", "actual_date": "2026-09-01", "owner_id": "owner",
        account_field: account, "funding_kind": "capital", "amount_fen": 700,
    }
    if account_type == "platform":
        save("platform_movement", "source-row", {
            "period": "2026-09", "actual_date": "2026-09-01",
            "platform_account_id": account, "direction": "inflow", "amount_fen": 700,
            "source_evidence_digest": proof, "source_location": "synthetic platform row 1",
        })
        funding["movement_ids"] = ["source-row"]
    save(kind, "capital", funding)
    publish(*(("source-row", "capital") if account_type == "platform" else ("capital",)))

    def displayed_account(expected_name):
        with engine.store.connection(read_only=True) as connection:
            version = connection.execute("SELECT version_id FROM voucher_current").fetchone()[0]
            assert require_fund_account(connection, account)["identity"] == identity
            verified = details.verify_fund_line_details(
                connection, version_ids=[version], calculation_ids=[],
            )["lines"]
            assert len(verified) == 1 and verified[0]["entity_id"] == account
            lines = [dict(row) for row in connection.execute(
                "SELECT account,debit,credit,cashflow FROM voucher_line "
                "WHERE version_id=? ORDER BY line_no", (version,),
            )]
            assert lines[0]["account"] == parent and lines[0]["debit"] == 700
            rendered = details.display_fund_lines(connection, version, lines)
            assert rendered[0]["account_name"] == ACCOUNT_NAMES[parent] + "—" + expected_name
            assert "Formal holder" not in rendered[0]["account_name"]

    displayed_account("Managed account alias")
    for revision, profile in enumerate(({"display_name": None}, {}), start=1):
        entities.update_entity_profile(
            account, profile, source="Synthetic management name withdrawal",
            expected_revision=revision, request_id=f"display-profile-{revision}",
        )
        displayed_account(fallback)
