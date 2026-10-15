"""summarize.jq / result.jq / table.jq against REAL aws-nuke v3.68.3 JSON logs and synthetic ones.

The fixtures/real-v3.68.3-*.jsonl files were produced by the released aws-nuke v3.68.3 linux-amd64
binary (sha256 ec43c22b...) running against a moto server via custom endpoints - the field names
(type, owner, name, state, resource_type, msg, level, ...) are therefore the real ones (libnuke v1.3.0).
"""
import json
import re
import subprocess
import shutil

import pytest

from nuke_test_support import FIXTURES, NUKE_DIR, run_jq

SUMMARIZE = NUKE_DIR / "summarize.jq"
RESULT = NUKE_DIR / "result.jq"
TABLE = NUKE_DIR / "table.jq"
RULES = json.loads((NUKE_DIR / "listing_errors.json").read_text())
NOISE = ["FMSPolicy", "FMSNotificationChannel", "ShieldProtection"]


def summarize(path=None, *, text=None, applied=False, exit_code=0, noise=None, rules=None):
    args = [("--arg", "applied", "true" if applied else "false"), ("--argjson", "exit_code", exit_code),
            ("--argjson", "rules", RULES if rules is None else rules)]
    if noise is not None:
        args.append(("--argjson", "known_noise", noise))
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
    assert s["listing_failed_unexpected"] == []              # both are known noise (listing_errors.json)
    assert s["listing_failed_buckets"]["expected"] == ["FMSPolicy", "QBusinessApplication"]
    assert "QBusiness: ListApplications" in s["listing_failed_by_type"]["QBusinessApplication"]["error"]
    # without the known-noise list both are HTTP 500 (moto) -> transient, i.e. real failures
    bare = summarize(FIXTURES / "real-v3.68.3-dryrun.jsonl", rules=dict(RULES, known_noise_types=[]))
    assert bare["listing_failed_unexpected"] == ["FMSPolicy", "QBusinessApplication"]
    assert bare["listing_failed_by_bucket"] == {"expected": 0, "maybe_scp": 0, "transient": 2, "unexpected": 0}
    # --argjson known_noise still adds names on top of the rules
    extra = summarize(FIXTURES / "real-v3.68.3-dryrun.jsonl", rules=dict(RULES, known_noise_types=[]),
                      noise=["FMSPolicy"])
    assert extra["listing_failed_unexpected"] == ["QBusinessApplication"]


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
    s = summarize(text=text, applied=True, exit_code=1, noise=NOISE)
    assert s["outcome"] == "gave_up_wait"
    assert s["counts"] == {"would_remove": 1, "removed": 1, "failed": 1, "remaining": 2, "filtered": 1,
                           "listing_failed": 4}
    assert s["remaining_by_type"] == {"IAMRole": 1, "S3Bucket": 1}
    assert s["removed_by_type"] == {"EC2Instance": 1}
    assert s["listing_failed_by_type"]["GlueJob"]["count"] == 2
    assert s["listing_failed_unexpected"] == ["GlueJob"]        # FMS = known noise, OpsWorks = DNS
    assert s["listing_failed_by_type"]["GlueJob"] == {"count": 2, "error": "ThrottlingException: Rate exceeded",
                                                      "bucket": "transient"}
    assert s["listing_failed_by_bucket"] == {"expected": 2, "maybe_scp": 0, "transient": 1, "unexpected": 0}


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
    assert "LISTING FAILED" not in out and "LIST-ERR:" not in out
    assert "listing failed (expected - service not used/subscribed): FMSPolicy, QBusinessApplication" in out


