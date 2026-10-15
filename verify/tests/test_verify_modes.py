"""--mode inventory|cost|auto and the broadened, cost-free inventory (member accounts whose SCPs deny ce:*)."""
import json

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore import xform_name
from botocore.stub import Stubber
from moto import mock_aws

import verify_cost as vc
from common import session as common_session

ACCOUNT = "123456789012"  # moto's default account
REGION = "ap-northeast-2"
SCP_MSG = ("User: arn:aws:iam::123456789012:root is not authorized to perform: ce:GetCostAndUsage "
           "with an explicit deny in a service control policy")


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("MOTO_EC2_LOAD_DEFAULT_AMIS", "false")
    with mock_aws():
        yield


@pytest.fixture
def no_ce(monkeypatch):
    """Fail the test if anything asks for a Cost Explorer client."""
    real = common_session.client
    asked = []

    def guarded(session, service, region=None):
        asked.append(service)
        if service == "ce":
            raise AssertionError("Cost Explorer must not be called in inventory mode")
        return real(session, service, region)
    monkeypatch.setattr(vc, "client", guarded)
    return asked


def _ce_stub(monkeypatch):
    ce = boto3.client("ce", region_name="us-east-1")
    stub = Stubber(ce)
    real = common_session.client
    monkeypatch.setattr(vc, "client", lambda s, svc, region=None: ce if svc == "ce" else real(s, svc, region))
    stub.activate()
    return stub


def _ce_error(code, msg=SCP_MSG):
    return ClientError({"Error": {"Code": code, "Message": msg},
                        "ResponseMetadata": {"HTTPStatusCode": 400}}, "GetCostAndUsage")


def run(tmp_path, *argv):
    out = tmp_path / "r.json"
    code = vc.main(list(argv) + ["--json-out", str(out)])
    return code, json.loads(out.read_text(encoding="utf-8"))


def _small_checks(monkeypatch, regional, global_checks=()):
    monkeypatch.setattr(vc, "REGIONAL_CHECKS", list(regional))
    monkeypatch.setattr(vc, "GLOBAL_CHECKS", list(global_checks))


def _raiser(exc):
    def fn(c, inv=None):
        raise exc
    return fn


# --------------------------------------------------------------------------- inventory mode
def test_inventory_mode_never_calls_ce_and_is_clean_on_empty_account(aws, no_ce, tmp_path, capsys):
    code, res = run(tmp_path, "--mode", "inventory", "--regions", REGION)
    assert "ce" not in no_ce
    assert code == vc.EXIT_OK == 0 and res["status"] == "CLEAN"
    for key in ("module", "account_id", "generated_at", "applied", "status", "mode", "inventory", "examples",
                "not_applicable", "blocked_by_policy"):
        assert key in res, key
    assert res["mode"] == "inventory" and res["mode_requested"] == "inventory"
    assert res["cleanup_date"] is None and res["window"] == {} and res["pending_reasons"] == []
    assert res["ce_requests"] == 0 and res["inventory_ran"] is True
    # broad coverage: ~70 regional types were attempted in the region
    attempted = set(res["inventory"][REGION]) | set(res["inventory_unchecked"].get(REGION, {})) \
        | set(res["not_applicable"].get(REGION, {}))
    assert len(attempted) == len(vc.REGIONAL_CHECKS) >= 40
    assert {"iam_roles", "iam_users", "route53_hosted_zones", "cloudfront_distributions"} <= set(res["inventory"]["global"])
    assert "CLEAN" in capsys.readouterr().out


def test_inventory_mode_needs_no_settle_wait_and_cleanup_date_is_optional(aws, no_ce, tmp_path):
    # cleanup "today": cost mode would be PENDING, inventory mode judges right away
    code, res = run(tmp_path, "--mode", "inventory", "--regions", REGION, "--cleanup-date", "2026-10-15",
                    "--now", "2026-10-15T01:00")
    assert code == 0 and res["status"] == "CLEAN" and res["cleanup_date"] == "2026-10-15"


def test_hub_empty_cleanup_date_is_accepted():
    args = vc.build_parser().parse_args(["--cleanup-date", ""])
    assert args.cleanup_date is None and args.mode == "auto"


