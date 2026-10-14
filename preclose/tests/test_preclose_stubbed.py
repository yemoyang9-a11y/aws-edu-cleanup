"""Scenario tests with botocore Stubber (services moto does not implement: marketplace-agreement, rbin,
savingsplans, bedrock PT, shield subscription state, EC2 recycle bin, reserved nodes)."""
import json

import pytest

from test_preclose_harness import (ACCOUNT, EDS_ARN, MP_UPFRONT, MP_USAGE, REGION, RULE_LOCKED, RULE_OPEN,
                                   TRAIL_LOCAL, TRAIL_MR, US, T0, T1, Harness, by_check, denied, fake_session, pcm,
                                   pt_summary, queue_clean, queue_dirty, run_pc)

needs_cancel_agreement = pytest.mark.skipif(
    "CancelAgreement" not in fake_session().client("marketplace-agreement", region_name=US).meta.service_model.operation_names,
    reason="installed botocore has no marketplace-agreement CancelAgreement")


# ------------------------------------------------------------------ dry-run
def test_preclose_dry_run_makes_no_mutating_calls():
    h = Harness()
    queue_dirty(h, apply=False)
    result, code = run_pc(h)
    h.assert_all_consumed()

    assert h.mutating() == [], h.mutating()
    assert code == pcm.EXIT_ISSUES
    assert result["applied"] is False and result["status"] == "ISSUES" and result["module"] == "preclose"
    assert all(f["action"] in ("none", "planned") for f in result["findings"])
    planned = {(f["check"], f["resource_id"]) for f in result["findings"] if f["action"] == "planned"}
    assert ("marketplace_agreement", MP_USAGE) in planned
    assert ("cloudtrail_trail", TRAIL_MR) in planned
    assert ("cloudtrail_event_data_store", EDS_ARN) in planned
    assert ("recycle_bin_rule", RULE_OPEN) in planned
    assert ("recycle_bin_rule_unlock", RULE_LOCKED) in planned
    assert ("route53_domain", "edu-lab.example") in planned
    assert ("shield_auto_renew", f"arn:aws:shield::{ACCOUNT}:subscription/abc") in planned
    assert ("account_alias", "edu-2026-10-07") not in planned  # --delete-alias not given


def test_preclose_dry_run_severities_and_commitments():
    h = Harness()
    queue_dirty(h, apply=False)
    result, _ = run_pc(h)
    sev = {(f["check"], f["resource_id"]): f["severity"] for f in result["findings"]}
    for key in [("ec2_reserved_instance", "ri-1"), ("rds_reserved_instance", "rds-ri-1"), ("savings_plan", "sp-1"),
                ("bedrock_provisioned_throughput", pt_summary("pt-committed", "x")["provisionedModelArn"]),
                ("shield_subscription", f"arn:aws:shield::{ACCOUNT}:subscription/abc")]:
        assert sev[key] == "BLOCKER", key
        f = next(x for x in result["findings"] if (x["check"], x["resource_id"]) == key)
        assert f["fixable"] is False and "AWS Support" in f["detail"]
    assert ("rds_reserved_instance", "rds-ri-old") not in sev  # retired reservation ignored
    assert sev[("bedrock_provisioned_throughput", pt_summary("pt-hourly", None)["provisionedModelArn"])] == "WARN"
    assert sev[("root_access_keys", "root")] == "WARN"
    assert sev[("recycle_bin_contents", "snapshots")] == "WARN"
    assert sev[("recycle_bin_contents", "volumes")] == "WARN"
    assert sev[("route53_domain", "edu-lab.example")] == "WARN"
    assert sev[("route53_domain", "old-lab.example")] == "INFO"
    assert sev[("account_alias", "edu-2026-10-07")] == "INFO"
    assert len(by_check(result, "closure_reminder")) == len(pcm.REMINDERS)
    vol = next(f for f in result["findings"] if f["resource_id"] == "volumes")
    assert "8 GiB" in vol["detail"]


