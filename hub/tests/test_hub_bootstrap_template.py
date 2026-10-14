import json
import re
import shutil
import subprocess
import sys
import types

import pytest

import hub_test_support  # noqa: F401  (sets sys.path)

HUB_DIR = hub_test_support.HUB_DIR
TEMPLATE = HUB_DIR / "bootstrap.yaml"


@pytest.fixture(scope="module")
def tpl():
    from cfnlint.decode import decode
    t, matches = decode(str(TEMPLATE))
    assert not matches, matches
    return t


def test_cfn_lint_clean():
    exe = shutil.which("cfn-lint")
    if exe:
        proc = subprocess.run([exe, str(TEMPLATE)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    else:  # fall back to the library API
        from cfnlint.api import lint_file
        assert lint_file(TEMPLATE) == []


def test_parameters(tpl):
    p = tpl["Parameters"]
    assert set(p) == {"HubPrincipalArn", "ExternalId", "AccountAlias"}
    assert p["ExternalId"]["NoEcho"] is True
    assert p["ExternalId"]["MinLength"] == 8
    assert p["AccountAlias"]["Default"] == ""


@pytest.mark.parametrize("alias, ok", [
    ("", True), ("edu-2026-10-01", True), ("abc", True),
    ("edu-prod-01", False), ("production", False), ("edu--2026", False), ("Edu-1", False), ("ab", False),
    ("-edu", False), ("edu-", False),
])
def test_alias_pattern(tpl, alias, ok):
    pattern = tpl["Parameters"]["AccountAlias"]["AllowedPattern"]
    assert bool(re.fullmatch(pattern, alias)) is ok


@pytest.mark.parametrize("arn, ok", [
    ("arn:aws:iam::123456789012:role/EduHubOrchestrator", True),
    ("arn:aws:iam::123456789012:role/path/EduHub", True),
    ("arn:aws:iam::123456789012:user/edu-hub", True),
    ("arn:aws:iam::12345678901:role/x", False),
    ("arn:aws:sts::123456789012:assumed-role/x/y", False),
])
def test_hub_principal_pattern(tpl, arn, ok):
    pattern = tpl["Parameters"]["HubPrincipalArn"]["AllowedPattern"]
    assert bool(re.fullmatch(pattern, arn)) is ok
    if ok:  # the template derives the hub account id with !Select [4, !Split [":", arn]]
        assert arn.split(":")[4] == "123456789012"


def test_role_trust_policy_has_both_statements(tpl):
    role = tpl["Resources"]["EduCleanupRole"]
    props = role["Properties"]
    assert role["Type"] == "AWS::IAM::Role"
    assert props["RoleName"] == "EduCleanupRole"
    assert props["MaxSessionDuration"] == 43200
    assert any("AdministratorAccess" in json.dumps(a) for a in props["ManagedPolicyArns"])
    stmts = props["AssumeRolePolicyDocument"]["Statement"]
    assert len(stmts) == 2

    hub = stmts[0]
    assert hub["Effect"] == "Allow" and hub["Action"] == "sts:AssumeRole"
    principal = json.dumps(hub["Principal"]["AWS"])
    assert ":root" in principal and "Fn::Select" in principal and "Fn::Split" in principal
    sub = hub["Principal"]["AWS"]["Fn::Sub"]
    assert sub[1]["HubAccountId"] == {"Fn::Select": [4, {"Fn::Split": [":", {"Ref": "HubPrincipalArn"}]}]}
    cond = hub["Condition"]
    assert cond["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"}
    arn_cond = cond.get("ArnEquals") or cond.get("ArnLike")
    assert arn_cond["aws:PrincipalArn"] == {"Ref": "HubPrincipalArn"}

    cfn = stmts[1]
    assert cfn["Effect"] == "Allow" and cfn["Action"] == "sts:AssumeRole"
    assert cfn["Principal"] == {"Service": "cloudformation.amazonaws.com"}


def test_protect_policy_not_attached_and_targets(tpl):
    res = tpl["Resources"]
    pol = [r for r in res.values() if r["Type"] == "AWS::IAM::ManagedPolicy"]
    assert len(pol) == 1
    props = pol[0]["Properties"]
    assert props["ManagedPolicyName"] == "EduProtectCleanupRole"
    for key in ("Users", "Groups", "Roles"):
        assert key not in props
    stmts = props["PolicyDocument"]["Statement"]
    role_stmt = next(s for s in stmts if s["Sid"] == "DenyCleanupRoleTampering")
    assert role_stmt["Effect"] == "Deny"
    assert set(role_stmt["Action"]) >= {"iam:*Role*", "iam:*RolePolicy*", "iam:*RolePolicies*"}
    assert role_stmt["Resource"] == {"Fn::GetAtt": ["EduCleanupRole", "Arn"]}
    stack_stmt = next(s for s in stmts if s["Sid"] == "DenyBootstrapStackChanges")
    assert {"cloudformation:DeleteStack", "cloudformation:UpdateStack"} <= set(stack_stmt["Action"])
    assert "stack/edu-bootstrap" in json.dumps(stack_stmt["Resource"])
    # nothing in the template attaches the policy to a principal
    for r in res.values():
        if r["Type"] in ("AWS::IAM::User", "AWS::IAM::Group"):
            pytest.fail("bootstrap must not create users/groups")


def test_alias_resources_are_conditional(tpl):
    res = tpl["Resources"]
    for name in ("AliasFunction", "AliasFunctionRole", "AccountAliasResource", "AliasFunctionLogGroup"):
        assert res[name].get("Condition") == "HasAlias"
    assert res["AccountAliasResource"]["Type"].startswith("Custom::")
    assert tpl["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["EduCleanupRole", "Arn"]}


# ---- the inline custom-resource Lambda --------------------------------------------------------
class FakeContext:
    log_stream_name = "stream"

    def get_remaining_time_in_millis(self):
        return 30000


class FakeIam:
    def __init__(self, existing=None, fail=False):
        self.existing = list(existing or [])
        self.created = []
        self.fail = fail

    def list_account_aliases(self):
        if self.fail:
            raise RuntimeError("AccessDenied")
        return {"AccountAliases": list(self.existing)}

    def create_account_alias(self, AccountAlias):
        self.created.append(AccountAlias)
        self.existing = [AccountAlias]


@pytest.fixture
def lambda_mod(tpl, monkeypatch):
    code = tpl["Resources"]["AliasFunction"]["Properties"]["Code"]["ZipFile"]
    assert len(code) <= 4096, "inline ZipFile code must stay under 4 KB"
    iam = FakeIam()
    fake_boto3 = types.SimpleNamespace(client=lambda name: iam)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    mod = types.ModuleType("alias_fn")
    exec(compile(code, "alias_fn", "exec"), mod.__dict__)
    sent = []

    class Resp:
        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "method": req.get_method(), "body": json.loads(req.data)})
        return Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return mod, iam, sent


def event(request_type, alias="edu-2026-10-01"):
    return {"RequestType": request_type, "ResponseURL": "https://cfn-response.example/abc",
            "StackId": "arn:aws:cloudformation:ap-northeast-2:111111111111:stack/edu-bootstrap/1",
            "RequestId": "r1", "LogicalResourceId": "AccountAliasResource",
            "ResourceProperties": {"ServiceToken": "x", "AccountAlias": alias}}


def test_lambda_creates_alias_when_none(lambda_mod):
    mod, iam, sent = lambda_mod
    mod.handler(event("Create"), FakeContext())
    assert iam.created == ["edu-2026-10-01"]
    assert len(sent) == 1 and sent[0]["method"] == "PUT"
    body = sent[0]["body"]
    assert body["Status"] == "SUCCESS" and body["Data"] == {"Alias": "edu-2026-10-01", "Action": "created"}
    assert body["PhysicalResourceId"] == "edu-account-alias"


def test_lambda_keeps_existing_alias(lambda_mod):
    mod, iam, sent = lambda_mod
    iam.existing = ["some-other-alias"]
    mod.handler(event("Create"), FakeContext())
    assert iam.created == []
    assert sent[0]["body"]["Status"] == "SUCCESS"
    assert sent[0]["body"]["Data"]["Action"] == "kept-existing"


def test_lambda_delete_is_noop(lambda_mod):
    mod, iam, sent = lambda_mod
    iam.fail = True  # any IAM call would raise
    ev = event("Delete")
    ev["PhysicalResourceId"] = "edu-account-alias"
    mod.handler(ev, FakeContext())
    assert iam.created == []
    assert sent[0]["body"]["Status"] == "SUCCESS"


def test_lambda_always_responds_on_error(lambda_mod):
    mod, iam, sent = lambda_mod
    iam.fail = True
    mod.handler(event("Create"), FakeContext())
    assert len(sent) == 1
    assert sent[0]["body"]["Status"] == "FAILED"
    assert "AccessDenied" in sent[0]["body"]["Reason"]


def test_lambda_refuses_prod_alias(lambda_mod):
    mod, iam, sent = lambda_mod
    mod.handler(event("Update", alias="edu-prod-1"), FakeContext())
    assert iam.created == []
    assert sent[0]["body"]["Status"] == "FAILED"


def test_hub_policy_json():
    pol = json.loads((HUB_DIR / "hub-policy.json").read_text(encoding="utf-8"))
    st = pol["Statement"]
    assert len(st) == 1
    assert st[0]["Effect"] == "Allow"
    assert st[0]["Action"] == "sts:AssumeRole"
    assert st[0]["Resource"] == "arn:aws:iam::*:role/EduCleanupRole"
