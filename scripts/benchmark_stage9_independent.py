"""Run the independent Stage 9 fixture from one selected source tree."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

if __package__:
    from .stage9_source import configure_source, workspace_root
else:
    from stage9_source import configure_source, workspace_root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path)
    args, remaining = parser.parse_known_args()
    workspace = workspace_root(Path(__file__).resolve().parents[1], args.workspace)
    source = configure_source(args.source, workspace)
    script = source / "tests/kernel/stage9_independent_book.py"
    sys.argv = [str(script), "--workspace", str(workspace), *remaining]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
