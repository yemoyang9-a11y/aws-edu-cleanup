"""Handler tests for the kill-switch Lambda (lambda_src.py) against moto IAM."""
import json

import boto3
import pytest

from killswitch_test_support import alarm_event, api_event, iam_user

POLICY = "EduKillSwitchDeny"


def inline(iam, user):
    return iam.list_user_policies(UserName=user)["PolicyNames"]


def key_status(iam, user):
    return {k["AccessKeyId"]: k["Status"] for k in iam.list_access_keys(UserName=user)["AccessKeyMetadata"]}


def tags(iam, user):
    return {t["Key"]: t["Value"] for t in iam.list_user_tags(UserName=user)["Tags"]}


# ---- policy document -----------------------------------------------------------------------

def test_deny_policy_covers_required_actions_and_fits_inline_quota(lam):
    doc = lam.deny_policy_document()
    actions = set(doc["Statement"][0]["Action"])
    for a in ("bedrock:*", "bedrock-mantle:*", "aws-marketplace:Subscribe", "iam:CreateAccessKey",
              "iam:CreateUser", "iam:CreateLoginProfile", "iam:CreateServiceSpecificCredential",
              "iam:Put*Policy", "iam:Attach*Policy", "iam:DeleteUserPolicy"):
        assert a in actions
    assert all(s["Effect"] == "Deny" for s in doc["Statement"])
    # IAM counts policy size without whitespace; user inline quota is 2,048 chars in total
    assert len(json.dumps(doc, separators=(",", ":"))) < 1400


# ---- alarm trigger ---------------------------------------------------------------------------

def test_alarm_locks_all_users_except_exempt(world):
    iam = world.iam
    out = world.lam.handler(alarm_event())
    assert out["status"] == "tripped", out
    assert out["trigger"] == "alarm"
    assert sorted(u["user"] for u in out["users"]) == ["alice", "bob", "carol"]
    assert out["exempt_skipped"] == ["Admin-Ops"]
    for user in ("alice", "bob", "carol"):
        assert inline(iam, user) == [POLICY]
        doc = iam.get_user_policy(UserName=user, PolicyName=POLICY)["PolicyDocument"]
        assert doc["Statement"][0]["Effect"] == "Deny"
        assert set(key_status(iam, user).values()) == {"Inactive"}
    # exempt operator untouched (case-insensitive exempt match)
    assert inline(iam, "Admin-Ops") == []
    assert set(key_status(iam, "Admin-Ops").values()) == {"Active"}
    assert world.ssc.status("Admin-Ops", "ACCAEXAMPLEOPERATOR1") == "Active"
    # Bedrock API keys deactivated across pages, CodeCommit credential left alone
    for cid in ("ACCAEXAMPLEBEDROCK01", "ACCAEXAMPLEBEDROCK02", "ACCAEXAMPLEBEDROCK03"):
        assert world.ssc.status("alice", cid) == "Inactive"
    assert world.ssc.status("alice", "ACCAEXAMPLECODECOMM1") == "Active"
    # login profiles are never deleted, roles never touched
    assert iam.list_role_policies(RoleName="EduCleanupRole")["PolicyNames"] == ["keep"]
    assert iam.list_attached_role_policies(RoleName="EduCleanupRole")["AttachedPolicies"] == []


def test_records_only_what_it_deactivated(world):
    iam = world.iam
    world.lam.handler(alarm_event())
    t = tags(iam, "carol")
    assert t["edu-killswitch:keys"].split() == [world.keys["carol"]]      # not the pre-disabled key
    assert "edu-killswitch:at" in t
    assert tags(iam, "alice")["edu-killswitch:ssc"].split() == ["ACCAEXAMPLEBEDROCK01", "ACCAEXAMPLEBEDROCK02", "ACCAEXAMPLEBEDROCK03"]
    assert "edu-killswitch:ssc" not in tags(iam, "bob")


def test_idempotent_second_region_firing(world):
    iam = world.iam
    first = world.lam.handler(alarm_event())
    tags_before = tags(iam, "alice")
    second = world.lam.handler(alarm_event(name="edu-killswitch-ap-northeast-2-bedrock-tokens"))
    assert first["status"] == second["status"] == "tripped"
    assert second["errors"] == []
    for user in ("alice", "bob", "carol"):
        assert inline(iam, user) == [POLICY]
        assert set(key_status(iam, user).values()) == {"Inactive"}
    # nothing new to deactivate the second time, and earlier records are kept
    alice = next(u for u in second["users"] if u["user"] == "alice")
    assert alice["actions"] == ["put-deny-policy"]
    assert tags(iam, "alice") == tags_before


def test_records_merge_with_new_keys_after_reset(world):
    iam = world.iam
    world.lam.handler(alarm_event())
    new = iam.create_access_key(UserName="bob")["AccessKey"]["AccessKeyId"]
    world.lam.handler(alarm_event())
    assert sorted(tags(iam, "bob")["edu-killswitch:keys"].split()) == sorted([world.keys["bob"], new])


