"""make_config.py: generated YAML parses, has the right structure, and every name exists in aws-nuke v3.68.3."""
import fnmatch
import json
import subprocess
import sys

import pytest
import yaml

from nuke_test_support import DOCS_RESOURCES, NUKE_DIR

import make_config as mc

ACCT = "123456789012"


def gen(**kw):
    return yaml.safe_load(mc.generate(ACCT, kw.pop("regions", ["ap-northeast-2", "us-east-1"]), **kw))


def expand(pattern, names):
    """Same semantics as libnuke types.Collection.Expand (mb0/glob): no match -> the literal name."""
    return [n for n in names if fnmatch.fnmatchcase(n, pattern)] or [pattern]


# ---------------------------------------------------------------- structure
def test_yaml_parses_and_has_required_keys():
    cfg = gen()
    assert cfg["regions"] == ["ap-northeast-2", "us-east-1", "global"]          # global LAST
    assert cfg["blocklist"] == ["000000000000"]                                 # non-empty placeholder
    assert list(cfg["accounts"]) == [ACCT]
    assert cfg["resource-types"]["excludes"][:2] == ["S3Object", "DynamoDBTableItem"]
    assert "bypass-alias-check-accounts" not in cfg


def test_yaml_equals_model():
    """render_yaml (hand-written YAML) parses to exactly build_config's model."""
    kw = dict(protect_users=["ops-kim"], protect_groups=["admins"], extra_excludes=["Foo"],
              protect_policy_arns=["arn:aws:iam::123456789012:policy/x"],
              protect_mfa_serials=["arn:aws:iam::123456789012:mfa/ops-kim"], bypass_alias_check=True,
              cfn_use_current_role=True, blocklist=["210987654321"])
    assert yaml.safe_load(mc.generate(ACCT, ["us-east-1"], **kw)) == mc.build_config(ACCT, ["us-east-1"], **kw)


def test_global_last_and_dedup_and_no_global():
    assert gen(regions=["global", "us-east-1", "us-east-1"])["regions"] == ["us-east-1", "global"]
    assert gen(regions=["ap-northeast-2"], include_global=False)["regions"] == ["ap-northeast-2"]
    assert gen(regions=["global"], include_global=False)["regions"] == ["global"]


@pytest.mark.parametrize("bad", [dict(account="12345"), dict(account="abc123456789"),
                                 dict(regions=["ap-northeast-2; rm -rf /"]),
                                 dict(blocklist=["123456789012"]), dict(blocklist=["nope"]),
                                 dict(protect_users=["bad name"]), dict(protect_policy_arns=["notanarn"])])
def test_rejects_bad_input(bad):
    account = bad.pop("account", ACCT)
    regions = bad.pop("regions", ["us-east-1"])
    with pytest.raises(mc.ConfigError):
        mc.build_config(account, regions, **bad)


def test_strings_are_safely_quoted():
    text = mc.generate(ACCT, ["us-east-1"], protect_users=["a.b@c=d"], extra_excludes=["AWS::X::*"])
    cfg = yaml.safe_load(text)
    assert cfg["accounts"][ACCT]["filters"]["IAMUser"][0]["value"] == "a.b@c=d"
    assert "AWS::X::*" in cfg["resource-types"]["excludes"]


def test_extra_excludes_appended_once():
    ex = gen(extra_excludes=["GlueJob,GlueJob", "S3Object"])["resource-types"]["excludes"]
    assert ex.count("GlueJob") == 1 and ex.count("S3Object") == 1 and ex[-1] == "GlueJob"


# ---------------------------------------------------------------- settings
def test_settings_required_by_task():
    s = gen()["settings"]
    assert s["EC2Instance"] == {"DisableStopProtection": True, "DisableDeletionProtection": True}
    assert s["RDSInstance"]["DisableDeletionProtection"] is True
    assert s["CloudFormationStack"]["DisableDeletionProtection"] is True
    assert s["DynamoDBTable"] == {"DisableDeletionProtection": True}
    assert s["ELBv2"] == {"DisableDeletionProtection": True}
    assert s["EC2Image"] == {"IncludeDisabled": True, "IncludeDeprecated": True, "DisableDeregistrationProtection": True}
    assert "UseCurrentRoleToDeleteStack" not in s["CloudFormationStack"]
    assert gen(cfn_use_current_role=True)["settings"]["CloudFormationStack"]["UseCurrentRoleToDeleteStack"] is True