# ------------------------------------------------------------------ multi-region trail dedupe
def test_preclose_multi_region_trail_dedupe_and_home_region_delete():
    h = Harness()
    shadow = {"Name": "org-audit", "TrailARN": TRAIL_MR, "HomeRegion": US, "IsMultiRegionTrail": True}
    h.stub("cloudtrail", REGION).add_response("describe_trails", {"trailList": [
        shadow, {"Name": "local-trail", "TrailARN": TRAIL_LOCAL, "HomeRegion": REGION}]}, {"includeShadowTrails": True})
    h.stub("cloudtrail", US).add_response("describe_trails", {"trailList": [shadow]}, {"includeShadowTrails": True})
    h.stub("cloudtrail", US).add_response("delete_trail", {}, {"Name": "org-audit"})
    h.stub("cloudtrail", REGION).add_response("delete_trail", {}, {"Name": "local-trail"})
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})

    result, code = run_pc(h, "--apply", checks=["cloudtrail_trails"], regions=f"{REGION},{US}")
    h.assert_all_consumed()
    trails = by_check(result, "cloudtrail_trail")
    assert sorted(f["resource_id"] for f in trails) == sorted([TRAIL_MR, TRAIL_LOCAL])  # reported once each
    mr = next(f for f in trails if f["resource_id"] == TRAIL_MR)
    assert mr["region"] == US and mr["action"] == "done"
    deletes = [(c[1], c[3]["Name"]) for c in h.calls if c[2] == "DeleteTrail"]
    assert sorted(deletes) == sorted([(US, "org-audit"), (REGION, "local-trail")])  # once, in the home region
    assert code == pcm.EXIT_OK


def test_preclose_trail_owned_by_other_account_is_info():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    org = "arn:aws:cloudtrail:us-east-1:210987654321:trail/org-trail"
    h.stub("cloudtrail", REGION).add_response("describe_trails", {"trailList": [
        {"Name": "org-trail", "TrailARN": org, "HomeRegion": US, "IsOrganizationTrail": True}]},
        {"includeShadowTrails": True})
    result, code = run_pc(h, "--apply", checks=["cloudtrail_trails"])
    assert by_check(result, "cloudtrail_trail")[0]["severity"] == "INFO"
    assert h.mutating() == [] and code == pcm.EXIT_OK


# ------------------------------------------------------------------ recycle bin
@needs_cancel_agreement
def test_preclose_locked_rbin_rule_is_not_deleted():
    h = Harness()
    queue_dirty(h, apply=True)
    result, code = run_pc(h, "--apply")
    h.assert_all_consumed()

    deleted = [c[3]["Identifier"] for c in h.calls if c[2] == "DeleteRule"]
    assert deleted == [RULE_OPEN]
    locked = next(f for f in by_check(result, "recycle_bin_rule") if f["resource_id"] == RULE_LOCKED)
    assert locked["severity"] == "BLOCKER" and locked["fixable"] is False and locked["action"] == "none"
    assert "7일" in locked["detail"]
    unlock = by_check(result, "recycle_bin_rule_unlock")[0]
    assert unlock["action"] == "done" and T1.isoformat()[:10] in unlock["detail"]
    assert code == pcm.EXIT_ISSUES  # locked rule + commitments remain


def test_preclose_pending_unlock_rule_reports_date():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    rb = h.stub("rbin", REGION)
    rb.add_response("list_rules", {"Rules": [{"Identifier": RULE_LOCKED, "LockState": "pending_unlock"}]},
                    {"ResourceType": "EBS_SNAPSHOT"})
    rb.add_response("get_rule", {"Identifier": RULE_LOCKED, "LockState": "pending_unlock", "LockEndTime": T1,
                                 "LockConfiguration": {"UnlockDelay": {"UnlockDelayValue": 30, "UnlockDelayUnit": "DAYS"}}},
                    {"Identifier": RULE_LOCKED})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EC2_IMAGE"})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EBS_VOLUME"})
    ec2 = h.stub("ec2", REGION)
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": []}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
    ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": []}, {})
    result, code = run_pc(h, "--apply", checks=["recycle_bin"])
    f = by_check(result, "recycle_bin_rule")[0]
    assert f["severity"] == "BLOCKER" and "2027-10-01" in f["detail"]
    assert h.mutating() == [] and not by_check(result, "recycle_bin_rule_unlock")
    assert code == pcm.EXIT_ISSUES


def _rbin_no_rules(h, region=REGION):
    rb = h.stub("rbin", region)
    for rtype in pcm.RBIN_TYPES:
        rb.add_response("list_rules", {"Rules": []}, {"ResourceType": rtype})
    return rb


