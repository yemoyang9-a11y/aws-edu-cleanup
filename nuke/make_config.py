#!/usr/bin/env python3
"""Generate an aws-nuke v3 (ekristen) YAML config for ONE training account.

Standard library + common/session.py only (no PyYAML): the YAML is written by hand,
every string is a JSON-quoted scalar, which is also valid YAML.

    python3 nuke/make_config.py --account-id 123456789012 --regions ap-northeast-2,us-east-1
    python3 nuke/make_config.py --account-id 123456789012 --regions ap-northeast-2 --no-global --out cfg.yaml
    python3 nuke/make_config.py --account-id 123456789012 --regions global --protect-iam-user ops-kim

Every resource-type name, filter property and setting below was checked against the
aws-nuke v3.68.3 source (docs/resources/*.md, resources/*.go) and libnuke v1.3.0.
run_nuke.sh calls this once per region (regional configs + one `global` config run last).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING  # noqa: E402,F401

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple  # noqa: E402

from common.session import BOOTSTRAP_STACK_PREFIX, ROLE_NAME, now_iso  # noqa: E402

MODULE = "nuke"
PLACEHOLDER_BLOCKLIST = "000000000000"
# The guardrail policy created by hub/bootstrap.yaml (resource EduProtectCleanupRolePolicy).
BOOTSTRAP_POLICY_NAME = "EduProtectCleanupRole"

# --------------------------------------------------------------------------------------
# resource-types.excludes
# Wildcards are expanded by libnuke (types.Collection.Expand -> mb0/glob) against the
# registered type names. Checked with `aws-nuke explain-config --with-excluded` (v3.68.3).
# RULE: only exclude what a NEW standalone student account cannot create (or what costs
# nothing / is removed by its parent). An excluded type is NEVER deleted.
# --------------------------------------------------------------------------------------
EXCLUDE_GROUPS: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("Removed by the parent resource - per-item deletion only adds time and failures", [
        ("S3Object",
         "S3Bucket.Remove empties every object version with 1000-key DeleteObjects batches (resources/s3-bucket.go)"),
        ("DynamoDBTableItem",
         "lister reads ONE Scan page and only the partition key (resources/dynamodb-item.go): DeleteItem fails on "
         "tables with a sort key and every pass re-scans; DeleteTable removes all items anyway"),
    ]),
    ("Services / APIs AWS has shut down (endpoint gone, disabled or 503 -> lister error or ~30 s timeout per call, "
     "aws-nuke#1013)", [
        ("OpsWorks*", "OpsWorks Stacks / Chef Automate / Puppet Enterprise: full shutdown 2024-05-01 (7 types)"),
        ("CodeStarProject",
         "AWS CodeStar: full shutdown 2024-07-25. NOT CodeStar*: CodeStarConnection (CodeConnections) and "
         "CodeStarNotificationRule are live services and stay in scope"),
        ("RoboMaker*", "AWS RoboMaker: full shutdown 2025-09-10 (3 types)"),
        ("QLDBLedger", "Amazon QLDB: full shutdown 2025-07-31"),
        ("ElasticTranscoder*", "Amazon Elastic Transcoder: full shutdown 2025-11-13 (Pipeline, Preset)"),
        ("MediaStore*", "AWS Elemental MediaStore: full shutdown 2025-11-12 (Container, DataItems)"),
        ("InspectorAssessment*",
         "Amazon Inspector Classic: full shutdown 2026-05-20 (Run, Target, Template). NOT Inspector2 (billed)"),
        ("AppMesh*", "AWS App Mesh: discontinued 2026-09-30 (7 types)"),
        # Lex V1 = the lexmodelbuildingservice API (resources/lex-*.go). Exact names, not Lex*: a future Lex V2
        # resource type must stay in scope. Checked 2026-10-15 (docs.aws.amazon.com/lex/latest/dg/what-is.html).
        ("LexBot", "Amazon Lex V1: end of support 2025-09-15 (V1 console + resources no longer accessible); "
                   "real dry-run 2026-10-15: HTTP 503 in every region. Lex V2 is not affected"),
        ("LexIntent", "Amazon Lex V1: end of support 2025-09-15; HTTP 503 in every region (2026-10-15)"),
        ("LexSlotType", "Amazon Lex V1: end of support 2025-09-15; HTTP 503 in every region (2026-10-15)"),
        ("LexModelBuildingServiceBotAlias",
         "Amazon Lex V1 bot aliases: end of support 2025-09-15; HTTP 503 in every region (2026-10-15)"),
        ("GlueDevEndpoint",
         "AWS Glue dev endpoints only run Glue 0.9/1.0, end of life 2026-04-01 (glue-version-support-policy); "
         "real dry-run 2026-10-15: GetDevEndpoints 'operation is currently disabled' in all 17 regions. "
         "Every other Glue type (GlueJob, GlueSession ...) stays in scope"),
    ]),
    ("Closed to new customers - a newly created student account cannot create these", [
        ("Cloud9Environment", "AWS Cloud9: closed to new customers 2024-07-25"),
        ("CloudSearchDomain", "Amazon CloudSearch: closed to new customers 2024-07-25"),
        ("DataPipelinePipeline", "AWS Data Pipeline: no longer available to new customers"),
        ("MachineLearning*", "Amazon Machine Learning: not accepting new users (4 types)"),
        ("CloudDirectoryDirectory",
         "Amazon Cloud Directory: no longer open to new customers, end of support 2027-07-24 (checked 2026-10-15); "
         "real dry-run 2026-10-15: 'Account ... is not authorized for region' in every region"),
        ("CloudDirectorySchema", "Amazon Cloud Directory: closed to new customers (see CloudDirectoryDirectory)"),
        ("AWS::Timestream::*",
         "Timestream for LiveAnalytics: closed to new customers 2025-06-20 (Cloud Control types Database, Table, "
         "ScheduledQuery registered by default). NOT TimestreamInfluxDBDbInstance (open and billed)"),
    ]),
    ("Impossible or pointless in a standalone account (no AWS Organizations)", [
        ("FMS*", "Firewall Manager needs Organizations + an FMS admin account; List always fails (FMSPolicy, "
                 "FMSNotificationChannel)"),
        ("ShieldProtection*",
         "needs a Shield Advanced subscription; deleting protections does not end the subscription (1-year "
         "commitment) - check the subscription before closure instead (ShieldProtection, ShieldProtectionGroup)"),
        ("ServiceCatalogTagOption*",
         "free; lister fails with TagOptionNotMigratedException on accounts that never used TagOptions "
         "(official starter-config, rebuy-de/aws-nuke#515)"),
    ]),
]

# Types that must stay in scope (students can create them and they cost money). Tests assert
# that no default exclude pattern matches any of these. Region-gated listers (Bedrock customization,
# Verified Access, DocDB in us-west-1, Rekognition/Textract/Timestream InfluxDB AccessDenied ...) are NOT
# excluded: they are classified as expected / maybe-SCP in the summary instead (listing_errors.json).
MUST_KEEP_EXAMPLES = (
    "QBusinessApplication", "QBusinessIndex", "GlueJob", "GlueSession", "GlueDataBrewJobs", "Inspector2",
    "TimestreamInfluxDBDbInstance", "RedshiftServerlessWorkgroup", "RedshiftServerlessNamespace",
    "CodeStarConnection", "CodeStarNotificationRule", "KinesisAnalyticsApplication", "BedrockAgent",
    "OSCollection", "SageMakerEndpoint", "EC2Instance", "CloudFormationStack", "DynamoDBTable", "S3Bucket",
)

# --------------------------------------------------------------------------------------
# settings - names verified in docs/resources/<type>.md "## Settings" (v3.68.3)
# --------------------------------------------------------------------------------------
SETTINGS: List[Tuple[str, List[str], str]] = [
    ("EC2Instance", ["DisableStopProtection", "DisableDeletionProtection"], "stop/termination protection"),
    ("EC2Image", ["IncludeDisabled", "IncludeDeprecated", "DisableDeregistrationProtection"],
     "also list disabled/deprecated AMIs and lift deregistration protection"),
    ("RDSInstance", ["DisableDeletionProtection", "StartClusterToDelete"],
     "instances of a STOPPED Aurora cluster cannot be deleted - start it first"),
    ("CloudFormationStack", ["DisableDeletionProtection", "CreateRoleToDeleteStack"],
     "termination protection; recreate a deleted stack service role (fixed for regional stacks in v3.68.1, #1033)"),
    ("DynamoDBTable", ["DisableDeletionProtection"], ""),
    ("ELBv2", ["DisableDeletionProtection"], ""),
    ("EKSCluster", ["DisableDeletionProtection"], ""),
    ("CloudWatchLogsLogGroup", ["DisableDeletionProtection"], ""),
    ("CognitoUserPool", ["DisableDeletionProtection"], ""),
    ("DocDBCluster", ["DisableDeletionProtection"], ""),
    ("DSQLCluster", ["DisableDeletionProtection"], ""),
    ("NeptuneCluster", ["DisableDeletionProtection"], ""),
    ("NeptuneInstance", ["DisableClusterDeletionProtection", "DisableDeletionProtection"], ""),
    ("NeptuneGraph", ["DisableDeletionProtection"], ""),
    ("PinpointPhoneNumber", ["DisableDeletionProtection"], "monthly-billed phone numbers"),
    ("QuickSightSubscription", ["DisableTerminationProtection"], "monthly-billed subscription"),
    ("S3Bucket", ["BypassGovernanceRetention", "RemoveObjectLegalHold"], "Object Lock (governance mode) buckets"),
    ("LightsailInstance", ["ForceDeleteAddOns"], ""),
    ("KMSKey", ["DeleteInUseKeys"],
     "schedule deletion (7-day window) even if a service still holds a grant; otherwise the key is held until "
     "--max-wait-retries and the run ends with exit 1"),
]
CFN_CURRENT_ROLE_SETTING = "UseCurrentRoleToDeleteStack"

_ACCOUNT_RE = re.compile(r"^\d{12}$")
_REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-\d+$")
_IAM_NAME_RE = re.compile(r"^[\w+=,.@-]{1,128}$")


class ConfigError(ValueError):
    pass


def _split(values: Optional[Iterable[str]]) -> List[str]:
    """Accept repeated flags and/or comma-separated values; strip, drop empties, keep order, dedupe."""
    out: List[str] = []
    for v in values or ():
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def default_excludes() -> List[str]:
    return [pattern for _, items in EXCLUDE_GROUPS for pattern, _ in items]


def normalize_regions(regions: Iterable[str], include_global: bool = True) -> List[str]:
    regs = parse_regions(list(regions))
    for r in regs:
        if r != "global" and not _REGION_RE.match(r):
            raise ConfigError(f"invalid region name: {r!r}")
    regs = [r for r in regs if r != "global"]
    if include_global or not regs:
        regs.append("global")          # global LAST: regional stacks/functions go before IAM roles
    return regs


def role_name_from_arn(arn: Optional[str]) -> Optional[str]:
    if not arn or ":role/" not in arn:
        return None
    return arn.rsplit("/", 1)[-1] or None


def build_filters(*, protect_roles: Sequence[str] = (), protect_users: Sequence[str] = (),
                  protect_groups: Sequence[str] = (), protect_policy_arns: Sequence[str] = (),
                  protect_mfa_serials: Sequence[str] = ()) -> List[Tuple[str, str, List[Dict[str, str]]]]:
    """Return [(resource_type, comment, [filter, ...]), ...] in output order.

    Property names verified in resources/*.go (v3.68.3):
      IAMRole Name (struct field) | IAMRolePolicyAttachment RoleName | IAMRolePolicy role:RoleName
      IAMPolicy Name/ARN | CloudFormationStack Name | LambdaFunction Name | CloudWatchLogsLogGroup Name
      IAMUser Name | IAMUserPolicyAttachment/IAMUserAccessKey/IAMLoginProfile/IAMUserMFADevice/
      IAMUserGroupAttachment/IAMUserSSHPublicKey/IAMUserHTTPSGitCredential/IAMSigningCertificate/
      IAMServiceSpecificCredential UserName | IAMVirtualMFADevice SerialNumber | IAMGroup Name |
      IAMGroupPolicyAttachment GroupName | IAMUserPolicy, IAMGroupPolicy: no properties -> match the
      legacy string "<user|group> -> <policy>" (filter without `property`).
    """
    roles = [ROLE_NAME] + [r for r in protect_roles if r != ROLE_NAME]
    boot = f"{BOOTSTRAP_STACK_PREFIX}*"

    def exact(prop: str, value: str) -> Dict[str, str]:
        return {"property": prop, "type": "exact", "value": value}

    def glob(prop: Optional[str], value: str) -> Dict[str, str]:
        f = {"type": "glob", "value": value}
        if prop:
            f = {"property": prop, **f}
        return f

    out: List[Tuple[str, str, List[Dict[str, str]]]] = [
        ("IAMRole", f"cleanup role{' + caller role(s)' if len(roles) > 1 else ''} + edu-bootstrap helper roles",
         [exact("Name", r) for r in roles] + [glob("Name", boot)]),
        ("IAMRolePolicyAttachment", "keep their managed-policy attachments (a role without policies is powerless)",
         [exact("RoleName", r) for r in roles] + [glob("RoleName", boot)]),
        ("IAMRolePolicy", "keep their inline policies",
         [exact("role:RoleName", r) for r in roles] + [glob("role:RoleName", boot)]),
        ("IAMPolicy", "bootstrap guardrail policy + customer-managed policies attached to protected principals",
         [exact("Name", BOOTSTRAP_POLICY_NAME)] + [exact("ARN", a) for a in protect_policy_arns]),
        ("CloudFormationStack", "bootstrap stack(s): edu-bootstrap*", [glob("Name", boot)]),
        ("LambdaFunction", "bootstrap alias custom-resource function", [glob("Name", boot)]),
        ("CloudWatchLogsLogGroup", "its log group", [glob("Name", f"/aws/lambda/{boot}")]),
    ]
    if protect_users:
        u = list(protect_users)
        out += [
            ("IAMUser", "protected IAM user(s) - the identity running this cleanup", [exact("Name", x) for x in u]),
            ("IAMUserPolicyAttachment", "", [exact("UserName", x) for x in u]),
            ("IAMUserPolicy", "no properties: match the string '<user> -> <policy>'", [glob(None, f"{x} -> *") for x in u]),
            ("IAMUserGroupAttachment", "group membership (often where the admin permission comes from)",
             [exact("UserName", x) for x in u]),
            ("IAMUserAccessKey", "", [exact("UserName", x) for x in u]),
            ("IAMLoginProfile", "console password", [exact("UserName", x) for x in u]),
            ("IAMUserMFADevice", "MFA assignment", [exact("UserName", x) for x in u]),
            ("IAMUserSSHPublicKey", "", [exact("UserName", x) for x in u]),
            ("IAMUserHTTPSGitCredential", "", [exact("UserName", x) for x in u]),
            ("IAMSigningCertificate", "", [exact("UserName", x) for x in u]),
            ("IAMServiceSpecificCredential", "", [exact("UserName", x) for x in u]),
        ]
    if protect_mfa_serials:
        out.append(("IAMVirtualMFADevice", "virtual MFA devices of protected users (no UserName property)",
                    [exact("SerialNumber", s) for s in protect_mfa_serials]))
    if protect_groups:
        g = list(protect_groups)
        out += [
            ("IAMGroup", "groups of protected users", [exact("Name", x) for x in g]),
            ("IAMGroupPolicyAttachment", "", [exact("GroupName", x) for x in g]),
            ("IAMGroupPolicy", "no properties: match the string '<group> -> <policy>'", [glob(None, f"{x} -> *") for x in g]),
        ]
    return out


def build_config(account: str, regions: Iterable[str], *, include_global: bool = True,
                 blocklist: Iterable[str] = (), extra_excludes: Iterable[str] = (),
                 protect_users: Iterable[str] = (), protect_roles: Iterable[str] = (),
                 protect_groups: Iterable[str] = (), protect_policy_arns: Iterable[str] = (),
                 protect_mfa_serials: Iterable[str] = (), bypass_alias_check: bool = False,
                 cfn_use_current_role: bool = False) -> Dict[str, Any]:
    """Validated, comment-free model of the config (what the YAML parses to)."""
    account = str(account).strip()
    if not _ACCOUNT_RE.match(account):
        raise ConfigError(f"--account-id must be 12 digits, got {account!r}")
    bl = _split(blocklist)
    for b in bl:
        if not _ACCOUNT_RE.match(b):
            raise ConfigError(f"blocklist entry must be 12 digits, got {b!r}")
    if account in bl:
        raise ConfigError(f"account {account} is in the blocklist - refusing to generate a config for it")
    users, roles, groups = _split(protect_users), _split(protect_roles), _split(protect_groups)
    for n in users + roles + groups:
        if not _IAM_NAME_RE.match(n):
            raise ConfigError(f"invalid IAM name: {n!r}")
    arns = _split(protect_policy_arns)
    serials = _split(protect_mfa_serials)
    for a in arns + serials:
        if not a.startswith("arn:"):
            raise ConfigError(f"expected an ARN, got {a!r}")
    excludes = default_excludes() + [e for e in _split(extra_excludes) if e not in default_excludes()]

    settings: Dict[str, Dict[str, bool]] = {}
    for rtype, names, _ in SETTINGS:
        settings[rtype] = {n: True for n in names}
    if cfn_use_current_role:
        settings["CloudFormationStack"][CFN_CURRENT_ROLE_SETTING] = True

    filters = {rtype: flist for rtype, _, flist in build_filters(
        protect_roles=roles, protect_users=users, protect_groups=groups,
        protect_policy_arns=arns, protect_mfa_serials=serials)}

    cfg: Dict[str, Any] = {
        "regions": normalize_regions(regions, include_global),
        "blocklist": bl or [PLACEHOLDER_BLOCKLIST],
    }
    if bypass_alias_check:
        cfg["bypass-alias-check-accounts"] = [account]
    cfg["settings"] = settings
    cfg["resource-types"] = {"excludes": excludes}
    cfg["accounts"] = {account: {"filters": filters}}
    return cfg


def _q(s: str) -> str:
    return json.dumps(str(s))


def render_yaml(cfg: Dict[str, Any], *, extra_excludes: Iterable[str] = (), protect_users: Iterable[str] = (),
                protect_roles: Iterable[str] = (), protect_groups: Iterable[str] = (),
                protect_policy_arns: Iterable[str] = (), protect_mfa_serials: Iterable[str] = ()) -> str:
    """Render the model as commented YAML. Pass the same protect_* lists used for build_config."""
    account = next(iter(cfg["accounts"]))
    lines: List[str] = [
        "# aws-nuke v3 (ekristen) config - generated by edu-cleanup nuke/make_config.py. Do not edit by hand.",
        f"# account {account}. Names verified against aws-nuke v3.68.3 / libnuke v1.3.0.",
        "# aws-nuke has no resource type for the IAM account alias (it only reads it), so the alias is never deleted.",
        "regions:",
    ]
    lines += [f"  - {_q(r)}" for r in cfg["regions"]]
    lines.append("blocklist:  # must be non-empty; the target account must not be in it")
    for b in cfg["blocklist"]:
        note = "  # placeholder (no real account)" if b == PLACEHOLDER_BLOCKLIST else ""
        lines.append(f"  - {_q(b)}{note}")
    if "bypass-alias-check-accounts" in cfg:
        lines.append("# dry-run only: the account has no alias yet (run_nuke.sh creates edu-<id> before --apply).")
        lines.append("# Needs `--no-alias-check` on the command line as well.")
        lines.append("bypass-alias-check-accounts:")
        lines += [f"  - {_q(a)}" for a in cfg["bypass-alias-check-accounts"]]

    lines.append("settings:")
    reasons = {rtype: why for rtype, _, why in SETTINGS}
    for rtype, values in cfg["settings"].items():
        why = reasons.get(rtype, "")
        if CFN_CURRENT_ROLE_SETTING in values:
            why = (why + "; " if why else "") + \
                "UseCurrentRoleToDeleteStack: DeleteStack passes EduCleanupRole (trusts cloudformation) as RoleARN"
        lines.append(f"  {rtype}:" + (f"  # {why}" if why else ""))
        lines += [f"    {k}: {'true' if v else 'false'}" for k, v in values.items()]

    lines.append("resource-types:")
    lines.append("  excludes:  # an excluded type is NEVER deleted - only things students cannot create / that cost nothing")
    for title, items in EXCLUDE_GROUPS:
        lines.append(f"    # --- {title}")
        for pattern, why in items:
            lines.append(f"    - {_q(pattern)}  # {why}")
    extra = [e for e in _split(extra_excludes) if e not in default_excludes()]
    if extra:
        lines.append("    # --- added with --extra-excludes")
        lines += [f"    - {_q(e)}" for e in extra]

    lines.append("accounts:")
    lines.append(f"  {_q(account)}:")
    lines.append("    filters:  # filtered = KEPT")
    comments = {rtype: c for rtype, c, _ in build_filters(
        protect_roles=_split(protect_roles), protect_users=_split(protect_users),
        protect_groups=_split(protect_groups), protect_policy_arns=_split(protect_policy_arns),
        protect_mfa_serials=_split(protect_mfa_serials))}
    for rtype, flist in cfg["accounts"][account]["filters"].items():
        c = comments.get(rtype, "")
        lines.append(f"      {rtype}:" + (f"  # {c}" if c else ""))
        for f in flist:
            keys = [k for k in ("property", "type", "value") if k in f]
            first = True
            for k in keys:
                lines.append(("        - " if first else "          ") + f"{k}: {_q(f[k])}")
                first = False
    return "\n".join(lines) + "\n"


def generate(account: str, regions: Iterable[str], **kw: Any) -> str:
    """build_config + render_yaml in one call (same keyword arguments as build_config)."""
    cfg = build_config(account, regions, **kw)
    return render_yaml(cfg, extra_excludes=kw.get("extra_excludes", ()),
                       protect_users=kw.get("protect_users", ()), protect_roles=kw.get("protect_roles", ()),
                       protect_groups=kw.get("protect_groups", ()),
                       protect_policy_arns=kw.get("protect_policy_arns", ()),
                       protect_mfa_serials=kw.get("protect_mfa_serials", ()))


def add_config_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("config")
    g.add_argument("--account-id", required=True, help="12-digit target account ID")
    g.add_argument("--no-global", action="store_true", help="do not append the 'global' pseudo-region")
    g.add_argument("--blocklist", action="append", default=[],
                   help="account IDs aws-nuke must refuse (comma list, repeatable). Default: a placeholder")
    g.add_argument("--extra-excludes", action="append", default=[], help="more resource types to exclude (comma list)")
    g.add_argument("--protect-iam-user", action="append", default=[], help="keep this IAM user and its credentials")
    g.add_argument("--protect-iam-role", action="append", default=[], help="keep this role (EduCleanupRole always kept)")
    g.add_argument("--protect-iam-group", action="append", default=[], help="keep this group + its policies")
    g.add_argument("--protect-iam-policy-arn", action="append", default=[], help="keep this customer-managed policy")
    g.add_argument("--protect-mfa-serial", action="append", default=[], help="keep this virtual MFA device (ARN)")
    g.add_argument("--bypass-alias-check", action="store_true",
                   help="add bypass-alias-check-accounts (dry-run of an account without alias; needs --no-alias-check)")
    g.add_argument("--cfn-use-current-role", action="store_true",
                   help="set CloudFormationStack.UseCurrentRoleToDeleteStack (automatic when --role-arn is EduCleanupRole)")
    g.add_argument("--out", help="write the YAML here instead of stdout")


def config_kwargs(args: argparse.Namespace) -> Dict[str, Any]:
    use_current = bool(args.cfn_use_current_role or role_name_from_arn(getattr(args, "role_arn", None)) == ROLE_NAME)
    return dict(include_global=not args.no_global, blocklist=args.blocklist, extra_excludes=args.extra_excludes,
                protect_users=args.protect_iam_user, protect_roles=args.protect_iam_role,
                protect_groups=args.protect_iam_group, protect_policy_arns=args.protect_iam_policy_arn,
                protect_mfa_serials=args.protect_mfa_serial, bypass_alias_check=args.bypass_alias_check,
                cfn_use_current_role=use_current)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_config_args(p)
    add_common_args(p)   # --regions/--json-out used; --profile/--role-arn only matter for the CFN role setting
    args = p.parse_args(argv)
    try:
        kw = config_kwargs(args)
        cfg = build_config(args.account_id, parse_regions(args.regions), **kw)
        text = generate(args.account_id, parse_regions(args.regions), **kw)
    except ConfigError as e:
        log(f"ERROR: {e}")
        write_json(args.json_out, {"module": MODULE, "account_id": str(args.account_id), "generated_at": now_iso(),
                                   "applied": False, "status": "ERROR", "exit_code": EXIT_ERROR, "error": str(e)})
        return EXIT_ERROR
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        log(f"wrote {args.out} (regions: {', '.join(cfg['regions'])})")
    else:
        sys.stdout.write(text)
    write_json(args.json_out, {"module": MODULE, "account_id": args.account_id, "generated_at": now_iso(),
                               "applied": False, "status": "OK", "exit_code": EXIT_OK, "config_path": args.out,
                               "regions": cfg["regions"], "excludes": cfg["resource-types"]["excludes"],
                               "settings": cfg["settings"],
                               "filters": cfg["accounts"][args.account_id]["filters"]})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
