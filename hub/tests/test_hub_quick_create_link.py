import json
import shlex
from urllib.parse import parse_qs, urlsplit

import pytest

import hub_test_support  # noqa: F401  (sets sys.path)
import make_quick_create_link as mql

TPL = "https://edu-bootstrap-tpl.s3.ap-northeast-2.amazonaws.com/edu/bootstrap.yaml?versionId=a+b/c"
HUB = "arn:aws:iam::999999999999:role/edu/EduHubOrchestrator"
EXT = "Ab+c=d,e.f@g:h/i-j_k"  # every special character IAM allows in an ExternalId


def split_link(url):
    parts = urlsplit(url)
    route, _, query = parts.fragment.partition("?")
    return parts, route, query


def test_url_structure_and_round_trip():
    url = mql.build_quick_create_url(TPL, HUB, EXT, "edu-2026-10-01", "ap-northeast-2")
    parts, route, query = split_link(url)
    assert parts.scheme == "https"
    assert parts.netloc == "ap-northeast-2.console.aws.amazon.com"
    assert parts.path == "/cloudformation/home"
    assert parts.query == "region=ap-northeast-2"
    assert route == "/stacks/quickcreate"
    q = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    assert q == {
        "templateURL": [TPL],
        "stackName": ["edu-bootstrap"],
        "param_HubPrincipalArn": [HUB],
        "param_ExternalId": [EXT],
        "param_AccountAlias": ["edu-2026-10-01"],
    }
    assert list(q) == ["templateURL", "stackName", "param_HubPrincipalArn", "param_ExternalId",
                       "param_AccountAlias"]


def test_values_are_fully_percent_encoded():
    url = mql.build_quick_create_url(TPL, HUB, EXT, "", "us-east-1")
    _, _, query = split_link(url)
    for pair in query.split("&"):
        key, _, val = pair.partition("=")
        # no raw separators or characters that the console/hash router could misread
        for ch in "&=?#:/+@, ":
            assert ch not in val, (key, val)
    assert "param_ExternalId=Ab%2Bc%3Dd%2Ce.f%40g%3Ah%2Fi-j_k" in query
    assert "templateURL=https%3A%2F%2Fedu-bootstrap-tpl.s3." in query
    assert "param_AccountAlias=&" in query or query.endswith("param_AccountAlias=")


def test_review_route_matches_aws_docs_form():
    url = mql.build_quick_create_url(TPL, HUB, EXT, "", "us-east-1", route="review")
    assert "#/stacks/create/review?templateURL=" in url
    assert url.startswith("https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#")


@pytest.mark.parametrize("kwargs, msg", [
    ({"template_url": "http://bucket.s3.amazonaws.com/t.yaml"}, "https S3"),
    ({"template_url": "https://example.com/t.yaml"}, "https S3"),
    ({"hub_principal_arn": "arn:aws:iam::123:role/x"}, "IAM role/user ARN"),
    ({"external_id": "short"}, "external id"),
    ({"alias": "edu-prod-1"}, "prod"),
    ({"alias": "Bad_Alias"}, "valid IAM account alias"),
    ({"region": "seoul"}, "invalid region"),
])
def test_invalid_inputs_rejected(kwargs, msg):
    args = {"template_url": TPL, "hub_principal_arn": HUB, "external_id": EXT, "alias": "", "region": "us-east-1"}
    args.update(kwargs)
    with pytest.raises(ValueError, match=msg):
        mql.build_quick_create_url(**args)


def test_cli_command_round_trips():
    cmd = mql.build_cli_command(TPL, HUB, EXT, "edu-2026-10-01", "ap-northeast-2")
    argv = shlex.split(cmd)
    assert argv[:3] == ["aws", "cloudformation", "create-stack"]
    assert argv[argv.index("--stack-name") + 1] == "edu-bootstrap"
    assert argv[argv.index("--template-url") + 1] == TPL
    assert argv[argv.index("--capabilities") + 1] == "CAPABILITY_NAMED_IAM"
    assert argv[argv.index("--region") + 1] == "ap-northeast-2"
    params = json.loads(argv[argv.index("--parameters") + 1])
    assert {p["ParameterKey"]: p["ParameterValue"] for p in params} == {
        "HubPrincipalArn": HUB, "ExternalId": EXT, "AccountAlias": "edu-2026-10-01"}


def test_registry_mode_prints_one_link_per_account(tmp_path, capsys):
    reg = hub_test_support.make_registry(tmp_path / "r.json", [("111111111111", "edu-2026-10-01"),
                                                              ("222222222222", "edu-2026-10-02"),
                                                              ("333333333333", "")])
    out_csv = tmp_path / "links.csv"
    rc = mql.main(["--registry", str(reg), "--template-url", TPL, "--hub-principal-arn", HUB,
                   "--csv", str(out_csv), "--bucket-policy"])
    assert rc == 0
    out = capsys.readouterr().out
    links = [line for line in out.splitlines() if line.startswith("https://")]
    assert len(links) == 3
    aliases = [parse_qs(split_link(u)[2], keep_blank_values=True)["param_AccountAlias"][0] for u in links]
    assert aliases == ["edu-2026-10-01", "edu-2026-10-02", ""]
    for u in links:
        assert parse_qs(split_link(u)[2])["param_ExternalId"] == [hub_test_support.EXTERNAL_ID]
    assert out.count("aws cloudformation create-stack") == 3
    raw = out_csv.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "s3:GetObject" in out and "arn:aws:s3:::edu-bootstrap-tpl/edu/bootstrap.yaml" in out


def test_registry_mode_account_filter_and_errors(tmp_path, capsys):
    reg = hub_test_support.make_registry(tmp_path / "r.json", [("111111111111", "edu-2026-10-01"),
                                                              ("222222222222", "edu-2026-10-02")])
    assert mql.main(["--registry", str(reg), "--template-url", TPL, "--hub-principal-arn", HUB,
                     "--accounts", "222222222222"]) == 0
    out = capsys.readouterr().out
    assert len([x for x in out.splitlines() if x.startswith("https://")]) == 1
    assert mql.main(["--registry", str(reg), "--template-url", TPL, "--hub-principal-arn", HUB,
                     "--accounts", "999999999999"]) == 2
    assert mql.main(["--template-url", TPL, "--hub-principal-arn", HUB]) == 2  # no external id


def test_single_mode_prints_link_and_cli(capsys):
    rc = mql.main(["--template-url", TPL, "--hub-principal-arn", HUB, "--external-id", EXT,
                   "--alias", "edu-2026-10-07", "--region", "us-east-1"])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("https://us-east-1.console.aws.amazon.com/")
    assert any(x.startswith("aws cloudformation create-stack") for x in out)


def test_bucket_policy_path_style_url():
    pol = mql.build_bucket_policy("https://s3.ap-northeast-2.amazonaws.com/my-bucket/a/b.yaml", ["111111111111"])
    st = pol["Statement"][0]
    assert st["Resource"] == "arn:aws:s3:::my-bucket/a/b.yaml"
    assert st["Principal"]["AWS"] == ["arn:aws:iam::111111111111:root"]