def test_alarm_ok_and_non_alarm_messages_are_ignored(world):
    for ev in (alarm_event(state="OK"), alarm_event(state="INSUFFICIENT_DATA")):
        out = world.lam.handler(ev)
        assert out["status"] == "ignored"
    own = alarm_event()
    own["Records"][0]["Sns"]["MessageAttributes"] = {"edu_killswitch_report": {"Type": "String", "Value": "1"}}
    assert world.lam.handler(own)["status"] == "ignored"
    junk = {"Records": [{"EventSource": "aws:sns", "Sns": {"Message": "hello"}}]}
    assert world.lam.handler(junk)["status"] == "ignored"
    assert world.lam.handler({"foo": 1})["status"] == "ignored"
    assert world.lam.handler("not a dict")["status"] == "ignored"
    assert inline(world.iam, "alice") == []
    assert world.messages() == []


# ---- dry run ------------------------------------------------------------------------------

def test_dry_run_reports_without_changing(world):
    iam = world.iam
    out = world.lam.handler({"trigger": "manual", "reason": "test", "dry_run": True})
    assert out["status"] == "dry-run"
    assert out["dry_run"] is True
    alice = next(u for u in out["users"] if u["user"] == "alice")
    assert "put-deny-policy" in alice["actions"]
    assert "deactivate-access-key:" + world.keys["alice"] in alice["actions"]
    assert "deactivate-bedrock-api-key:ACCAEXAMPLEBEDROCK03" in alice["actions"]
    for user in ("alice", "bob", "carol"):
        assert inline(iam, user) == []
        assert tags(iam, user) == {}
    assert key_status(iam, "alice")[world.keys["alice"]] == "Active"
    assert world.ssc.status("alice", "ACCAEXAMPLEBEDROCK01") == "Active"
    assert not [c for c in world.ssc.calls if c[0] == "update"]
    # the dry-run summary is still published
    msgs = world.messages()
    assert len(msgs) == 1
    body = json.loads(json.loads(msgs[0]["Body"])["Message"])
    assert body["status"] == "dry-run"


def test_dry_run_flag_on_alarm_event(world):
    out = world.lam.handler(alarm_event(dry_run="true"))
    assert out["status"] == "dry-run"
    assert inline(world.iam, "alice") == []


# ---- group mode -----------------------------------------------------------------------------

def test_group_mode_locks_only_members(world, monkeypatch):
    monkeypatch.setenv("TARGET_MODE", "GROUP")
    monkeypatch.setenv("TARGET_GROUP", "students")
    out = world.lam.handler(alarm_event())
    assert sorted(u["user"] for u in out["users"]) == ["alice", "bob"]
    assert inline(world.iam, "carol") == []
    assert key_status(world.iam, "carol")[world.keys["carol"]] == "Active"


def test_group_mode_respects_exempt_member(world, monkeypatch):
    world.iam.add_user_to_group(GroupName="students", UserName="Admin-Ops")
    monkeypatch.setenv("TARGET_MODE", "GROUP")
    monkeypatch.setenv("TARGET_GROUP", "students")
    monkeypatch.setenv("EXEMPT_USERS", " bob , Admin-Ops ")
    out = world.lam.handler(alarm_event())
    assert [u["user"] for u in out["users"]] == ["alice"]
    assert sorted(out["exempt_skipped"]) == ["Admin-Ops", "bob"]


def test_missing_group_reports_partial(world, monkeypatch):
    monkeypatch.setenv("TARGET_MODE", "GROUP")
    monkeypatch.setenv("TARGET_GROUP", "nope")
    out = world.lam.handler(alarm_event())
    assert out["status"] == "partial"
    assert any("listing targets failed" in e for e in out["errors"])
    assert out["users"] == []


# ---- CloudTrail API triggers ------------------------------------------------------------------

def test_api_event_by_student_locks_everyone_plus_new_user(world):
    world.iam.create_user(UserName="evil-new")
    ev = api_event("CreateUser", iam_user("carol"), {"userName": "evil-new"})
    out = world.lam.handler(ev)
    assert out["trigger"] == "api"
    assert out["status"] == "tripped"
    assert "iam:CreateUser" in out["reason"]
    assert inline(world.iam, "evil-new") == [POLICY]
    assert inline(world.iam, "carol") == [POLICY]


def test_api_event_in_group_mode_also_locks_non_member_actor(world, monkeypatch):
    monkeypatch.setenv("TARGET_MODE", "GROUP")
    monkeypatch.setenv("TARGET_GROUP", "students")
    ev = api_event("PutUseCaseForModelAccess", iam_user("carol"), {}, source="bedrock.amazonaws.com")
    out = world.lam.handler(ev)
    assert sorted(u["user"] for u in out["users"]) == ["alice", "bob", "carol"]


