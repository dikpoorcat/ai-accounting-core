"""Independent dialogue entry points retain their own seed and subprocess route."""

import asyncio
import itertools
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest
import question_reuse_agent_harness as question
import reading_agent_harness as reading
import staged_accounting_agent_harness as staged
import work_draft_agent_harness as work_draft

VARIANTS = (work_draft, staged, question)


@pytest.mark.parametrize(
    "order", list(itertools.permutations(module.__name__ for module in VARIANTS)),
)
def test_import_order_keeps_default_reading_and_variant_routes(order):
    script = """
import importlib
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import reading_agent_harness as reading
original = (reading.seed, reading.Workload, reading.parameters, reading.relay)
modules = [importlib.import_module(name) for name in sys.argv[2:]]
assert (reading.seed, reading.Workload, reading.parameters, reading.relay) == original
for module in modules:
    params = module.parameters(Path('synthetic'), source_dir=Path('selected-source'),
                               resume=True, instrument=True, cache_build_id='selected-build')
    assert params.command == sys.executable
    expected_args = ['-X', 'utf8', str(Path(module.__file__).resolve()),
                     'mcp', 'synthetic', '--source-dir', 'selected-source',
                     '--cache-build-id', 'selected-build', '--resume', '--instrument']
    if module.__name__ == 'staged_accounting_agent_harness':
        expected_args.extend(['--scenario', 'dependency'])
    assert params.args == expected_args
"""
    subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parent), *order],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize("module,scenario", [
    (reading, None), (work_draft, None), (staged, None), (question, None),
    (staged, "dependency"), (staged, "stage4_recovery"),
])
@pytest.mark.parametrize("mode", ("mcp", "host"))
def test_cli_dispatch_passes_variant_factories_and_options(
    tmp_path, monkeypatch, module, scenario, mode,
):
    calls = []

    def process(directory, **options):
        calls.append((directory, options))

    async def relay(directory, **options):
        calls.append((directory, options))

    monkeypatch.setattr(reading, "mcp_process", process)
    monkeypatch.setattr(reading, "relay", relay)
    monkeypatch.setattr(work_draft, "relay", relay)
    source = tmp_path / "selected-source"
    monkeypatch.setattr(sys, "path", list(sys.path))
    argv = [
        module.__file__, mode, str(tmp_path), "--source-dir", str(source),
        "--resume", "--instrument", "--cache-build-id", "selected-build",
    ]
    if scenario is not None:
        argv.extend(["--scenario", scenario])
    monkeypatch.setattr(sys, "argv", argv)
    module.main()
    directory, options = calls.pop()
    assert directory == tmp_path.resolve() and not calls
    assert options["resume"] and options["instrument"]
    assert sys.path[0] == str(source.resolve())
    if mode == "mcp":
        assert options["cache_build_id"] == "selected-build"
        factory, expected = options["seed_factory"], None if module is reading else module.seed
        assert options["workload_factory"] is (
            None if module is reading else work_draft.Workload
        )
    else:
        assert options["source_dir"] == source
        factory = options["parameters_factory"]
        expected = None if module is reading else module.parameters
    if module is staged:
        assert isinstance(factory, partial) and factory.func is expected
        assert factory.args == () and factory.keywords == {"scenario": scenario or "dependency"}
    else:
        assert factory is expected


@pytest.mark.parametrize("module", (reading, work_draft))
def test_relay_uses_explicit_parameter_factory(tmp_path, module):
    calls = []

    class ParametersSelected(Exception):
        pass

    def parameters(directory, **options):
        calls.append((directory, options))
        raise ParametersSelected

    directory = tmp_path / "relay"
    with pytest.raises(ParametersSelected):
        asyncio.run(module.relay(
            directory, source_dir=tmp_path / "source", instrument=True,
            parameters_factory=parameters,
        ))
    assert calls == [(directory, {
        "source_dir": tmp_path / "source", "resume": False, "instrument": True,
    })]
