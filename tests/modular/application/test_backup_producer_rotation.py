"""Every publication break preserves verified packages and supports exact resumption."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
from zipfile import ZipFile

import pytest

from ai_accounting_modular.application import _backup_rotation
from ai_accounting_modular.application._backup_rotation import Rotation
from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.files.public import ProductionError

from .test_backup_producer import backup_case as backup_case


class Interrupted(BaseException):
    pass


def _advance(case, amount):
    case.advance(str(amount))
    plan, context = case.capture()
    delivered = case.install(plan, context)
    return plan, context, delivered.read_bytes()


def _paths(context):
    root = context.company.backup_directory
    current, previous = Rotation.names(context.company.credit_code)
    return root / current, root / previous


def _rewrite(path, *, manifest=None, corrupt=False):
    with ZipFile(path) as archive:
        entries = [(item.filename, archive.read(item)) for item in archive.infolist()]
    with ZipFile(path, "w") as archive:
        for name, raw in entries:
            if manifest is not None and name == "manifest.json":
                raw = (canonical_json(manifest) + "\n").encode()
            if corrupt and name == "runtime/company.sqlite3":
                raw += b"deliberate corruption"
            archive.writestr(name, raw)


@pytest.mark.parametrize("interruption", ["before_current", "after_current", "after_previous"])
def test_retry_finishes_one_roll_and_does_not_roll_current_into_previous_twice(
    backup_case, monkeypatch, interruption
):
    case = backup_case
    _advance(case, 10)
    _, _, old = _advance(case, 20)
    previous_plan, previous_context = case.capture()
    current, previous = _paths(previous_context)
    before_previous = previous.read_bytes()
    case.advance("30")
    plan, context = case.capture()
    original_finish, original_copy = Rotation.finish, _backup_rotation.copy_bytes
    with monkeypatch.context() as patch:
        if interruption == "before_current":

            def copy(value, root, relative, **kwargs):
                if relative == current.name:
                    raise Interrupted()
                return original_copy(value, root, relative, **kwargs)

            patch.setattr(_backup_rotation, "copy_bytes", copy)
        else:

            def finish(self, pending, target):
                if interruption == "after_previous":
                    original_finish(self, pending, target)
                raise Interrupted()

            patch.setattr(Rotation, "finish", finish)
        with pytest.raises(Interrupted):
            case.install(plan, context)
    if interruption == "before_current":
        assert current.read_bytes() == old
        assert previous.read_bytes() == before_previous
    else:
        assert current.read_bytes() != old
        assert previous.read_bytes() == (
            old if interruption == "after_previous" else before_previous
        )
    installed = case.install(plan, context).read_bytes()
    assert previous.read_bytes() == old
    assert case.install(plan, context).read_bytes() == installed
    assert previous.read_bytes() == old
    assert not list(current.parent.glob(".backup--*.zip"))
    # Independent earlier capture remains intact; it did not become a rolling log.
    case.producer.cleanup(previous_context, keep_capture=False)
    assert previous_plan["scope_head"] != plan["scope_head"]


def test_new_c_first_completes_interrupted_b_then_rolls_c_b(backup_case, monkeypatch):
    case = backup_case
    _advance(case, 10)
    _, _, a_bytes = _advance(case, 20)
    case.advance("30")
    b_plan, b_context = case.capture()
    with monkeypatch.context() as patch:
        patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            case.install(b_plan, b_context)
    current, previous = _paths(b_context)
    b_bytes = current.read_bytes()
    assert previous.read_bytes() != a_bytes
    c_plan, c_context, c_bytes = _advance(case, 40)
    assert c_plan["scope_head"] != b_plan["scope_head"]
    assert current.read_bytes() == c_bytes
    assert previous.read_bytes() == b_bytes
    assert case.install(c_plan, c_context).read_bytes() == c_bytes
    assert previous.read_bytes() == b_bytes
    assert not list(current.parent.glob(".backup--*.zip"))


def test_damaged_owned_current_is_replaced_without_destroying_valid_previous(backup_case):
    case = backup_case
    _advance(case, 10)
    _, context, _ = _advance(case, 20)
    current, previous = _paths(context)
    preserved = previous.read_bytes()
    _rewrite(current, corrupt=True)
    _, _, installed = _advance(case, 30)
    assert current.read_bytes() == installed
    assert previous.read_bytes() == preserved


def test_damaged_owned_previous_can_be_replaced_by_verified_current(backup_case):
    case = backup_case
    _advance(case, 10)
    _, context, old = _advance(case, 20)
    current, previous = _paths(context)
    _rewrite(previous, corrupt=True)
    _advance(case, 30)
    assert current.read_bytes() != old
    assert previous.read_bytes() == old


@pytest.mark.parametrize(
    "target,foreign",
    [("current", False), ("current", True), ("previous", False), ("previous", True)],
)
def test_foreign_or_unidentified_destination_is_never_overwritten(backup_case, target, foreign):
    case = backup_case
    first_plan, _, _ = _advance(case, 10)
    _, context, _ = _advance(case, 20)
    current, previous = _paths(context)
    destination = current if target == "current" else previous
    if foreign:
        manifest = deepcopy(first_plan["manifest"])
        manifest.update(company_id=str(uuid4()), database_id=str(uuid4()))
        _rewrite(destination, manifest=manifest)
    else:
        destination.write_bytes(b"unidentified user file")
    before = current.read_bytes(), previous.read_bytes()
    case.advance("30")
    plan, context = case.capture()
    with pytest.raises(ProductionError) as failure:
        case.install(plan, context)
    assert failure.value.code == "backup_collision"
    assert not failure.value.retryable
    assert (current.read_bytes(), previous.read_bytes()) == before


def test_untouched_foreign_previous_does_not_block_first_current_installation(backup_case):
    case = backup_case
    plan, context = case.capture()
    current, previous = _paths(context)
    previous.parent.mkdir(parents=True, exist_ok=True)
    previous.write_bytes(b"do not touch this unrelated previous file")
    untouched = previous.read_bytes()
    case.install(plan, context)
    assert current.exists()
    assert previous.read_bytes() == untouched


@pytest.mark.parametrize("unfinished", [False, True])
def test_late_publication_of_older_point_cannot_touch_newer_package_or_pending_roll(
    backup_case, monkeypatch, unfinished
):
    case = backup_case
    old, old_context = case.capture()
    artifact = case.producer.produce(old, old_context)
    case.advance("200000")
    newer, context = case.capture()
    if unfinished:
        # Install a predecessor so the newer publication has a genuine roll.
        case.producer.publish(old, artifact, old_context)
        with monkeypatch.context() as patch:
            patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
            with pytest.raises(Interrupted):
                case.install(newer, context)
    else:
        case.install(newer, context)
    current, previous = _paths(context)
    before = {path.name: path.read_bytes() for path in current.parent.iterdir()}
    with pytest.raises(ProductionError) as failure:
        case.producer.publish(old, artifact, old_context)
    assert failure.value.code == "backup_superseded"
    assert not failure.value.retryable
    assert {path.name: path.read_bytes() for path in current.parent.iterdir()} == before
    case.install(newer, context)
    assert current.exists()


@pytest.mark.parametrize("next_point", [False, True])
def test_damaged_installed_b_does_not_block_finishing_verified_a_and_installing_valid_package(
    backup_case, monkeypatch, next_point
):
    case = backup_case
    _advance(case, 10)
    _, _, a_bytes = _advance(case, 20)
    case.advance("30")
    b_plan, b_context = case.capture()
    with monkeypatch.context() as patch:
        patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            case.install(b_plan, b_context)
    current, previous = _paths(b_context)
    _rewrite(current, corrupt=True)
    if next_point:
        _, _, result = _advance(case, 40)
    else:
        result = case.install(b_plan, b_context).read_bytes()
    assert current.read_bytes() == result
    assert previous.read_bytes() == a_bytes
    assert not list(current.parent.glob(".backup--*.zip"))


@pytest.mark.parametrize("foreign", [False, True])
def test_pending_roll_never_uses_unknown_or_foreign_current_header_to_overwrite_previous(
    backup_case, monkeypatch, foreign
):
    case = backup_case
    _advance(case, 10)
    _advance(case, 20)
    case.advance("30")
    b_plan, b_context = case.capture()
    with monkeypatch.context() as patch:
        patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            case.install(b_plan, b_context)
    current, _ = _paths(b_context)
    if foreign:
        changed = deepcopy(b_plan["manifest"])
        changed.update(company_id=str(uuid4()), database_id=str(uuid4()))
        _rewrite(current, manifest=changed)
    else:
        current.write_bytes(b"unidentified current beside genuine pending")
    before = {path.name: path.read_bytes() for path in current.parent.iterdir()}
    case.advance("40")
    c_plan, c_context = case.capture()
    with pytest.raises(ProductionError) as failure:
        case.install(c_plan, c_context)
    assert failure.value.code == "backup_collision"
    assert {path.name: path.read_bytes() for path in current.parent.iterdir()} == before


def test_publication_cleans_only_owned_plain_orphan_staging_files(backup_case):
    case = backup_case
    _, context, a_bytes = _advance(case, 10)
    current, previous = _paths(context)
    rotation = Rotation(context, None)
    partial = current.parent / (rotation.temporary_prefix + uuid4().hex + ".tmp")
    complete = current.parent / (rotation.temporary_prefix + uuid4().hex + ".tmp")
    partial.write_bytes(b"an OS kill left a partially written copy")
    complete.write_bytes(a_bytes)
    unrelated = [
        current.parent / ".backup-write-unknown.tmp",
        current.parent / (".backup--" + str(uuid4()) + "--write--" + uuid4().hex + ".tmp"),
        current.parent / (rotation.temporary_prefix + "not-a-uuid.tmp"),
    ]
    for path in unrelated:
        path.write_bytes(b"unrelated")
    directory = current.parent / (rotation.temporary_prefix + uuid4().hex + ".tmp")
    directory.mkdir()
    (directory / "keep").write_bytes(b"do not recursively clean a directory")
    _advance(case, 20)
    assert previous.read_bytes() == a_bytes
    assert not partial.exists() and not complete.exists()
    assert all(path.read_bytes() == b"unrelated" for path in unrelated)
    assert (directory / "keep").read_bytes() == b"do not recursively clean a directory"


@pytest.mark.parametrize("damaged", ["capture", "candidate"])
def test_failed_new_package_never_changes_existing_verified_current_or_previous(
    backup_case, damaged
):
    case = backup_case
    _advance(case, 10)
    _, context, _ = _advance(case, 20)
    current, previous = _paths(context)
    before = current.read_bytes(), previous.read_bytes()
    case.advance("30")
    plan, context = case.capture()
    with case.producer.recover_lease(plan, context):
        if damaged == "capture":
            database = context.capture_root / "runtime/company.sqlite3"
            raw = database.read_bytes()
            database.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
            with pytest.raises(ProductionError) as failure:
                case.producer.produce(plan, context)
            assert failure.value.code == "capture_invalid"
            assert not list((context.task_root / "work").iterdir())
        else:
            artifact = case.producer.produce(plan, context)
            _rewrite(context.task_root / artifact.relative_path, corrupt=True)
            with pytest.raises(ContractError):
                case.producer.publish(plan, artifact, context)
    assert (current.read_bytes(), previous.read_bytes()) == before
    assert not list(current.parent.glob(".backup--*.zip"))


@pytest.mark.parametrize("next_point", [False, True])
def test_damaged_owned_pending_is_not_promoted_and_does_not_permanently_block_new_backups(
    backup_case, monkeypatch, next_point
):
    case = backup_case
    _advance(case, 10)
    _advance(case, 20)
    case.advance("30")
    b_plan, b_context = case.capture()
    with monkeypatch.context() as patch:
        patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            case.install(b_plan, b_context)
    current, previous = _paths(b_context)
    b_bytes, preserved_previous = current.read_bytes(), previous.read_bytes()
    pending = next(current.parent.glob(".backup--*.zip"))
    _rewrite(pending, corrupt=True)
    if next_point:
        _advance(case, 40)
        assert previous.read_bytes() == b_bytes
    else:
        assert case.install(b_plan, b_context).read_bytes() == b_bytes
        assert previous.read_bytes() == preserved_previous
    assert not pending.exists()


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("occupied", [False, True])
def test_unknown_or_foreign_pending_only_blocks_when_the_exact_path_must_be_overwritten(
    backup_case, foreign, occupied
):
    case = backup_case
    _advance(case, 10)
    existing, context, a_bytes = _advance(case, 20)
    current, previous = _paths(context)
    case.advance("30")
    plan, context = case.capture()
    rotation = Rotation(context, None)
    wanted = plan["scope_head"] if occupied else "0" * 64
    pending = current.parent / (
        rotation.prefix
        + context.company.credit_code
        + "--"
        + wanted
        + "--"
        + sha256(a_bytes).hexdigest()
        + ".zip"
    )
    if foreign:
        pending.write_bytes(a_bytes)
        header = deepcopy(existing["manifest"])
        header.update(company_id=str(uuid4()), database_id=str(uuid4()))
        _rewrite(pending, manifest=header)
    else:
        pending.write_bytes(b"unidentified pending-shaped file")
    preserved = pending.read_bytes()
    if occupied:
        before = {path.name: path.read_bytes() for path in current.parent.iterdir()}
        with pytest.raises(ProductionError) as failure:
            case.install(plan, context)
        assert failure.value.code == "backup_collision"
        assert {path.name: path.read_bytes() for path in current.parent.iterdir()} == before
    else:
        case.install(plan, context)
        assert previous.read_bytes() == a_bytes
    assert pending.read_bytes() == preserved


@pytest.mark.parametrize("mismatch", ["scope", "digest"])
def test_valid_owned_pending_with_inconsistent_handoff_is_preserved_and_reported(
    backup_case, monkeypatch, mismatch
):
    case = backup_case
    _advance(case, 10)
    _advance(case, 20)
    case.advance("30")
    plan, context = case.capture()
    with monkeypatch.context() as patch:
        patch.setattr(Rotation, "finish", lambda *args: (_ for _ in ()).throw(Interrupted()))
        with pytest.raises(Interrupted):
            case.install(plan, context)
    current, _ = _paths(context)
    rotation = Rotation(context, None)
    pending = next(current.parent.glob(".backup--*.zip"))
    code, scope, digest = rotation.pattern.fullmatch(pending.name).groups()
    if mismatch == "scope":
        scope = "0" * 64
    else:
        digest = "0" * 64
    pending.rename(
        current.parent / (rotation.prefix + code + "--" + scope + "--" + digest + ".zip")
    )
    before = {path.name: path.read_bytes() for path in current.parent.iterdir()}
    case.advance("40")
    fresh, following = case.capture()
    with pytest.raises(ProductionError) as failure:
        case.install(fresh, following)
    assert failure.value.code == "backup_transition_invalid"
    assert {path.name: path.read_bytes() for path in current.parent.iterdir()} == before
