"""The controlled packager checks exact files before ZIP and before relocation execution."""

import json

import pytest

from scripts.package_local_kernel import sha256, verify_package_inventory


def _package(tmp_path):
    root = tmp_path / "package"
    (root / "app").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "app/core.py").write_bytes(b"print('synthetic package')\n")
    (root / "runtime/python312._pth").write_bytes(b"../app\nimport site\n")
    manifest = {
        "files": {
            path.relative_to(root).as_posix(): {
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in (root / "app/core.py", root / "runtime/python312._pth")
        }
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root, manifest, sha256(root / "manifest.json")


def test_package_inventory_accepts_exact_software_files(tmp_path):
    root, manifest, manifest_sha = _package(tmp_path)
    verify_package_inventory(root, manifest, manifest_sha)


@pytest.mark.parametrize("change", ["extra", "cache", "missing", "tampered", "manifest"])
def test_package_inventory_rejects_unknown_missing_or_changed_files(tmp_path, change):
    root, manifest, manifest_sha = _package(tmp_path)
    if change == "extra":
        (root / "runtime/Lib").mkdir()
        (root / "runtime/Lib/injected.pth").write_text("import injected\n", encoding="utf-8")
    elif change == "cache":
        (root / "app/__pycache__").mkdir()
        (root / "app/__pycache__/core.pyc").write_bytes(b"synthetic cache")
    elif change == "missing":
        (root / "app/core.py").unlink()
    elif change == "tampered":
        (root / "app/core.py").write_bytes(b"print('modified package')\n")
    else:
        (root / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory|changed"):
        verify_package_inventory(root, manifest, manifest_sha)


def test_package_inventory_rejects_file_symlink(tmp_path):
    root, manifest, manifest_sha = _package(tmp_path)
    try:
        (root / "extra.py").symlink_to(root / "app/core.py")
    except OSError as error:
        pytest.skip(f"This host cannot create test symlinks: {error}")
    with pytest.raises(ValueError, match="link"):
        verify_package_inventory(root, manifest, manifest_sha)
