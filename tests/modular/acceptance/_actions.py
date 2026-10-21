"""The fixed action table maps test verbs onto real public operations."""

import base64
import json

from . import _files
from ._observations import document_observations, normalized
from ._support import COMPANY, SPEC, atomic, expense


def business(self, step, request):
    if self.stopped:
        # C08's next explicit command is the only replay of the lost reply.
        self.restart()
    before = self.snapshot()
    result = self.host.call(
        "apply", request=SPEC["requests"][step["request"]], fail_at=step.get("fail_at")
    )
    if "stopped" in result:
        self.stopped = True
        evidence = json.loads((self.root / "crash.json").read_text(encoding="utf-8"))
        return dict(
            process_stopped=result["stopped"] in (81, 82),
            response_lost=result["stopped"] == 83,
            **evidence,
        )
    after = self.snapshot()
    observed = self.observe(after)
    error = result.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    observed.update(
        accepted=result.get("completion") == "complete",
        code=code,
        formal_changes=int(atomic(before) != atomic(after)),
    )
    observed["_before"] = before
    observed["_after"] = after
    if self.case["id"] == "C07":
        issue = self.host.call("issues", scope="materials:2026-01")
        observed.update(
            issue_scope_head=issue["head"],
            issue_ids=[item["issue_id"] for item in issue["issues"]],
            issue_count=len(issue["issues"]),
        )
    return observed


def observe(self, step, request):
    action = step["invoke"]
    if action == "observe_atomic_scope":
        observed = self.observe()
        observed["reading_targets"] = self.reading_heads(prepare=True)
        observed["scope"]["reading_targets"] = observed["reading_targets"]
        return observed
    observed = self.observe()
    response = self.host.call(
        "ready",
        parameters=dict(company_id=COMPANY, period="2026-01", section="vouchers", limit=500),
    )
    vouchers = response["data"]["collections"]["vouchers"]["items"]
    identity = request["business_ids"][0]
    current = next(
        item
        for item in self.latest_snapshot["businesses"]
        if item["state"]["business_id"] == identity
    )
    result_id = current["state"]["results"][0]["result_id"]
    candidates = [
        item
        for item in vouchers
        if json.loads(base64.urlsafe_b64decode(item["voucher_version_id"] + "=="))["result_id"]
        == result_id
    ]
    assert candidates, (identity, vouchers)
    observed.update(
        voucher_version_id=candidates[-1]["voucher_version_id"],
        source_head=response["snapshot_version"],
    )
    return observed


def period(self, step, request):
    result = self.host.call("period", request=request)
    assert "error" not in result, result
    return {
        **result,
        "closed": result["state"] == "closed",
        "approval_records": self._no_process_archives("application"),
    }


def frozen(self, step, request):
    locator = json.loads(base64.urlsafe_b64decode(request["voucher_version_id"] + "=="))
    values = self.snapshot()["vouchers"]
    matched = [
        value
        for value in values
        if value["voucher_id"] == locator["voucher_id"]
        and value["result"]["result_id"] == locator["result_id"]
        and value["result"]["generation"] == locator["generation"]
    ]
    assert len(matched) == 1
    return dict(expense_total_fen=expense(matched), number=matched[0]["number"])


def brief(self, step, request):
    response = self.host.call("ready", parameters=request)
    return dict(
        snapshot_version=response["snapshot_version"],
        expense_total_fen=response["data"]["position"]["month_expense_fen"],
        voucher_count=response["data"]["voucher_count"],
    )


def vouchers(self, step, request):
    response = self.host.call("http", page="brief", parameters=request)
    if response["http_status"] != 200:
        body = response["body"]
        return dict(
            http_status=response["http_status"],
            code=body.get("code"),
            response_items_absent="response" not in body and "items" not in body,
            old_generation_rejected_even_with_current_page_head=body.get("code")
            == "dashboard_snapshot_changed",
        )
    body = response["body"]
    data = body.get("response", body)["data"]
    page = data["collections"]["vouchers"]
    full = self.host.call("ready", parameters=dict(company_id=COMPANY, period=request["period"]))
    return {
        **page["page"],
        "complete_expense_total_fen": full["data"]["position"]["month_expense_fen"],
    }