def test_inventory_mode_residual_exit_1_with_examples_and_table(aws, no_ce, tmp_path, capsys):
    ec2 = boto3.client("ec2", region_name=REGION)
    ec2.run_instances(ImageId="ami-12345678", MinCount=3, MaxCount=3, InstanceType="t3.micro")
    boto3.client("sqs", region_name="us-east-1").create_queue(QueueName="student-queue")
    boto3.client("iam", region_name="us-east-1").create_user(UserName="student01")
    code, res = run(tmp_path, "--mode", "inventory", "--regions", "%s,us-east-1" % REGION, "--max-examples", "2")
    assert code == vc.EXIT_ISSUES == 1 and res["status"] == "RESIDUAL"
    assert res["inventory"][REGION]["ec2_instances"] == 3
    assert len(res["examples"][REGION]["ec2_instances"]) == 2           # capped by --max-examples
    assert len(res["inventory_items"][REGION]["ec2_instances"]) == 3
    assert res["examples"]["us-east-1"]["sqs_queues"] == ["student-queue"]
    assert res["examples"]["global"]["iam_users"] == ["student01"]
    out = capsys.readouterr().out
    assert "RESIDUAL" in out and "ec2_instances" in out and REGION in out and "외 1개" in out


def test_default_vpc_set_ignored_but_non_default_vpc_counted(aws, no_ce, tmp_path):
    ec2 = boto3.client("ec2", region_name=REGION)
    # the default VPC, its default subnets/IGW/default SG exist already (moto creates them)
    assert ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    inv = vc.run_inventory(boto3.Session(region_name="us-east-1"), [REGION])
    for t in ("vpcs", "subnets", "security_groups", "internet_gateways"):
        assert inv.counts[REGION][t] == 0, t

    vpc = ec2.create_vpc(CidrBlock="10.1.0.0/16")["Vpc"]["VpcId"]
    ec2.create_subnet(VpcId=vpc, CidrBlock="10.1.1.0/24")
    ec2.create_security_group(GroupName="student-web", Description="x", VpcId=vpc)
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    inv = vc.run_inventory(boto3.Session(region_name="us-east-1"), [REGION])
    c = inv.counts[REGION]
    assert (c["vpcs"], c["subnets"], c["security_groups"], c["internet_gateways"]) == (1, 1, 1, 1)
    assert inv.items[REGION]["vpcs"] == [vpc]
    assert inv.items[REGION]["security_groups"][0].endswith("/student-web")  # its own 'default' SG is ignored


def test_protected_names_stacks_and_their_resources_ignored(aws, no_ce, tmp_path):
    cfn = boto3.client("cloudformation", region_name=REGION)
    template = {"Resources": {
        "Queue": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "plain-name-owned-by-killswitch"}},
        "Topic": {"Type": "AWS::SNS::Topic"},
    }}
    cfn.create_stack(StackName="edu-killswitch-%s" % REGION, TemplateBody=json.dumps(template))
    cfn.create_stack(StackName="edu-bootstrap", TemplateBody=json.dumps(
        {"Resources": {"T": {"Type": "AWS::SNS::Topic", "Properties": {"TopicName": "bootstrap-notify"}}}}))
    iam = boto3.client("iam", region_name="us-east-1")
    iam.create_role(RoleName="EduCleanupRole", AssumeRolePolicyDocument="{}")
    iam.create_role(RoleName="OrganizationAccountAccessRole", AssumeRolePolicyDocument="{}")
    iam.create_role(RoleName="AWSServiceRoleForSupport", Path="/aws-service-role/support.amazonaws.com/",
                    AssumeRolePolicyDocument="{}")
    iam.create_user(UserName="moto")  # the caller (moto's STS identity is user/moto) is protected
    boto3.client("logs", region_name=REGION).create_log_group(logGroupName="/aws/lambda/edu-bootstrap-account-alias")
    boto3.client("logs", region_name=REGION).create_log_group(logGroupName="aws-controltower/CloudTrailLogs")
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName="lab-keep-me", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}])

    code, res = run(tmp_path, "--mode", "inventory", "--regions", REGION, "--protect-prefix", "lab-keep")
    assert code == 0 and res["status"] == "CLEAN", res["inventory_items"]
    prot = res["inventory_protected"]
    assert sorted(prot[REGION]["cloudformation_stacks"]) == ["edu-bootstrap", "edu-killswitch-%s" % REGION]
    assert prot[REGION]["sqs_queues"] == ["plain-name-owned-by-killswitch"]  # via stack physical IDs
    assert len(prot[REGION]["sns_topics"]) == 2
    assert prot[REGION]["dynamodb_tables"] == ["lab-keep-me"]                 # --protect-prefix
    assert sorted(prot["global"]["iam_roles"]) == ["OrganizationAccountAccessRole"]  # EduCleanupRole skipped outright
    assert prot["global"]["iam_users"] == ["moto"]
    assert prot[REGION]["cloudwatch_log_groups"] == ["/aws/lambda/edu-bootstrap-account-alias"]
    assert res["inventory"][REGION]["cloudwatch_log_groups"] == 0             # aws-controltower/* skipped
    assert res["inventory"]["global"]["iam_roles"] == 0                       # service-linked role ignored


