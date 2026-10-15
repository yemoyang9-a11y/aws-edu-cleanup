"""Shared test harness for preclose (imported by the other test_preclose_* files) + pure-logic tests.

Clients are created through PreClose's client_factory hook. Every client records the operations
it is asked to perform (provide-client-params fires before botocore validation, stubbing or sending),
so tests can prove that a dry-run never even *attempts* a mutating call.
"""
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import boto3
import pytest
from botocore.stub import Stubber

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # preclose/ -> import preclose_check
sys.path.insert(0, str(HERE))         # tests/   -> import this harness from sibling test files

import preclose_check as pcm  # noqa: E402

ACCOUNT = "123456789012"  # moto's default account id
REGION = "ap-northeast-2"
US = "us-east-1"
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2027, 10, 1, tzinfo=dt.timezone.utc)


def fake_session() -> boto3.Session:
    return boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                         aws_session_token="testing", region_name=US)


class Harness:
    """client_factory for PreClose: Stubber-backed clients (default) or real clients (for moto)."""

    def __init__(self, session: Optional[boto3.Session] = None, stub_services: Optional[Iterable[str]] = None):
        self.session = session or fake_session()
        self.stub_all = stub_services is None
        self.stub_services = set(stub_services or ())
        self.clients: Dict[Tuple[str, str], Any] = {}
        self.stubbers: Dict[Tuple[str, str], Stubber] = {}
        self.calls: List[Tuple[str, str, str, Dict[str, Any]]] = []

    def client(self, service: str, region: str) -> Any:
        key = (service, region)
        if key not in self.clients:
            cl = self.session.client(service, region_name=region)

            def _record(params: Dict[str, Any], model: Any, **_: Any) -> None:
                self.calls.append((service, region, model.name, dict(params)))

            cl.meta.events.register_first("provide-client-params", _record)
            if self.stub_all or service in self.stub_services:
                st = Stubber(cl)
                st.activate()
                self.stubbers[key] = st
            self.clients[key] = cl
        return self.clients[key]

    factory = client

    def stub(self, service: str, region: str) -> Stubber:
        self.client(service, region)
        return self.stubbers[(service, region)]

    def ops(self) -> List[str]:
        return [c[2] for c in self.calls]

    def mutating(self) -> List[Tuple[str, str, str, Dict[str, Any]]]:
        return [c for c in self.calls if c[2].startswith(pcm.MUTATING_PREFIXES)]

    def assert_all_consumed(self) -> None:
        for st in self.stubbers.values():
            st.assert_no_pending_responses()


def make_args(*extra: str, regions: str = REGION):
    return pcm.build_parser().parse_args(["--regions", regions, *extra])


def run_pc(h: Harness, *extra: str, checks: Optional[List[str]] = None, regions: str = REGION):
    return pcm.run(h.session, ACCOUNT, make_args(*extra, regions=regions), client_factory=h.factory,
                   sleep=lambda _s: None, checks=checks)


def by_check(result: Dict[str, Any], check: str) -> List[Dict[str, Any]]:
    return [f for f in result["findings"] if f["check"] == check]


def denied(st: Stubber, method: str, code: str = "AccessDeniedException") -> None:
    st.add_client_error(method, service_error_code=code, service_message="User is not authorized", http_status_code=403)


# ------------------------------------------------------------------ scenario builders
MGMT_ACCOUNT, ORG_ID = "999988887777", "o-abcde12345"


def queue_org(h: Harness, status: str = "standalone") -> None:
    """Queue organizations:DescribeOrganization for standalone / member / unknown (AccessDenied)."""
    st = h.stub("organizations", US)
    if status == "standalone":
        st.add_client_error("describe_organization", service_error_code="AWSOrganizationsNotInUseException",
                            service_message="Your account is not a member of an organization.", http_status_code=400)
    elif status == "member":
        st.add_response("describe_organization", {"Organization": {
            "Id": ORG_ID, "Arn": f"arn:aws:organizations::{MGMT_ACCOUNT}:organization/{ORG_ID}", "FeatureSet": "ALL",
            "MasterAccountId": MGMT_ACCOUNT, "MasterAccountEmail": "aws-billing@reseller.example",
            "MasterAccountArn": f"arn:aws:organizations::{MGMT_ACCOUNT}:account/{ORG_ID}/{MGMT_ACCOUNT}"}}, {})
    else:
        denied(st, "describe_organization")


