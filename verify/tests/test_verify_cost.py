"""Cost Explorer side of verify_cost.py, using botocore Stubber (moto's CE support is limited)."""
import datetime as dt
import json

import boto3
import pytest
from botocore.stub import Stubber

import verify_cost as vc

ACCOUNT = "123456789012"
CLEANUP = "2026-10-10"
NOW = "2026-10-14T03:00"  # UTC → window 10-11..10-13, earliest verdict 10-12 12:00 UTC


def _group(service, usage_type, amount):
    return {"Keys": [service, usage_type],
            "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


def _day(day, groups, estimated=True):
    nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    return {"TimePeriod": {"Start": day, "End": nxt}, "Total": {}, "Groups": groups, "Estimated": estimated}


def _expected(start="2026-10-11", end="2026-10-14", token=None, record_filter=None):
    params = {
        "TimePeriod": {"Start": start, "End": end},
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "Filter": record_filter or {"Not": {"Dimensions": {"Key": "RECORD_TYPE",
                                                           "Values": ["Credit", "Refund", "Tax"]}}},
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    }
    if token:
        params["NextPageToken"] = token
    return params


class FakeSession:
    """Stands in for get_session(); hands out pre-stubbed clients."""

    def __init__(self, ce):
        self.ce = ce

    def client(self, service, region_name=None, config=None):
        if service == "ce":
            assert region_name == "us-east-1"
            return self.ce
        raise AssertionError("unexpected client %s" % service)


@pytest.fixture
def ce_stub(monkeypatch):
    ce = boto3.client("ce", region_name="us-east-1")
    stub = Stubber(ce)
    stub.activate()
    monkeypatch.setattr(vc, "get_session", lambda args: FakeSession(ce))
    monkeypatch.setattr(vc, "account_id", lambda session: ACCOUNT)
    yield stub
    stub.deactivate()


def run(tmp_path, *extra, now=NOW, cleanup=CLEANUP):
    out = tmp_path / "result.json"
    argv = ["--cleanup-date", cleanup, "--now", now, "--json-out", str(out)] + list(extra)
    code = vc.main(argv)
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_verify_clean(ce_stub, tmp_path, capsys):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [_group("Amazon Simple Storage Service", "APN2-TimedStorage-ByteHrs", "0.0004")]),
        _day("2026-10-12", []),
        _day("2026-10-13", [_group("AWS Cost Explorer", "USE1-APIRequest", "0.03")]),  # builtin allow
    ]}, _expected())
    code, res = run(tmp_path)
    ce_stub.assert_no_pending_responses()
    assert code == vc.EXIT_OK == 0
    for key in ("module", "account_id", "generated_at", "applied", "status"):
        assert key in res
    assert res["module"] == "verify" and res["account_id"] == ACCOUNT and res["applied"] is False
    assert res["status"] == "CLEAN"
    assert res["window"]["start"] == "2026-10-11" and res["window"]["end"] == "2026-10-13"
    assert res["flagged"] == []
    assert [d["date"] for d in res["daily_totals"]] == ["2026-10-11", "2026-10-12", "2026-10-13"]
    assert res["daily_totals"][2]["amount_usd"] == pytest.approx(0.03)
    assert res["ce_requests"] == 1
    assert len(res["allowed"]) == 1 and res["allowed"][0]["service"] == "AWS Cost Explorer"
    assert "CLEAN" in capsys.readouterr().out