def test_every_setting_exists_in_aws_nuke(resource_docs):
    cfg = gen(cfn_use_current_role=True)
    for rtype, values in cfg["settings"].items():
        assert rtype in resource_docs, f"unknown resource type in settings: {rtype}"
        for name in values:
            assert name in resource_docs[rtype]["settings"], f"{rtype} has no setting {name}"


def test_cfn_current_role_only_for_educleanuprole():
    def kw(role_arn, flag=False):
        ns = mc.argparse.Namespace(no_global=False, blocklist=[], extra_excludes=[], protect_iam_user=[],
                                   protect_iam_role=[], protect_iam_group=[], protect_iam_policy_arn=[],
                                   protect_mfa_serial=[], bypass_alias_check=False, cfn_use_current_role=flag,
                                   role_arn=role_arn)
        return mc.config_kwargs(ns)["cfn_use_current_role"]
    assert kw("arn:aws:iam::123456789012:role/EduCleanupRole") is True
    assert kw("arn:aws:iam::123456789012:role/Other") is False
    assert kw(None) is False and kw(None, True) is True


# ---------------------------------------------------------------- excludes
def test_every_exclude_matches_a_registered_type(resource_docs):
    names = list(resource_docs)
    for pattern in mc.default_excludes():
        hits = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
        assert hits, f"exclude {pattern!r} matches no aws-nuke v3.68.3 resource type"


def test_excludes_cover_the_noisy_services(resource_docs):
    excluded = {n for p in mc.default_excludes() for n in expand(p, list(resource_docs))}
    for t in ["S3Object", "DynamoDBTableItem", "FMSPolicy", "FMSNotificationChannel", "ShieldProtection",
              "ShieldProtectionGroup", "OpsWorksApp", "OpsWorksCMServer", "Cloud9Environment", "CodeStarProject",
              "MachineLearningMLModel", "CloudSearchDomain", "AWS::Timestream::Database", "AWS::Timestream::Table",
              "AWS::Timestream::ScheduledQuery", *RETIRED_2026_10]:
        assert t in excluded, t


# Added after the real 17-region CloudShell dry-run of 2026-10-15: listers that failed in EVERY region because
# the service/API is retired (not region gating). Exact names - each must be a registered v3.68.3 type.
RETIRED_2026_10 = ["LexBot", "LexIntent", "LexSlotType", "LexModelBuildingServiceBotAlias",
                   "CloudDirectoryDirectory", "CloudDirectorySchema", "GlueDevEndpoint"]


def test_retired_2026_10_excludes_exist_and_are_exact(resource_docs):
    for t in RETIRED_2026_10:
        assert t in resource_docs, f"{t} is not an aws-nuke v3.68.3 resource type"
        assert t in mc.default_excludes(), t                 # exact name, no wildcard
    # Lex: only the V1 (lexmodelbuildingservice) types exist in v3.68.3 - all four excluded, nothing else
    assert sorted(n for n in resource_docs if n.startswith("Lex")) == sorted(RETIRED_2026_10[:4])
    src = DOCS_RESOURCES.parent.parent / "resources"
    for f in src.glob("lex-*.go"):
        assert "service/lexmodelbuildingservice" in f.read_text(), f.name   # Lex V1 API, not lexmodelsv2


def test_region_gated_types_are_not_excluded(resource_docs):
    """Listers that fail only in SOME regions (or with a bare AccessDenied) stay in scope - students can use them
    elsewhere and they bill. The summary classifies those errors instead (listing_errors.json)."""
    excluded = {n for p in mc.default_excludes() for n in expand(p, list(resource_docs))}
    for t in ("BedrockCustomModel", "BedrockModelCustomizationJob", "BedrockEvaluationJob", "BedrockPrompt",
              "BedrockFlowAlias", "EC2VerifiedAccessInstance", "DocDBCluster", "ComprehendDocumentClassifier",
              "TranscribeLanguageModel", "RekognitionProject", "RekognitionDataset", "TextractAdapter",
              "TimestreamInfluxDBDbInstance", "SSMQuickSetupConfigurationManager"):
        assert t in resource_docs and t not in excluded, t


