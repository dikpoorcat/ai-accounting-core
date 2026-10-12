"""Immutable development-runtime identity captured and pinned for one process."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

_LOCK = threading.Lock()
_SNAPSHOT = None
_RUNTIME_PACKAGE_ROOT = None
_RUNTIME_SOURCE_DIGEST = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    build_id: str
    company_schema_fingerprint: str
    catalog_schema_fingerprint: str
    contracts_digest: str
    static_assets_digest: str
    runtime_source_digest: str
    runtime_path: str | None
    static_assets: tuple[tuple[str, bytes], ...]

    def public(self):
        result = asdict(self)
        result.pop("static_assets")
        return result


def _hash_files(files, package):
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(package).as_posix()):
        digest.update(path.relative_to(package).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_digest(package, *, exclude=()):
    excluded = tuple(item.strip("/") for item in exclude)
    digest = hashlib.sha256()
    files = []
    for path in package.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(package).as_posix()
        if any(relative == item or relative.startswith(item + "/") for item in excluded):
            continue
        files.append(path)
    files.sort(key=lambda item: item.relative_to(package).as_posix())
    for path in files:
        relative = path.relative_to(package).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _switch_loaded_package_paths(package_path):
    for name, module in tuple(sys.modules.items()):
        if name != "ai_accounting" and not name.startswith("ai_accounting."):
            continue
        suffix = name.split(".")[1:]
        if hasattr(module, "__path__"):
            module_path = package_path.joinpath(*suffix)
            origin = module_path / "__init__.py"
            if not module_path.is_dir() or not origin.is_file():
                continue
            module.__path__ = [str(module_path)]
        else:
            origin = package_path.joinpath(*suffix).with_suffix(".py")
            if not origin.is_file():
                continue
        module.__file__ = str(origin)
        spec = getattr(module, "__spec__", None)
        if spec is not None:
            spec.origin = str(origin)
            if spec.submodule_search_locations is not None:
                spec.submodule_search_locations[:] = [str(module_path)]


def materialize_runtime_package(root):
    """Copy code and dashboard assets into one owner-private runtime snapshot."""
    import ai_accounting

    from .permissions import ensure_private_directory, private_temporary_directory

    source = Path(ai_accounting.__file__).resolve().parent
    checkout = source.parents[1]
    dashboard_source = checkout / "frontend" / "dist"
    has_dashboard = dashboard_source.is_dir() and (dashboard_source / "index.html").is_file()
    source_digest = _tree_digest(source, exclude=("static/dashboard",) if has_dashboard else ())
    dashboard_digest = _tree_digest(dashboard_source) if has_dashboard else None
    snapshots = ensure_private_directory(Path(root) / ".runtime-snapshots", parents=True)
    package_path = None
    with private_temporary_directory(snapshots, prefix=".runtime-copy-") as staging:
        copied = staging / "ai_accounting"
        shutil.copytree(
            source,
            copied,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        copied_source_digest = _tree_digest(
            copied, exclude=("static/dashboard",) if has_dashboard else ()
        )
        if copied_source_digest != source_digest:
            raise RuntimeError("captured runtime source failed its pre-copy content check")
        if has_dashboard:
            dashboard_target = copied / "static" / "dashboard"
            if dashboard_target.exists():
                shutil.rmtree(dashboard_target)
            shutil.copytree(dashboard_source, dashboard_target)
            if _tree_digest(dashboard_target) != dashboard_digest:
                raise RuntimeError("captured dashboard assets failed their content check")
        source_digest = _tree_digest(staging)
        for path in copied.rglob("*"):
            if path.is_file():
                try:
                    path.chmod(0o444)
                except OSError:
                    pass
        destination = snapshots / source_digest
        package_path = destination / "ai_accounting"
        if destination.exists():
            if not package_path.is_dir() or _tree_digest(destination) != source_digest:
                raise RuntimeError(
                    "captured runtime destination already exists with different contents"
                )
        else:
            os.replace(staging, destination)
    if package_path is None or not package_path.is_dir():
        raise RuntimeError("captured runtime package is missing after publication")
    if _tree_digest(package_path.parent) != source_digest:
        raise RuntimeError("captured runtime directory changed after publication")

    _switch_loaded_package_paths(package_path)
    return package_path, source_digest


def _read_active_runtime(snapshots):
    from .permissions import assert_private_directory, assert_private_file

    pointer = snapshots / "active"
    if not pointer.exists():
        return None
    assert_private_directory(snapshots)
    assert_private_file(pointer)
    source_digest = pointer.read_text(encoding="ascii").strip()
    if len(source_digest) != 64 or any(ch not in "0123456789abcdef" for ch in source_digest):
        raise RuntimeError("captured runtime pointer is invalid")
    snapshot_root = snapshots / source_digest
    package_path = snapshot_root / "ai_accounting"
    if not package_path.is_dir() or _tree_digest(snapshot_root) != source_digest:
        raise RuntimeError("captured runtime is missing or has changed")
    return package_path, source_digest


def _write_active_runtime(snapshots, source_digest):
    from .permissions import create_private_file, ensure_private_file

    pointer = snapshots / "active"
    temporary = snapshots / ("active-" + uuid.uuid4().hex + ".tmp")
    create_private_file(temporary)
    try:
        temporary.write_text(source_digest + "\n", encoding="ascii")
        if pointer.exists():
            ensure_private_file(pointer)
        os.replace(temporary, pointer)
    finally:
        if temporary.exists():
            temporary.unlink()


def activate_runtime_package(root):
    """Use the persisted runtime after restart, or capture the current code once."""
    from .permissions import ensure_private_directory

    snapshots = ensure_private_directory(Path(root) / ".runtime-snapshots", parents=True)
    selected = _read_active_runtime(snapshots)
    if selected is None:
        selected = materialize_runtime_package(root)
        _write_active_runtime(snapshots, selected[1])
    configure_runtime_package(*selected)
    _switch_loaded_package_paths(selected[0])
    return selected


def bootstrap_runtime(root):
    """Select an already activated package before importing daemon modules."""

    from .permissions import assert_private_directory

    root = Path(root)
    snapshots = root / ".runtime-snapshots"
    if not snapshots.exists():
        return None
    assert_private_directory(snapshots)
    selected = _read_active_runtime(snapshots)
    if selected is None:
        return None
    package_path, source_digest = selected
    configure_runtime_package(package_path, source_digest)
    _switch_loaded_package_paths(package_path)
    return selected


def configure_runtime_package(package_path, source_digest):
    global _RUNTIME_PACKAGE_ROOT, _RUNTIME_SOURCE_DIGEST
    with _LOCK:
        _RUNTIME_PACKAGE_ROOT = Path(package_path).resolve()
        _RUNTIME_SOURCE_DIGEST = source_digest


def capture_runtime_snapshot(*, refresh=False, bundle=None):
    """Return the process-pinned build, contract and static-resource identity.

    ``refresh=True`` is reserved for an explicit development update checkpoint.
    Ordinary service discovery must use the already pinned snapshot.
    """
    global _SNAPSHOT
    with _LOCK:
        if _SNAPSHOT is not None and not refresh:
            return _SNAPSHOT
        from .build import calculator_build_id
        from .contract_files import production_contract_file_names

        if bundle is None:
            from .schema_bundle import production_bundle

            bundle = production_bundle()

        package = (
            _RUNTIME_PACKAGE_ROOT
            if _RUNTIME_PACKAGE_ROOT is not None
            else Path(__file__).resolve().parents[1]
        )
        contracts = [
            package / "kernel" / "schema_contracts" / name
            for name in production_contract_file_names()
        ]
        assets = [
            path
            for path in package.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".xlsx", ".json", ".html", ".css", ".js"}
            and "schema_contracts" not in path.parts
        ]
        _SNAPSHOT = RuntimeSnapshot(
            build_id=calculator_build_id(package),
            company_schema_fingerprint=bundle.database_format("company")["fingerprint"],
            catalog_schema_fingerprint=bundle.database_format("catalog")["fingerprint"],
            contracts_digest=_hash_files(contracts, package),
            static_assets_digest=_hash_files(assets, package),
            runtime_source_digest=(
                _RUNTIME_SOURCE_DIGEST
                if _RUNTIME_SOURCE_DIGEST is not None
                else _tree_digest(package)
            ),
            runtime_path=str(package) if _RUNTIME_SOURCE_DIGEST is not None else None,
            static_assets=tuple(
                (path.relative_to(package).as_posix(), path.read_bytes())
                for path in sorted(assets, key=lambda item: item.relative_to(package).as_posix())
            ),
        )
        return _SNAPSHOT


def explicit_update_checkpoint():
    """Advance the in-process snapshot after a caller explicitly accepts an update."""
    return capture_runtime_snapshot(refresh=True)


def pinned_build_id():
    """Identity used by long-lived runtime operations after startup capture."""
    return capture_runtime_snapshot().build_id


def static_resource_bytes(relative_path):
    """Read a package static resource from the process-pinned runtime snapshot."""
    snapshot = capture_runtime_snapshot()
    for name, content in snapshot.static_assets:
        if name == relative_path:
            return content
    raise FileNotFoundError("resource is not part of the pinned runtime snapshot")
