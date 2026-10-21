"""One strict candidate proof follows actual independent bytes to publication."""

from hashlib import sha256

from ._support import report_plan


def test_publish_receives_the_strictly_verified_snapshot_and_download_hashes_it_once(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "one byte proof")
    evidence = []

    def inspect(plan, artifact, context):
        assert artifact.claim.company_id == company.company_id
        assert artifact.claim.task_id == context.task_id == task["task_id"]
        assert artifact.claim.scope_head == plan["scope_head"]
        assert not artifact.stream.writable()
        raw = artifact.stream.read()
        assert sha256(raw).hexdigest() == artifact.sha256
        assert len(raw) == artifact.byte_size
        evidence.append(raw)
        return context.task_root / artifact.relative_path

    producer.publish_hook = inspect
    done = files.run(company.company_id, task["task_id"])
    assert done["state"] == "succeeded", done
    assert len([event for event in producer.events if event[0] == "verify"]) == 1
    with files.verified_download(company.company_id, task["task_id"]) as download:
        assert download.stream.read() == evidence[0]
    assert len([event for event in producer.events if event[0] == "verify"]) == 1