# ------------------------------------------------------------------ real CloudShell run (17 regions, 2026-10-15)
# fixtures/real-cloudshell-v3.68.3-listing-failed.jsonl = every "LISTING FAILED" line of a real CloudShell dry-run
# (aws-nuke v3.68.3, empty member account, 17 regions) converted back to the aws-nuke JSON lister-error shape.
# The error text is the real one: summarize.jq's `short` of each line reproduces the printed text exactly.
CLOUDSHELL = FIXTURES / "real-cloudshell-v3.68.3-listing-failed.jsonl"
REAL_BUCKETS = {
    # API not offered for this account in that region -> nothing can hide there
    "BedrockCustomModel": "expected", "BedrockModelCustomizationJob": "expected", "BedrockEvaluationJob": "expected",
    "CloudDirectoryDirectory": "expected", "CloudDirectorySchema": "expected", "GlueDevEndpoint": "expected",
    "ComprehendDocumentClassifier": "expected", "TranscribeLanguageModel": "expected",
    "DocDBCluster": "expected", "DocDBInstance": "expected", "DocDBParameterGroup": "expected",
    "DocDBSnapshot": "expected", "EC2VerifiedAccessEndpoint": "expected", "EC2VerifiedAccessGroup": "expected",
    "EC2VerifiedAccessInstance": "expected", "EC2VerifiedAccessTrustProvider": "expected",
    # AccessDenied without a reason: SCP or region gating -> one quiet line
    "RekognitionDataset": "maybe_scp", "RekognitionProject": "maybe_scp", "TextractAdapter": "maybe_scp",
    "TextractAdapterVersion": "maybe_scp", "TimestreamInfluxDBDbInstance": "maybe_scp",
    "SSMQuickSetupConfigurationManager": "maybe_scp",
    # 5xx: real failures, collapsed to one line (Lex V1 503s disappear with the new excludes)
    "BedrockFlowAlias": "transient", "BedrockPrompt": "transient", "LexBot": "transient", "LexIntent": "transient",
    "LexSlotType": "transient", "LexModelBuildingServiceBotAlias": "transient",
}


def cloudshell_events():
    return [json.loads(ln) for ln in CLOUDSHELL.read_text().splitlines()]


def cloudshell_region_logs(drop_excluded=False):
    """{region: jsonl text} - real lister errors + the scan/dry-run end lines of a completed dry-run."""
    import fnmatch
    import make_config as mc
    excl = mc.default_excludes()
    out = {e["owner"]: [] for e in cloudshell_events()}      # all 17 regions, even when nothing is left
    for e in cloudshell_events():
        if drop_excluded and any(fnmatch.fnmatchcase(e["resource_type"], p) for p in excl):
            continue      # the lister of an excluded type is never run
        out.setdefault(e["owner"], []).append(json.dumps(e))
    tail = [line(msg="Scan complete: 0 total, 0 nukeable, 0 filtered.\n", total=0, nukeable=0, filtered=0),
            line(msg="The above resources would be deleted with the supplied configuration.")]
    return {r: "\n".join(v + tail) + "\n" for r, v in out.items()}


def test_cloudshell_fixture_shape(resource_docs):
    evs = cloudshell_events()
    assert len(evs) == 117 and len({e["owner"] for e in evs}) == 17
    real = [json.loads(ln) for ln in (FIXTURES / "real-v3.68.3-dryrun.jsonl").read_text().splitlines()]
    shape = {k for e in real if e.get("msg", "").startswith("listing failed") for k in e}
    for e in evs:
        assert set(e) == shape and e["level"] == "error" and e["msg"].startswith("listing failed:\n    ")
        assert e["resource_type"] in resource_docs, e["resource_type"]       # registered in v3.68.3


def test_cloudshell_errors_are_classified():
    seen = {}
    for region, text in cloudshell_region_logs().items():
        s = summarize(text=text)
        assert s["outcome"] == "completed"
        for t, v in s["listing_failed_by_type"].items():
            assert v["bucket"] == REAL_BUCKETS[t], (region, t, v)
            seen[t] = v["bucket"]
        b = s["listing_failed_buckets"]
        assert s["listing_failed_unexpected"] == sorted(b["transient"] + b["unexpected"])
        assert s["listing_failed_by_bucket"] == {k: len(v) for k, v in b.items()}
        assert b["unexpected"] == []          # nothing in an empty account looks like a hidden resource
    assert seen == REAL_BUCKETS               # every real error is covered by a rule
    # error text preserved exactly as aws-nuke printed it
    s = summarize(text=cloudshell_region_logs()["us-west-1"])
    assert s["listing_failed_by_type"]["DocDBCluster"]["error"] == (
        "operation error DocDB: DescribeDBClusters, https response error StatusCode: 400, RequestID: "
        "954f13b6-eaec-4bc2-a57d-046a2b94c83f, api error InvalidParameterValue: Unrecognized engine name: docdb")
    s = summarize(text=cloudshell_region_logs()["eu-west-1"])
    assert s["listing_failed_by_type"]["LexBot"]["error"] == ":  | status code: 503, request id: "


