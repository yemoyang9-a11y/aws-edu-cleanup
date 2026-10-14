"""Missing stack service roles: RoleARN fallback order and temporary-role lifecycle."""
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cfn_predelete_fakes import ACCOUNT, FakeCfn, FakeClock, by_name, ps, run, stack_id  # noqa: E402

R = "ap-northeast-2"
GONE_ROLE = f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-cfn-exec-role-{ACCOUNT}-{R}"
EDU_ARN = f"arn:aws:iam::{ACCOUNT}:role/EduCleanupRole"
TEMP_ARN = f"arn:aws:iam::{ACCOUNT}:role/edu-cfn-delete-temp"
CFN_TRUST = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"Service": "cloudformation.amazonaws.com"}, "Action": "sts:AssumeRole"},
    {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::999999999999:root"}, "Action": "sts:AssumeRole"}]})
HUB_ONLY_TRUST = json.dumps({"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::999999999999:root"}, "Action": "sts:AssumeRole"}]})


@pytest.fixture
def iam(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
                 "MOTO_IAM_LOAD_MANAGED_POLICIES": "true"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.client("iam", region_name="us-east-1")


def cfn_with_missing_role(iam) -> FakeCfn:
    f = FakeCfn(R)
    f.add("cdk-app", role=GONE_ROLE)

    def role_ok(arn):                      # CloudFormation can assume a role iff it exists in (moto) IAM
        try:
            return iam.get_role(RoleName=arn.rsplit("/", 1)[-1])["Role"]["Arn"] == arn
        except iam.exceptions.NoSuchEntityException:
            return False
    f.role_ok = role_ok
    return f


def role_names(iam):
    return {r["RoleName"] for r in iam.list_roles()["Roles"]}


def test_cli_role_arn_is_preferred(iam):
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    iam.create_role(RoleName="my-cfn-role", AssumeRolePolicyDocument=CFN_TRUST)
    f = cfn_with_missing_role(iam)
    mine = f"arn:aws:iam::{ACCOUNT}:role/my-cfn-role"
    out = run({R: f}, iam, cli_role_arn=mine)
    r = by_name(out)["cdk-app"]
    assert r["final_status"] == ps.GONE and r["role_used"] == mine and r["role_source"] == "cli"
    assert r["role_missing"] is True
    assert all(p["RoleARN"] == mine for p in f.calls_of("DeleteStack"))
    assert out["role_fallback"]["source"] == "cli"


def test_educleanuprole_is_used_when_it_trusts_cloudformation(iam):
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    f = cfn_with_missing_role(iam)
    f.add("plain-app")                       # no stored role: never gets RoleARN
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["cdk-app"]["role_used"] == EDU_ARN and res["cdk-app"]["role_source"] == "EduCleanupRole"
    assert res["cdk-app"]["final_status"] == ps.GONE
    assert res["plain-app"]["role_used"] is None and res["plain-app"]["role_source"] == "caller"
    plain = [p for p in f.calls_of("DeleteStack") if p["StackName"] == stack_id(R, "plain-app")]
    assert "RoleARN" not in plain[0]
    assert "edu-cfn-delete-temp" not in role_names(iam)


def test_existing_stored_role_is_left_alone(iam):
    iam.create_role(RoleName="svc-role", AssumeRolePolicyDocument=CFN_TRUST)
    f = cfn_with_missing_role(iam)
    stored = f"arn:aws:iam::{ACCOUNT}:role/svc-role"
    f.add("sam-app", role=stored)
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    out = run({R: f}, iam)
    r = by_name(out)["sam-app"]
    assert r["role_used"] == stored and r["role_source"] == "stored" and r["role_missing"] is False
    assert "RoleARN" not in [p for p in f.calls_of("DeleteStack") if p["StackName"] == stack_id(R, "sam-app")][0]


def test_temp_role_created_used_and_deleted(iam):
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=HUB_ONLY_TRUST)  # old bootstrap
    f = cfn_with_missing_role(iam)
    clock = FakeClock()
    seen = {}
    orig = f._op_DeleteStack

    def spy(p):
        if p.get("RoleARN") == TEMP_ARN:
            seen["policies"] = [a["PolicyArn"] for a in
                                iam.list_attached_role_policies(RoleName="edu-cfn-delete-temp")["AttachedPolicies"]]
            seen["trust"] = iam.get_role(RoleName="edu-cfn-delete-temp")["Role"]["AssumeRolePolicyDocument"]
        return orig(p)
    f._op_DeleteStack = spy
    out = run({R: f}, iam, clock=clock, propagation_delay=10)
    r = by_name(out)["cdk-app"]
    assert r["final_status"] == ps.GONE and r["role_used"] == TEMP_ARN and r["role_source"] == "temp-role"
    assert seen["policies"] == ["arn:aws:iam::aws:policy/AdministratorAccess"]
    assert "cloudformation.amazonaws.com" in json.dumps(seen["trust"])
    assert 10 in clock.sleeps                                    # IAM propagation wait
    assert "edu-cfn-delete-temp" not in role_names(iam)
    assert out["role_fallback"]["temp_role_created"] and out["role_fallback"]["temp_role_deleted"]
    assert out["exit_code"] == 0


