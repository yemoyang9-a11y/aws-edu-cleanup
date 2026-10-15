"""Inventory side of verify_cost.py using moto (ec2/rds/s3/lambda/dynamodb) + an AccessDenied region."""
import datetime as dt
import io
import json
import zipfile

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from moto import mock_aws

import verify_cost as vc
from common import session as common_session

DENIED_REGION = "eu-west-1"


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")  # else OwnerIds=self returns public snapshots
    with mock_aws():
        yield


def _deny_region(session, region):
    """Make every API call to `region` fail with AccessDenied (simulates an SCP/permission gap)."""
    def handler(model, request_signer=None, **kwargs):
        if request_signer is not None and request_signer.region_name == region:
            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "not authorized in %s" % region}}, model.name)
    session.events.register("before-call", handler)


def _lambda_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.py", "def handler(e, c):\n    return 1\n")
    return buf.getvalue()


def _seed(region="ap-northeast-2"):
    ec2 = boto3.client("ec2", region_name=region)
    ec2.run_instances(ImageId="ami-12345678", MinCount=2, MaxCount=2, InstanceType="t3.micro")
    vol = ec2.create_volume(AvailabilityZone=region + "a", Size=8)
    ec2.create_snapshot(VolumeId=vol["VolumeId"])
    ec2.allocate_address(Domain="vpc")
    boto3.client("rds", region_name=region).create_db_instance(
        DBInstanceIdentifier="student-db", DBInstanceClass="db.t3.micro", Engine="mysql",
        MasterUsername="admin", MasterUserPassword="password123", AllocatedStorage=20)
    boto3.client("dynamodb", region_name=region).create_table(
        TableName="student-table", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])
    role = boto3.client("iam", region_name="us-east-1").create_role(
        RoleName="lambda-role", AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}))
    boto3.client("lambda", region_name=region).create_function(
        FunctionName="student-fn", Runtime="python3.12", Role=role["Role"]["Arn"], Handler="index.handler",
        Code={"ZipFile": _lambda_zip()})
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="student-bucket-apn2", CreateBucketConfiguration={"LocationConstraint": region})
    s3.create_bucket(Bucket="student-bucket-use1")
    s3.create_bucket(Bucket="edu-bootstrap-artifacts")  # protected by CONTRACTS → not counted


def test_verify_inventory_counts_and_access_denied_region(aws):
    _seed()
    session = boto3.Session(region_name="us-east-1")
    _deny_region(session, DENIED_REGION)
    inv = vc.run_inventory(session, ["ap-northeast-2", "us-east-1", DENIED_REGION])

    apn2 = inv.counts["ap-northeast-2"]
    assert apn2["ec2_instances"] == 2
    assert apn2["ebs_volumes"] >= 1          # explicit volume (+ moto root volumes)
    assert apn2["ebs_snapshots"] == 1
    assert apn2["elastic_ips"] == 1
    assert apn2["rds_instances"] == 1
    assert apn2["dynamodb_tables"] == 1
    assert apn2["lambda_functions"] == 1
    assert apn2["s3_buckets"] == 1
    assert inv.counts["us-east-1"]["s3_buckets"] == 1
    assert inv.counts["us-east-1"]["ec2_instances"] == 0
    assert inv.protected["us-east-1"]["s3_buckets"] == ["edu-bootstrap-artifacts"]
    assert "student-db" in inv.items["ap-northeast-2"]["rds_instances"]

    # the denied region is "blocked by policy" for every type (or n/a where the service does not exist),
    # never a crash, never a count, never an inventory error
    denied = inv.blocked[DENIED_REGION]
    assert denied["ec2_instances"] == "AccessDeniedException"
    assert denied["dynamodb_tables"] == "AccessDeniedException"
    assert len(denied) + len(inv.not_applicable.get(DENIED_REGION, {})) == len(vc.REGIONAL_CHECKS)
    assert DENIED_REGION not in inv.unchecked
    assert DENIED_REGION not in inv.counts or sum(inv.counts[DENIED_REGION].values()) == 0
    assert not any(e["region"] == DENIED_REGION for e in inv.errors)
    assert inv.blocked_count() == len(denied)


def test_verify_inventory_empty_account_counts_zero(aws):
    session = boto3.Session(region_name="us-east-1")
    inv = vc.run_inventory(session, ["ap-northeast-2"])
    assert inv.total() == 0
    for rtype in ("ec2_instances", "ebs_snapshots", "rds_instances", "lambda_functions", "dynamodb_tables",
                  "nat_gateways", "load_balancers_v2", "load_balancers_classic", "sagemaker_endpoints",
                  "bedrock_custom_models", "ecs_clusters", "eks_clusters"):
        assert inv.counts["ap-northeast-2"].get(rtype) == 0, rtype