def test_preclose_purge_recycle_bin_restores_then_deletes():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    rb = _rbin_no_rules(h)
    ec2 = h.stub("ec2", REGION)
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": [{"SnapshotId": "snap-0aaaaaaaaaaaaaaaa"}]}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": [{"ImageId": "ami-0aaaaaaaaaaaaaaaa"}]}, {})
    for rtype in pcm.PURGE_RULE_TYPES:  # fresh re-check right before purging
        rb.add_response("list_rules", {"Rules": []}, {"ResourceType": rtype})
    ec2.add_response("restore_snapshot_from_recycle_bin", {"SnapshotId": "snap-0aaaaaaaaaaaaaaaa", "State": "recovering"},
                     {"SnapshotId": "snap-0aaaaaaaaaaaaaaaa"})
    ec2.add_response("describe_snapshots", {"Snapshots": [{"SnapshotId": "snap-0aaaaaaaaaaaaaaaa", "State": "recovering"}]},
                     {"SnapshotIds": ["snap-0aaaaaaaaaaaaaaaa"]})
    ec2.add_response("describe_snapshots", {"Snapshots": [{"SnapshotId": "snap-0aaaaaaaaaaaaaaaa", "State": "completed"}]},
                     {"SnapshotIds": ["snap-0aaaaaaaaaaaaaaaa"]})
    ec2.add_response("restore_image_from_recycle_bin", {"Return": True}, {"ImageId": "ami-0aaaaaaaaaaaaaaaa"})
    ec2.add_response("describe_images", {"Images": [{"ImageId": "ami-0aaaaaaaaaaaaaaaa", "State": "available"}]},
                     {"ImageIds": ["ami-0aaaaaaaaaaaaaaaa"]})
    ec2.add_response("deregister_image", {"Return": True},
                     {"ImageId": "ami-0aaaaaaaaaaaaaaaa", "DeleteAssociatedSnapshots": True})
    ec2.add_client_error("delete_snapshot", "InvalidSnapshot.NotFound", "already deleted with its AMI", 400,
                         expected_params={"SnapshotId": "snap-0aaaaaaaaaaaaaaaa"})
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": []}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
    ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": []}, {})

    result, code = run_pc(h, "--apply", "--purge-recycle-bin", checks=["recycle_bin"])
    h.assert_all_consumed()
    contents = {f["resource_id"]: f for f in by_check(result, "recycle_bin_contents")}
    assert contents["snapshots"]["action"] == "done" and contents["images"]["action"] == "done"
    ops = h.ops()
    assert ops.index("RestoreSnapshotFromRecycleBin") < ops.index("RestoreImageFromRecycleBin") \
        < ops.index("DeregisterImage") < ops.index("DeleteSnapshot")
    assert code == pcm.EXIT_OK


def test_preclose_purge_skipped_when_rules_were_just_deleted():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    rb = h.stub("rbin", REGION)
    rb.add_response("list_rules", {"Rules": [{"Identifier": RULE_OPEN}]}, {"ResourceType": "EBS_SNAPSHOT"})
    rb.add_response("delete_rule", {}, {"Identifier": RULE_OPEN})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EC2_IMAGE"})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EBS_VOLUME"})
    ec2 = h.stub("ec2", REGION)
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": [{"SnapshotId": "snap-0aaaaaaaaaaaaaaaa"}]}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
    ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": []}, {})
    result, code = run_pc(h, "--apply", "--purge-recycle-bin", checks=["recycle_bin"])
    h.assert_all_consumed()
    assert not any(op.startswith(("Restore", "DeleteSnapshot")) for op in h.ops())
    snaps = by_check(result, "recycle_bin_contents")[0]
    assert snaps["action"] == "none" and "다시 실행" in snaps["detail"]
    assert by_check(result, "recycle_bin_rule")[0]["action"] == "done"
    assert code == pcm.EXIT_OK  # rule deleted; bin contents are WARN


def test_preclose_purge_requires_flag():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    _rbin_no_rules(h)
    ec2 = h.stub("ec2", REGION)
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": [{"SnapshotId": "snap-0aaaaaaaaaaaaaaaa"}]}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
    ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": []}, {})
    result, _ = run_pc(h, "--apply", checks=["recycle_bin"])
    h.assert_all_consumed()
    assert h.mutating() == []
    assert "--purge-recycle-bin" in by_check(result, "recycle_bin_contents")[0]["detail"]


