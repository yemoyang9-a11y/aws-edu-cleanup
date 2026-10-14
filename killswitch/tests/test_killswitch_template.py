"""Static checks of killswitch.yaml: cfn-lint, inline code == lambda_src.py, IAM safety."""
import ast
import json
import shutil
import subprocess
import sys

import pytest
from cfnlint.decode import decode

from killswitch_test_support import KS, load_module

TEMPLATE = KS / "killswitch.yaml"


@pytest.fixture(scope="module")
def tpl():
    t, matches = decode(str(TEMPLATE))
    assert not matches, matches
    return t


def test_cfn_lint_clean():
    exe = shutil.which("cfn-lint")
    cmd = [exe] if exe else [sys.executable, "-m", "cfnlint"]
    r = subprocess.run(cmd + [str(TEMPLATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_inline_code_matches_lambda_src(tpl):
    code = str(tpl["Resources"]["KillSwitchFunction"]["Properties"]["Code"]["ZipFile"])
    assert code == (KS / "lambda_src.py").read_text(encoding="utf-8"), \
        "run: python3 killswitch/sync_inline_code.py"
    ast.parse(code)
    sync = load_module("ks_sync", KS / "sync_inline_code.py")
    assert sync.main(["--check"]) == 0


def test_size_limits():
    # CreateStack TemplateBody limit is 51,200 bytes (hub sends the body directly);
    # inline ZipFile is zipped by CloudFormation and must stay under 4 MB.
    assert TEMPLATE.stat().st_size < 51200
    assert (KS / "lambda_src.py").stat().st_size < 4 * 1024 * 1024


def test_parameters_match_contract(tpl):
    p = tpl["Parameters"]
    for key in ("TokenThreshold", "PeriodMinutes", "TargetMode", "TargetGroupName", "ExemptUserNames",
                "NotificationEmail", "EnableApiTriggers"):
        assert key in p
    assert p["TokenThreshold"]["Type"] == "Number"
    assert p["PeriodMinutes"]["Default"] == 5
    assert p["TargetMode"]["AllowedValues"] == ["ALL_IAM_USERS", "GROUP"]
    assert p["ExemptUserNames"]["Type"] == "CommaDelimitedList"
    assert p["EnableApiTriggers"]["Default"] == "true"
    for key, val in p.items():
        if key != "TokenThreshold":
            assert "Default" in val, key  # hub may omit every optional parameter
    # every allowed period has a mapping entry
    periods = {str(v) for v in p["PeriodMinutes"]["AllowedValues"]}
    assert periods == set(tpl["Mappings"]["PeriodSeconds"])
    assert all(v["Seconds"] == int(k) * 60 for k, v in tpl["Mappings"]["PeriodSeconds"].items())


def test_no_named_iam_resources_capability_iam_only(tpl):
    for name, res in tpl["Resources"].items():
        if res["Type"].startswith("AWS::IAM::"):
            props = res.get("Properties", {})
            for named in ("RoleName", "UserName", "GroupName", "ManagedPolicyName", "InstanceProfileName"):
                assert named not in props, (name, named)


def test_alarm_metrics(tpl):
    a = tpl["Resources"]["TokenAlarm"]["Properties"]
    assert a["TreatMissingData"] == "notBreaching"
    assert a["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    returned = [m for m in a["Metrics"] if m["ReturnData"]]
    assert len(returned) == 1 and returned[0]["Expression"].startswith("SUM(")
    stats = {(m["MetricStat"]["Metric"]["Namespace"], m["MetricStat"]["Metric"]["MetricName"])
             for m in a["Metrics"] if "MetricStat" in m}
    assert stats == {("AWS/Bedrock", "InputTokenCount"), ("AWS/Bedrock", "OutputTokenCount"),
                     ("AWS/BedrockMantle", "TotalInputTokens"), ("AWS/BedrockMantle", "TotalOutputTokens")}
    for m in a["Metrics"]:
        if "MetricStat" in m:
            assert "Dimensions" not in m["MetricStat"]["Metric"]  # account-level series
            assert m["MetricStat"]["Stat"] == "Sum"
    assert "SEARCH" not in json.dumps(a["Metrics"])  # SEARCH cannot back an alarm


def test_event_rules(tpl):
    r = tpl["Resources"]
    bed = r["BedrockApiRule"]["Properties"]["EventPattern"]
    assert bed["detail-type"] == ["AWS API Call via CloudTrail"]
    assert bed["detail"]["eventSource"] == ["bedrock.amazonaws.com"]
    assert set(bed["detail"]["eventName"]) == {"PutUseCaseForModelAccess", "CreateFoundationModelAgreement",
                                               "DeleteModelInvocationLoggingConfiguration"}
    iam = r["IamApiRule"]
    assert iam["Condition"] == "IamApiTriggers"
    assert iam["Properties"]["EventPattern"]["detail"]["eventSource"] == ["iam.amazonaws.com"]
    assert set(iam["Properties"]["EventPattern"]["detail"]["eventName"]) == {
        "CreateUser", "CreateAccessKey", "CreateServiceSpecificCredential"}
    cond = tpl["Conditions"]
    assert cond["IsUsEast1"] == {"Fn::Equals": [{"Ref": "AWS::Region"}, "us-east-1"]}


def test_lambda_role_least_privilege(tpl):
    role = tpl["Resources"]["FunctionRole"]["Properties"]
    stmts = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = {a for s in stmts for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    assert not any(a.endswith("*") for a in actions), actions
    assert all(s["Effect"] == "Allow" for s in stmts)
    # write actions are scoped to users / the one fallback policy - never roles
    for s in stmts:
        acts = [s["Action"]] if isinstance(s["Action"], str) else s["Action"]
        if any(a.split(":")[1].startswith(("Put", "Update", "Tag", "Attach", "Create")) for a in acts
               if a.startswith("iam:")):
            res = json.dumps(s["Resource"])
            assert ":user/" in res or ":policy/edu-killswitch/" in res, s
            assert ":role/" not in res
    assert "iam:DeleteUserPolicy" not in actions and "iam:DeleteLoginProfile" not in actions
    fn = tpl["Resources"]["KillSwitchFunction"]["Properties"]
    assert fn["Handler"] == "index.handler" and fn["Runtime"].startswith("python3.1")
    assert fn["Timeout"] <= 900


def test_lambda_runtime_supported_by_cfn_lint_lifecycle():
    import os
    import cfnlint
    spec = json.loads(open(os.path.join(os.path.dirname(cfnlint.__file__), "data", "AdditionalSpecs",
                                        "LmbdRuntimeLifecycle.json")).read())
    t, _ = decode(str(TEMPLATE))
    runtime = t["Resources"]["KillSwitchFunction"]["Properties"]["Runtime"]
    assert runtime in spec and spec[runtime]["deprecated"] >= "2029-01-01"


def test_outputs(tpl):
    for key in ("AlarmName", "TopicArn", "FunctionName"):
        assert key in tpl["Outputs"]


def test_hub_reads_same_parameter_names():
    """hub/orchestrate.py parses parameter names without PyYAML - make sure it sees ours."""
    try:
        sys.path.insert(0, str(KS.parent / "hub"))
        orch = load_module("ks_hub_orch", KS.parent / "hub" / "orchestrate.py")
    except Exception as exc:  # hub module is built in parallel; skip if absent/broken
        pytest.skip("hub/orchestrate.py not importable: %s" % exc)
    if not hasattr(orch, "template_parameter_names"):
        pytest.skip("hub has no template_parameter_names")
    names = orch.template_parameter_names(TEMPLATE.read_text(encoding="utf-8"))
    for key in ("TokenThreshold", "PeriodMinutes", "TargetMode", "TargetGroupName", "ExemptUserNames",
                "NotificationEmail", "EnableApiTriggers"):
        assert key in names
