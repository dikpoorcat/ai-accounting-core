"""Check generated SQL against packaged contracts or exclusively freeze released v1."""

import argparse
import json
from pathlib import Path

from ai_accounting.kernel.catalog import catalog_sql
from ai_accounting.kernel.content_v1 import content_contract
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.schema_bundle import APPLICATION_ID, FAMILY, STATUS, VERSION
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.versions import contract, fingerprint


def _release_outputs(directory, registry):
    """Finish every candidate before publishing any released file."""

    outputs = {}
    for kind, script in (("company", schema_sql(registry)), ("catalog", catalog_sql())):
        items = contract(script)
        data = {
            "family": FAMILY,
            "kind": kind,
            "status": "released",
            "version": 1,
            "application_id": APPLICATION_ID,
            "objects": items,
            "sha256": fingerprint(items).hex(),
        }
        outputs[directory / kind / "v1.json"] = (
            json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    outputs[directory / "content-v1.json"] = (
        json.dumps(content_contract(registry), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    return outputs


def _publish_release(outputs):
    """Create only absent targets; undo this invocation's files on failure."""

    existing = [path for path in outputs if path.exists()]
    if existing:
        raise FileExistsError("released v1 already exists: " + ", ".join(map(str, existing)))
    created = []
    try:
        for path, output in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                created.append(path)
                handle.write(output)
    except BaseException as error:
        failed_cleanup = []
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                failed_cleanup.append(path)
        if failed_cleanup:
            raise RuntimeError(
                "incomplete v1 release needs manual inspection: "
                + ", ".join(map(str, failed_cleanup))
            ) from error
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="replace only the package draft JSONs")
    mode.add_argument(
        "--candidate-v1", action="store_true", help="print exact released v1 candidates as JSON"
    )
    mode.add_argument("--freeze-v1", action="store_true", help="create released v1 contracts once")
    mode.add_argument("--check", action="store_true", help="verify without writing (the default)")
    args = parser.parse_args()
    if args.freeze_v1 and (STATUS, VERSION) != ("released", 1):
        parser.error("freeze-v1 requires the production bundle to select released / 1")
    if args.write and (STATUS, VERSION) != ("draft", 0):
        parser.error("--write is limited to the development draft")
    directory = Path(__file__).resolve().parents[1] / "src/ai_accounting/kernel/schema_contracts"
    if args.candidate_v1:
        outputs = _release_outputs(directory, default_registry())
        print(
            json.dumps(
                {path.relative_to(directory).as_posix(): body for path, body in outputs.items()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return
    status, version, filename = (
        ("released", 1, "v1.json")
        if args.freeze_v1
        else (STATUS, VERSION, "draft.json" if STATUS == "draft" else f"v{VERSION}.json")
    )
    if args.freeze_v1:
        targets = [directory / kind / filename for kind in ("company", "catalog")]
        targets.append(directory / "content-v1.json")
        if any(path.exists() for path in targets):
            parser.error("released v1 already exists; refusing to replace any contract")
        outputs = _release_outputs(directory, default_registry())
        try:
            _publish_release(outputs)
        except FileExistsError as error:
            parser.error(str(error))
        print("Released contracts created")
        return
    changed = []
    registry = default_registry()
    for kind, script in (("company", schema_sql(registry)), ("catalog", catalog_sql())):
        items = contract(script)
        data = {
            "family": FAMILY,
            "kind": kind,
            "status": status,
            "version": version,
            "application_id": APPLICATION_ID,
            "objects": items,
            "sha256": fingerprint(items).hex(),
        }
        output = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        path = directory / kind / filename
        if not path.exists() or path.read_text("utf-8") != output:
            changed.append(kind)
            if args.write:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as handle:
                    handle.write(output)
    if status == "released" and version == 1:
        path = directory / "content-v1.json"
        output = json.dumps(content_contract(registry), ensure_ascii=False, indent=2) + "\n"
        if not path.exists() or path.read_text("utf-8") != output:
            changed.append("content")
    if changed and not args.write:
        label = "Draft" if status == "draft" else "Released"
        parser.exit(1, label + " contracts differ: " + ", ".join(changed) + "\n")
    label = "Draft" if status == "draft" else "Released"
    action = "written" if args.write else "verified"
    print(label + " contracts " + action)


if __name__ == "__main__":
    main()