# ------------------------------------------------------------------ apply outcomes
@needs_cancel_agreement
def test_preclose_apply_records_per_item_outcomes():
    h = Harness()
    queue_dirty(h, apply=True)
    result, code = run_pc(h, "--apply")
    h.assert_all_consumed()
    mp = {f["resource_id"]: f for f in by_check(result, "marketplace_agreement")}
    assert mp[MP_USAGE]["action"] == "done"
    assert mp[MP_UPFRONT]["action"] == "failed" and "ValidationException" in mp[MP_UPFRONT]["error"]
    assert "판매자" in mp[MP_UPFRONT]["detail"]
    cancels = [c[3]["agreementId"] for c in h.calls if c[2] == "CancelAgreement"]
    assert cancels == [MP_UPFRONT, MP_USAGE]  # upfront/contract agreement first, as AWS documents
    eds = by_check(result, "cloudtrail_event_data_store")[0]
    assert eds["action"] == "done"
    ops = h.ops()
    assert ops.index("UpdateEventDataStore") < ops.index("DeleteEventDataStore")
    shield = by_check(result, "shield_auto_renew")[0]
    assert shield["action"] == "failed" and "LockedSubscriptionException" in shield["error"]
    assert by_check(result, "route53_domain")[0]["action"] == "done"
    assert result["summary"]["blockers_remaining"] > 0 and code == pcm.EXIT_ISSUES
    assert result["applied"] is True


def test_preclose_clean_account_exit_0_and_delete_alias():
    h = Harness()
    queue_clean(h, delete_alias_call=True)
    result, code = run_pc(h, "--apply", "--delete-alias")
    h.assert_all_consumed()
    assert code == pcm.EXIT_OK and result["status"] == "OK" and result["ready_to_close"] is True
    alias = by_check(result, "account_alias")[0]
    assert alias["action"] == "done" and alias["severity"] == "INFO"
    assert [c[2] for c in h.mutating()] == ["DeleteAccountAlias"]


def test_preclose_alias_not_deleted_without_flag():
    h = Harness()
    queue_clean(h)
    result, code = run_pc(h, "--apply")
    h.assert_all_consumed()
    assert h.mutating() == [] and code == pcm.EXIT_OK
    assert by_check(result, "account_alias")[0]["action"] == "none"


def test_preclose_alias_kept_while_blockers_remain():
    h = Harness()
    queue_clean(h, skip={"ec2-ri"})
    h.stub("ec2", REGION).add_response("describe_reserved_instances", {"ReservedInstances": [
        {"ReservedInstancesId": "ri-1", "State": "active"}]},
        {"Filters": [{"Name": "state", "Values": ["active", "payment-pending"]}]})
    result, code = run_pc(h, "--apply", "--delete-alias")
    h.assert_all_consumed()
    assert "DeleteAccountAlias" not in h.ops()
    assert "보류" in by_check(result, "account_alias")[0]["detail"]
    assert code == pcm.EXIT_ISSUES


def test_preclose_prod_alias_aborts_apply():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": ["acme-prod"], "IsTruncated": False})
    result, code = run_pc(h, "--apply")
    assert code == pcm.EXIT_ERROR and result["status"] == "ERROR" and "prod" in result["error"]
    assert h.mutating() == [] and h.ops() == ["ListAccountAliases"]


# ------------------------------------------------------------------ graceful failures
def test_preclose_access_denied_on_one_check_does_not_crash():
    h = Harness()
    queue_clean(h, skip={"marketplace-agreement", "rbin"})
    denied(h.stub("marketplace-agreement", US), "search_agreements")
    rb = h.stub("rbin", REGION)
    denied(rb, "list_rules")
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EC2_IMAGE"})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EBS_VOLUME"})
    result, code = run_pc(h)
    h.assert_all_consumed()

    warn = by_check(result, "marketplace_agreement")
    assert len(warn) == 1 and warn[0]["severity"] == "WARN" and "권한 없음" in warn[0]["detail"]
    assert "AccessDenied" in warn[0]["error"]
    assert any(f["check"] == "recycle_bin_rule" and f["severity"] == "WARN" for f in result["findings"])
    assert by_check(result, "closure_reminder")  # later checks still ran
    assert "marketplace_agreement@global" in result["summary"]["checks_failed"]
    assert code == pcm.EXIT_OK and result["ready_to_close"] is False


def test_preclose_unexpected_exception_in_check_is_contained():
    h = Harness()
    queue_clean(h)

    class Boom:
        def __getattr__(self, _name):
            raise RuntimeError("boom")

    real = h.factory
    result, code = pcm.run(h.session, ACCOUNT, pcm.build_parser().parse_args(["--regions", REGION]),
                           client_factory=lambda s, r: Boom() if s == "shield" else real(s, r),
                           sleep=lambda _s: None)
    f = by_check(result, "shield_subscription")[0]
    assert f["severity"] == "WARN" and "RuntimeError" in f["error"]
    assert code == pcm.EXIT_OK


