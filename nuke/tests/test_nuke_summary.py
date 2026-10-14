"""summarize.jq / result.jq / table.jq against REAL aws-nuke v3.68.3 JSON logs and synthetic ones.

The fixtures/real-v3.68.3-*.jsonl files were produced by the released aws-nuke v3.68.3 linux-amd64
binary (sha256 ec43c22b...) running against a moto server via custom endpoints - the field names
(type, owner, name, state, resource_type, msg, level, ...) are therefore the real ones (libnuke v1.3.0).
"""
import json
import subprocess
import shutil

import pytest

from nuke_test_support import FIXTURES, NUKE_DIR, run_jq

SUMMARIZE = NUKE_DIR / "summarize.jq"
RESULT = NUKE_DIR / "result.jq"
TABLE = NUKE_DIR / "table.jq"
NOISE = ["FMSPolicy", "FMSNotificationChannel", "ShieldProtection"]


def summarize(path=None, *, text=None, applied=False, exit_code=0, noise=NOISE):
    args = [("--arg", "applied", "true" if applied else "false"), ("--argjson", "exit_code", exit_code),
            ("--argjson", "known_noise", noise)]
    return run_jq(SUMMARIZE, path, raw_input=True, args=args, stdin=text)


def result(regions, applied):
    return run_jq(RESULT, null_input=True, args=[("--argjson", "regions", regions),
                                                 ("--arg", "account_id", "123456789012"),
                                                 ("--arg", "applied", "true" if applied else "false"),
                                                 ("--arg", "generated_at", "2026-10-15T00:00:00+00:00")])


def line(**kw):
    return json.dumps({"level": "info", "time": "t", **kw})


def item(state, typ, name, owner="ap-northeast-2"):
    msg = {"new": "would remove", "pending": "triggered remove", "waiting": "waiting for removal",
           "failed": "failed", "finished": "removed", "filtered": "filtered: filtered by config"}[state]
    return line(msg=msg, type=typ, name=name, owner=owner, state=state, state_code=0)


def lister_error(typ, err, owner="ap-northeast-2"):
    return json.dumps({"level": "error", "time": "t", "resource_type": typ, "owner": owner, "error": err,
                       "msg": "listing failed:\n    " + err})


# ------------------------------------------------------------------ real logs
def test_real_dryrun_log():
    s = summarize(FIXTURES / "real-v3.68.3-dryrun.jsonl")
    assert s["outcome"] == "completed"
    assert s["counts"] == {"would_remove": 9, "removed": 0, "failed": 0, "remaining": 0, "filtered": 8,
                           "listing_failed": 2}
    assert s["would_remove_by_type"] == {"IAMPolicy": 2, "IAMRole": 1, "IAMRolePolicyAttachment": 1, "IAMUser": 1,
                                         "IAMUserAccessKey": 1, "S3Bucket": 1, "SQSQueue": 2}
    assert s["scan"] == {"total": 17, "nukeable": 9, "filtered": 8}
    assert set(s["listing_failed_by_type"]) == {"FMSPolicy", "QBusinessApplication"}
    assert s["listing_failed_unexpected"] == ["QBusinessApplication"]       # FMSPolicy is known noise
    assert "QBusiness: ListApplications" in s["listing_failed_by_type"]["QBusinessApplication"]["error"]


def test_real_apply_that_gave_up():
    """Real run: role + attachment kept failing -> libnuke 'failed' fatal after 2 failure-only passes."""
    s = summarize(FIXTURES / "real-v3.68.3-apply-failed.jsonl", applied=True, exit_code=1)
    assert s["outcome"] == "gave_up_failed"
    assert s["counts"]["removed"] == 7 and s["counts"]["failed"] == 2 and s["counts"]["remaining"] == 0
    assert s["failed_by_type"] == {"IAMRole": 1, "IAMRolePolicyAttachment": 1}
    assert {"type": "IAMRole", "name": "student-role"} in s["failed_items"]
    assert s["fatal"] == ["failed"]
    assert any("DeleteConflict" in e for e in s["errors"])
    r = result({"us-east-1": s}, applied=True)
    assert (r["status"], r["exit_code"]) == ("ISSUES", 1)


def test_real_blocklisted_is_error():
    s = summarize(FIXTURES / "real-v3.68.3-blocklisted.jsonl", exit_code=1)
    assert s["outcome"] == "error" and s["fatal"] == ["account is in blocklist"]
    assert result({"ap-northeast-2": s}, applied=False)["status"] == "ERROR"


def test_real_generated_filters_keep_protected_identities():
    """make_config.py filters, run by the real binary against moto: protected things are 'filtered'."""
    s = summarize(FIXTURES / "real-v3.68.3-generated-filters.jsonl")
    kept = s["filtered_by_type"]
    for t in ("IAMRole", "IAMRolePolicy", "IAMRolePolicyAttachment", "IAMUser", "IAMUserPolicy",
              "IAMUserPolicyAttachment", "IAMUserAccessKey", "IAMLoginProfile", "IAMUserGroupAttachment",
              "IAMGroup", "IAMGroupPolicy", "IAMGroupPolicyAttachment", "IAMPolicy"):
        assert kept.get(t), t
    assert s["would_remove_by_type"] == {"IAMGroup": 1, "IAMRole": 1, "IAMRolePolicyAttachment": 1}


def test_field_names_present_in_real_logs():
    evs = [json.loads(ln) for ln in (FIXTURES / "real-v3.68.3-dryrun.jsonl").read_text().splitlines()]
    items = [e for e in evs if "state" in e]
    assert items and all({"type", "owner", "name", "state", "state_code", "msg"} <= set(e) for e in items)
    lf = [e for e in evs if e.get("msg", "").startswith("listing failed")]
    assert lf and all({"resource_type", "owner", "error", "level"} <= set(e) and e["level"] == "error" for e in lf)