def test_never_excludes_services_students_can_still_buy(resource_docs):
    names = list(resource_docs)
    excluded = {n for p in mc.default_excludes() for n in expand(p, names)}
    for t in mc.MUST_KEEP_EXAMPLES:
        if t in names:   # (OSCollection has no docs page in v3.68.3 but is registered)
            assert t not in excluded, f"{t} must stay deletable"
    retired = {"GlueDevEndpoint"}   # Glue 0.9/1.0 only (EOL 2026-04-01), API disabled - see make_config.py
    for family in ("QBusiness", "Glue", "Bedrock", "SageMaker", "EC2", "RDS", "Lambda", "OS", "ES", "Kinesis"):
        fam = [n for n in names if n.startswith(family)]
        assert fam and not ((set(fam) - retired) & excluded), family
    assert excluded & {n for n in names if n.startswith("Glue")} == retired


def test_each_exclude_has_a_justification():
    for _, items in mc.EXCLUDE_GROUPS:
        for pattern, why in items:
            assert len(why) > 20, pattern
    text = mc.generate(ACCT, ["us-east-1"])
    for pattern in mc.default_excludes():
        line = next(ln for ln in text.splitlines() if ln.strip().startswith(f"- {json.dumps(pattern)}"))
        assert "  # " in line, f"no comment for {pattern}"


# ---------------------------------------------------------------- filters
FILTER_PROPS = {   # property names read from resources/*.go (v3.68.3) for types whose docs page lists none
    "IAMRolePolicyAttachment": {"RoleName", "RolePath", "RoleLastUsed", "RoleCreateDate", "PolicyName", "PolicyArn"},
    "IAMRolePolicy": {"PolicyName", "role:RoleName", "role:RoleID", "role:Path"},
    "IAMUserPolicyAttachment": {"PolicyArn", "PolicyName", "UserName"},
    "IAMUserAccessKey": {"UserName", "AccessKeyID", "CreateDate"},
    "IAMUserGroupAttachment": {"GroupName", "UserName"},
    "IAMUserSSHPublicKey": {"UserName", "SSHKeyID"},
    "IAMUserHTTPSGitCredential": {"UserName", "ServiceSpecificCredentialId"},
    "IAMSigningCertificate": {"UserName", "CertificateId", "Status"},
    "IAMServiceSpecificCredential": {"ServiceName", "ID", "Name", "UserName"},
    "IAMGroup": {"Name", "Path", "ID"},
    "IAMGroupPolicyAttachment": {"GroupName", "PolicyName", "PolicyArn"},
}


def all_filters():
    return gen(protect_users=["ops-kim"], protect_groups=["admins"], protect_roles=["OpsRole"],
               protect_policy_arns=["arn:aws:iam::123456789012:policy/p"],
               protect_mfa_serials=["arn:aws:iam::123456789012:mfa/ops-kim"])["accounts"][ACCT]["filters"]


def test_every_filter_type_and_property_exists(resource_docs):
    for rtype, flist in all_filters().items():
        assert rtype in resource_docs, f"unknown resource type in filters: {rtype}"
        known = set(resource_docs[rtype]["props"]) | FILTER_PROPS.get(rtype, set())
        for f in flist:
            assert f["type"] in ("exact", "glob"), f
            if "property" in f:
                assert f["property"] in known, f"{rtype} has no property {f['property']} (known: {sorted(known)})"


def test_filters_protect_cleanup_role_and_bootstrap():
    f = gen()["accounts"][ACCT]["filters"]
    assert {"property": "Name", "type": "exact", "value": "EduCleanupRole"} in f["IAMRole"]
    assert {"property": "RoleName", "type": "exact", "value": "EduCleanupRole"} in f["IAMRolePolicyAttachment"]
    assert {"property": "role:RoleName", "type": "exact", "value": "EduCleanupRole"} in f["IAMRolePolicy"]
    assert {"property": "Name", "type": "glob", "value": "edu-bootstrap*"} in f["CloudFormationStack"]
    assert {"property": "Name", "type": "exact", "value": "EduProtectCleanupRole"} in f["IAMPolicy"]
    assert "IAMUser" not in f           # no user protected unless asked