def test_preclose_opt_in_region_error_is_warn():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    h.stub("ec2", "me-central-1").add_client_error("describe_reserved_instances", service_error_code="OptInRequired",
                                                   service_message="region not enabled", http_status_code=401)
    result, code = run_pc(h, checks=["ec2_reserved"], regions="me-central-1")
    f = by_check(result, "ec2_reserved_instance")[0]
    assert f["severity"] == "WARN" and "옵트인" in f["detail"] and code == pcm.EXIT_OK


def test_preclose_cloudtrail_lake_unsupported_is_skipped():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    h.stub("cloudtrail", REGION).add_client_error("list_event_data_stores", "UnsupportedOperationException",
                                                  "Lake not available", 400)
    result, code = run_pc(h, checks=["cloudtrail_lake"])
    assert result["findings"] == [] and code == pcm.EXIT_OK


def test_preclose_eds_federation_is_disabled_before_delete():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    ct = h.stub("cloudtrail", REGION)
    ct.add_response("list_event_data_stores", {"EventDataStores": [
        {"EventDataStoreArn": EDS_ARN, "Name": "lake", "Status": "ENABLED", "TerminationProtectionEnabled": False}]}, {})
    ct.add_client_error("delete_event_data_store", "EventDataStoreFederationEnabledException", "federation on", 400)
    ct.add_response("disable_federation", {}, {"EventDataStore": EDS_ARN})
    ct.add_response("delete_event_data_store", {}, {"EventDataStore": EDS_ARN})
    result, code = run_pc(h, "--apply", checks=["cloudtrail_lake"])
    h.assert_all_consumed()
    assert by_check(result, "cloudtrail_event_data_store")[0]["action"] == "done" and code == pcm.EXIT_OK


def test_preclose_savings_plan_queued_and_returnable_advice():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    h.stub("savingsplans", US).add_response("describe_savings_plans", {"savingsPlans": [
        {"savingsPlanId": "sp-q", "state": "queued"},
        {"savingsPlanId": "sp-r", "state": "active", "returnableUntil": "2999-01-01T00:00:00Z"}],
        "nextToken": "n1"}, {"states": ["active", "queued", "payment-pending"]})
    h.stub("savingsplans", US).add_response("describe_savings_plans", {"savingsPlans": [
        {"savingsPlanId": "sp-x", "state": "active"}]},
        {"states": ["active", "queued", "payment-pending"], "nextToken": "n1"})
    result, code = run_pc(h, checks=["savings_plans"])
    h.assert_all_consumed()
    d = {f["resource_id"]: f["detail"] for f in by_check(result, "savings_plan")}
    assert set(d) == {"sp-q", "sp-r", "sp-x"}  # pagination followed
    assert "DeleteQueuedSavingsPlan" in d["sp-q"] and "ReturnSavingsPlan" in d["sp-r"]
    assert code == pcm.EXIT_ISSUES


# ------------------------------------------------------------------ regions
def test_preclose_all_regions_uses_account_list_regions():
    h = Harness()
    h.stub("account", US).add_response("list_regions", {"Regions": [
        {"RegionName": "us-east-1", "RegionOptStatus": "ENABLED_BY_DEFAULT"},
        {"RegionName": "ap-east-1", "RegionOptStatus": "ENABLED"}]},
        {"RegionOptStatusContains": ["ENABLED", "ENABLED_BY_DEFAULT"]})
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    for r in ("ap-east-1", "us-east-1"):
        h.stub("ec2", r).add_response("describe_reserved_instances", {"ReservedInstances": []},
                                      {"Filters": [{"Name": "state", "Values": ["active", "payment-pending"]}]})
    result, code = run_pc(h, "--all-regions", checks=["ec2_reserved"])
    h.assert_all_consumed()
    assert result["regions"] == ["ap-east-1", "us-east-1"] and code == pcm.EXIT_OK


