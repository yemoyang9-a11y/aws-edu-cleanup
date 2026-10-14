"""Deletion behaviour: EXPRESS, FORCE_DELETE_STACK, polling, timeout, parallelism."""
import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cfn_predelete_fakes import FakeCfn, FakeClock, by_name, ps, run, stack_id  # noqa: E402

R = "ap-northeast-2"
R2 = "us-east-1"
MODERN_BOTOCORE = "DeploymentConfig" in boto3.client(
    "cloudformation", region_name=R, aws_access_key_id="x", aws_secret_access_key="y"
).meta.service_model.operation_model("DeleteStack").input_shape.members
needs_modern_botocore = pytest.mark.skipif(not MODERN_BOTOCORE, reason="botocore < 1.43 (Python 3.9) lacks DeploymentConfig")
CONTRACT_RESULT_KEYS = {"region", "stack_name", "stack_id", "initial_status", "action", "final_status",
                        "used_express", "used_force", "role_used", "error"}


@pytest.fixture
def iam(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.client("iam", region_name="us-east-1")


@needs_modern_botocore
def test_installed_botocore_has_express_and_force_shapes():
    c = boto3.client("cloudformation", region_name=R, aws_access_key_id="x", aws_secret_access_key="y")
    shape = c.meta.service_model.operation_model("DeleteStack").input_shape
    assert shape.members["DeploymentConfig"].members["Mode"].enum == ["STANDARD", "EXPRESS"]
    assert "FORCE_DELETE_STACK" in shape.members["DeletionMode"].enum
    assert ps.model_supports(c, "DeploymentConfig") and ps.model_supports(c, "DeletionMode")


@needs_modern_botocore
def test_exact_deletestack_request_shape_with_stubber():
    """Pins the wire parameters: TP off, then DeleteStack(StackName, DeploymentConfig={'Mode': 'EXPRESS'})."""
    c = boto3.client("cloudformation", region_name=R, aws_access_key_id="x", aws_secret_access_key="y")
    sid = stack_id(R, "app")
    task = ps.StackTask(region=R, stack_name="app", stack_id=sid, initial_status="CREATE_COMPLETE",
                        termination_protection=True)
    runner = ps.Runner({R: c}, ps.RoleResolver(None, "123456789012"), apply=True, timeout_s=60,
                       max_parallel=1, poll_interval=1)
    with Stubber(c) as stub:
        stub.add_response("update_termination_protection", {"StackId": sid},
                          {"StackName": sid, "EnableTerminationProtection": False})
        stub.add_response("delete_stack", {}, {"StackName": sid, "DeploymentConfig": {"Mode": "EXPRESS"}})
        runner._start(c, task)
        stub.assert_no_pending_responses()
    assert task.used_express and task.tp_disabled and task.final_status is None


@needs_modern_botocore
def test_exact_force_request_shape_with_stubber():
    c = boto3.client("cloudformation", region_name=R, aws_access_key_id="x", aws_secret_access_key="y")
    sid = stack_id(R, "broken")
    role = "arn:aws:iam::123456789012:role/EduCleanupRole"
    task = ps.StackTask(region=R, stack_name="broken", stack_id=sid, initial_status="DELETE_FAILED")
    task.role_override = role
    runner = ps.Runner({R: c}, ps.RoleResolver(None, "123456789012"), apply=True, timeout_s=60,
                       max_parallel=1, poll_interval=1)
    with Stubber(c) as stub:
        stub.add_response("delete_stack", {}, {"StackName": sid, "RoleARN": role,
                                               "DeletionMode": "FORCE_DELETE_STACK",
                                               "DeploymentConfig": {"Mode": "EXPRESS"}})
        runner._start(c, task)
        stub.assert_no_pending_responses()
    assert task.used_force and task.action == "FORCE_DELETE"


def test_happy_path_uses_express_and_reports_contract_keys(iam):
    f = FakeCfn(R)
    f.add("app", tp=True, polls_to_delete=3)
    out = run({R: f}, iam)
    r = by_name(out)["app"]
    assert CONTRACT_RESULT_KEYS <= set(r)
    assert {"module", "account_id", "generated_at", "applied", "status"} <= set(out)
    assert out["module"] == "cfn_predelete" and out["applied"] is True
    assert r["final_status"] == ps.GONE and r["used_express"] is True and r["used_force"] is False
    assert r["action"] == "DELETE" and r["role_used"] is None and r["error"] is None
    assert f.ops().index("UpdateTerminationProtection") < f.ops().index("DeleteStack")
    assert f.calls_of("DeleteStack") == [{"StackName": stack_id(R, "app"), "DeploymentConfig": {"Mode": "EXPRESS"}}]


def test_express_rejected_falls_back_without_it(iam):
    f = FakeCfn(R, express_supported=False)
    f.add("a")
    f.add("b", fail_delete=True)
    out = run({R: f}, iam)
    res = by_name(out)
    for name in ("a", "b"):
        assert res[name]["final_status"] == ps.GONE
        assert res[name]["used_express"] is False and res[name]["express_rejected"] is True
        assert any("EXPRESS rejected" in n for n in res[name]["notes"])
    deletes_a = [p for p in f.calls_of("DeleteStack") if p["StackName"] == stack_id(R, "a")]
    assert deletes_a == [{"StackName": stack_id(R, "a"), "DeploymentConfig": {"Mode": "EXPRESS"}},
                         {"StackName": stack_id(R, "a")}]
    # once rejected, the force retry of "b" does not try EXPRESS again
    deletes_b = [p for p in f.calls_of("DeleteStack") if p["StackName"] == stack_id(R, "b")]
    assert deletes_b[-1] == {"StackName": stack_id(R, "b"), "DeletionMode": "FORCE_DELETE_STACK"}


def test_old_botocore_sends_express_and_force_as_raw_query_params(iam, monkeypatch):
    """Python 3.9 CloudShell: botocore <= 1.42.97 has no DeploymentConfig in its model."""
    real = ps._model_members
    monkeypatch.setattr(ps, "_model_members", lambda c: real(c) - {"DeploymentConfig", "DeletionMode"})
    f = FakeCfn(R)
    f.add("a")
    f.add("b", fail_delete=True)
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["a"]["final_status"] == ps.GONE and res["a"]["used_express"] is True
    assert res["b"]["final_status"] == ps.GONE and res["b"]["used_force"] is True
    assert f.calls_of("DeleteStack")[0] == {"StackName": stack_id(R, "a"), "DeploymentConfig": {"Mode": "EXPRESS"}}
    assert {"StackName": stack_id(R, "b"), "DeletionMode": "FORCE_DELETE_STACK",
            "DeploymentConfig": {"Mode": "EXPRESS"}} in f.calls_of("DeleteStack")
    assert out["botocore_has_deployment_config"] is False
    assert any("raw query parameter" in n for n in res["a"]["notes"])


def test_raw_query_injection_reaches_the_wire(monkeypatch):
    """The real HTTP body (after serialization) carries the injected parameters; handler is removed after."""
    from botocore.awsrequest import AWSResponse
    c = boto3.client("cloudformation", region_name=R, aws_access_key_id="x", aws_secret_access_key="y")
    real = ps._model_members
    monkeypatch.setattr(ps, "_model_members", lambda cl: real(cl) - {"DeploymentConfig", "DeletionMode"})
    bodies = []

    def send(request, **_):
        bodies.append(request.body.decode() if isinstance(request.body, bytes) else request.body)
        xml = (b'<DeleteStackResponse xmlns="http://cloudformation.amazonaws.com/doc/2010-05-15/">'
               b'<ResponseMetadata><RequestId>1</RequestId></ResponseMetadata></DeleteStackResponse>')

        class Raw:
            def stream(self, **kw):
                yield xml
        return AWSResponse(request.url, 200, {}, Raw())
    c.meta.events.register("before-send.cloudformation", send)
    ps.delete_stack(c, StackName="s1", DeploymentConfig={"Mode": "EXPRESS"}, DeletionMode="FORCE_DELETE_STACK")
    ps.delete_stack(c, StackName="s2")
    assert "DeploymentConfig.Mode=EXPRESS" in bodies[0] and "DeletionMode=FORCE_DELETE_STACK" in bodies[0]
    assert "StackName=s1" in bodies[0] and "Action=DeleteStack" in bodies[0]
    assert "DeploymentConfig" not in bodies[1] and "DeletionMode" not in bodies[1]


def test_other_errors_are_not_mistaken_for_express_rejection(iam):
    f = FakeCfn(R)
    f.add("a")
    f._op_DeleteStack = lambda p: f._fail("AccessDenied", "User is not authorized to perform cloudformation:DeleteStack")
    out = run({R: f}, iam)
    r = by_name(out)["a"]
    assert r["final_status"] == ps.FAILED and "AccessDenied" in r["error"]
    assert len(f.calls_of("DeleteStack")) == 1          # no blind retry without EXPRESS
    assert r["express_rejected"] is False
    assert out["exit_code"] == ps.EXIT_ISSUES


def test_delete_failed_is_force_deleted_once(iam):
    f = FakeCfn(R)
    f.add("bucket-stack", fail_delete=True, polls_to_delete=2)
    out = run({R: f}, iam)
    r = by_name(out)["bucket-stack"]
    assert r["final_status"] == ps.GONE and r["used_force"] is True and r["action"] == "DELETE+FORCE"
    modes = [p.get("DeletionMode") for p in f.calls_of("DeleteStack")]
    assert modes == [None, "FORCE_DELETE_STACK"]
    assert any("DELETE_FAILED" in n for n in r["notes"])


def test_initially_delete_failed_goes_straight_to_force(iam):
    f = FakeCfn(R)
    f.add("stuck", status="DELETE_FAILED")
    out = run({R: f}, iam)
    r = by_name(out)["stuck"]
    assert r["action"] == "FORCE_DELETE" and r["final_status"] == ps.GONE
    assert [p.get("DeletionMode") for p in f.calls_of("DeleteStack")] == ["FORCE_DELETE_STACK"]


def test_still_failing_after_force_is_failed(iam):
    f = FakeCfn(R)
    f.add("hopeless", fail_delete=True)
    f.knobs[stack_id(R, "hopeless")]["forced"] = False

    orig = f._op_DeleteStack

    def delete_but_never_force(p):
        res = orig(p)
        f.knobs[stack_id(R, "hopeless")]["forced"] = False
        return res
    f._op_DeleteStack = delete_but_never_force
    out = run({R: f}, iam)
    r = by_name(out)["hopeless"]
    assert r["final_status"] == ps.FAILED and "after FORCE_DELETE_STACK" in r["error"]
    assert out["exit_code"] == ps.EXIT_ISSUES and out["status"] == "ISSUES"


def test_already_delete_in_progress_is_only_polled(iam):
    f = FakeCfn(R)
    f.add("going", status="DELETE_IN_PROGRESS", polls_to_delete=3)
    out = run({R: f}, iam)
    r = by_name(out)["going"]
    assert r["action"] == "POLL" and r["final_status"] == ps.GONE
    assert "DeleteStack" not in f.ops() and "UpdateTerminationProtection" not in f.ops()


def test_timeout_is_reported_fast_and_does_not_block_others(iam):
    f = FakeCfn(R)
    f.add("slow-vpc-lambda", never_finishes=True)
    f.add("quick1", polls_to_delete=1)
    f.add("quick2", polls_to_delete=2)
    clock = FakeClock()
    out = run({R: f}, iam, clock=clock, timeout_s=600, poll_interval=15)
    res = by_name(out)
    assert res["slow-vpc-lambda"]["final_status"] == ps.TIMEOUT
    assert "timeout" in res["slow-vpc-lambda"]["error"]
    assert res["quick1"]["final_status"] == ps.GONE and res["quick2"]["final_status"] == ps.GONE
    assert res["quick2"]["duration_s"] < 60                  # not stuck behind the slow stack
    assert out["exit_code"] == ps.EXIT_ISSUES
    assert 600 <= clock.now - 1000 <= 600 + 1                # stopped at the deadline
    assert all(s <= 15 * 1.2 + 1e-6 for s in clock.sleeps)  # polling interval with jitter


def test_max_parallel_limits_concurrent_deletes(iam):
    f = FakeCfn(R)
    for i in range(5):
        f.add(f"s{i}", polls_to_delete=2)
    seen_max = {"n": 0}
    orig = f._op_DeleteStack

    def counting(p):
        res = orig(p)
        n = sum(1 for s in f.stacks.values() if s["StackStatus"] == "DELETE_IN_PROGRESS")
        seen_max["n"] = max(seen_max["n"], n)
        return res
    f._op_DeleteStack = counting
    out = run({R: f}, iam, max_parallel=2)
    assert seen_max["n"] == 2
    assert all(r["final_status"] == ps.GONE for r in out["results"])


def test_regions_run_concurrently_and_independently(iam):
    a, b = FakeCfn(R), FakeCfn(R2)
    a.add("seoul-app", polls_to_delete=2)
    b.add("virginia-app", never_finishes=True)
    out = run({R: a, R2: b}, iam, timeout_s=300)
    res = by_name(out)
    assert res["seoul-app"]["final_status"] == ps.GONE and res["seoul-app"]["region"] == R
    assert res["virginia-app"]["final_status"] == ps.TIMEOUT and res["virginia-app"]["region"] == R2
    assert out["regions"] == [R, R2]


def test_second_pass_catches_stacks_that_appear_later(iam):
    f = FakeCfn(R)
    f.add("first")
    orig = f._op_DescribeStacks
    state = {"added": False}

    def describe(p):
        if not p.get("StackName") and f.deleted_order and not state["added"]:
            state["added"] = True
            f.add("late", parent="first")          # orphaned nested stack left behind
        return orig(p)
    f._op_DescribeStacks = describe
    out = run({R: f}, iam)
    res = by_name(out)
    assert res["late"]["final_status"] == ps.GONE and res["late"]["orphan_nested"] is True


def test_region_error_is_exit_2(iam):
    f = FakeCfn(R)
    f._op_DescribeStacks = lambda p: f._fail("AccessDenied", "not authorized to perform cloudformation:DescribeStacks")
    out = run({R: f}, iam)
    assert out["status"] == "ERROR" and out["exit_code"] == ps.EXIT_ERROR
    assert out["errors"][0]["region"] == R


def test_stale_delete_failed_right_after_force_gets_one_more_poll(iam):
    f = FakeCfn(R)
    sid = f.add("slow-to-switch", status="DELETE_FAILED")
    orig_delete, orig_describe = f._op_DeleteStack, f._op_DescribeStacks
    state = {"stale": 1}

    def delete(p):
        res = orig_delete(p)
        f.stacks[sid]["StackStatus"] = "DELETE_FAILED"       # status flips only after one more poll
        return res

    def describe(p):
        if p.get("StackName") and state["stale"] and sid in f.stacks:
            state["stale"] -= 1
            return f._ok({"Stacks": [dict(f.stacks[sid])]})
        if sid in f.stacks and f.knobs[sid].get("forced"):
            f.stacks[sid]["StackStatus"] = "DELETE_IN_PROGRESS"
        return orig_describe(p)
    f._op_DeleteStack, f._op_DescribeStacks = delete, describe
    out = run({R: f}, iam)
    r = by_name(out)["slow-to-switch"]
    assert r["final_status"] == ps.GONE and r["used_force"] is True
    assert len(f.calls_of("DeleteStack")) == 1