def test_verify_inventory_terminated_instances_ignored(aws):
    ec2 = boto3.client("ec2", region_name="ap-northeast-2")
    ids = [i["InstanceId"] for i in ec2.run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)["Instances"]]
    ec2.terminate_instances(InstanceIds=ids)
    inv = vc.run_inventory(boto3.Session(region_name="us-east-1"), ["ap-northeast-2"])
    assert inv.counts["ap-northeast-2"]["ec2_instances"] == 0


def _ce_client_with_zero_cost():
    ce = boto3.client("ce", region_name="us-east-1")
    stub = Stubber(ce)
    days = []
    for d in ("2026-10-11", "2026-10-12", "2026-10-13"):
        nxt = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
        days.append({"TimePeriod": {"Start": d, "End": nxt}, "Total": {}, "Groups": [], "Estimated": True})
    stub.add_response("get_cost_and_usage", {"ResultsByTime": days})
    stub.activate()
    return ce


def _route_ce(monkeypatch, ce):
    real = common_session.client

    def fake_client(session, service, region=None):
        return ce if service == "ce" else real(session, service, region)
    monkeypatch.setattr(vc, "client", fake_client)


def test_verify_inventory_leftovers_make_zero_cost_residual(aws, monkeypatch, tmp_path, capsys):
    _seed()
    _route_ce(monkeypatch, _ce_client_with_zero_cost())
    out = tmp_path / "r.json"
    code = vc.main(["--cleanup-date", "2026-10-10", "--now", "2026-10-14T03:00", "--inventory",
                    "--regions", "ap-northeast-2,us-east-1,global", "--json-out", str(out)])
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["flagged"] == []
    assert code == 1 and res["status"] == "RESIDUAL"
    assert res["inventory_regions"] == ["ap-northeast-2", "us-east-1"]  # 'global' dropped
    assert res["inventory"]["ap-northeast-2"]["rds_instances"] == 1
    assert "인벤토리 잔여물" in capsys.readouterr().out


def test_verify_inventory_clean_account_and_unchecked_region_stays_clean(aws, monkeypatch, tmp_path):
    _route_ce(monkeypatch, _ce_client_with_zero_cost())
    real_get_session = vc.get_session

    def get_session(args):
        s = real_get_session(args)
        _deny_region(s, DENIED_REGION)
        return s
    monkeypatch.setattr(vc, "get_session", get_session)
    out = tmp_path / "r.json"
    code = vc.main(["--cleanup-date", "2026-10-10", "--now", "2026-10-14T03:00", "--inventory",
                    "--regions", "ap-northeast-2,%s" % DENIED_REGION, "--json-out", str(out)])
    res = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and res["status"] == "CLEAN"
    assert res["inventory_complete"] is False
    assert res["blocked_by_policy"][DENIED_REGION]["ec2_instances"] == "AccessDeniedException"
    assert res["blocked_by_policy_count"] >= 1 and DENIED_REGION not in res["inventory_unchecked"]


def test_verify_all_regions_uses_account_list_regions_paginated(monkeypatch):
    acct = boto3.client("account", region_name="us-east-1")
    stub = Stubber(acct)
    want = {"RegionOptStatusContains": ["ENABLED", "ENABLED_BY_DEFAULT"]}
    stub.add_response("list_regions", {"Regions": [{"RegionName": "us-east-1", "RegionOptStatus": "ENABLED_BY_DEFAULT"}],
                                       "NextToken": "t1"}, want)
    stub.add_response("list_regions", {"Regions": [{"RegionName": "ap-northeast-2", "RegionOptStatus": "ENABLED_BY_DEFAULT"},
                                                   {"RegionName": "af-south-1", "RegionOptStatus": "ENABLED"}]},
                      dict(want, NextToken="t1"))
    stub.activate()
    monkeypatch.setattr(vc, "client", lambda s, svc, region=None: acct)
    args = vc.build_parser().parse_args(["--all-regions"])
    errors = []
    assert vc.resolve_inventory_regions(object(), args, errors) == ["af-south-1", "ap-northeast-2", "us-east-1"]
    assert errors == []
    stub.assert_no_pending_responses()


def test_verify_all_regions_falls_back_to_describe_regions(monkeypatch):
    acct = boto3.client("account", region_name="us-east-1")
    ec2 = boto3.client("ec2", region_name="us-east-1")
    sa, se = Stubber(acct), Stubber(ec2)
    sa.add_client_error("list_regions", "AccessDeniedException", "no account:ListRegions")
    se.add_response("describe_regions", {"Regions": [{"RegionName": "us-east-1"}, {"RegionName": "ap-northeast-2"}]})
    sa.activate(); se.activate()
    monkeypatch.setattr(vc, "client", lambda s, svc, region=None: acct if svc == "account" else ec2)
    args = vc.build_parser().parse_args(["--all-regions"])
    errors = []
    assert vc.resolve_inventory_regions(object(), args, errors) == ["ap-northeast-2", "us-east-1"]
    assert errors and errors[0]["stage"] == "regions"
