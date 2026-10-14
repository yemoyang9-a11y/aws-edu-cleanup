"""reset_killswitch.py against moto (IAM + CloudWatch) after a real lock by the Lambda."""
import json

import boto3
import pytest

from killswitch_test_support import KS, alarm_event, load_module

POLICY = "EduKillSwitchDeny"
REGIONS = "ap-northeast-2,us-east-1"


@pytest.fixture
def reset(world, monkeypatch):
    mod = load_module("ks_reset", KS / "reset_killswitch.py")
    real_client = mod.client

    def client(session, service, region=None):
        c = real_client(session, service, region)
        if service == "iam":
            world.ssc.attach(c)
        return c

    monkeypatch.setattr(mod, "client", client)
    return mod


@pytest.fixture
def locked(world):
    world.lam.handler(alarm_event())
    for region in ("ap-northeast-2", "us-east-1"):
        cw = boto3.client("cloudwatch", region_name=region)
        name = "edu-killswitch-%s-bedrock-tokens" % region
        cw.put_metric_alarm(
            AlarmName=name, ComparisonOperator="GreaterThanOrEqualToThreshold", EvaluationPeriods=1,
            Threshold=1000, Metrics=[
                {"Id": "tokens", "Expression": "SUM([a])", "ReturnData": True},
                {"Id": "a", "ReturnData": False, "MetricStat": {
                    "Metric": {"Namespace": "AWS/Bedrock", "MetricName": "InputTokenCount"},
                    "Period": 300, "Stat": "Sum"}}])
        cw.set_alarm_state(AlarmName=name, StateValue="ALARM", StateReason="test")
    return world


def run(reset, tmp_path, *extra):
    out = tmp_path / "out.json"
    code = reset.main(["--regions", REGIONS, "--json-out", str(out)] + list(extra))
    return code, json.loads(out.read_text(encoding="utf-8"))


def key_status(iam, user):
    return {k["AccessKeyId"]: k["Status"] for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]}


def alarm_state(region):
    cw = boto3.client("cloudwatch", region_name=region)
    return cw.describe_alarms(AlarmNamePrefix="edu-killswitch-")["MetricAlarms"][0]["StateValue"]


def test_dry_run_is_default_and_changes_nothing(locked, reset, tmp_path, capsys):
    code, res = run(reset, tmp_path, "--reactivate-keys")
    assert code == 1
    assert res["module"] == "killswitch-reset"
    assert res["applied"] is False and res["status"] == "locked"
    assert res["account_id"] == "123456789012" and res["generated_at"]
    assert sorted(u["user"] for u in res["users"]) == ["alice", "bob", "carol"]
    alice = next(u for u in res["users"] if u["user"] == "alice")
    assert "delete-inline-policy" in alice["actions"]
    assert "reactivate-bedrock-api-key:ACCAEXAMPLEBEDROCK01" in alice["actions"]
    assert {a["region"] for a in res["alarms"] if a["action"] == "set-state-OK"} == {"ap-northeast-2", "us-east-1"}
    assert locked.iam.list_user_policies(UserName="alice")["PolicyNames"] == [POLICY]
    assert alarm_state("us-east-1") == "ALARM"
    assert "would reset" in capsys.readouterr().out


def test_apply_removes_policy_but_keeps_keys_by_default(locked, reset, tmp_path):
    iam = locked.iam
    code, res = run(reset, tmp_path, "--apply")
    assert code == 0, res
    assert res["status"] == "reset" and res["applied"] is True
    for user in ("alice", "bob", "carol"):
        assert iam.list_user_policies(UserName=user)["PolicyNames"] == []
        assert set(key_status(iam, user).values()) == {"Inactive"}
    # tags stay so keys can still be re-activated later
    assert any(t["Key"] == "edu-killswitch:keys" for t in iam.list_user_tags(UserName="bob")["Tags"])
    assert locked.ssc.status("alice", "ACCAEXAMPLEBEDROCK01") == "Inactive"
    assert alarm_state("us-east-1") == "OK" and alarm_state("ap-northeast-2") == "OK"
    # second run: keys still recorded -> dry-run reports nothing to do except re-activation if asked
    code, res = run(reset, tmp_path)
    assert code == 0 and res["status"] == "clean"


