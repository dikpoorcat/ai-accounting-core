"""Export HTTP response schemas, or check them without modifying the repository."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_accounting.kernel.response_contracts import response_schemas

TARGET = (
    Path(__file__).resolve().parents[1] / "frontend/src/api/generated/dashboardResponseSchemas.json"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=TARGET)
    args = parser.parse_args()
    content = (
        json.dumps(response_schemas(mode="serialization"), ensure_ascii=False, indent=2) + "\n"
    )
    if args.check:
        with TemporaryDirectory(prefix="response-contracts-") as directory:
            generated = Path(directory) / TARGET.name
            generated.write_text(content, encoding="utf-8", newline="\n")
            if not args.output.is_file() or args.output.read_bytes() != generated.read_bytes():
                parser.exit(1, f"Response schema differs: {args.output}\nRun contracts:generate.\n")
        print("Python response schemas are synchronized.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8", newline="\n")
        print(f"Generated {args.output}")


if __name__ == "__main__":
    main()