def test_preclose_all_regions_falls_back_to_ec2():
    h = Harness()
    denied(h.stub("account", US), "list_regions")
    h.stub("ec2", US).add_response("describe_regions", {"Regions": [
        {"RegionName": "eu-west-1", "OptInStatus": "opt-in-not-required"},
        {"RegionName": "af-south-1", "OptInStatus": "not-opted-in"},
        {"RegionName": "me-south-1", "OptInStatus": "opted-in"}]}, {})
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    result, _ = run_pc(h, "--all-regions", checks=[])
    assert result["regions"] == ["eu-west-1", "me-south-1"]


# ------------------------------------------------------------------ main(): JSON + stdout
def test_preclose_main_writes_contract_json(tmp_path, capsys, monkeypatch):
    h = Harness()
    queue_dirty(h, apply=False)
    monkeypatch.setattr(pcm, "get_session", lambda args: h.session)
    monkeypatch.setattr(pcm, "account_id", lambda session: ACCOUNT)
    out = tmp_path / "preclose.json"
    code = pcm.main(["--regions", REGION, "--json-out", str(out)], client_factory=h.factory)
    assert code == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    for key in ("module", "account_id", "generated_at", "applied", "status", "findings", "summary", "ready_to_close"):
        assert key in data
    assert data["module"] == "preclose" and data["account_id"] == ACCOUNT and data["status"] == "ISSUES"
    stdout = capsys.readouterr().out
    assert "BLOCKER" in stdout and "해지 보류" in stdout


def test_preclose_main_auth_failure_exit_2(tmp_path, monkeypatch, capsys):
    def boom(args):
        raise RuntimeError("no credentials")
    monkeypatch.setattr(pcm, "get_session", boom)
    out = tmp_path / "o.json"
    assert pcm.main(["--json-out", str(out)]) == pcm.EXIT_ERROR
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["status"] == "ERROR" and data["module"] == "preclose" and "no credentials" in data["error"]


@pytest.mark.parametrize("state,expected", [("ACTIVE", 1), ("INACTIVE", 0)])
def test_preclose_shield_state(state, expected):
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    sh = h.stub("shield", US)
    sh.add_response("get_subscription_state", {"SubscriptionState": state}, {})
    if state == "ACTIVE":
        sh.add_response("describe_subscription", {"Subscription": {
            "StartTime": T0, "EndTime": T1, "AutoRenew": "DISABLED",
            "SubscriptionLimits": {"ProtectionLimits": {"ProtectedResourceTypeLimits": []},
                                   "ProtectionGroupLimits": {"MaxProtectionGroups": 1, "PatternTypeLimits": {
                                       "ArbitraryPatternLimits": {"MaxMembers": 1}}}}}}, {})
    result, code = run_pc(h, checks=["shield"])
    h.assert_all_consumed()
    assert len(by_check(result, "shield_subscription")) == expected and code == expected
    assert not by_check(result, "shield_auto_renew")


def test_preclose_old_boto3_missing_api_is_failed_with_upgrade_advice():
    """CloudShell may ship an older boto3 (e.g. 1.42.x has no CancelAgreement): record 'failed' + advice."""
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    h.stub("marketplace-agreement", US).add_response("search_agreements", {"agreementViewSummaries": [
        {"agreementId": MP_USAGE, "status": "ACTIVE"}]})
    h.stub("marketplace-agreement", US).add_response("get_agreement_terms", {"acceptedTerms": []})
    real = h.factory

    class OldClient:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            if name == "cancel_agreement":
                raise AttributeError("'AgreementService' object has no attribute 'cancel_agreement'")
            return getattr(self._inner, name)

    result, code = pcm.run(h.session, ACCOUNT, pcm.build_parser().parse_args(["--regions", REGION, "--apply"]),
                           client_factory=lambda s, r: OldClient(real(s, r)) if s == "marketplace-agreement" else real(s, r),
                           sleep=lambda _s: None, checks=["marketplace"])
    f = by_check(result, "marketplace_agreement")[0]
    assert f["action"] == "failed" and "최신 boto3" in f["detail"]
    assert code == pcm.EXIT_ISSUES


def test_preclose_alias_kept_when_a_check_could_not_run():
    h = Harness()
    queue_clean(h, skip={"marketplace-agreement"})
    denied(h.stub("marketplace-agreement", US), "search_agreements")
    result, code = run_pc(h, "--apply", "--delete-alias")
    h.assert_all_consumed()
    assert "DeleteAccountAlias" not in h.ops()
    alias = by_check(result, "account_alias")[0]
    assert alias["action"] == "none" and "확인 불가 점검 1건" in alias["detail"]
    assert code == pcm.EXIT_OK and result["ready_to_close"] is False