def test_filters_protect_iam_user_everything():
    f = all_filters()
    for t in ("IAMUserPolicyAttachment", "IAMUserGroupAttachment", "IAMUserAccessKey", "IAMLoginProfile",
              "IAMUserMFADevice", "IAMUserSSHPublicKey", "IAMUserHTTPSGitCredential", "IAMSigningCertificate",
              "IAMServiceSpecificCredential"):
        assert f[t] == [{"property": "UserName", "type": "exact", "value": "ops-kim"}], t
    assert f["IAMUser"] == [{"property": "Name", "type": "exact", "value": "ops-kim"}]
    assert f["IAMUserPolicy"] == [{"type": "glob", "value": "ops-kim -> *"}]      # legacy string "user -> policy"
    assert f["IAMVirtualMFADevice"] == [{"property": "SerialNumber", "type": "exact",
                                         "value": "arn:aws:iam::123456789012:mfa/ops-kim"}]
    assert {"property": "Name", "type": "exact", "value": "OpsRole"} in f["IAMRole"]
    assert f["IAMGroupPolicy"] == [{"type": "glob", "value": "admins -> *"}]


def test_no_account_alias_resource_exists(resource_docs):
    """aws-nuke has no resource type for the IAM account alias -> nothing to protect (it is never deleted)."""
    assert not [n for n in resource_docs if "accountalias" in n.lower()]
    assert "IAMAccountAlias" not in resource_docs


# ---------------------------------------------------------------- CLI
def run_cli(*args):
    return subprocess.run([sys.executable, str(NUKE_DIR / "make_config.py"), *args], capture_output=True, text=True)


def test_cli_stdout_and_json_out(tmp_path):
    out = tmp_path / "r.json"
    p = run_cli("--account-id", ACCT, "--regions", "ap-northeast-2", "--protect-iam-user", "ops-kim",
                "--json-out", str(out))
    assert p.returncode == 0, p.stderr
    cfg = yaml.safe_load(p.stdout)
    assert cfg["regions"] == ["ap-northeast-2", "global"]
    res = json.loads(out.read_text())
    assert {"module", "account_id", "generated_at", "applied", "status"} <= set(res)
    assert res["module"] == "nuke" and res["applied"] is False and res["status"] == "OK"


def test_cli_out_file_and_bad_account(tmp_path):
    p = run_cli("--account-id", ACCT, "--regions", "us-east-1", "--no-global", "--out", str(tmp_path / "c.yaml"))
    assert p.returncode == 0 and p.stdout == ""
    assert yaml.safe_load((tmp_path / "c.yaml").read_text())["regions"] == ["us-east-1"]
    bad = run_cli("--account-id", "42", "--json-out", str(tmp_path / "e.json"))
    assert bad.returncode == 2
    assert json.loads((tmp_path / "e.json").read_text())["status"] == "ERROR"


# ---------------------------------------------------------------- real binary (optional)
def test_real_aws_nuke_parses_config(aws_nuke_bin, tmp_path):
    """`aws-nuke explain-config --account-id` parses + resolves the config without any AWS credentials."""
    cfg = tmp_path / "c.yaml"
    cfg.write_text(mc.generate(ACCT, ["ap-northeast-2", "us-east-1"], protect_users=["ops-kim"],
                               protect_groups=["g"], bypass_alias_check=True))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    p = subprocess.run([aws_nuke_bin, "explain-config", "-c", str(cfg), "--account-id", ACCT, "--with-excluded"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stdout + p.stderr
    excluded = [ln.strip() for ln in p.stdout.split("Excluded Resource Types:")[1].splitlines() if ln.startswith("  ")]
    assert "S3Object" in excluded and "DynamoDBTableItem" in excluded and "AWS::Timestream::Table" in excluded
    assert set(RETIRED_2026_10) <= set(excluded)
    assert not [x for x in excluded if x.startswith(("QBusiness", "Glue", "CodeStarConnection", "Inspector2"))
                and x != "GlueDevEndpoint"]
    m = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("Excluded:")][0]
    assert int(m.split()[-1]) == len(excluded) == 51