def test_other_stack_counts_and_unprotected_stack_resources_count(aws, no_ce, tmp_path):
    boto3.client("cloudformation", region_name=REGION).create_stack(
        StackName="student-lab", TemplateBody=json.dumps(
            {"Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {"QueueName": "lab-q"}}}}))
    code, res = run(tmp_path, "--mode", "inventory", "--regions", REGION)
    assert code == 1
    assert res["inventory"][REGION]["cloudformation_stacks"] == 1 and res["inventory"][REGION]["sqs_queues"] == 1


# --------------------------------------------------------------------------- error classes
@pytest.mark.parametrize("exc", [
    EndpointConnectionError(endpoint_url="https://kendra.ap-northeast-2.amazonaws.com"),
    ClientError({"Error": {"Code": "InvalidAction", "Message": "not valid for this web service"}}, "X"),
    ClientError({"Error": {"Code": "SubscriptionRequiredException", "Message": "needs a subscription"}}, "X"),
    ClientError({"Error": {"Code": "OptInRequired", "Message": "opt in"}}, "X"),
    ClientError({"Error": {"Code": "UnrecognizedClientException", "Message": "invalid token"}}, "X"),
    ClientError({"Error": {"Code": "ValidationException", "Message": "This API is not supported in this region"}}, "X"),
    ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Account is not authorized for region"}}, "X"),
])
def test_not_applicable_errors_never_residual(aws, no_ce, monkeypatch, tmp_path, exc):
    _small_checks(monkeypatch, [("ec2_instances", "ec2", vc._ec2_instances), ("kendra_indexes", "kendra", _raiser(exc))])
    code, res = run(tmp_path, "--mode", "inventory", "--regions", "us-east-1")
    assert code == 0 and res["status"] == "CLEAN"
    assert "kendra_indexes" in res["not_applicable"]["us-east-1"]
    assert res["not_applicable_count"] == 1 and res["inventory_unchecked"] == {} and res["blocked_by_policy"] == {}
    assert res["inventory_complete"] is True


def test_scp_access_denied_recorded_as_blocked_by_policy(aws, no_ce, monkeypatch, tmp_path, capsys):
    scp = ClientError({"Error": {"Code": "AccessDeniedException", "Message":
                                 "User: arn:aws:sts::1:assumed-role/EduCleanupRole/x is not authorized to perform: "
                                 "bedrock:ListCustomModels with an explicit deny in a service control policy"},
                       "ResponseMetadata": {"HTTPStatusCode": 400}}, "ListCustomModels")
    plain = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"},
                         "ResponseMetadata": {"HTTPStatusCode": 403}}, "X")
    _small_checks(monkeypatch, [("ec2_instances", "ec2", vc._ec2_instances),
                                ("bedrock_custom_models", "bedrock", _raiser(scp)),
                                ("sagemaker_endpoints", "sagemaker", _raiser(plain))])
    code, res = run(tmp_path, "--mode", "inventory", "--regions", "us-east-1")
    assert code == 0 and res["status"] == "CLEAN"
    assert res["blocked_by_policy"]["us-east-1"] == {"bedrock_custom_models": "AccessDeniedException (SCP)",
                                                     "sagemaker_endpoints": "AccessDenied"}
    assert res["blocked_by_policy_count"] == 2
    assert res["inventory_unchecked"] == {} and not [e for e in res["errors"] if e["stage"] == "inventory"]
    assert res["inventory_complete"] is False and res["warnings"]
    assert "정책(SCP 등)으로 조회 차단: 2건" in capsys.readouterr().out


def test_real_errors_are_could_not_check_but_not_residual(aws, no_ce, monkeypatch, tmp_path):
    boom = ClientError({"Error": {"Code": "InternalFailure", "Message": "oops"}}, "X")
    _small_checks(monkeypatch, [("ec2_instances", "ec2", vc._ec2_instances), ("glue_jobs", "glue", _raiser(boom)),
                                ("glue_crawlers", "glue", _raiser(KeyError("x")))])
    code, res = run(tmp_path, "--mode", "inventory", "--regions", "us-east-1")
    assert code == 0
    assert res["inventory_unchecked"]["us-east-1"] == {"glue_jobs": "InternalFailure", "glue_crawlers": "KeyError"}
    assert {e["check"] for e in res["errors"] if e["stage"] == "inventory"} == {"glue_jobs", "glue_crawlers"}


def test_dead_region_marks_rest_not_applicable(aws, no_ce, monkeypatch):
    dead = ClientError({"Error": {"Code": "AuthFailure", "Message": "region not enabled"}}, "DescribeInstances")
    called = []

    def later(c, inv=None):
        called.append(1)
        return []
    _small_checks(monkeypatch, [("ec2_instances", "ec2", _raiser(dead)), ("lambda_functions", "lambda", later)])
    inv = vc.run_inventory(boto3.Session(region_name="us-east-1"), ["us-east-1"])
    assert called == []
    assert inv.not_applicable["us-east-1"] == {"ec2_instances": "AuthFailure", "lambda_functions": "AuthFailure"}


def test_classify_check_error_table():
    def ce(code, msg="", status=400):
        return ClientError({"Error": {"Code": code, "Message": msg}, "ResponseMetadata": {"HTTPStatusCode": status}}, "X")
    assert vc.classify_check_error(ce("AccessDeniedException", "explicit deny in a service control policy"))[0] == vc.KIND_BLOCKED
    assert vc.classify_check_error(ce("UnauthorizedOperation"))[0] == vc.KIND_BLOCKED
    assert vc.classify_check_error(ce("AuthorizationError"))[0] == vc.KIND_BLOCKED
    assert vc.classify_check_error(ce("SomethingElse", status=403))[0] == vc.KIND_BLOCKED
    assert vc.classify_check_error(ce("InvalidAction"))[0] == vc.KIND_NA
    assert vc.classify_check_error(ce("AuthFailure"))[0] == vc.KIND_NA
    assert vc.classify_check_error(ce("ExpiredToken", status=403))[0] == vc.KIND_ERROR
    assert vc.classify_check_error(ce("ThrottlingException"))[0] == vc.KIND_ERROR
    assert vc.classify_check_error(EndpointConnectionError(endpoint_url="https://x"))[0] == vc.KIND_NA


def test_deleting_state_still_counted_but_flagged():
    inv = vc.Inventory()
    inv.record("r", "rds_instances", [("db-1", "deleting"), ("db-2", "available"), ("edu-bootstrap-db", "available")])
    assert inv.counts["r"]["rds_instances"] == 2
    assert inv.items["r"]["rds_instances"] == ["db-1 (deleting)", "db-2"]
    assert inv.deleting == {"r": {"rds_instances": 1}}
    assert inv.protected["r"]["rds_instances"] == ["edu-bootstrap-db"]


def test_protection_matches_path_and_arn_segments():
    inv = vc.Inventory(vc.PROTECTED_PREFIXES, ("ops-admin",))
    assert inv.is_protected("/aws/lambda/edu-killswitch-us-east-1")
    assert inv.is_protected("arn:aws:sns:us-east-1:1:edu-killswitch-us-east-1-Topic-ABC")
    assert inv.is_protected("ops-admin") and inv.is_protected("StackSet-Reseller-Baseline-1234")
    assert not inv.is_protected("student-bucket") and not inv.is_protected("ops-admin-2")
    inv.add_protected_ids(["abc", "physical-id-123"])  # too-short IDs are ignored (avoid generic matches)
    assert inv.is_protected("/x/physical-id-123") and not inv.is_protected("abc")


# --------------------------------------------------------------------------- exit codes
def test_inventory_total_failure_exit_2(aws, no_ce, monkeypatch, tmp_path):
    def deny_all(model, **kwargs):
        if model.service_model.service_name != "sts":
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "explicit deny in a service control policy"}},
                              model.name)
    real = vc.get_session

    def get_session(args):
        s = real(args)
        s.events.register("before-call", deny_all)
        return s
    monkeypatch.setattr(vc, "get_session", get_session)
    code, res = run(tmp_path, "--mode", "inventory", "--regions", "us-east-1")
    assert code == vc.EXIT_ERROR == 2 and res["status"] == "ERROR"
    assert res["inventory_checked"] == 0 and res["blocked_by_policy_count"] > 0