def all_vouchers(self, step, request):
    response = self.host.call(
        "ready", parameters=dict(company_id=COMPANY, period=request["period"])
    )
    version, cursor, items, counts = response["snapshot_version"], None, [], []
    while True:
        params = dict(
            company_id=COMPANY,
            period=request["period"],
            section="vouchers",
            limit=request["first_limit"] if not counts else request["continuation_limit"],
            expected_version=version,
        )
        if cursor:
            params["cursor"] = cursor
        result = self.host.call("http", page="brief", parameters=params)
        assert result["http_status"] == 200, result
        data = result["body"].get("response", result["body"])
        assert data["snapshot_version"] == version
        page = data["data"]["collections"]["vouchers"]
        items.extend(page["items"])
        counts.append(len(page["items"]))
        cursor = page["page"]["next_cursor"]
        if not page["page"]["has_more"]:
            break
    return dict(
        voucher_count=len(items),
        unique_voucher_count=len({item["voucher_version_id"] for item in items}),
        expense_total_fen=response["data"]["position"]["month_expense_fen"],
        returned_pages=counts,
        has_more_after_last=page["page"]["has_more"],
        next_cursor_after_last=cursor,
    )


def issues(self, step, request):
    result = self.host.call("issues", scope=request["scope"])
    return dict(head=result["head"], issue_ids=[item["issue_id"] for item in result["issues"]])


def documents(self, step, request):
    action = step["invoke"]
    status = (
        self.host.call("documents", request=request) if action == "accounting_documents" else {}
    )
    published = self.host.call("published")
    observed = {**document_observations(published, self.root), **status}
    if status:
        observed["publication_ack_matches_manifest"] = (
            status["set_id"] == published["set_id"] and status["state"] == "ready"
        )
    if step["id"] == "finish" and self.case["id"] == "C06":
        after = self.snapshot()
        changed = normalized(after)["formal_results"] != self.resumed_formal_results
        observed["reposted_voucher_count"] = max(self._voucher_reposts(after), int(changed))
        assert not changed, "Document resumption must not advance any adopted generation"
    return observed


def prepare_models(self, step, request):
    observed = self.observe()
    return {
        **self.host.call("heads"),
        **{key: observed[key] for key in ("formal_results", "document_set_id")},
    }


def restart(self, step, request):
    assert request.get("calculation_revisions_unchanged", True) is True
    assert request.get("public_contracts_unchanged", True) is True
    self.restart(request.get("implementation_build_label"))
    if self.case["id"] == "C06":
        resumed = self.snapshot()
        business = next(
            item
            for item in resumed["businesses"]
            if item["state"]["business_id"] == SPEC["ids"]["expense_a"]
        )
        assert business["fact"]["period"] == "2026-02"
        self.resumed_formal_results = normalized(resumed)["formal_results"]
    if self.case["id"] == "C11":
        receipt = self.host.call("job", task_id=self.captures["create.task_id"])
        assert receipt["state"] == "pending", receipt
    if self.case["id"] == "C09":
        assert self.host.call("run_pending") == []
        observed = {**self.host.call("heads"), **self.observe()}
        observed["reposted_result_count"] = int(
            observed["formal_results"] != self.captures["ready.formal_results"]
        )
        assert self.host.call("run_pending") == []
        return observed
    return {}


def backup(self, step, request):
    action = step["invoke"]
    result = self.host.call(
        "backup",
        request=request,
        pause=action.startswith("start_"),
        fail_at=step.get("fail_at"),
    )
    if "stopped" in result:
        self.stopped = True
        evidence = json.loads((self.root / "crash.json").read_text(encoding="utf-8"))
        return dict(process_stopped=result["stopped"] == 84, **evidence)
    if action == "accounting_backup_wait_verified":
        return _files.finish_backup(self, result["task_id"])
    return result


def wait_file(self, step, request):
    return _files.finish_backup(self, request["task_id"])


def stop(self, step, request):
    self.close()
    self.stopped = True
    return dict(source_host_stopped=self.host.process.poll() == 0)


def isolated(self, step, request):
    return _files.isolated(self, request)


ACTIONS = {
    "accounting_apply": business,
    "observe_business": observe,
    "observe_atomic_scope": observe,
    "accounting_period": period,
    "observe_frozen_voucher": frozen,
    "dashboard_brief_ready": brief,
    "dashboard_exact_voucher": vouchers,
    "dashboard_brief_vouchers": vouchers,
    "dashboard_all_vouchers_same_new_head": all_vouchers,
    "observe_issue_scope": issues,
    "accounting_documents": documents,
    "read_published_documents": documents,
    "prepare_all_selected_models": prepare_models,
    "restart_synthetic_host": restart,
    "accounting_backup": backup,
    "accounting_backup_wait_verified": backup,
    "start_backup_and_pause_after_snapshot_lease": backup,
    "wait_current_file_task": wait_file,
    "resume_backup": wait_file,
    "stop_synthetic_source_host": stop,
    "accounting_restore": isolated,
    "accounting_rebuild": isolated,
}