def cloudshell_result(drop_excluded):
    regions = {r: dict(summarize(text=t), duration_s=1) for r, t in sorted(cloudshell_region_logs(drop_excluded).items())}
    return result(regions, False)


def render(r):
    return subprocess.run(["jq", "-r", "-f", str(TABLE)], input=json.dumps(r), capture_output=True, text=True,
                          check=True).stdout


def rows(out):
    """{region: [columns...]} of the table rows."""
    return {ln.split()[0]: ln.split() for ln in out.splitlines() if re_region_row(ln)}


def re_region_row(ln):
    parts = ln.split()
    return len(parts) >= 7 and parts[0].count("-") >= 2 and not ln.startswith("[")


def test_cloudshell_with_new_excludes_only_real_failures_count():
    r = cloudshell_result(drop_excluded=True)
    assert r["status"] == "OK"
    unexpected = {k: v["listing_failed_unexpected"] for k, v in r["regions"].items() if v["listing_failed_unexpected"]}
    assert unexpected == {"us-west-1": ["BedrockFlowAlias", "BedrockPrompt"]}      # the 500s - nothing else
    assert r["totals"]["listing_failed_unexpected"] == 2
    tb = r["totals"]["listing_failed_by_bucket"]
    assert tb["transient"] == 2 and tb["unexpected"] == 0 and tb["maybe_scp"] == 8 and tb["expected"] > 0
    assert sum(tb.values()) == sum(len(v["listing_failed_by_type"]) for v in r["regions"].values())
    out = render(r)
    head = next(ln for ln in out.splitlines() if ln.startswith("REGION"))
    assert head.split()[-2:] == ["LIST-ERR", "EXP/SCP"]
    rw = rows(out)
    assert len(rw) == 17
    for region, cols in rw.items():
        assert cols[-2] == ("2" if region == "us-west-1" else "-"), (region, cols)    # LIST-ERR
    assert rw["us-west-1"][-1] == "8/3" and rw["us-east-1"][-1] == "-"
    assert rw["ap-northeast-3"][-1] == "7/1"
    assert "LIST-ERR: 2 lister failure(s)" in out


def test_cloudshell_table_is_compact():
    """Per region: at most one line per quiet bucket, one line for 5xx; no per-type LISTING FAILED lines."""
    out = render(cloudshell_result(drop_excluded=False))
    lines = out.splitlines()
    for region in cloudshell_region_logs():
        mine = [ln for ln in lines if ln.startswith(f"[{region}] ")]
        for label in ("listing failed (expected", "access denied (maybe SCP)", "LISTING FAILED (5xx/throttling"):
            assert sum(label in ln for ln in mine) <= 1, (region, label)
        assert not [ln for ln in mine if re.match(r"^\[[^]]+\] LISTING FAILED \w", ln)], mine
        assert len(mine) <= 3
    assert ("[us-west-1] access denied (maybe SCP): RekognitionDataset, RekognitionProject, "
            "TimestreamInfluxDBDbInstance") in lines
    assert "[us-west-1] LISTING FAILED (5xx/throttling - re-run to confirm): BedrockFlowAlias, BedrockPrompt" in lines
    assert ("[eu-west-1] LISTING FAILED (5xx/throttling - re-run to confirm): LexBot, LexIntent, "
            "LexModelBuildingServiceBotAlias, LexSlotType") in lines
    assert ("[ap-northeast-3] listing failed (expected - service not used/subscribed): BedrockCustomModel, "
            "BedrockEvaluationJob, BedrockModelCustomizationJob, EC2VerifiedAccessEndpoint, EC2VerifiedAccessGroup, "
            "EC2VerifiedAccessInstance, EC2VerifiedAccessTrustProvider, GlueDevEndpoint") in lines
    assert len([ln for ln in lines if ln.startswith("[")]) <= 3 * 17


