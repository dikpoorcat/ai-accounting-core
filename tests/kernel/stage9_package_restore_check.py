"""Verify a packaged synthetic ZIP restore and its frozen/current business state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]


def state(path):
    with closing(sqlite3.connect(path)) as connection:
        closes = [
            {"period": period, "digest": digest.hex(), "manifest": manifest}
            for period, digest, manifest in connection.execute(
                "SELECT period,digest,manifest FROM period_close ORDER BY period"
            )
        ]
        current = {
            subject: (
                connection.execute(
                    "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject,)
                ).fetchone()
                or (None,)
            )[0]
            for subject in (
                "wage-2016-01-0",
                "stage9-agent-report-completion",
                "stage9-agent-report-review",
            )
        }
        calculations = {
            subject: (
                connection.execute(
                    "SELECT calculation_id FROM calculation_current WHERE subject_id=?", (subject,)
                ).fetchone()
                or (None,)
            )[0]
            for subject in current
        }
        return {
            "closes": closes,
            "current_facts": current,
            "current_calculations": calculations,
            "material_close_rules": connection.execute(
                "SELECT count(*) FROM material_close_rule"
            ).fetchone()[0],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "source", "target", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--close-digest", required=True)
    parser.add_argument("--wage-fact-id", required=True)
    parser.add_argument("--completion-fact-id", required=True)
    parser.add_argument("--review-fact-id", required=True)
    args = parser.parse_args()
    temporary = (REPOSITORY / ".tmp").resolve()
    paths = {
        name: getattr(args, name).resolve() for name in ("archive", "source", "target", "output")
    }
    if any(not path.is_relative_to(temporary) for path in paths.values()):
        raise ValueError("Only isolated synthetic paths under repository .tmp are accepted")
    if paths["target"].exists() or paths["output"].exists():
        raise ValueError("Restore target and report must be absent")

    from ai_accounting.kernel import backup

    source_verified = backup.verify_file(paths["source"], expected_company_id=args.company_id)
    source_state = state(paths["source"])
    restored = backup.restore_portable(
        paths["archive"],
        paths["target"],
        expected_company_id=args.company_id,
    )
    restored_verified = backup.verify_file(paths["target"], expected_company_id=args.company_id)
    restored_state = state(paths["target"])
    assert source_state == restored_state, "Frozen and current business state changed on restore"
    assert len(restored_state["closes"]) == 1
    assert restored_state["closes"][0]["digest"] == args.close_digest
    assert restored_state["material_close_rules"] == 1
    assert restored_state["current_facts"] == {
        "wage-2016-01-0": args.wage_fact_id,
        "stage9-agent-report-completion": args.completion_fact_id,
        "stage9-agent-report-review": args.review_fact_id,
    }
    assert all(restored_state["current_calculations"].values())
    assert source_verified["database_format"] == restored_verified["database_format"]
    assert (
        source_verified["verification"]["status"]
        == restored_verified["verification"]["status"]
        == "verified"
    )
    report = {
        "status": "passed",
        "archive": str(paths["archive"]),
        "source": str(paths["source"]),
        "restored": str(paths["target"]),
        "database_format": restored_verified["database_format"],
        "source_evidence_count": source_verified["evidence_count"],
        "restored_evidence_count": restored_verified["evidence_count"],
        "restored_result": {"latest_closed_period": restored["latest_closed_period"]},
        "state": restored_state,
    }
    paths["output"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": "passed", "output": str(paths["output"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
