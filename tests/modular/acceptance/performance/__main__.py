"""Separate build / public qualification / actual Vue measurements.

Examples (repository venv):
  -m tests.modular.acceptance.performance plan --months 120
  -m tests.modular.acceptance.performance build NEW_ROOT --months 12
  -m tests.modular.acceptance.performance suite NEW_ROOT
  -m tests.modular.acceptance.performance verify ROOT
  -m tests.modular.acceptance.performance browser ROOT --node EXE
      --playwright-module MODULE --static-directory RELEASE_DIST
Use --interactions for the existing complete local-interaction matrix.
No command builds frontend assets, changes launchers or contacts port 5173.
"""

import argparse
import json
from pathlib import Path

from .dataset import build
from .environment import capture as environment
from .environment import code_identity
from .ledger_variant import MODES
from .workload import CONTRACTORS, EMPLOYEES, INITIAL_BANK, PEOPLE, SCALES, plan_month


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--months", type=int, choices=SCALES, required=True)
    build_parser = commands.add_parser("build")
    build_parser.add_argument("root", type=Path)
    build_parser.add_argument("--months", type=int, choices=SCALES, required=True)
    build_parser.add_argument("--mode", choices=MODES, default="original")
    build_parser.add_argument(
        "--prefix",
        action="store_true",
        help="Build months-1, reserving the final month for lifecycle samples",
    )
    suite = commands.add_parser("suite")
    suite.add_argument("root", type=Path)
    suite.add_argument("--mode", choices=MODES, default="original")
    suite.add_argument(
        "--probe", action="store_true", help="Non-official 2/3-month, 240-business probe"
    )
    verify = commands.add_parser("verify")
    verify.add_argument("root", type=Path)
    browser = commands.add_parser("browser")
    browser.add_argument("root", type=Path)
    browser.add_argument("--mode", choices=MODES, default="original")
    browser.add_argument("--node", type=Path, required=True)
    browser.add_argument("--playwright-module", type=Path, required=True)
    browser.add_argument("--static-directory", type=Path)
    browser.add_argument("--interactions", action="store_true")
    browser.add_argument("--probe", action="store_true")
    browser.add_argument("--concurrent", action="store_true")
    lifecycle = commands.add_parser("lifecycle")
    lifecycle.add_argument("root", type=Path)
    lifecycle.add_argument("output_root", type=Path)
    lifecycle.add_argument("--mode", choices=MODES, default="original")
    lifecycle.add_argument(
        "--scenario", choices=("commit_to_ready", "month_archive_and_ready"), required=True
    )
    lifecycle.add_argument("--probe", action="store_true")
    args = parser.parse_args()
    started_environment = environment()
    if args.action == "plan":
        balance, rows = INITIAL_BANK, []
        for index in range(args.months):
            selected = plan_month(index, opening_bank=balance)
            balance = selected.ending_bank_fen
            rows.append(
                {
                    "period": selected.period,
                    "counts": selected.counts,
                    "batch_sizes": [len(item["mutations"]) for item in selected.requests]
                    + [len(selected.reconciliations)],
                }
            )
        result = {
            "scale_months": args.months,
            "people": PEOPLE,
            "employees": EMPLOYEES,
            "contractors": CONTRACTORS,
            "businesses_per_month": 1000,
            "months": rows,
        }
    elif args.action == "build":
        result = build(args.root, args.months, args.mode, pending_last_month=args.prefix)
    elif args.action == "suite":
        from .suite import build as build_suite

        result = build_suite(
            args.root,
            args.mode,
            **({"probe_scales": (2, 3), "probe_business_count": 240} if args.probe else {}),
        )
    elif args.action == "verify":
        from .qualification import run

        result = run(args.root)
    elif args.action == "browser":
        from .browser import run

        result = run(
            args.root,
            args.mode,
            node=args.node,
            playwright_module=args.playwright_module,
            static_directory=args.static_directory,
            interactions=args.interactions,
            probe=args.probe,
            concurrent=args.concurrent,
        )
    else:
        from .scenarios import run

        result = run(args.root, args.output_root, args.mode, args.scenario, probe=args.probe)
    finished_code = code_identity()
    unchanged = all(started_environment[key] == value for key, value in finished_code.items())
    print(
        json.dumps(
            {
                "environment": started_environment,
                "code_after": finished_code,
                "source_unchanged": unchanged,
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not unchanged and args.action in {"verify", "browser", "lifecycle"}:
        raise SystemExit("实现或测量代码在运行期间变化，本次结果不能作为验收依据。")
    if "status" in result and result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
