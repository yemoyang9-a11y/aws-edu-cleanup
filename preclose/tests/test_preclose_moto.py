"""moto-backed tests for services moto implements (cloudtrail trails, iam, route53domains list, ec2 regions).
Everything moto lacks is still served by Stubber via Harness(stub_services=...)."""
import boto3
import pytest
from moto import mock_aws

from test_preclose_harness import ACCOUNT, REGION, US, Harness, by_check, pcm, run_pc

STUBBED = {"marketplace-agreement", "rbin", "savingsplans", "bedrock", "shield", "rds", "elasticache", "redshift",
           "account"}


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": US}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.Session(region_name=US)


def _bucket(session, name="trail-logs"):
    session.client("s3", region_name=US).create_bucket(Bucket=name)
    return name


def test_preclose_moto_multi_region_trail_found_once_and_deleted(aws):
    bucket = _bucket(aws)
    aws.client("cloudtrail", region_name=US).create_trail(Name="global-audit", S3BucketName=bucket,
                                                         IsMultiRegionTrail=True)
    aws.client("cloudtrail", region_name=REGION).create_trail(Name="seoul-only", S3BucketName=bucket)
    aws.client("iam").create_account_alias(AccountAlias="edu-2026-10-07")

    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, checks=["cloudtrail_trails"], regions=f"{REGION},{US}")
    trails = by_check(result, "cloudtrail_trail")
    assert sorted(f["resource_id"].rsplit("/", 1)[1] for f in trails) == ["global-audit", "seoul-only"]
    assert {f["resource_id"].rsplit("/", 1)[1]: f["region"] for f in trails} == {"global-audit": US,
                                                                               "seoul-only": REGION}
    assert all(f["severity"] == "BLOCKER" and f["action"] == "planned" for f in trails)
    assert h.mutating() == [] and code == pcm.EXIT_ISSUES

    h2 = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h2, "--apply", checks=["cloudtrail_trails"], regions=f"{REGION},{US}")
    assert all(f["action"] == "done" for f in by_check(result, "cloudtrail_trail")) and code == pcm.EXIT_OK
    for r in (US, REGION):
        assert aws.client("cloudtrail", region_name=r).describe_trails()["trailList"] == []


def test_preclose_moto_shadow_trail_of_unscanned_home_region(aws):
    """A multi-region trail homed in us-west-2 is still found when only Seoul is scanned, and is deleted
    in us-west-2 (its home region)."""
    bucket = _bucket(aws)
    aws.client("cloudtrail", region_name="us-west-2").create_trail(Name="west-mr", S3BucketName=bucket,
                                                                  IsMultiRegionTrail=True)
    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, "--apply", checks=["cloudtrail_trails"], regions=REGION)
    f = by_check(result, "cloudtrail_trail")[0]
    assert f["region"] == "us-west-2" and f["action"] == "done"
    assert [c[1] for c in h.calls if c[2] == "DeleteTrail"] == ["us-west-2"]
    assert aws.client("cloudtrail", region_name="us-west-2").describe_trails()["trailList"] == []
    assert code == pcm.EXIT_OK


def test_preclose_moto_iam_alias_and_root_keys(aws):
    aws.client("iam").create_account_alias(AccountAlias="edu-2026-10-07")
    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, checks=["root_access_keys", "account_alias"])
    assert by_check(result, "root_access_keys") == []  # moto: AccountAccessKeysPresent == 0
    alias = by_check(result, "account_alias")[0]
    assert alias["resource_id"] == "edu-2026-10-07" and alias["action"] == "none" and "--delete-alias" in alias["detail"]

    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, "--apply", "--delete-alias", checks=["account_alias"])
    assert by_check(result, "account_alias")[0]["action"] == "done" and code == pcm.EXIT_OK
    assert aws.client("iam").list_account_aliases()["AccountAliases"] == []


def test_preclose_moto_route53_domains(aws):
    contact = {"FirstName": "Edu", "LastName": "Ops", "ContactType": "COMPANY", "OrganizationName": "Edu",
               "AddressLine1": "1 Main St", "City": "Seoul", "CountryCode": "KR", "ZipCode": "04524",
               "PhoneNumber": "+82.212345678", "Email": "ops@example.com"}
    aws.client("route53domains", region_name=US).register_domain(
        DomainName="edu-lab.com", DurationInYears=2, AutoRenew=True,
        AdminContact=contact, RegistrantContact=contact, TechContact=contact)
    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, checks=["route53_domains"])
    f = by_check(result, "route53_domain")[0]
    assert f["resource_id"] == "edu-lab.com" and f["severity"] == "WARN" and f["action"] == "planned"
    assert h.mutating() == [] and code == pcm.EXIT_OK

    # moto does not implement DisableDomainAutoRenew -> the attempt is recorded as failed, run continues.
    h = Harness(aws, stub_services=STUBBED)
    result, code = run_pc(h, "--apply", checks=["route53_domains", "reminders"])
    f = by_check(result, "route53_domain")[0]
    assert "DisableDomainAutoRenew" in h.ops() and f["action"] in ("done", "failed")
    assert by_check(result, "closure_reminder") and code == pcm.EXIT_OK


def test_preclose_moto_all_regions_fallback_to_describe_regions(aws):
    """moto has no account:ListRegions -> falls back to ec2:DescribeRegions (moto returns every region)."""
    h = Harness(aws, stub_services=set())  # account is NOT stubbed: real moto 404 -> fallback
    result, _ = run_pc(h, "--all-regions", checks=[])
    assert REGION in result["regions"] and US in result["regions"] and len(result["regions"]) > 10


def test_preclose_moto_dry_run_full_account_no_mutations(aws):
    """Full dry-run with moto + stubs: no mutating call, unimplemented moto ops degrade to WARN."""
    bucket = _bucket(aws)
    aws.client("cloudtrail", region_name=REGION).create_trail(Name="seoul-only", S3BucketName=bucket)
    aws.client("iam").create_account_alias(AccountAlias="edu-2026-10-07")
    h = Harness(aws, stub_services=STUBBED)
    h.stub("marketplace-agreement", US).add_response("search_agreements", {"agreementViewSummaries": []})
    rb = h.stub("rbin", REGION)
    for rtype in pcm.RBIN_TYPES:
        rb.add_response("list_rules", {"Rules": []}, {"ResourceType": rtype})
    h.stub("savingsplans", US).add_response("describe_savings_plans", {"savingsPlans": []})
    h.stub("bedrock", REGION).add_response("list_provisioned_model_throughputs", {"provisionedModelSummaries": []})
    h.stub("shield", US).add_response("get_subscription_state", {"SubscriptionState": "INACTIVE"})
    h.stub("rds", REGION).add_response("describe_reserved_db_instances", {"ReservedDBInstances": []})
    h.stub("elasticache", REGION).add_response("describe_reserved_cache_nodes", {"ReservedCacheNodes": []})
    h.stub("redshift", REGION).add_response("describe_reserved_nodes", {"ReservedNodes": []})

    result, code = run_pc(h)
    h.assert_all_consumed()
    assert h.mutating() == []
    assert by_check(result, "cloudtrail_trail")[0]["action"] == "planned"
    # moto lacks ListEventDataStores / ec2 recycle-bin / DescribeReservedInstances: graceful WARNs
    failed = result["summary"]["checks_failed"]
    assert any(c.startswith("cloudtrail_event_data_store@") for c in failed)
    assert any(c.startswith("ec2_reserved_instance@") for c in failed)
    assert code == pcm.EXIT_ISSUES and result["account_id"] == ACCOUNT