@pytest.mark.parametrize("identity", [
    {"type": "Root", "arn": "arn:aws:iam::123456789012:root"},
    {"type": "AWSService", "invokedBy": "bedrock.amazonaws.com"},
    iam_user("admin-ops"),
    {"type": "AssumedRole", "arn": "arn:aws:sts::123456789012:assumed-role/EduCleanupRole/hub",
     "sessionContext": {"sessionIssuer": {"type": "Role", "userName": "EduCleanupRole"}}},
])
def test_api_events_from_root_service_exempt_or_cleanup_role_are_ignored(world, identity):
    out = world.lam.handler(api_event("CreateAccessKey", identity, {"userName": "alice"}))
    assert out["status"] == "ignored", out
    assert inline(world.iam, "alice") == []


def test_failed_api_call_is_ignored(world):
    out = world.lam.handler(api_event("CreateUser", iam_user("carol"), {"userName": "x"}, error="AccessDenied"))
    assert out["status"] == "ignored"


def test_api_event_never_locks_exempt_target_user(world):
    ev = api_event("CreateAccessKey", iam_user("carol"), {"userName": "Admin-Ops"})
    out = world.lam.handler(ev)
    assert "Admin-Ops" not in [u["user"] for u in out["users"]]
    assert inline(world.iam, "Admin-Ops") == []


# ---- reporting and robustness ----------------------------------------------------------------

def test_summary_published_with_loop_guard_attribute(world):
    world.lam.handler(alarm_event())
    msgs = world.messages()
    assert len(msgs) == 1
    envelope = json.loads(msgs[0]["Body"])
    assert envelope["Subject"].startswith("[EduKillSwitch] TRIPPED 123456789012 us-east-1: 3 user(s)")
    assert "edu_killswitch_report" in envelope["MessageAttributes"]
    body = json.loads(envelope["Message"])
    assert body["module"] == "killswitch" and body["account_id"] == "123456789012"


def test_publish_failure_does_not_break_locking(world, monkeypatch):
    monkeypatch.setenv("TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:does-not-exist")
    out = world.lam.handler(alarm_event())
    assert out["status"] == "tripped"
    assert "publish_error" in out
    assert inline(world.iam, "alice") == [POLICY]


def test_inline_quota_full_falls_back_to_managed_policy(world):
    iam = world.iam
    real = world.lambda_iam.put_user_policy

    def boom(**kw):
        if kw["UserName"] == "bob":
            raise iam.exceptions.LimitExceededException(
                {"Error": {"Code": "LimitExceeded", "Message": "Maximum policy size exceeded"}}, "PutUserPolicy")
        return real(**kw)

    world.lambda_iam.put_user_policy = boom
    out = world.lam.handler(alarm_event())
    assert out["status"] == "tripped", out["errors"]
    bob = next(u for u in out["users"] if u["user"] == "bob")
    assert "attach-fallback-deny-policy" in bob["actions"]
    attached = iam.list_attached_user_policies(UserName="bob")["AttachedPolicies"]
    assert attached == [{"PolicyName": POLICY,
                         "PolicyArn": "arn:aws:iam::123456789012:policy/edu-killswitch/EduKillSwitchDeny"}]
    assert set(key_status(iam, "bob").values()) == {"Inactive"}
    # second run reuses the existing managed policy
    out2 = world.lam.handler(alarm_event())
    assert out2["status"] == "tripped", out2["errors"]


def test_one_user_failure_is_reported_and_others_still_locked(world):
    real = world.lambda_iam.update_access_key

    def flaky(**kw):
        if kw["UserName"] == "alice":
            raise RuntimeError("throttled")
        return real(**kw)

    world.lambda_iam.update_access_key = flaky
    out = world.lam.handler(alarm_event())
    assert out["status"] == "partial"
    assert any(e.startswith("alice deactivate-access-key") for e in out["errors"])
    assert set(key_status(world.iam, "bob").values()) == {"Inactive"}
    assert inline(world.iam, "alice") == [POLICY]


def test_list_users_pagination(world, monkeypatch):
    for i in range(130):
        world.iam.create_user(UserName="s%03d" % i)
    out = world.lam.handler({"trigger": "manual", "dry_run": True})
    assert len(out["users"]) == 133


def test_classify_unit(lam):
    c = lam.classify(alarm_event(), [])
    assert c["trigger"] == "alarm" and c["ignore"] is None and c["dry_run"] is False
    c = lam.classify({"trigger": "manual", "dry_run": "yes"}, [])
    assert c["trigger"] == "manual" and c["dry_run"] is True
    c = lam.classify(api_event("CreateAccessKey", iam_user("x"), {"userName": "y"}), ["Y"])
    assert c["involved"] == ["x"]


def test_handler_works_without_mocked_client_factory(aws, monkeypatch, lam):
    """The real _iam()/_sns() factories work (moto) when nothing is patched."""
    boto3.client("iam", region_name="us-east-1").create_user(UserName="solo")
    monkeypatch.setenv("TOPIC_ARN", "")
    monkeypatch.setenv("EXEMPT_USERS", "")
    monkeypatch.setattr(lam, "_list_ssc", lambda iam, user: iter(()))
    out = lam.handler({"trigger": "manual"})
    assert out["status"] == "tripped"
    assert boto3.client("iam", region_name="us-east-1").list_user_policies(UserName="solo")["PolicyNames"] == [POLICY]