def test_verify_residual_sorted_with_totals(ce_stub, tmp_path, capsys):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [
            _group("EC2 - Other", "APN2-EBS:VolumeUsage.gp3", "0.25"),
            _group("Amazon Bedrock", "USE1-CustomModelStorage", "1.90"),
            _group("Amazon Simple Storage Service", "APN2-Requests-Tier1", "0.005"),  # below threshold
        ]),
        _day("2026-10-12", [_group("Amazon Elastic Compute Cloud - Compute", "USW2-BoxUsage:t3.micro", "0.25")]),
    ]}, _expected())
    code, res = run(tmp_path)
    assert code == vc.EXIT_ISSUES == 1
    assert res["status"] == "RESIDUAL"
    got = [(f["amount_usd"], f["service"], f["usage_type"]) for f in res["flagged"]]
    assert got == [(1.9, "Amazon Bedrock", "USE1-CustomModelStorage"),
                   (0.25, "EC2 - Other", "APN2-EBS:VolumeUsage.gp3"),
                   (0.25, "Amazon Elastic Compute Cloud - Compute", "USW2-BoxUsage:t3.micro")]
    assert res["flagged"][2]["region_hint"] == "us-west-2"  # region outside the nuke config
    totals = {d["date"]: d for d in res["daily_totals"]}
    assert totals["2026-10-11"]["amount_usd"] == pytest.approx(2.155)
    assert totals["2026-10-11"]["flagged_usd"] == pytest.approx(2.15)
    out = capsys.readouterr().out
    assert "RESIDUAL" in out and "Amazon Bedrock" in out


def test_verify_pending_by_date_still_reports_residual(ce_stub, tmp_path):
    # D+1 morning: settle not reached → PENDING when nothing found ...
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [_day("2026-10-11", [])]},
                         _expected(end="2026-10-12"))
    code, res = run(tmp_path, now="2026-10-12T05:00")
    assert code == vc.EXIT_PENDING == 3
    assert res["status"] == "PENDING"
    assert res["pending_reasons"] == ["lag"]
    assert res["window"]["earliest_verdict_utc"] == "2026-10-12T12:00:00+00:00"


def test_verify_pending_by_settle_days(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [_day("2026-10-11", [])]},
                         _expected(end="2026-10-12"))
    code, res = run(tmp_path, "--settle-days", "3", now="2026-10-12T13:00")
    assert code == 3 and res["pending_reasons"] == ["settle"]
    assert res["window"]["earliest_verdict_kst"] == "2026-10-13 09:00 KST"


def test_verify_pending_by_date_but_cost_already_visible_is_residual(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [_group("Amazon SageMaker", "APN2-Host:ml.m5.large", "2.3")])]},
        _expected(end="2026-10-12"))
    code, res = run(tmp_path, now="2026-10-12T05:00")
    assert code == 1 and res["status"] == "RESIDUAL"


def test_verify_pending_no_ce_call_when_window_empty(ce_stub, tmp_path):
    # same UTC day as cleanup: nothing to query, no CE request (no $0.01 spent) in cost mode
    code, res = run(tmp_path, "--mode", "cost", now="2026-10-10T20:00")
    ce_stub.assert_no_pending_responses()
    assert code == 3 and res["status"] == "PENDING"
    assert res["ce_requests"] == 0 and res["window"]["end"] is None
    assert "empty-window" in res["pending_reasons"]


def test_verify_pending_when_first_day_not_returned(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": []}, _expected())
    code, res = run(tmp_path, now="2026-10-14T03:00")
    assert code == 3 and "no-data" in res["pending_reasons"]


def test_verify_pending_by_estimated_true_with_require_final(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [], estimated=False),
        _day("2026-10-12", [], estimated=False),
        _day("2026-10-13", [], estimated=True),
    ]}, _expected())
    code, res = run(tmp_path, "--require-final")
    assert code == 3 and res["status"] == "PENDING"
    assert res["pending_reasons"] == ["estimated"]
    assert res["data_final"] is False


def test_verify_estimated_true_without_require_final_is_clean_but_reported(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [], estimated=True), _day("2026-10-12", [], estimated=True),
        _day("2026-10-13", [], estimated=True)]}, _expected())
    code, res = run(tmp_path)
    assert code == 0 and res["data_final"] is False
    assert all(d["estimated"] for d in res["daily_totals"])


def test_verify_require_final_all_final_is_clean(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [], estimated=False), _day("2026-10-12", [], estimated=False),
        _day("2026-10-13", [], estimated=False)]}, _expected())
    code, res = run(tmp_path, "--require-final")
    assert code == 0 and res["data_final"] is True


