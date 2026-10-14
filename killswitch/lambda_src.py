"""EduKillSwitch Lambda - edu-cleanup module 3 (Bedrock emergency brake).

Source of truth for the code embedded in killswitch.yaml (Code.ZipFile,
handler index.handler). After editing run: python3 killswitch/sync_inline_code.py

Triggers: SNS (CloudWatch alarm, ALARM state only), EventBridge (CloudTrail
"AWS API Call via CloudTrail"), or a direct invoke
{"trigger": "manual", "reason": "..."}. "dry_run": true in any event = report only.

For each target IAM user (all users or one group's members, minus the exempt
list; IAM roles such as EduCleanupRole are never touched):
  1. put inline deny policy EduKillSwitchDeny (fallback when the 2,048-char
     inline quota is full: attach managed policy /edu-killswitch/EduKillSwitchDeny)
  2. deactivate access keys        3. deactivate Bedrock API keys
  4. remember deactivated IDs in user tags (used by reset_killswitch.py)
then publish a summary to the SNS topic. Login profiles are kept.
Every step is idempotent (both regions may fire).
"""
import datetime
import json
import os

import boto3
from botocore.config import Config

POLICY_NAME = "EduKillSwitchDeny"
POLICY_PATH = "/edu-killswitch/"
PROTECTED = {"educleanuprole"}
REPORT_ATTR = "edu_killswitch_report"
TAG_KEYS = "edu-killswitch:keys"
TAG_SSC = "edu-killswitch:ssc"
TAG_AT = "edu-killswitch:at"
CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

DENY_ACTIONS = [
    "bedrock:*",
    "bedrock-mantle:*",
    "aws-marketplace:Subscribe",
    "iam:CreateUser",
    "iam:CreateRole",
    "iam:CreateAccessKey",
    "iam:UpdateAccessKey",
    "iam:CreateLoginProfile",
    "iam:UpdateLoginProfile",
    "iam:CreateServiceSpecificCredential",
    "iam:UpdateServiceSpecificCredential",
    "iam:ResetServiceSpecificCredential",
    "iam:Put*Policy",
    "iam:Attach*Policy",
    "iam:Detach*Policy",
    "iam:DeleteUserPolicy",
    "iam:Delete*PermissionsBoundary",
    "iam:CreatePolicyVersion",
    "iam:SetDefaultPolicyVersion",
    "iam:UpdateAssumeRolePolicy",
    "iam:PassRole",
    "iam:TagUser",
    "iam:UntagUser",
    "sts:AssumeRole",
]


# a locked (possibly admin) user must not be able to dismantle the switch itself
PROTECT_ACTIONS = ["cloudformation:*", "lambda:*", "cloudwatch:*", "events:*", "sns:*", "logs:*", "iam:*"]
PROTECT_RESOURCES = [
    "arn:*:cloudformation:*:*:stack/edu-killswitch-*",
    "arn:*:lambda:*:*:function:edu-killswitch-*",
    "arn:*:cloudwatch:*:*:alarm:edu-killswitch-*",
    "arn:*:events:*:*:rule/edu-killswitch-*",
    "arn:*:sns:*:*:edu-killswitch-*",
    "arn:*:logs:*:*:log-group:edu-killswitch-*",
    "arn:*:iam::*:role/edu-killswitch-*",
    "arn:*:iam::*:role/EduCleanupRole",
    "arn:*:iam::*:policy/edu-killswitch/*",
]


def deny_policy_document():
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "EduKillSwitchDeny", "Effect": "Deny",
             "Action": list(DENY_ACTIONS), "Resource": "*"},
            {"Sid": "EduKillSwitchProtect", "Effect": "Deny",
             "Action": list(PROTECT_ACTIONS), "Resource": list(PROTECT_RESOURCES)},
        ],
    }


def _iam():
    return boto3.client("iam", config=CFG)


