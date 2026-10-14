#!/usr/bin/env python3
"""Undo the EduKillSwitch after the incident has been investigated.

Dry-run by default: lists the locked IAM users and what would be undone.
With --apply, for every locked user (or only --users):
  * removes the inline policy EduKillSwitchDeny
    (and detaches the fallback managed policy /edu-killswitch/EduKillSwitchDeny),
  * with --reactivate-keys: re-activates exactly the access keys and Bedrock API
    keys the kill switch deactivated (recorded in the user's edu-killswitch:* tags)
    and then removes those tags,
  * re-arms the edu-killswitch-* alarms in --regions (sets them to OK) so the
    switch can fire again.

Run it as root, as an exempt IAM user, or via EduCleanupRole: a locked user
cannot remove its own deny policy.

If usage is still above the threshold, the re-armed alarm fires again and the
switch re-locks everyone - that is intended.

Exit codes: 0 nothing to undo / everything reset, 1 something left to undo
(dry-run) or some steps failed, 2 error.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING  # noqa: E402,F401,E501
from common.session import now_iso, KILLSWITCH_STACK_PREFIX  # noqa: E402

import argparse  # noqa: E402

MODULE = "killswitch-reset"
POLICY_NAME = "EduKillSwitchDeny"
POLICY_PATH = "/edu-killswitch/"
TAG_KEYS = "edu-killswitch:keys"
TAG_SSC = "edu-killswitch:ssc"
TAG_AT = "edu-killswitch:at"
ALL_TAGS = (TAG_KEYS, TAG_SSC, TAG_AT)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common_args(p)
    p.add_argument("--users", default="",
                   help="Comma-separated IAM user names to reset (default: every locked user)")
    p.add_argument("--reactivate-keys", action="store_true",
                   help="Also re-activate the access keys / Bedrock API keys the switch deactivated")
    p.add_argument("--no-rearm", action="store_true",
                   help="Do not reset the edu-killswitch-* alarms to OK")
    return p.parse_args(argv)


def _pages(iam, op, key, **kwargs):
    for page in iam.get_paginator(op).paginate(**kwargs):
        for item in page.get(key, []):
            yield item


def _list_ssc(iam, user):
    kwargs = {"UserName": user}
    while True:
        page = iam.list_service_specific_credentials(**kwargs)
        for cred in page.get("ServiceSpecificCredentials", []):
            yield cred
        if not page.get("IsTruncated") or not page.get("Marker"):
            return
        kwargs["Marker"] = page["Marker"]


def inspect_user(iam, user):
    """Return the kill-switch state of one user, or None if untouched."""
    inline = POLICY_NAME in set(_pages(iam, "list_user_policies", "PolicyNames", UserName=user))
    attached = [p["PolicyArn"] for p in _pages(iam, "list_attached_user_policies", "AttachedPolicies",
                                                UserName=user, PathPrefix=POLICY_PATH)
                if p["PolicyName"] == POLICY_NAME]
    tags = {t["Key"]: t["Value"] for t in _pages(iam, "list_user_tags", "Tags", UserName=user)}
    if not (inline or attached or any(k in tags for k in ALL_TAGS)):
        return None
    return {
        "user": user,
        "inline_policy": inline,
        "attached_policies": attached,
        "locked_at": tags.get(TAG_AT, ""),
        "recorded_access_keys": tags.get(TAG_KEYS, "").split(),
        "recorded_bedrock_api_keys": tags.get(TAG_SSC, "").split(),
        "actions": [],
        "errors": [],
    }


def reset_user(iam, st, reactivate, apply):
    user = st["user"]

    def step(label, func):
        st["actions"].append(label)
        if not apply:
            return True
        try:
            func()
            return True
        except Exception as exc:  # keep going, report at the end
            st["errors"].append("%s: %s" % (label, exc))
            return False

    if st["inline_policy"]:
        step("delete-inline-policy", lambda: iam.delete_user_policy(UserName=user, PolicyName=POLICY_NAME))
    for arn in st["attached_policies"]:
        step("detach-policy:" + arn, lambda arn=arn: iam.detach_user_policy(UserName=user, PolicyArn=arn))
    if not reactivate:
        return st
    ok = True
    existing = {k["AccessKeyId"]: k.get("Status") for k in
                _pages(iam, "list_access_keys", "AccessKeyMetadata", UserName=user)}
    for kid in st["recorded_access_keys"]:
        if kid not in existing:
            st["actions"].append("skip-missing-access-key:" + kid)
        elif existing[kid] != "Active":
            ok &= step("reactivate-access-key:" + kid, lambda kid=kid: iam.update_access_key(
                UserName=user, AccessKeyId=kid, Status="Active"))
    if st["recorded_bedrock_api_keys"]:
        creds = {c["ServiceSpecificCredentialId"]: c.get("Status") for c in _list_ssc(iam, user)}
        for cid in st["recorded_bedrock_api_keys"]:
            if cid not in creds:
                st["actions"].append("skip-missing-bedrock-api-key:" + cid)
            elif creds[cid] != "Active":
                ok &= step("reactivate-bedrock-api-key:" + cid, lambda cid=cid: iam.update_service_specific_credential(
                    UserName=user, ServiceSpecificCredentialId=cid, Status="Active"))
    if ok:
        step("untag-user", lambda: iam.untag_user(UserName=user, TagKeys=list(ALL_TAGS)))
    return st


def rearm_alarms(session, regions, apply):
    out = []
    for region in regions:
        try:
            cw = client(session, "cloudwatch", region)
            for page in cw.get_paginator("describe_alarms").paginate(
                    AlarmNamePrefix=KILLSWITCH_STACK_PREFIX + "-", AlarmTypes=["MetricAlarm"]):
                for alarm in page.get("MetricAlarms", []):
                    item = {"region": region, "alarm": alarm["AlarmName"], "state": alarm["StateValue"],
                            "action": None, "error": None}
                    if alarm["StateValue"] != "OK":
                        item["action"] = "set-state-OK"
                        if apply:
                            try:
                                cw.set_alarm_state(AlarmName=alarm["AlarmName"], StateValue="OK",
                                                   StateReason="reset_killswitch.py: re-armed by operator")
                            except Exception as exc:
                                item["error"] = str(exc)
                    out.append(item)
        except Exception as exc:
            out.append({"region": region, "alarm": None, "state": None, "action": None, "error": str(exc)})
    return out


def main(argv=None):
    args = parse_args(argv)
    result = {"module": MODULE, "account_id": None, "generated_at": now_iso(), "applied": bool(args.apply),
              "status": "error", "reactivate_keys": bool(args.reactivate_keys), "users": [], "alarms": [],
              "errors": []}
    try:
        session = get_session(args)
        regions = [r for r in parse_regions(args.regions) if r != "global"]
        result["account_id"] = account_id(session)
        log("account %s, %s" % (result["account_id"], "APPLY" if args.apply else "dry-run (add --apply to act)"))
        iam = client(session, "iam", "us-east-1")
        wanted = [u.strip() for u in args.users.split(",") if u.strip()]
        names = wanted or [u["UserName"] for u in _pages(iam, "list_users", "Users")]
        for name in names:
            try:
                st = inspect_user(iam, name)
            except Exception as exc:
                result["errors"].append("%s: %s" % (name, exc))
                continue
            if st is None:
                if wanted:
                    log("  %s: not locked by the kill switch" % name)
                continue
            reset_user(iam, st, args.reactivate_keys, args.apply)
            result["users"].append(st)
            log("  %s: %s%s" % (name, ", ".join(st["actions"]) or "nothing to do",
                                (" ERRORS: " + "; ".join(st["errors"])) if st["errors"] else ""))
        if not args.no_rearm:
            result["alarms"] = rearm_alarms(session, regions, args.apply)
            for a in result["alarms"]:
                if a["error"]:
                    result["errors"].append("alarm %s %s: %s" % (a["region"], a["alarm"] or "", a["error"]))
        result["errors"] += ["%s %s" % (u["user"], e) for u in result["users"] for e in u["errors"]]
        pending = [u for u in result["users"] if u["actions"]] + [a for a in result["alarms"] if a["action"]]
        if result["errors"]:
            result["status"] = "partial"
            code = EXIT_ISSUES
        elif not pending:
            result["status"] = "clean"
            code = EXIT_OK
        elif args.apply:
            result["status"] = "reset"
            code = EXIT_OK
        else:
            result["status"] = "locked"
            code = EXIT_ISSUES
    except Exception as exc:
        log("ERROR: %s" % exc)
        result["errors"].append(str(exc))
        result["status"] = "error"
        code = EXIT_ERROR
    write_json(args.json_out, result)
    verb = "reset" if args.apply else "would reset"
    print("killswitch reset [%s] account=%s users %s: %d (%s) alarms re-armed: %d errors: %d" % (
        result["status"], result["account_id"], verb, len(result["users"]),
        ", ".join(u["user"] for u in result["users"]) or "-",
        sum(1 for a in result["alarms"] if a["action"] and not a["error"]), len(result["errors"])))
    return code


if __name__ == "__main__":
    sys.exit(main())