def pt_summary(name: str, commitment: Optional[str]) -> Dict[str, Any]:
    s = {"provisionedModelName": name,
         "provisionedModelArn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:provisioned-model/{name}",
         "modelArn": "arn:aws:bedrock:ap-northeast-2::foundation-model/m", "desiredModelArn": "arn:aws:bedrock:ap-northeast-2::foundation-model/m",
         "foundationModelArn": "arn:aws:bedrock:ap-northeast-2::foundation-model/m", "modelUnits": 1, "desiredModelUnits": 1,
         "status": "InService", "creationTime": T0, "lastModifiedTime": T0}
    if commitment:
        s.update(commitmentDuration=commitment, commitmentExpirationTime=T1)
    return s


def queue_clean(h: Harness, *, alias: str = "edu-2026-10-07", delete_alias_call: bool = False,
                skip: Iterable[str] = (), org: str = "standalone") -> None:
    """Queue responses for an account where nothing is left (one region). `skip` = services the test queues itself."""
    skip = set(skip)
    iam = h.stub("iam", US)
    iam.add_response("list_account_aliases", {"AccountAliases": [alias] if alias else [], "IsTruncated": False})
    queue_org(h, org)
    if "marketplace-agreement" not in skip:
        h.stub("marketplace-agreement", US).add_response(
            "search_agreements", {"agreementViewSummaries": []},
            {"catalog": "AWSMarketplace", "filters": pcm.MP_FILTERS})
    if "cloudtrail" not in skip:
        ct = h.stub("cloudtrail", REGION)
        ct.add_response("describe_trails", {"trailList": []}, {"includeShadowTrails": True})
        ct.add_response("list_event_data_stores", {"EventDataStores": []}, {})
    if "rbin" not in skip:
        rb = h.stub("rbin", REGION)
        for rtype in pcm.RBIN_TYPES:
            rb.add_response("list_rules", {"Rules": []}, {"ResourceType": rtype})
    ec2 = h.stub("ec2", REGION)
    if "ec2-rbin" not in skip:
        ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": []}, {})
        ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
        ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": []}, {})
    if "ec2-ri" not in skip:
        ec2.add_response("describe_reserved_instances", {"ReservedInstances": []},
                         {"Filters": [{"Name": "state", "Values": ["active", "payment-pending"]}]})
    h.stub("rds", REGION).add_response("describe_reserved_db_instances", {"ReservedDBInstances": []}, {})
    h.stub("elasticache", REGION).add_response("describe_reserved_cache_nodes", {"ReservedCacheNodes": []}, {})
    h.stub("redshift", REGION).add_response("describe_reserved_nodes", {"ReservedNodes": []}, {})
    if "savingsplans" not in skip:
        h.stub("savingsplans", US).add_response("describe_savings_plans", {"savingsPlans": []},
                                                {"states": ["active", "queued", "payment-pending"]})
    h.stub("bedrock", REGION).add_response("list_provisioned_model_throughputs", {"provisionedModelSummaries": []}, {})
    h.stub("shield", US).add_response("get_subscription_state", {"SubscriptionState": "INACTIVE"}, {})
    h.stub("route53domains", US).add_response("list_domains", {"Domains": []}, {})
    iam.add_response("get_account_summary", {"SummaryMap": {"AccountAccessKeysPresent": 0}}, {})
    if delete_alias_call:
        iam.add_response("delete_account_alias", {}, {"AccountAlias": alias})


MP_USAGE, MP_UPFRONT = "agmt-usage0000000001", "agmt-upfront00000002"
TRAIL_MR = f"arn:aws:cloudtrail:{US}:{ACCOUNT}:trail/org-audit"
TRAIL_LOCAL = f"arn:aws:cloudtrail:{REGION}:{ACCOUNT}:trail/local-trail"
EDS_ARN = f"arn:aws:cloudtrail:{REGION}:{ACCOUNT}:eventdatastore/0b1c2d3e-aaaa-bbbb-cccc-0123456789ab"
RULE_OPEN, RULE_LOCKED = "openRule001", "lockedRul01"