def test_temp_role_deleted_when_a_region_worker_crashes(iam, monkeypatch):
    f = cfn_with_missing_role(iam)

    def crash(self, cfn, t):
        self.resolver.fallback()                  # temp role gets created ...
        raise KeyboardInterrupt                   # ... then the worker dies

    monkeypatch.setattr(ps.Runner, "_start", crash)
    out = run({R: f}, iam)
    assert out["exit_code"] == ps.EXIT_ERROR and out["status"] == "ERROR"
    assert "edu-cfn-delete-temp" not in role_names(iam)
    assert out["role_fallback"]["temp_role_deleted"] is True


def test_temp_role_deleted_when_runner_raises(iam, monkeypatch):
    f = cfn_with_missing_role(iam)
    created = {}

    def boom(self):
        self.resolver.fallback()
        created["arn"] = self.resolver.role_arn
        raise RuntimeError("unexpected")

    monkeypatch.setattr(ps.Runner, "run", boom)
    with pytest.raises(RuntimeError):
        run({R: f}, iam)
    assert created["arn"] == TEMP_ARN
    assert "edu-cfn-delete-temp" not in role_names(iam)


def test_no_temp_role_in_dry_run(iam):
    f = cfn_with_missing_role(iam)
    out = run({R: f}, iam, apply=False)
    r = by_name(out)["cdk-app"]
    assert r["role_source"] == "temp-role-planned" and r["final_status"] == ps.PLANNED
    assert "edu-cfn-delete-temp" not in role_names(iam)
    assert out["role_fallback"]["temp_role_created"] is False


def test_role_rejected_at_delete_time_switches_to_fallback(iam):
    """get_role says the role exists, but CloudFormation still cannot assume it (trust/permissions)."""
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    iam.create_role(RoleName="broken-trust", AssumeRolePolicyDocument=HUB_ONLY_TRUST)
    stored = f"arn:aws:iam::{ACCOUNT}:role/broken-trust"
    f = FakeCfn(R, existing_roles={EDU_ARN})
    f.add("app", role=stored)
    out = run({R: f}, iam)
    r = by_name(out)["app"]
    assert r["final_status"] == ps.GONE and r["role_used"] == EDU_ARN and r["role_missing"] is False
    assert [p.get("RoleARN") for p in f.calls_of("DeleteStack")] == [None, EDU_ARN]


def test_permission_delete_failed_retries_with_fallback_role_before_force(iam):
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    iam.create_role(RoleName="stripped", AssumeRolePolicyDocument=CFN_TRUST)
    stored = f"arn:aws:iam::{ACCOUNT}:role/stripped"
    f = FakeCfn(R, existing_roles={EDU_ARN, stored})
    sid = f.add("app", role=stored)
    orig = f._op_DescribeStacks

    def describe(p):
        st = f.stacks.get(sid)
        k = f.knobs[sid]
        if p.get("StackName") and st and st["StackStatus"] == "DELETE_IN_PROGRESS" and not k.get("failed_once"):
            k["failed_once"] = True           # first delete (stored, stripped role) fails on permissions
            st["StackStatus"] = "DELETE_FAILED"
            st["StackStatusReason"] = "User: ...stripped is not authorized to perform: lambda:DeleteFunction"
            return f._ok({"Stacks": [dict(st)]})
        return orig(p)
    f._op_DescribeStacks = describe
    out = run({R: f}, iam)
    r = by_name(out)["app"]
    calls = f.calls_of("DeleteStack")
    assert [(c.get("RoleARN"), c.get("DeletionMode")) for c in calls] == [(None, None), (EDU_ARN, None)]
    assert r["role_used"] == EDU_ARN and r["final_status"] == ps.GONE and r["used_force"] is False


def test_no_fallback_available_is_failed_not_crash(iam, monkeypatch):
    f = cfn_with_missing_role(iam)
    f.add("other")

    def deny(*a, **kw):
        raise iam.exceptions.ClientError({"Error": {"Code": "AccessDenied", "Message": "no iam:CreateRole"}},
                                         "CreateRole")
    monkeypatch.setattr(iam, "create_role", deny)
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["cdk-app"]["final_status"] == ps.FAILED and "no fallback role" in res["cdk-app"]["error"]
    assert res["other"]["final_status"] == ps.GONE
    assert out["exit_code"] == ps.EXIT_ISSUES
    assert "AccessDenied" in out["role_fallback"]["error"]


def test_initial_permission_delete_failed_uses_fallback_role_without_force(iam):
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument=CFN_TRUST)
    iam.create_role(RoleName="stripped", AssumeRolePolicyDocument=CFN_TRUST)
    stored = f"arn:aws:iam::{ACCOUNT}:role/stripped"
    f = FakeCfn(R, existing_roles={EDU_ARN, stored})
    sid = f.add("app", role=stored, status="DELETE_FAILED")
    f.stacks[sid]["StackStatusReason"] = "Role ...stripped is not authorized to perform: ec2:DeleteVpc"
    out = run({R: f}, iam)
    r = by_name(out)["app"]
    assert r["final_status"] == ps.GONE and r["used_force"] is False and r["action"] == "DELETE"
    assert [(c.get("RoleARN"), c.get("DeletionMode")) for c in f.calls_of("DeleteStack")] == [(EDU_ARN, None)]