@pytest.mark.parametrize("err,bucket", [
    # IAM permission missing (no SCP wording): resources of this type may exist -> its own line, counted
    ("operation error Rekognition: DescribeProjects, https response error StatusCode: 400, RequestID: x, "
     "AccessDeniedException: User: arn:aws:sts::123456789012:assumed-role/EduCleanupRole/s is not authorized to "
     "perform: rekognition:DescribeProjects because no identity-based policy allows the "
     "rekognition:DescribeProjects action", "unexpected"),
    ("AccessDeniedException: User: arn:aws:iam::123456789012:user/x is not authorized to perform: glue:GetJobs "
     "on resource: arn:aws:glue:us-east-1:123456789012:job/* with an explicit deny in a service control policy",
     "maybe_scp"),
    ("AccessDeniedException\n\tstatus code: 400, request id: abc", "maybe_scp"),
    ("ThrottlingException: Rate exceeded\n\tstatus code: 400, request id: abc", "transient"),
    ("operation error EC2: DescribeX, https response error StatusCode: 503, RequestID: x, api error Unavailable",
     "transient"),
    ("ResourceNotFoundException: something odd", "unexpected"),
    ("dial tcp: lookup comprehend.eu-north-1.amazonaws.com: no such host", "expected"),
])
def test_bucket_rules(err, bucket):
    s = summarize(text=lister_error("SomeType", err))
    assert s["listing_failed_by_type"]["SomeType"]["bucket"] == bucket
    assert (s["listing_failed_unexpected"] == ["SomeType"]) == (bucket in ("transient", "unexpected"))


def test_unexpected_failure_gets_its_own_line():
    text = "\n".join([lister_error("GlueJob", "ResourceNotFoundException: odd"),
                      lister_error("SageMakerDomain", "ValidationException: weird thing"),
                      lister_error("RekognitionProject", "AccessDeniedException: ")]) + "\n"
    r = result({"ap-northeast-2": summarize(text=text)}, False)
    out = render(r)
    assert "[ap-northeast-2] LISTING FAILED GlueJob: ResourceNotFoundException: odd" in out
    assert "[ap-northeast-2] LISTING FAILED SageMakerDomain: ValidationException: weird thing" in out
    assert "[ap-northeast-2] access denied (maybe SCP): RekognitionProject" in out
    assert rows(out)["ap-northeast-2"][-2:] == ["2", "0/1"]


def test_worst_bucket_wins_per_type():
    text = "\n".join([lister_error("GlueJob", "AccessDeniedException: "),
                      lister_error("GlueJob", "ResourceNotFoundException: odd")]) + "\n"
    v = summarize(text=text)["listing_failed_by_type"]["GlueJob"]
    assert v == {"count": 2, "bucket": "unexpected", "error": "ResourceNotFoundException: odd"}


def test_rules_file_is_well_formed():
    assert set(RULES) == {"_doc", "known_noise_types", "expected", "maybe_scp", "transient"}
    for b in ("expected", "maybe_scp", "transient"):
        for rule in RULES[b]:
            assert set(rule) == {"re", "why"} and len(rule["why"]) > 15, rule
    summarize(text="")   # every regex compiles in jq (an invalid one would make jq fail)
    for b in ("expected", "maybe_scp", "transient"):
        for rule in RULES[b]:
            run_jq(SUMMARIZE, raw_input=True, stdin=lister_error("X", "probe"),
                   args=[("--arg", "applied", "false"), ("--argjson", "exit_code", 0),
                         ("--argjson", "rules", {"known_noise_types": [], b: [rule]})])
