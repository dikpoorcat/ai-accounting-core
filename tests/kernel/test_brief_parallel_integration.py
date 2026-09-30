"""Real spawned readers keep complete responses and company scope intact."""

import json
import multiprocessing
from time import perf_counter, sleep
from types import SimpleNamespace

import pytest
from material_fixture import supporting_text
from pydantic import SecretStr

from ai_accounting.kernel.brief_parallel import _BriefParallelAttempt
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.service import LocalService

PERIOD = "2026-09"


def _without_generated(value):
    if isinstance(value, dict):
        return {
            key: _without_generated(item) for key, item in value.items() if key != "generated_at"
        }
    if isinstance(value, list):
        return [_without_generated(item) for item in value]
    return value


def _fund(engine, label, amount):
    entities = Entities(engine)
    owner = entities.register_entity(
        "person",
        {"display_name": label},
        source="synthetic explicit owner",
        request_id=label + "-owner",
    )["entity_id"]
    bank = entities.register_entity(
        "fund_account",
        {},
        account_type="bank",
        source="synthetic bank account",
        request_id=label + "-bank",
    )["entity_id"]
    proof = engine.register_evidence(
        ("Synthetic funding confirmation " + label).encode(),
        "text/plain",
        label,
        request_id=label + "-evidence",
    )["digest"]
    supporting_text(engine, proof, period=PERIOD)
    engine.save_fact(
        "funding",
        label,
        {
            "period": PERIOD,
            "owner_id": owner,
            "bank_account_id": bank,
            "amount_fen": amount,
            "funding_kind": "capital",
            "actual_date": PERIOD + "-02",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id=label + "-save",
    )
    preview = engine.preview([label])
    engine.confirm(
        [label],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id=label + "-publish",
    )
    return bank


@pytest.fixture
def parallel_service(tmp_path):
    root = tmp_path / "synthetic-parallel-root"
    service = LocalService(root, enable_read_pool=True, enable_parallel_brief=True)
    password = SecretStr("Synthetic-parallel-owner-123")
    service.security.provision("owner", password)
    token = service.security.login("owner", password).session_token
    first = service.catalog.create_company("91310000123456789A", "并行合成甲")
    second = service.catalog.create_company("91310000123456789B", "并行合成乙")
    _fund(service.engine(first["id"]), "first-funding", 123456)
    _fund(service.engine(second["id"]), "second-funding", 987654)
    try:
        yield service, token, first, second
    finally:
        service.close()


def _brief(service, token, company, **options):
    return service.dispatch(
        "dashboard_brief",
        {"company_id": company["id"], "period": PERIOD, **options},
        session_token=token,
    )


def test_spawned_brief_matches_serial_and_switches_company(parallel_service, monkeypatch):
    service, token, first, second = parallel_service
    finished = []
    original_finish = _BriefParallelAttempt.finish

    def observed_finish(attempt):
        original_finish(attempt)
        finished.append((attempt.started, attempt.finished))

    monkeypatch.setattr(_BriefParallelAttempt, "finish", observed_finish)
    for company, amount in ((first, 123456), (second, 987654), (first, 123456)):
        expected = Dashboard(service.engine(company["id"], dashboard_read=True)).brief(PERIOD)
        actual = _brief(service, token, company)
        assert _without_generated(actual) == _without_generated(expected)
        assert actual["read_context"]["company_id"] == company["id"]
        assert actual["data"]["position"]["bank_fen"] == amount
        assert actual["data"]["voucher_count"] == 1
        assert actual["data"]["period_preparation"] is not None
    http = service.dispatch(
        "dashboard_brief",
        {"company_id": first["id"], "period": PERIOD},
        session_token=token,
        response_format="http_json",
    )
    assert json.loads(http)["data"]["position"]["bank_fen"] == "123456"
    assert finished == [(True, True)] * 4


def test_parent_scope_is_reused_without_worker_settlement_read(parallel_service, monkeypatch):
    from ai_accounting.kernel import settlement_projection

    service, token, first, _ = parallel_service
    expected = Dashboard(service.engine(first["id"], dashboard_read=True)).brief(PERIOD)
    original_position_rows = settlement_projection.settlement_position_rows
    parent_reads = []
    finished = []
    original_finish = _BriefParallelAttempt.finish

    def observed_finish(attempt):
        original_finish(attempt)
        finished.append(True)

    def observed_position_rows(*args, **kwargs):
        parent_reads.append(True)
        return original_position_rows(*args, **kwargs)

    monkeypatch.setattr(settlement_projection, "settlement_position_rows", observed_position_rows)
    monkeypatch.setattr(_BriefParallelAttempt, "finish", observed_finish)
    actual = _brief(service, token, first)
    assert _without_generated(actual) == _without_generated(expected)
    assert finished == [True]
    assert len(parent_reads) == 1


def test_supplied_position_rows_skip_second_settlement_read(parallel_service, monkeypatch):
    from ai_accounting.kernel import settlement_projection
    from ai_accounting.kernel.dashboard import RECLASS, _position

    service, _, first, _ = parallel_service
    with Dashboard(service.engine(first["id"]))._snapshot(PERIOD) as snap:
        rows = settlement_projection.settlement_position_rows(
            snap.connection, snap.period, RECLASS, reads=snap.reads
        )
        expected = _position(snap)

        def forbidden(*args, **kwargs):
            raise AssertionError("worker must use the supplied checked rows")

        monkeypatch.setattr(settlement_projection, "settlement_position_rows", forbidden)
        assert _position(snap, position_obligations=rows) == expected


def test_large_position_message_does_not_block_sender_while_receiver_waits(monkeypatch):
    from ai_accounting.kernel import settlement_projection

    attempt = _BriefParallelAttempt(None, None)
    attempt.started = True
    receive, attempt._position_send_pipe = multiprocessing.Pipe(duplex=False)
    snap = SimpleNamespace(
        connection=None, reads=None, period=PERIOD,
        store=SimpleNamespace(
            path="synthetic.sqlite", company_id="company", database_id="database",
            taxpayer_id="91310000123456789A",
        ),
    )
    huge_party = "p" * (8 * 1024 * 1024)
    monkeypatch.setattr(
        settlement_projection, "settlement_position_rows",
        lambda *args, **kwargs: [{
            "account": "2202", "category": "payable", "counterparty_id": huge_party,
            "remaining": 1, "unknown": False,
        }],
    )
    try:
        start = perf_counter()
        attempt.supply_position_obligations(snap)
        assert perf_counter() - start < 1
        sleep(0.1)  # The worker is still busy with the report issue packet.
        assert not attempt._position_send_done.is_set()
        packet = receive.recv()
        assert packet["binding"][1:3] == ("company", "database")
        assert packet["period"] == PERIOD
        assert packet["obligations"][0]["counterparty_id"] == huge_party
        assert attempt._position_send_done.wait(2)
        assert attempt._position_send_error is None
    finally:
        receive.close()
        attempt._position_send_pipe.close()
        attempt._position_sender.join(timeout=2)


def test_large_position_sender_is_reaped_when_receiver_never_reads(monkeypatch):
    from ai_accounting.kernel import settlement_projection

    recycled = []
    attempt = _BriefParallelAttempt(SimpleNamespace(_recycle=lambda: recycled.append(True)), None)
    attempt.started = True
    attempt._position_pipe, attempt._position_send_pipe = multiprocessing.Pipe(duplex=False)
    snap = SimpleNamespace(
        connection=None, reads=None, period=PERIOD,
        store=SimpleNamespace(
            path="synthetic.sqlite", company_id="company", database_id="database",
            taxpayer_id="91310000123456789A",
        ),
    )
    monkeypatch.setattr(
        settlement_projection, "settlement_position_rows",
        lambda *args, **kwargs: [{"counterparty_id": "p" * (8 * 1024 * 1024)}],
    )
    attempt.supply_position_obligations(snap)
    sleep(0.1)
    assert attempt._position_sender.is_alive()
    attempt.close()
    assert recycled == [True]
    assert not attempt._position_sender.is_alive()


def test_parent_failure_before_position_send_reaps_waiting_worker(parallel_service, monkeypatch):
    from ai_accounting.kernel import dashboard

    service, token, first, _ = parallel_service
    children = tuple(service.brief_parallel.pool._pool)
    original = dashboard._open_items
    failed = []

    def fail_once(*args, **kwargs):
        if not failed:
            failed.append(True)
            raise KernelError("synthetic_open_items_failure", "synthetic open item failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(dashboard, "_open_items", fail_once)
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first)
    assert rejected.value.code == "synthetic_open_items_failure"
    assert service.brief_parallel.pool is None
    assert all(not child.is_alive() for child in children)
    assert _brief(service, token, first)["data"]["position"]["bank_fen"] == 123456


def test_empty_company_returns_without_starting_position_sender(parallel_service, monkeypatch):
    service, token, _, _ = parallel_service
    empty = service.catalog.create_company("91310000123456789C", "并行合成空库")
    from ai_accounting.kernel import brief_parallel

    started = []
    original = brief_parallel._BriefParallelAttempt.start

    def observed_start(attempt, snap):
        started.append(True)
        return original(attempt, snap)

    monkeypatch.setattr(brief_parallel._BriefParallelAttempt, "start", observed_start)
    response = service.dispatch(
        "dashboard_brief", {"company_id": empty["id"]}, session_token=token
    )
    assert response["data"] is None
    assert started == []


def test_broken_position_pipe_discards_group_and_falls_back(parallel_service, monkeypatch):
    service, token, first, _ = parallel_service
    children = tuple(service.brief_parallel.pool._pool)
    original = _BriefParallelAttempt.supply_position_obligations
    failed = []

    def break_once(attempt, snap):
        if not failed:
            failed.append(True)
            attempt._position_send_pipe.close()
        return original(attempt, snap)

    monkeypatch.setattr(_BriefParallelAttempt, "supply_position_obligations", break_once)
    actual = _brief(service, token, first)
    expected = Dashboard(service.engine(first["id"])).brief(PERIOD)
    assert failed == [True]
    assert _without_generated(actual) == _without_generated(expected)
    assert service.brief_parallel.pool is None
    assert all(not child.is_alive() for child in children)


def test_commit_after_position_send_discards_group(parallel_service, monkeypatch):
    service, token, first, _ = parallel_service
    original = _BriefParallelAttempt.supply_position_obligations
    committed = []

    def commit_after_send(attempt, snap):
        original(attempt, snap)
        if not committed:
            _fund(service.engine(first["id"]), "commit-after-position-send", 333)
            committed.append(True)

    monkeypatch.setattr(_BriefParallelAttempt, "supply_position_obligations", commit_after_send)
    actual = _brief(service, token, first)
    expected = Dashboard(service.engine(first["id"])).brief(PERIOD)
    assert committed == [True]
    assert _without_generated(actual) == _without_generated(expected)
    assert actual["data"]["position"]["bank_fen"] == 123789
    assert service.brief_parallel.pool is None


def test_commit_during_parallel_read_discards_entire_old_response(parallel_service, monkeypatch):
    service, token, first, _ = parallel_service
    original_position = _BriefParallelAttempt.position
    changes = []

    def commit_after_worker_result(attempt):
        position = original_position(attempt)
        _fund(service.engine(first["id"]), "new-during-read", 333)
        changes.append(True)
        return position

    monkeypatch.setattr(_BriefParallelAttempt, "position", commit_after_worker_result)
    actual = _brief(service, token, first)
    expected = Dashboard(service.engine(first["id"])).brief(PERIOD)
    assert changes == [True]
    assert _without_generated(actual) == _without_generated(expected)
    assert actual["data"]["position"]["bank_fen"] == 123789
    assert actual["data"]["voucher_count"] == 2
    assert service.brief_parallel.pool is None


def test_busy_group_and_closed_pool_use_serial_without_reviving_workers(
    parallel_service, monkeypatch
):
    service, token, first, _ = parallel_service
    coordinator = service.brief_parallel
    submitted = []
    original_submit = coordinator.pool.apply_async

    def observed_submit(*args, **kwargs):
        submitted.append(True)
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(coordinator.pool, "apply_async", observed_submit)
    assert coordinator._gate.acquire(blocking=False)
    try:
        assert _brief(service, token, first)["data"]["voucher_count"] == 1
    finally:
        coordinator._gate.release()
    assert submitted == []
    children = tuple(coordinator.pool._pool)
    coordinator.close()
    assert all(not child.is_alive() for child in children)
    assert _brief(service, token, first)["data"]["voucher_count"] == 1
    assert coordinator.pool is None


@pytest.mark.parametrize("failure", ["timeout", "partial_submission"])
def test_worker_failures_reap_processes_and_return_fresh_serial_read(
    parallel_service, monkeypatch, failure
):
    service, token, first, _ = parallel_service
    coordinator = service.brief_parallel
    children = tuple(coordinator.pool._pool)
    original_submit = coordinator.pool.apply_async
    submissions = []

    class Unavailable:
        def get(self, *, timeout):
            raise multiprocessing.TimeoutError("synthetic lost result")

    def failing_submit(*args, **kwargs):
        submissions.append(True)
        if failure == "timeout" and len(submissions) == 1:
            return Unavailable()
        if failure == "partial_submission" and len(submissions) == 2:
            raise BrokenPipeError("synthetic submission channel failure")
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(coordinator.pool, "apply_async", failing_submit)
    result = _brief(service, token, first)
    assert result["data"]["position"]["bank_fen"] == 123456
    assert coordinator.pool is None
    assert all(not child.is_alive() for child in children)
    # A subsequent request gets a new group, never the abandoned packets.
    assert _brief(service, token, first)["data"]["position"]["bank_fen"] == 123456
    assert coordinator.pool is not None


def test_client_cannot_supply_parallel_values(parallel_service):
    service, token, first, _ = parallel_service
    properties = service.command_models["dashboard_brief"].json_schema()["properties"]
    assert not any(key.startswith("_") for key in properties)
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first, _parallel_attempt={"already_verified": True})
    assert rejected.value.code == "invalid_command"


def test_worker_company_identity_error_is_not_a_business_question(parallel_service, monkeypatch):
    service, token, first, second = parallel_service
    coordinator = service.brief_parallel
    original_submit = coordinator.pool.apply_async
    original_finish = _BriefParallelAttempt.finish
    finished = []

    def wrong_company_submit(function, args):
        if args[0] == "materials":
            kind, binding, period, as_of, pipe = args
            path, _company_id, database_id, taxpayer_id = binding
            args = (kind, (path, second["id"], database_id, taxpayer_id), period, as_of, pipe)
        return original_submit(function, args)

    def observed_finish(attempt):
        finished.append(True)
        return original_finish(attempt)

    monkeypatch.setattr(coordinator.pool, "apply_async", wrong_company_submit)
    monkeypatch.setattr(_BriefParallelAttempt, "finish", observed_finish)
    with pytest.raises(KernelError) as rejected:
        _brief(service, token, first)
    assert rejected.value.code == "company_mismatch"
    assert rejected.value.response()["status"] == "rejected"
    assert "fact_issues" not in rejected.value.response()
    assert finished == []
    assert coordinator.pool is None