def test_apply_reactivate_restores_exactly_recorded_keys(locked, reset, tmp_path):
    iam = locked.iam
    code, res = run(reset, tmp_path, "--apply", "--reactivate-keys")
    assert code == 0, res
    for user in ("alice", "bob", "carol"):
        assert iam.list_user_policies(UserName=user)["PolicyNames"] == []
        assert iam.list_user_tags(UserName=user)["Tags"] == []
    assert key_status(iam, "carol") == {locked.keys["carol"]: "Active", locked.keys["carol-old"]: "Inactive"}
    for cid in ("ACCAEXAMPLEBEDROCK01", "ACCAEXAMPLEBEDROCK02", "ACCAEXAMPLEBEDROCK03"):
        assert locked.ssc.status("alice", cid) == "Active"
    # idempotent
    code, res = run(reset, tmp_path, "--apply", "--reactivate-keys")
    assert code == 0 and res["status"] == "clean" and res["users"] == []


def test_users_filter_and_missing_key(locked, reset, tmp_path):
    iam = locked.iam
    iam.delete_access_key(UserName="bob", AccessKeyId=locked.keys["bob"])
    code, res = run(reset, tmp_path, "--apply", "--reactivate-keys", "--users", "bob,Admin-Ops", "--no-rearm")
    assert code == 0, res
    assert [u["user"] for u in res["users"]] == ["bob"]
    assert "skip-missing-access-key:" + locked.keys["bob"] in res["users"][0]["actions"]
    assert iam.list_user_policies(UserName="alice")["PolicyNames"] == [POLICY]
    assert res["alarms"] == []
    assert alarm_state("us-east-1") == "ALARM"


def test_detaches_fallback_managed_policy(world, reset, tmp_path):
    iam = world.iam
    arn = iam.create_policy(PolicyName=POLICY, Path="/edu-killswitch/",
                            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [
                                {"Effect": "Deny", "Action": "bedrock:*", "Resource": "*"}]}))["Policy"]["Arn"]
    iam.attach_user_policy(UserName="carol", PolicyArn=arn)
    code, res = run(reset, tmp_path, "--apply", "--no-rearm")
    assert code == 0
    assert iam.list_attached_user_policies(UserName="carol")["AttachedPolicies"] == []
    assert res["users"][0]["actions"] == ["detach-policy:" + arn]


def test_clean_account(world, reset, tmp_path):
    code, res = run(reset, tmp_path)
    assert code == 0 and res["status"] == "clean"


def test_step_failure_gives_exit_1(locked, reset, tmp_path, monkeypatch):
    real = reset.client

    def client(session, service, region=None):
        c = real(session, service, region)
        if service == "iam":
            def boom(**kw):
                raise RuntimeError("AccessDenied")
            c.delete_user_policy = boom
        return c

    monkeypatch.setattr(reset, "client", client)
    code, res = run(reset, tmp_path, "--apply", "--no-rearm")
    assert code == 1 and res["status"] == "partial"
    assert any("AccessDenied" in e for e in res["errors"])


def test_auth_error_gives_exit_2(world, reset, tmp_path, monkeypatch):
    def broken(args):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(reset, "get_session", broken)
    code, res = run(reset, tmp_path)
    assert code == 2 and res["status"] == "error"
    assert res["module"] == "killswitch-reset" and res["applied"] is False


def test_common_args_present(reset):
    args = reset.parse_args(["--profile", "p", "--role-arn", "arn:aws:iam::1:role/EduCleanupRole",
                             "--external-id", "x", "--session-name", "s", "--regions", "us-east-1",
                             "--json-out", "o.json", "--apply", "--reactivate-keys"])
    assert args.apply and args.reactivate_keys and args.role_arn.endswith("EduCleanupRole")