def _sns():
    return boto3.client("sns", config=CFG)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _csv(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _config():
    return {
        "mode": os.environ.get("TARGET_MODE", "").strip() or "ALL_IAM_USERS",
        "group": os.environ.get("TARGET_GROUP", "").strip(),
        "exempt": _csv(os.environ.get("EXEMPT_USERS", "")),
        "topic": os.environ.get("TOPIC_ARN", "").strip(),
    }


def _is_true(value):
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def classify(event, exempt):
    """Decide whether this event trips the switch.

    Returns dict(trigger, reason, involved, ignore, dry_run). ``ignore`` is the
    reason nothing is done (or None). ``involved`` = IAM users named by an API
    event (caller / affected user), locked in addition to the targets.
    """
    ex = {e.lower() for e in exempt} | PROTECTED
    out = {"trigger": "unknown", "reason": "", "involved": [], "ignore": None, "dry_run": False}
    if not isinstance(event, dict):
        out["ignore"] = "unrecognised event"
        return out
    out["dry_run"] = _is_true(event.get("dry_run"))
    records = event.get("Records") or []
    if records and isinstance(records[0], dict) and records[0].get("EventSource") == "aws:sns":
        sns = records[0].get("Sns") or {}
        out["trigger"] = "alarm"
        if REPORT_ATTR in (sns.get("MessageAttributes") or {}):
            out["ignore"] = "own report message"
            return out
        try:
            msg = json.loads(sns.get("Message") or "")
        except ValueError:
            msg = None
        if not isinstance(msg, dict) or "AlarmName" not in msg:
            out["ignore"] = "SNS message is not a CloudWatch alarm notification"
            return out
        if msg.get("NewStateValue") != "ALARM":
            out["ignore"] = "alarm state is %s" % msg.get("NewStateValue")
            return out
        out["reason"] = "%s: %s" % (msg.get("AlarmName"), msg.get("NewStateReason", ""))
        return out
    if event.get("detail-type") == "AWS API Call via CloudTrail":
        d = event.get("detail") or {}
        out["trigger"] = "api"
        ui = d.get("userIdentity") or {}
        kind = ui.get("type", "")
        call = "%s:%s" % (str(d.get("eventSource", "")).split(".")[0], d.get("eventName"))
        if d.get("errorCode"):
            out["ignore"] = "failed call %s (%s)" % (call, d.get("errorCode"))
            return out
        if kind in ("Root", "AWSService"):
            out["ignore"] = "%s made by %s" % (call, kind)
            return out
        issuer = ((ui.get("sessionContext") or {}).get("sessionIssuer") or {}).get("userName", "")
        if kind == "AssumedRole" and issuer.lower() in ex:
            out["ignore"] = "%s made by exempt role %s" % (call, issuer)
            return out
        actor = ui.get("userName", "") if kind == "IAMUser" else ""
        if actor and actor.lower() in ex:
            out["ignore"] = "%s made by exempt user %s" % (call, actor)
            return out
        params = d.get("requestParameters") or {}
        for name in (actor, params.get("userName")):
            if isinstance(name, str) and name and name.lower() not in ex:
                out["involved"].append(name)
        out["reason"] = "%s by %s" % (call, ui.get("arn") or kind or "unknown")
        return out
    if event.get("trigger") == "manual":
        out["trigger"] = "manual"
        out["reason"] = str(event.get("reason") or "manual invoke")
        return out
    out["ignore"] = "unrecognised event"
    return out


def _paginate(iam, op, key, **kwargs):
    for page in iam.get_paginator(op).paginate(**kwargs):
        for item in page.get(key, []):
            yield item


def _list_ssc(iam, user):
    """All service-specific credentials of a user (botocore has no paginator)."""
    kwargs = {"UserName": user}
    while True:
        page = iam.list_service_specific_credentials(**kwargs)
        for cred in page.get("ServiceSpecificCredentials", []):
            yield cred
        if not page.get("IsTruncated") or not page.get("Marker"):
            return
        kwargs["Marker"] = page["Marker"]


def resolve_targets(iam, cfg, involved):
    """Return (targets, exempt_skipped, errors)."""
    errors, names = [], []
    try:
        if cfg["mode"] == "GROUP":
            if not cfg["group"]:
                raise ValueError("TargetMode is GROUP but TargetGroupName is empty")
            names = [u["UserName"] for u in _paginate(iam, "get_group", "Users", GroupName=cfg["group"])]
        else:
            names = [u["UserName"] for u in _paginate(iam, "list_users", "Users")]
    except Exception as exc:  # report it and still lock the involved users
        errors.append("listing targets failed: %s" % exc)
    ex = {e.lower() for e in cfg["exempt"]} | PROTECTED
    targets, skipped, seen = [], [], set()
    for name in list(names) + list(involved):
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        (skipped if name.lower() in ex else targets).append(name)
    return targets, skipped, errors


def _fallback_policy_arn(iam):
    """ARN of the managed copy of the deny policy (created on first need)."""
    acct = os.environ.get("ACCOUNT_ID") or boto3.client("sts", config=CFG).get_caller_identity()["Account"]
    part = os.environ.get("AWS_PARTITION", "aws")
    arn = "arn:%s:iam::%s:policy%s%s" % (part, acct, POLICY_PATH, POLICY_NAME)
    try:
        iam.get_policy(PolicyArn=arn)
    except iam.exceptions.NoSuchEntityException:
        try:
            iam.create_policy(PolicyName=POLICY_NAME, Path=POLICY_PATH,
                              PolicyDocument=json.dumps(deny_policy_document()),
                              Description="EduKillSwitch fallback deny (inline policy quota full)")
        except iam.exceptions.EntityAlreadyExistsException:
            pass
    return arn


def _remember(iam, user, keys, creds, stamp):
    """Merge deactivated IDs into user tags (read-merge-write, re-checked
    because the Lambdas of two regions may run at the same moment)."""
    for _ in range(3):
        tags = {t["Key"]: t["Value"] for t in _paginate(iam, "list_user_tags", "Tags", UserName=user)}
        new = [{"Key": TAG_AT, "Value": tags.get(TAG_AT) or stamp}]
        for key, ids in ((TAG_KEYS, keys), (TAG_SSC, creds)):
            merged = sorted(set(tags.get(key, "").split()) | set(ids))
            if merged:
                new.append({"Key": key, "Value": " ".join(merged)[:256]})
        iam.tag_user(UserName=user, Tags=new)
        after = {t["Key"]: t["Value"] for t in _paginate(iam, "list_user_tags", "Tags", UserName=user)}
        if set(keys) <= set(after.get(TAG_KEYS, "").split()) and set(creds) <= set(after.get(TAG_SSC, "").split()):
            return


def lock_user(iam, user, dry_run, stamp):
    res = {"user": user, "actions": [], "errors": []}

    def step(label, func):
        try:
            if not dry_run:
                func()
            res["actions"].append(label)
            return True
        except Exception as exc:
            res["errors"].append("%s: %s" % (label, exc))
            return False

    doc = json.dumps(deny_policy_document(), separators=(",", ":"))
    if not step("put-deny-policy", lambda: iam.put_user_policy(
            UserName=user, PolicyName=POLICY_NAME, PolicyDocument=doc)):
        # e.g. LimitExceeded: the user's inline policies already use the 2,048-char quota
        if step("attach-fallback-deny-policy", lambda: iam.attach_user_policy(
                UserName=user, PolicyArn=_fallback_policy_arn(iam))):
            res["notes"] = res["errors"]
            res["errors"] = []  # the deny is in place after all
    keys, creds = [], []
    try:
        for k in _paginate(iam, "list_access_keys", "AccessKeyMetadata", UserName=user):
            kid = k["AccessKeyId"]
            if k.get("Status") == "Active" and step("deactivate-access-key:" + kid, lambda kid=kid: iam.update_access_key(
                    UserName=user, AccessKeyId=kid, Status="Inactive")):
                keys.append(kid)
    except Exception as exc:
        res["errors"].append("list-access-keys: %s" % exc)
    try:
        for c in _list_ssc(iam, user):
            cid = c["ServiceSpecificCredentialId"]
            if ("bedrock" in c.get("ServiceName", "") and c.get("Status") == "Active"
                    and step("deactivate-bedrock-api-key:" + cid, lambda cid=cid: iam.update_service_specific_credential(
                        UserName=user, ServiceSpecificCredentialId=cid, Status="Inactive"))):
                creds.append(cid)
    except Exception as exc:
        res["errors"].append("list-service-specific-credentials: %s" % exc)
    if not dry_run:
        try:
            _remember(iam, user, keys, creds, stamp)
        except Exception as exc:
            res["errors"].append("tag-user: %s" % exc)
    return res


def _publish(topic, summary):
    if not topic:
        return None
    subject = "[EduKillSwitch] %s %s %s: %d user(s)" % (
        summary["status"].upper(), summary.get("account_id") or "-",
        summary.get("region") or "-", len(summary.get("users", [])))
    body = json.dumps(summary, indent=1, default=str, ensure_ascii=False)
    if len(body.encode("utf-8")) > 200000:
        body = body.encode("utf-8")[:200000].decode("utf-8", "ignore") + "\n...truncated"
    try:
        _sns().publish(TopicArn=topic, Subject=subject[:99], Message=body,
                       MessageAttributes={REPORT_ATTR: {"DataType": "String", "StringValue": "1"}})
        return None
    except Exception as exc:
        return str(exc)


def handler(event, context=None):
    cfg = _config()
    info = classify(event, cfg["exempt"])
    arn = getattr(context, "invoked_function_arn", "") or ""
    summary = {
        "module": "killswitch",
        "account_id": os.environ.get("ACCOUNT_ID") or (arn.split(":")[4] if arn.count(":") >= 5 else ""),
        "region": os.environ.get("AWS_REGION", ""),
        "generated_at": _now(),
        "trigger": info["trigger"],
        "reason": info["reason"],
        "dry_run": info["dry_run"],
        "target_mode": cfg["mode"],
        "target_group": cfg["group"],
    }
    if info["ignore"]:
        summary.update(status="ignored", ignored_because=info["ignore"])
        print(json.dumps(summary, default=str))
        return summary
    iam = _iam()
    targets, skipped, errors = resolve_targets(iam, cfg, info["involved"])
    results = [lock_user(iam, u, info["dry_run"], summary["generated_at"]) for u in targets]
    errors += ["%s %s" % (r["user"], e) for r in results for e in r["errors"]]
    if info["dry_run"]:
        status = "dry-run"
    else:
        status = "partial" if errors else "tripped"
    summary.update(status=status, users=results, exempt_skipped=skipped, errors=errors)
    for r in results:
        print("%s %s %s %s" % (status, r["user"], ",".join(r["actions"]), "; ".join(r["errors"])))
    publish_error = _publish(cfg["topic"], summary)
    if publish_error:
        summary["publish_error"] = publish_error
    print(json.dumps(summary, default=str))
    return summary