def test_verify_allowlist_glob_match_and_cap(ce_stub, tmp_path):
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([
        {"_comment": "ignored"},
        {"service": "Amazon Route 53", "usage_type_pattern": "HostedZone", "max_usd_per_day": 0.5,
         "reason": "monthly hosted zone fee"},
        {"service": "Amazon Simple Storage Service", "usage_type_pattern": "*earlydelete*",
         "max_usd_per_day": 1.0, "reason": "early delete"},
    ]), encoding="utf-8")
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [
            _group("Amazon Route 53", "HostedZone", "0.50"),
            _group("Amazon Simple Storage Service", "APN2-EarlyDelete-SIA", "0.40"),  # case-insensitive glob
            _group("Amazon Simple Storage Service", "EarlyDelete-ZIA", "3.00"),       # over the cap
        ]),
    ]}, _expected())
    code, res = run(tmp_path, "--allowlist", str(allow))
    assert code == 1
    assert {a["usage_type"] for a in res["allowed"]} == {"HostedZone", "APN2-EarlyDelete-SIA"}
    assert [f["usage_type"] for f in res["flagged"]] == ["EarlyDelete-ZIA"]
    assert res["flagged"][0]["allowlist_exceeded"]["max_usd_per_day"] == 1.0


def test_verify_allowlist_only_matching_service(ce_stub, tmp_path):
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([{"service": "Amazon Route 53", "usage_type_pattern": "*",
                                  "max_usd_per_day": 5, "reason": "r"}]), encoding="utf-8")
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [_group("Amazon Simple Storage Service", "HostedZone", "0.50")])]}, _expected())
    code, res = run(tmp_path, "--allowlist", str(allow))
    assert code == 1 and len(res["flagged"]) == 1


def test_verify_invalid_allowlist_is_error(ce_stub, tmp_path):
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps([{"service": "x", "usage_type_pattern": "*", "max_usd_per_day": 1}]),
                     encoding="utf-8")  # no reason
    code, res = run(tmp_path, "--allowlist", str(allow))
    assert code == 2 and res["errors"][0]["code"] == "InvalidAllowlist"


def test_verify_example_allowlist_loads():
    rules = vc.load_allowlist(str(vc.Path(vc.__file__).with_name("allowlist.example.json")))
    assert any(r.get("builtin") for r in rules)
    assert all(r["reason"] for r in rules)


def test_verify_pagination_merges_pages(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"NextPageToken": "page-2", "ResultsByTime": [
        _day("2026-10-11", [_group("EC2 - Other", "APN2-EBS:SnapshotUsage", "0.10")], estimated=False),
    ]}, _expected())
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [
        _day("2026-10-11", [_group("Amazon Virtual Private Cloud", "APN2-PublicIPv4:IdleAddress", "0.12")],
             estimated=True),
        _day("2026-10-12", [_group("EC2 - Other", "APN2-EBS:SnapshotUsage", "0.10")]),
    ]}, _expected(token="page-2"))
    code, res = run(tmp_path)
    ce_stub.assert_no_pending_responses()
    assert code == 1 and res["ce_requests"] == 2
    assert len(res["flagged"]) == 3
    d11 = [d for d in res["daily_totals"] if d["date"] == "2026-10-11"][0]
    assert d11["amount_usd"] == pytest.approx(0.22)
    assert d11["estimated"] is True  # any page saying estimated wins