def test_inventory_auth_failure_exit_2(monkeypatch, tmp_path):
    def boom(args):
        raise ClientError({"Error": {"Code": "InvalidClientTokenId", "Message": "bad"}}, "GetCallerIdentity")
    monkeypatch.setattr(vc, "get_session", boom)
    code, res = run(tmp_path, "--mode", "inventory")
    assert code == 2 and res["errors"][0]["stage"] == "auth" and res["mode"] == "inventory"


def test_decide_inventory():
    assert vc.decide_inventory(0, 10) == "CLEAN"
    assert vc.decide_inventory(3, 10) == "RESIDUAL"
    assert vc.decide_inventory(0, 0) == "ERROR"


# --------------------------------------------------------------------------- auto mode
CE_DAY_ARGS = ["--cleanup-date", "2026-10-10", "--now", "2026-10-14T03:00", "--regions", REGION]


@pytest.mark.parametrize("code,msg,reason", [
    ("AccessDeniedException", SCP_MSG, "scp-deny"),
    ("AccessDeniedException", "User not enabled for cost explorer access", "not-enabled"),
    ("DataUnavailableException", "Data is not available.", "data-unavailable"),
    ("AccessDeniedException", "not authorized to perform: ce:GetCostAndUsage", "access-denied"),
])
def test_auto_falls_back_to_inventory_when_ce_unusable(aws, monkeypatch, tmp_path, capsys, code, msg, reason):
    stub = _ce_stub(monkeypatch)
    stub.add_client_error("get_cost_and_usage", service_error_code=code, service_message=msg, http_status_code=400)
    rc, res = run(tmp_path, *CE_DAY_ARGS)
    stub.assert_no_pending_responses()
    assert rc == 0 and res["status"] == "CLEAN"
    assert res["mode"] == "inventory" and res["mode_requested"] == "auto"
    assert res["ce_fallback"]["code"] == code and res["ce_fallback"]["reason"] == reason
    assert res["inventory_ran"] is True and res["pending_reasons"] == [] and res["ce_requests"] == 0
    assert not [e for e in res["errors"] if e["stage"] == "cost"]
    out = capsys.readouterr().out
    assert "auto: Cost Explorer 사용 불가로 대체" in out