def queue_dirty(h: Harness, *, apply: bool) -> None:
    """Queue responses for an account where every check has something to report (one region)."""
    iam = h.stub("iam", US)
    iam.add_response("list_account_aliases", {"AccountAliases": ["edu-2026-10-07"], "IsTruncated": False})
    queue_org(h, "standalone")

    mp = h.stub("marketplace-agreement", US)
    mp.add_response("search_agreements", {"agreementViewSummaries": [
        {"agreementId": MP_USAGE, "status": "ACTIVE", "endTime": T1, "proposer": {"accountId": "999988887777"},
         "proposalSummary": {"resources": [{"id": "prod-usage", "type": "SaaSProduct"}], "offerId": "offer-1"}},
        {"agreementId": MP_UPFRONT, "status": "ACTIVE", "endTime": T1, "proposer": {"accountId": "999988887777"},
         "proposalSummary": {"resources": [{"id": "prod-contract", "type": "SaaSProduct"}], "offerId": "offer-2"}},
    ]}, {"catalog": "AWSMarketplace", "filters": pcm.MP_FILTERS})
    mp.add_response("get_agreement_terms", {"acceptedTerms": [
        {"usageBasedPricingTerm": {"type": "UsageBasedPricingTerm"}}]}, {"agreementId": MP_USAGE})
    mp.add_response("get_agreement_terms", {"acceptedTerms": [
        {"configurableUpfrontPricingTerm": {"type": "ConfigurableUpfrontPricingTerm"}}]},
        {"agreementId": MP_UPFRONT})
    if apply:  # contract (upfront) agreement is tried first, and the seller-only one fails
        mp.add_client_error("cancel_agreement", service_error_code="ValidationException",
                            service_message="Agreement is not eligible for cancellation", http_status_code=400,
                            expected_params={"agreementId": MP_UPFRONT})
        mp.add_response("cancel_agreement", {}, {"agreementId": MP_USAGE})

    ct = h.stub("cloudtrail", REGION)
    ct.add_response("describe_trails", {"trailList": [
        {"Name": "org-audit", "TrailARN": TRAIL_MR, "HomeRegion": US, "IsMultiRegionTrail": True, "S3BucketName": "b"},
        {"Name": "local-trail", "TrailARN": TRAIL_LOCAL, "HomeRegion": REGION, "IsMultiRegionTrail": False,
         "S3BucketName": "b"},
    ]}, {"includeShadowTrails": True})
    if apply:
        h.stub("cloudtrail", US).add_response("delete_trail", {}, {"Name": "org-audit"})
        ct.add_response("delete_trail", {}, {"Name": "local-trail"})
    ct.add_response("list_event_data_stores", {"EventDataStores": [
        {"EventDataStoreArn": EDS_ARN, "Name": "lake", "Status": "ENABLED", "TerminationProtectionEnabled": True,
         "RetentionPeriod": 2557}]}, {})
    if apply:
        ct.add_response("update_event_data_store", {}, {"EventDataStore": EDS_ARN, "TerminationProtectionEnabled": False})
        ct.add_response("delete_event_data_store", {}, {"EventDataStore": EDS_ARN})

    rb = h.stub("rbin", REGION)
    rb.add_response("list_rules", {"Rules": [
        {"Identifier": RULE_OPEN, "RetentionPeriod": {"RetentionPeriodValue": 365, "RetentionPeriodUnit": "DAYS"}},
        {"Identifier": RULE_LOCKED, "LockState": "locked",
         "RetentionPeriod": {"RetentionPeriodValue": 365, "RetentionPeriodUnit": "DAYS"}},
    ]}, {"ResourceType": "EBS_SNAPSHOT"})
    if apply:
        rb.add_response("delete_rule", {}, {"Identifier": RULE_OPEN})
    rb.add_response("get_rule", {"Identifier": RULE_LOCKED, "LockState": "locked", "LockConfiguration": {
        "UnlockDelay": {"UnlockDelayValue": 7, "UnlockDelayUnit": "DAYS"}}}, {"Identifier": RULE_LOCKED})
    if apply:
        rb.add_response("unlock_rule", {"Identifier": RULE_LOCKED, "LockState": "pending_unlock", "LockEndTime": T1},
                        {"Identifier": RULE_LOCKED})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EC2_IMAGE"})
    rb.add_response("list_rules", {"Rules": []}, {"ResourceType": "EBS_VOLUME"})

    ec2 = h.stub("ec2", REGION)
    ec2.add_response("list_snapshots_in_recycle_bin", {"Snapshots": [
        {"SnapshotId": "snap-0aaaaaaaaaaaaaaaa", "RecycleBinEnterTime": T0, "RecycleBinExitTime": T1}]}, {})
    ec2.add_response("list_images_in_recycle_bin", {"Images": []}, {})
    ec2.add_response("list_volumes_in_recycle_bin", {"Volumes": [
        {"VolumeId": "vol-0aaaaaaaaaaaaaaaa", "Size": 8, "RecycleBinExitTime": T1}]}, {})
    ec2.add_response("describe_reserved_instances", {"ReservedInstances": [
        {"ReservedInstancesId": "ri-1", "InstanceType": "t3.micro", "InstanceCount": 1, "State": "active", "End": T1}]},
        {"Filters": [{"Name": "state", "Values": ["active", "payment-pending"]}]})
    h.stub("rds", REGION).add_response("describe_reserved_db_instances", {"ReservedDBInstances": [
        {"ReservedDBInstanceId": "rds-ri-1", "DBInstanceClass": "db.t3.micro", "DBInstanceCount": 1, "State": "active",
         "StartTime": T0, "Duration": 31536000},
        {"ReservedDBInstanceId": "rds-ri-old", "State": "retired"}]}, {})
    h.stub("elasticache", REGION).add_response("describe_reserved_cache_nodes", {"ReservedCacheNodes": []}, {})
    h.stub("redshift", REGION).add_response("describe_reserved_nodes", {"ReservedNodes": []}, {})
    h.stub("savingsplans", US).add_response("describe_savings_plans", {"savingsPlans": [
        {"savingsPlanId": "sp-1", "state": "active", "savingsPlanType": "Compute", "commitment": "0.1",
         "end": "2027-10-01T00:00:00Z", "returnableUntil": "2026-10-01T00:00:00Z"}]},
        {"states": ["active", "queued", "payment-pending"]})
    h.stub("bedrock", REGION).add_response("list_provisioned_model_throughputs", {"provisionedModelSummaries": [
        pt_summary("pt-committed", "OneMonth"), pt_summary("pt-hourly", None)]}, {})
    sh = h.stub("shield", US)
    sh.add_response("get_subscription_state", {"SubscriptionState": "ACTIVE"}, {})
    sh.add_response("describe_subscription", {"Subscription": {
        "StartTime": T0, "EndTime": T1, "AutoRenew": "ENABLED",
        "SubscriptionArn": f"arn:aws:shield::{ACCOUNT}:subscription/abc",
        "SubscriptionLimits": {"ProtectionLimits": {"ProtectedResourceTypeLimits": []},
                               "ProtectionGroupLimits": {"MaxProtectionGroups": 1, "PatternTypeLimits": {
                                   "ArbitraryPatternLimits": {"MaxMembers": 1}}}}}}, {})
    if apply:
        sh.add_client_error("update_subscription", service_error_code="LockedSubscriptionException",
                            service_message="not in the last 30 days", http_status_code=400,
                            expected_params={"AutoRenew": "DISABLED"})
    r53 = h.stub("route53domains", US)
    r53.add_response("list_domains", {"Domains": [
        {"DomainName": "edu-lab.example", "AutoRenew": True, "Expiry": T1},
        {"DomainName": "old-lab.example", "AutoRenew": False, "Expiry": T1}]}, {})
    if apply:
        r53.add_response("disable_domain_auto_renew", {}, {"DomainName": "edu-lab.example"})
    iam.add_response("get_account_summary", {"SummaryMap": {"AccountAccessKeysPresent": 1}}, {})


