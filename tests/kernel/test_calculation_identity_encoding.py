"""Exact identity wire equivalence and local encoding work; no timing claims."""

import hashlib
import json

import pytest

from ai_accounting.kernel import history_encoding_v1, integrity, types
from ai_accounting.kernel.calculation_identity import (
    calculation_identity_digest,
    calculation_identity_prefix,
)
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError


@pytest.mark.parametrize("encoder", (types, history_encoding_v1), ids=("current", "fixed-v1"))
@pytest.mark.parametrize("count", (1, 100, 500))
@pytest.mark.parametrize("self_read", (False, True))
def test_identity_bytes_and_single_outcome_encoding(encoder, count, self_read, monkeypatch):
    sha256 = hashlib.sha256
    for index in range(count):
        outcome = {
            "values": {"中文": '引号"和反斜线\\', "null": None, "large": 2**63 - 1,
                       "index": index},
            "lines": [], "balances": [],
        }
        fact = f"事实-{index}"
        reads = [{"source": "fact", "kind": '类型"\\', "key": fact, "before_period": None}]
        versions = sorted(["上游-1", fact] if self_read else ["上游-1"])
        payload = {"fact": fact, "outcome": outcome, "reads": reads,
                   "program": '程序"\\', "versions": versions}
        encoded_values = []

        def counted(value, *, outcome=outcome, encoded_values=encoded_values):
            # Record role only, never business values or raw encodings.
            encoded_values.append("outcome" if value is outcome else "field")
            return encoder.canonical(value)

        encoded = counted(outcome)
        wires = []

        def capture(raw, *, wires=wires):
            wires.append(raw)
            return sha256(raw)

        with monkeypatch.context() as patch:
            patch.setattr(hashlib, "sha256", capture)
            actual = calculation_identity_digest(
                fact_id=fact, outcome_encoding=encoded, reads=reads,
                program=payload["program"], versions=versions, encode=counted,
            )
        assert wires == [encoder.canonical(payload).encode("utf-8")]
        assert actual == encoder.digest(payload)
        assert actual == hashlib.sha256(encoder.canonical(payload).encode("utf-8")).digest()
        assert encoded_values == ["outcome", "field", "field", "field", "field"]


@pytest.mark.parametrize("version, encoder", ((None, types), (1, history_encoding_v1)))
def test_noncanonical_stored_outcome_is_decoded_and_encoded_once(monkeypatch, version, encoder):
    outcome = {"values": {"中文": '引号"\\', "nullable": None}, "balances": [], "lines": []}
    raw = json.dumps(outcome, ensure_ascii=True, indent=3)
    assert raw != encoder.canonical(outcome)
    row = {"id": "synthetic-result", "outcome": raw, "digest": encoder.digest(outcome)}
    calls = []
    original = integrity.canonical

    def counted(value):
        calls.append(1)
        return original(value)

    monkeypatch.setattr(integrity, "canonical", counted)
    with historical_content(version):
        decoded, encoded = integrity._outcome_with_encoding(row)
    assert decoded == outcome
    assert encoded == encoder.canonical(outcome) != raw
    assert calls == [1]


@pytest.mark.parametrize("version, encoder", ((None, types), (1, history_encoding_v1)))
@pytest.mark.parametrize("damage", ("unbalanced", "float", "negative"))
def test_matching_digest_does_not_bypass_strict_outcome_amount_checks(version, encoder, damage):
    debit = 1.0 if damage == "float" else -1 if damage == "negative" else 2
    outcome = {
        "values": {}, "balances": [],
        "lines": [{"account": "cash", "debit": debit, "credit": 0, "cashflow": None},
                  {"account": "equity", "debit": 0, "credit": 1, "cashflow": None}],
    }
    row = {"id": "synthetic-invalid", "outcome": encoder.canonical(outcome),
           "digest": encoder.digest(outcome)}
    with historical_content(version), pytest.raises(KernelError) as failure:
        integrity._outcome_with_encoding(row)
    assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("encoder", (types, history_encoding_v1), ids=("current", "fixed-v1"))
@pytest.mark.parametrize("count", (1, 100, 500))
def test_two_identity_candidates_encode_invariants_once(encoder, count, monkeypatch):
    sha256 = hashlib.sha256
    for index in range(count):
        outcome = {"values": {"中文": '引号"\\', "index": index}, "lines": [], "balances": []}
        identity = {
            "fact_id": f"fact-{index}", "program": 'program"\\',
            "reads": [{"key": "中文", "before_period": None}],
            "outcome_encoding": encoder.canonical(outcome),
        }
        encoded = []

        def counted(value, *, encoded=encoded):
            encoded.append(value)
            return encoder.canonical(value)

        identity["encode"] = counted
        prefix = calculation_identity_prefix(**identity)
        wires = []

        def capture(raw, *, wires=wires):
            wires.append(raw)
            return sha256(raw)

        candidates = (["upstream"], sorted(["upstream", identity["fact_id"]]))
        with monkeypatch.context() as patch:
            patch.setattr(hashlib, "sha256", capture)
            actual = [calculation_identity_digest(**identity, prefix=prefix, versions=versions)
                      for versions in candidates]
        payload = {"fact": identity["fact_id"], "program": identity["program"],
                   "reads": identity["reads"], "outcome": outcome}
        assert wires == [encoder.canonical({**payload, "versions": versions}).encode("utf-8")
                         for versions in candidates]
        assert actual == [encoder.digest({**payload, "versions": versions})
                          for versions in candidates]
        assert encoded == [identity["fact_id"], identity["program"], identity["reads"], *candidates]


@pytest.mark.parametrize("version", (None, 1))
@pytest.mark.parametrize("raw", (
    '{"values":{},"values":{},"lines":[],"balances":[]}',
    '{"values":{"bad":NaN},"lines":[],"balances":[]}',
    '{"values":{"bad":Infinity},"lines":[],"balances":[]}',
    '{"values":{},"lines":null,"balances":[]}',
))
def test_retained_line_proof_does_not_bypass_illegal_json(version, raw):
    encoder = history_encoding_v1 if version == 1 else types
    try:
        saved_digest = encoder.digest(json.loads(raw))
    except ValueError:
        saved_digest = b"\0" * 32
    row = {"id": "synthetic-invalid", "outcome": raw, "digest": saved_digest}
    with historical_content(version), pytest.raises((KernelError, ValueError)):
        integrity._outcome_with_lines(row)