def test_auto_fallback_finds_residual_exit_1(aws, monkeypatch, tmp_path):
    boto3.client("ec2", region_name=REGION).run_instances(ImageId="ami-12345678", MinCount=1, MaxCount=1)
    stub = _ce_stub(monkeypatch)
    stub.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException", service_message=SCP_MSG)
    rc, res = run(tmp_path, *CE_DAY_ARGS)
    assert rc == 1 and res["status"] == "RESIDUAL" and res["inventory"][REGION]["ec2_instances"] == 1


def test_auto_does_not_fall_back_on_other_errors(aws, monkeypatch, tmp_path):
    stub = _ce_stub(monkeypatch)
    stub.add_client_error("get_cost_and_usage", service_error_code="ThrottlingException", service_message="slow down")
    rc, res = run(tmp_path, *CE_DAY_ARGS)
    assert rc == 2 and res["status"] == "ERROR" and res["mode"] == "cost" and res["ce_fallback"] is None


def test_cost_mode_never_falls_back(aws, monkeypatch, tmp_path):
    stub = _ce_stub(monkeypatch)
    stub.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException", service_message=SCP_MSG)
    rc, res = run(tmp_path, "--mode", "cost", *CE_DAY_ARGS)
    assert rc == 2 and res["mode"] == "cost" and res["inventory_ran"] is False