# ------------------------------------------------------------------ synthetic
def test_last_state_wins_and_counts_by_type():
    text = "\n".join([
        item("new", "EC2Instance", "i-1"), item("new", "EC2Instance", "i-2"), item("new", "S3Bucket", "b"),
        item("new", "IAMRole", "r", owner="global"),
        item("pending", "EC2Instance", "i-1"), item("waiting", "EC2Instance", "i-1"), item("finished", "EC2Instance", "i-1"),
        item("pending", "EC2Instance", "i-2"), item("failed", "EC2Instance", "i-2"),
        item("pending", "S3Bucket", "b"), item("waiting", "S3Bucket", "b"),
        item("filtered", "IAMRole", "EduCleanupRole", owner="global"),
        lister_error("GlueJob", "ThrottlingException: Rate exceeded"),
        lister_error("GlueJob", "ThrottlingException: again"),
        lister_error("FMSPolicy", "AccessDeniedException"),
        lister_error("OpsWorksApp", "dial tcp: lookup opsworks.x.amazonaws.com: no such host"),
        json.dumps({"level": "fatal", "msg": "max wait retries of 120 exceeded", "time": "t"}),
    ]) + "\n"
    s = summarize(text=text, applied=True, exit_code=1)
    assert s["outcome"] == "gave_up_wait"
    assert s["counts"] == {"would_remove": 1, "removed": 1, "failed": 1, "remaining": 2, "filtered": 1,
                           "listing_failed": 4}
    assert s["remaining_by_type"] == {"IAMRole": 1, "S3Bucket": 1}
    assert s["removed_by_type"] == {"EC2Instance": 1}
    assert s["listing_failed_by_type"]["GlueJob"]["count"] == 2
    assert s["listing_failed_unexpected"] == ["GlueJob"]        # FMS = known noise, OpsWorks = DNS
    assert s["listing_failed_by_type"]["GlueJob"]["error"] == "ThrottlingException: Rate exceeded"


def test_same_name_in_two_owners_is_two_items():
    text = item("new", "S3Bucket", "b", owner="ap-northeast-2") + "\n" + item("new", "S3Bucket", "b", owner="us-east-1")
    assert summarize(text=text)["counts"]["would_remove"] == 2


def test_dry_run_never_reports_remaining():
    s = summarize(text=item("new", "EC2Instance", "i-1") + "\n" + line(msg="The above resources would be deleted"))
    assert s["counts"]["remaining"] == 0 and s["counts"]["would_remove"] == 1 and s["outcome"] == "completed"


def test_panic_output_is_not_json_and_is_an_error():
    text = line(msg="starting scan for resources") + "\npanic: boom [recovered]\n\ngoroutine 1 [running]:\n"
    s = summarize(text=text, exit_code=2)
    assert s["non_json_lines"] == 2 and s["outcome"] == "error"


def test_apply_with_nothing_to_delete_is_completed():
    s = summarize(text=line(msg="Scan complete: 1 total, 0 nukeable, 1 filtered.\n", total=1, nukeable=0, filtered=1)
                  + "\n" + line(msg="No resource to delete."), applied=True)
    assert s["outcome"] == "completed" and s["counts"]["remaining"] == 0


def test_empty_log():
    s = summarize(text="", exit_code=2)
    assert s["outcome"] == "error" and s["counts"]["would_remove"] == 0


# ------------------------------------------------------------------ result.jq status rules
def region(outcome="completed", failed=0, remaining=0, would=0, lf=0, unexpected=()):
    return {"exit_code": 0 if outcome == "completed" else 1, "outcome": outcome,
            "counts": {"would_remove": would, "removed": 0, "failed": failed, "remaining": remaining,
                       "filtered": 0, "listing_failed": lf}, "listing_failed_unexpected": list(unexpected)}


@pytest.mark.parametrize("applied,regions,status", [
    (False, {"a": region(would=5, lf=3, unexpected=["GlueJob"]), "global": region()}, "OK"),
    (False, {"a": region(outcome="error"), "global": region()}, "ERROR"),
    (True, {"a": region(), "global": region()}, "OK"),
    (True, {"a": region(failed=1, outcome="gave_up_failed"), "global": region()}, "ISSUES"),
    (True, {"a": region(remaining=2, outcome="gave_up_wait"), "global": region()}, "ISSUES"),
    (True, {"a": region(failed=1), "global": region(outcome="error")}, "ERROR"),
])
def test_result_status(applied, regions, status):
    r = result(regions, applied)
    assert r["status"] == status
    assert r["exit_code"] == {"OK": 0, "ISSUES": 1, "ERROR": 2}[status]
    assert {"module", "account_id", "generated_at", "applied", "status", "regions"} <= set(r)
    assert r["module"] == "nuke" and r["applied"] is applied and r["regions"] == regions


def test_table_renders_real_apply():
    if not shutil.which("jq"):
        pytest.skip("jq")
    s = summarize(FIXTURES / "real-v3.68.3-apply-failed.jsonl", applied=True, exit_code=1)
    r = result({"us-east-1": s, "global": summarize(text=line(msg="No resource to delete."), applied=True)}, True)
    out = subprocess.run(["jq", "-r", "-f", str(TABLE)], input=json.dumps(r), capture_output=True, text=True, check=True).stdout
    assert "REMOVED" in out and "FAILED" in out and "RESULT: ISSUES (exit 1)" in out
    assert "x IAMRole student-role" in out
    assert "LISTING FAILED QBusinessApplication" in out
    assert "listing failed (expected - service not used/subscribed): FMSPolicy" in out