def test_verify_record_types_include_mode(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [_day("2026-10-11", [])]},
                         _expected(record_filter={"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}))
    code, res = run(tmp_path, "--record-types", "Usage")
    assert res["record_filter"] == {"mode": "include", "values": ["Usage"]}
    assert code == 0


def test_verify_days_limits_window(ce_stub, tmp_path):
    ce_stub.add_response("get_cost_and_usage", {"ResultsByTime": [_day("2026-10-13", [])]},
                         _expected(start="2026-10-13"))
    code, res = run(tmp_path, "--days", "1", now="2026-10-14T13:00")
    assert res["window"]["start"] == "2026-10-13" and code == 0


@pytest.mark.parametrize("code,message,needle", [
    ("AccessDeniedException", "User not enabled for cost explorer access", "콘솔에서 Cost Explorer를 한 번 열어 활성화하세요"),
    ("DataUnavailableException", "Data is not available. Please try to adjust the time period.",
     "콘솔에서 Cost Explorer를 한 번 열어 활성화하세요"),
    ("AccessDeniedException", "User: arn:aws:iam::123:user/x is not authorized to perform: ce:GetCostAndUsage",
     "ce:GetCostAndUsage"),
])
def test_verify_ce_errors_exit_2_with_korean_hint(ce_stub, tmp_path, capsys, code, message, needle):
    # --mode cost keeps the strict behaviour (auto would fall back to the inventory, see test_verify_modes.py)
    ce_stub.add_client_error("get_cost_and_usage", service_error_code=code, service_message=message,
                             http_status_code=400, expected_params=_expected())
    rc, res = run(tmp_path, "--mode", "cost")
    assert rc == vc.EXIT_ERROR == 2
    assert res["status"] == "ERROR"
    err = [e for e in res["errors"] if e["stage"] == "cost"][0]
    assert err["code"] == code and needle in err["hint"]
    assert needle in capsys.readouterr().out


def test_verify_auth_failure_exit_2(monkeypatch, tmp_path):
    def boom(args):
        raise vc.ClientError({"Error": {"Code": "InvalidClientTokenId", "Message": "bad"}}, "GetCallerIdentity")
    monkeypatch.setattr(vc, "get_session", boom)
    code, res = run(tmp_path)
    assert code == 2 and res["errors"][0]["stage"] == "auth"


def test_verify_unexpected_exception_is_exit_2_not_1(monkeypatch, tmp_path):
    monkeypatch.setattr(vc, "get_session", lambda args: object())
    monkeypatch.setattr(vc, "account_id", lambda s: ACCOUNT)
    monkeypatch.setattr(vc, "fetch_cost_and_usage", lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    code, res = run(tmp_path)
    assert code == 2 and res["status"] == "ERROR"


# ---- pure helpers -------------------------------------------------------------------------
def _now(s):
    return vc._utc_datetime(s)


def test_verify_window_dates_utc_and_kst():
    w = vc.compute_window(dt.date(2026, 10, 10), 2, 12.0, None, _now("2026-10-12T11:59"))
    assert "settle" not in w["_pending"] and "lag" in w["_pending"]
    w = vc.compute_window(dt.date(2026, 10, 10), 2, 12.0, None, _now("2026-10-12T12:00"))
    assert w["_pending"] == [] and w["start"] == "2026-10-11" and w["end"] == "2026-10-11"
    assert w["end_exclusive"] == "2026-10-12"
    assert w["earliest_verdict_kst"] == "2026-10-12 21:00 KST"
    w = vc.compute_window(dt.date(2026, 10, 10), 5, 12.0, None, _now("2026-10-13T00:00"))
    assert w["_pending"] == ["settle"]


def test_verify_month_boundary_window():
    w = vc.compute_window(dt.date(2026, 10, 30), 2, 12.0, None, _now("2026-11-03T00:00"))
    assert (w["start"], w["end"], w["end_exclusive"], w["days"]) == ("2026-10-31", "2026-11-02", "2026-11-03", 3)


def test_verify_region_hint():
    assert vc.region_hint("APN2-BoxUsage:t3.micro") == "ap-northeast-2"
    assert vc.region_hint("BoxUsage:t3.micro") == "us-east-1/global"
    assert vc.region_hint("HostedZone") == "us-east-1/global"
    assert vc.region_hint("MEC1-BoxUsage:t3.micro") == "unknown(MEC1)"