def test_auto_probes_ce_when_window_empty_and_falls_back(aws, monkeypatch, tmp_path):
    # cleanup today: no CE day to judge yet; auto still learns (1 request) that CE is denied
    stub = _ce_stub(monkeypatch)
    stub.add_client_error("get_cost_and_usage", service_error_code="AccessDeniedException", service_message=SCP_MSG,
                          expected_params={"TimePeriod": {"Start": "2026-10-14", "End": "2026-10-15"},
                                           "Granularity": "DAILY", "Metrics": ["UnblendedCost"]})
    rc, res = run(tmp_path, "--cleanup-date", "2026-10-15", "--now", "2026-10-15T01:00", "--regions", REGION)
    stub.assert_no_pending_responses()
    assert rc == 0 and res["mode"] == "inventory" and res["ce_fallback"]["reason"] == "scp-deny"


def test_auto_probe_ok_stays_pending(aws, monkeypatch, tmp_path):
    stub = _ce_stub(monkeypatch)
    stub.add_response("get_cost_and_usage", {"ResultsByTime": []})
    rc, res = run(tmp_path, "--cleanup-date", "2026-10-15", "--now", "2026-10-15T01:00", "--regions", REGION)
    assert rc == 3 and res["status"] == "PENDING" and res["mode"] == "cost"
    assert res["ce_requests"] == 1 and res["ce_probe"] == "ok" and res["inventory_ran"] is False


# --------------------------------------------------------------------------- botocore names
def _op_model(c, op):
    names = {xform_name(o): o for o in c.meta.service_model.operation_names}
    assert op in names, "%s has no %s" % (c.meta.service_model.service_name, op)
    return c.meta.service_model.operation_model(names[op])


@pytest.mark.parametrize("rtype,service,fn", vc.REGIONAL_CHECKS + vc.GLOBAL_CHECKS,
                         ids=[c[0] for c in vc.REGIONAL_CHECKS + vc.GLOBAL_CHECKS])
def test_every_check_uses_real_botocore_names(rtype, service, fn):
    c = boto3.client(service, region_name="us-east-1")
    assert getattr(fn, "ops", None), "%s must declare the operations it calls" % rtype
    for op in fn.ops:
        assert hasattr(c, op)
        _op_model(c, op)
    spec = getattr(fn, "spec", None)
    if not spec:
        return
    om = _op_model(c, spec["op"])
    out = om.output_shape.members
    assert spec["key"] in out, "%s: %s not in %s output" % (rtype, spec["key"], spec["op"])
    item = out[spec["key"]].member
    for field in spec["fields"]:
        assert item.type_name == "structure" and field in item.members, "%s: no field %s" % (rtype, field)
    for k in spec["kwargs"]:
        assert k in om.input_shape.members, "%s: %s is not an input of %s" % (rtype, k, spec["op"])
    if c.can_paginate(spec["op"]):
        page_keys = c.get_paginator(spec["op"]).result_keys or []
        assert spec["key"] in [k.expression for k in page_keys], "%s: paginator does not aggregate %s" % (rtype, spec["key"])


def test_inventory_covers_at_least_forty_types():
    names = [c[0] for c in vc.REGIONAL_CHECKS + vc.GLOBAL_CHECKS]
    assert len(names) == len(set(names)) and len(names) + 1 >= 40  # + s3_buckets


def test_bedrock_region_gating_is_not_applicable():
    """Live 2026-10-15: bedrock imported/custom model listings fail only in regions without the feature."""
    from botocore.exceptions import ClientError

    def ce(code, msg="", status=400):
        return ClientError({"Error": {"Code": code, "Message": msg},
                            "ResponseMetadata": {"HTTPStatusCode": status}}, "List")

    assert vc.classify_check_error(ce("ValidationException", "Unknown operation"), "bedrock_custom_models")[0] == vc.KIND_NA
    assert vc.classify_check_error(ce("AccessDeniedException", "", 403), "bedrock_imported_models")[0] == vc.KIND_NA
    assert vc.classify_check_error(
        ce("AccessDeniedException", "Your account is not authorized to invoke this API operation."), "x")[0] == vc.KIND_NA
    # an explicit SCP deny on a Bedrock check is still "blocked by policy"
    assert vc.classify_check_error(
        ce("AccessDeniedException", "with an explicit deny in a service control policy"), "bedrock_imported_models")[0] == vc.KIND_BLOCKED
    # non-gated checks keep the old behaviour
    assert vc.classify_check_error(ce("AccessDeniedException", "", 403), "ec2_instances")[0] == vc.KIND_BLOCKED