# ------------------------------------------------------------------ pure-logic tests
def _f(severity: str, action: str = "none") -> Dict[str, Any]:
    return pcm._finding("x", "global", "id", "d", severity, fixable=True, action=action)


@pytest.mark.parametrize("findings, expected", [
    ([], 0),
    ([_f("WARN"), _f("INFO")], 0),
    ([_f("BLOCKER", "planned")], 1),
    ([_f("BLOCKER", "failed")], 1),
    ([_f("BLOCKER", "none")], 1),
    ([_f("BLOCKER", "done")], 0),
    ([_f("BLOCKER", "done"), _f("BLOCKER", "failed")], 1),
])
def test_preclose_exit_code_logic(findings, expected):
    assert pcm.exit_code_for(findings) == expected
    assert pcm.EXIT_OK == 0 and pcm.EXIT_ISSUES == 1 and pcm.EXIT_ERROR == 2


def test_preclose_finding_shape():
    f = pcm._finding("c", "r", "id", "d", "WARN")
    assert set(f) == {"check", "region", "resource_id", "detail", "severity", "fixable", "action", "error"}
    assert f["fixable"] is False and f["action"] == "none" and f["error"] is None


def test_preclose_cli_flags():
    a = pcm.build_parser().parse_args([])
    assert (a.apply, a.delete_alias, a.all_regions, a.purge_recycle_bin) == (False, False, False, False)
    assert a.regions == "ap-northeast-2,us-east-1"
    a = pcm.build_parser().parse_args(["--apply", "--delete-alias", "--all-regions", "--purge-recycle-bin",
                                       "--role-arn", "arn:aws:iam::1:role/EduCleanupRole", "--external-id", "x",
                                       "--json-out", "o.json", "--profile", "p", "--session-name", "s"])
    assert a.apply and a.delete_alias and a.all_regions and a.purge_recycle_bin


def test_preclose_end_time_helpers():
    assert pcm._end_time(T0, 31536000).startswith("2027-10-01")
    assert pcm._end_time(None, 1) == "?"
    assert pcm._parse_iso("2026-10-01T00:00:00Z") == T0
    assert pcm._parse_iso("not a date") is None
