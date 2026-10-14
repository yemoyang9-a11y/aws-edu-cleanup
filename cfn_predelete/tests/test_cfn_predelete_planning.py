"""Planning: which stacks, in which order, and dry-run makes no changes."""
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cfn_predelete_fakes import (ACCOUNT, READ_ONLY_CFN, FakeCfn, FakeClock, by_name, ps,  # noqa: E402
                                      run, stack_id)

R = "ap-northeast-2"


@pytest.fixture
def iam(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
                 "MOTO_IAM_LOAD_MANAGED_POLICIES": "true"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.client("iam", region_name="us-east-1")


def test_protected_bootstrap_stacks_are_skipped(iam):
    f = FakeCfn(R)
    f.add("edu-bootstrap", tp=True)
    f.add("edu-bootstrap-v2")
    f.add("app")
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["edu-bootstrap"]["final_status"] == ps.SKIPPED
    assert res["edu-bootstrap-v2"]["final_status"] == ps.SKIPPED
    assert res["app"]["final_status"] == ps.GONE
    touched = {p["StackName"] for p in f.calls_of("DeleteStack") + f.calls_of("UpdateTerminationProtection")}
    assert touched == {stack_id(R, "app")}
    assert out["status"] == "OK" and out["exit_code"] == ps.EXIT_OK


def test_stack_that_created_protected_identity_is_skipped(iam):
    f = FakeCfn(R)
    f.add("hub-role-stack")
    f.own("EduCleanupRole", "hub-role-stack")
    f.add("cloudshell-user-stack")
    f.own("student-admin", "cloudshell-user-stack")
    f.add("app")
    out = run({R: f}, iam, protect_ids=["EduCleanupRole", "student-admin"])
    res = by_name(out)
    assert res["hub-role-stack"]["final_status"] == ps.SKIPPED
    assert res["cloudshell-user-stack"]["final_status"] == ps.SKIPPED
    assert "EduCleanupRole" in " ".join(res["hub-role-stack"]["notes"])
    assert res["app"]["final_status"] == ps.GONE


def test_protected_identities_from_caller_arn():
    assert ps.protected_identities("arn:aws:iam::123456789012:user/kim") == ["EduCleanupRole", "kim"]
    assert ps.protected_identities("arn:aws:iam::123456789012:user/path/kim") == ["EduCleanupRole", "kim"]
    assert ps.protected_identities(
        "arn:aws:sts::123456789012:assumed-role/EduCleanupRole/edu-cleanup") == ["EduCleanupRole"]
    assert ps.protected_identities("arn:aws:iam::123456789012:root") == ["EduCleanupRole"]


def test_nested_stacks_are_excluded_and_counted(iam):
    f = FakeCfn(R)
    f.add("root")
    f.add("root-NestedA", parent="root")
    f.add("root-NestedA-Deeper", parent="root-NestedA")
    f.add("root-NestedB", parent="root")
    out = run({R: f}, iam, apply=False)
    res = by_name(out)
    assert list(res) == ["root"]
    assert res["root"]["nested_count"] == 3
    assert res["root"]["final_status"] == ps.PLANNED


def test_orphaned_nested_stack_is_deleted_itself(iam):
    f = FakeCfn(R)
    f.add("leftover-Nested", parent="already-gone-root", status="DELETE_FAILED")
    out = run({R: f}, iam, apply=False)
    r = by_name(out)["leftover-Nested"]
    assert r["orphan_nested"] is True and r["action"] == "FORCE_DELETE"


def test_listing_is_paginated_and_delete_complete_ignored(iam):
    f = FakeCfn(R, page_size=2)
    for i in range(5):
        f.add(f"s{i}")
    f.add("old", status="DELETE_COMPLETE")
    out = run({R: f}, iam, apply=False)
    assert sorted(by_name(out)) == [f"s{i}" for i in range(5)]
    assert len(f.calls_of("DescribeStacks")) == 3


def test_cdktoolkit_is_ordered_and_deleted_last(iam):
    f = FakeCfn(R)
    f.add("CDKToolkit", tp=True)
    f.add("AppStackA", polls_to_delete=4)
    f.add("AppStackB", polls_to_delete=2)
    f.add("CustomToolkit", outputs=[{"OutputKey": "BootstrapVersion", "OutputValue": "28"}])
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["CDKToolkit"]["order"] > res["AppStackA"]["order"]
    assert res["CustomToolkit"]["order"] > res["AppStackB"]["order"]
    assert res["CDKToolkit"]["is_cdk_toolkit"] and res["CustomToolkit"]["is_cdk_toolkit"]
    assert set(f.deleted_order[-2:]) == {"CDKToolkit", "CustomToolkit"}
    # the toolkit DeleteStack is sent only after both app stacks are already gone
    ops = f.calls
    first_tk = min(i for i, (op, p) in enumerate(ops)
                   if op == "DeleteStack" and p["StackName"] == stack_id(R, "CDKToolkit"))
    assert f.deleted_order.index("AppStackA") < f.deleted_order.index("CDKToolkit")
    assert not any(op == "DeleteStack" and p["StackName"] in (stack_id(R, "AppStackA"), stack_id(R, "AppStackB"))
                   for op, p in ops[first_tk:])
    assert all(r["final_status"] == ps.GONE for r in out["results"])


def test_toolkit_not_started_when_app_stack_fails(iam):
    f = FakeCfn(R)
    f.add("CDKToolkit")
    f.add("App", never_finishes=True)
    out = run({R: f}, iam, timeout_s=120)
    res = by_name(out)
    assert res["App"]["final_status"] == ps.TIMEOUT
    assert res["CDKToolkit"]["final_status"] == ps.TIMEOUT
    assert "not started" in res["CDKToolkit"]["error"]
    assert stack_id(R, "CDKToolkit") not in {p["StackName"] for p in f.calls_of("DeleteStack")}


def test_importers_are_deleted_before_exporters(iam):
    f = FakeCfn(R)
    f.add("network", polls_to_delete=1)
    f.add("service", polls_to_delete=3)
    f.export("network", "VpcId", ["service"])
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["network"]["depends_on"] == ["service"]
    assert res["network"]["order"] == 2 and res["service"]["order"] == 1
    assert f.deleted_order == ["service", "network"]


def test_dry_run_makes_no_mutating_calls(iam):
    f = FakeCfn(R)
    f.add("edu-bootstrap")
    f.add("cdk-app", role=f"arn:aws:iam::{ACCOUNT}:role/cdk-hnb659fds-cfn-exec-role", tp=True)
    f.add("broken", status="DELETE_FAILED")
    f.add("busy", status="DELETE_IN_PROGRESS")
    f.add("CDKToolkit")
    f.export("cdk-app", "X", ["broken"])
    iam_calls = []
    iam.meta.events.register("before-parameter-build.iam", lambda model, **_: iam_calls.append(model.name))
    roles_before = iam.list_roles()["Roles"]
    out = run({R: f}, iam, apply=False)

    assert set(f.ops()) <= READ_ONLY_CFN
    assert set(iam_calls) <= {"GetRole", "ListRoles"}
    assert iam.list_roles()["Roles"] == roles_before
    assert out["applied"] is False and out["status"] == "OK" and out["exit_code"] == 0
    res = by_name(out)
    assert res["edu-bootstrap"]["final_status"] == ps.SKIPPED
    assert {r["final_status"] for n, r in res.items() if n != "edu-bootstrap"} == {ps.PLANNED}
    assert res["broken"]["action"] == "FORCE_DELETE"
    assert res["busy"]["action"] == "POLL"
    app = res["cdk-app"]
    assert app["role_missing"] is True and app["termination_protection"] is True
    assert app["role_source"] == "temp-role-planned"
    assert res["CDKToolkit"]["order"] == max(r["order"] for r in out["results"])
    assert out["role_fallback"]["temp_role_created"] is False
    table = ps.render_table(out)
    assert "DRY-RUN" in table and "role missing" in table and "TP" in table
