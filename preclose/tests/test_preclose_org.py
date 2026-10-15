"""AWS Organizations membership: standalone / member (reseller org) / unknown, conditional closure
reminders, and SCP-blocked billing checks (Stubber, plus moto for standalone/management)."""
import boto3
import pytest
from moto import mock_aws

from test_preclose_harness import (ACCOUNT, MGMT_ACCOUNT, ORG_ID, REGION, US, Harness, by_check, denied, pcm,
                                   queue_clean, queue_org, run_pc)


def _scp_denied(st, method):
    st.add_client_error(method, service_error_code="AccessDeniedException", http_status_code=403,
                        service_message=(f"User: arn:aws:sts::{ACCOUNT}:assumed-role/EduCleanupRole/x is not "
                                         "authorized to perform: x with an explicit deny in a service control policy"))


def _reminder_ids(result):
    return [f["resource_id"] for f in by_check(result, "closure_reminder")]


@pytest.mark.parametrize("status, rid, reminders", [
    ("standalone", "standalone", pcm.REMINDERS),
    ("member", ORG_ID, pcm.MEMBER_REMINDERS),
    ("unknown", "unknown", pcm.UNKNOWN_ORG_REMINDERS),
])
def test_preclose_org_status_and_conditional_reminders(status, rid, reminders):
    h = Harness()
    queue_clean(h, org=status)
    result, code = run_pc(h)
    h.assert_all_consumed()

    org = by_check(result, "organization")
    assert len(org) == 1 and org[0]["severity"] == "INFO" and org[0]["resource_id"] == rid
    assert result["organization"]["status"] == status
    assert _reminder_ids(result) == [r[0] for r in reminders]
    assert "root-email" in _reminder_ids(result)  # root email reuse rule applies in every case
    assert code == pcm.EXIT_OK and result["ready_to_close"] is True  # org detection never blocks closure
    assert "DescribeOrganization" in h.ops() and h.mutating() == []


def test_preclose_member_org_detail_shows_ids_and_email_domain_only():
    h = Harness()
    queue_clean(h, org="member")
    result, _ = run_pc(h)
    detail = by_check(result, "organization")[0]["detail"]
    assert detail.startswith(f"member of org {ORG_ID}, management account {MGMT_ACCOUNT} (reseller.example)")
    assert "aws-billing@" not in detail and "@" not in str(result["organization"])
    assert result["organization"] == {"status": "member", "org_id": ORG_ID, "management_account_id": MGMT_ACCOUNT,
                                      "management_email_domain": "reseller.example", "feature_set": "ALL",
                                      "error": None}


def test_preclose_member_reminders_cover_reseller_closure_and_billing():
    text = " ".join(t for _, t in pcm.MEMBER_REMINDERS)
    for needle in ("관리 계정(리셀러)", "CloseAccount", "SCP", "90일", "청구서", "리셀러를 통해", "루트 이메일"):
        assert needle in text, needle
    standalone = " ".join(t for _, t in pcm.REMINDERS)
    assert "독립 계정 해지는 API가 없음" in standalone and "리셀러" not in standalone


def test_preclose_unknown_org_is_info_not_a_failed_check():
    h = Harness()
    queue_clean(h, org="unknown")
    result, _ = run_pc(h)
    org = by_check(result, "organization")[0]
    assert org["detail"].startswith("unknown") and "AccessDenied" in org["detail"]
    assert result["summary"]["checks_failed"] == [] and result["summary"]["checks_blocked_by_org"] == []


def test_preclose_member_scp_blocked_billing_checks_are_warn_not_blocker():
    """Reseller SCPs deny billing APIs even for root: WARN 'blocked by organization policy', no BLOCKER,
    not a 'could not check' failure, so --delete-alias still proceeds."""
    h = Harness()
    queue_clean(h, org="member", skip={"marketplace-agreement", "savingsplans"}, delete_alias_call=True)
    denied(h.stub("marketplace-agreement", US), "search_agreements")  # generic message: inferred from membership
    _scp_denied(h.stub("savingsplans", US), "describe_savings_plans")
    result, code = run_pc(h, "--apply", "--delete-alias")
    h.assert_all_consumed()

    for check in ("marketplace_agreement", "savings_plan"):
        f = by_check(result, check)
        assert len(f) == 1 and f[0]["severity"] == "WARN", check
        assert "blocked by organization policy (SCP) - check with the reseller" in f[0]["detail"]
    s = result["summary"]
    assert s["blockers_remaining"] == 0 and s["checks_failed"] == []
    assert sorted(s["checks_blocked_by_org"]) == ["marketplace_agreement@global", "savings_plan@global"]
    assert code == pcm.EXIT_OK
    assert by_check(result, "account_alias")[0]["action"] == "done"
    assert "리셀러에게 확인" in pcm.render_summary(result)
    assert "리셀러" in pcm.render_summary(result).splitlines()[-2]  # member verdict mentions the reseller


def test_preclose_standalone_generic_access_denied_stays_could_not_check():
    h = Harness()
    queue_clean(h, org="standalone", skip={"marketplace-agreement"})
    denied(h.stub("marketplace-agreement", US), "search_agreements")
    result, _ = run_pc(h)
    f = by_check(result, "marketplace_agreement")[0]
    assert "권한 없음" in f["detail"] and "SCP" not in f["detail"]
    assert result["summary"]["checks_failed"] == ["marketplace_agreement@global"]
    assert result["summary"]["checks_blocked_by_org"] == []


def test_preclose_scp_message_marks_any_check_blocked_by_org_without_describe():
    """An explicit SCP deny message is enough, even when membership is not loaded (no extra API call)."""
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    _scp_denied(h.stub("shield", US), "get_subscription_state")
    result, code = run_pc(h, checks=["shield"])
    h.assert_all_consumed()
    assert "DescribeOrganization" not in h.ops()
    assert result["summary"]["checks_blocked_by_org"] == ["shield_subscription@global"] and code == pcm.EXIT_OK


def test_preclose_reminders_only_lazily_loads_org():
    h = Harness()
    h.stub("iam", US).add_response("list_account_aliases", {"AccountAliases": [], "IsTruncated": False})
    queue_org(h, "member")
    result, _ = run_pc(h, checks=["reminders"])
    h.assert_all_consumed()
    assert _reminder_ids(result) == [r[0] for r in pcm.MEMBER_REMINDERS]


# ------------------------------------------------------------------ moto
@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": US}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield boto3.Session(region_name=US)


def test_preclose_moto_standalone_and_management(aws):
    h = Harness(aws, stub_services=set())
    result, _ = run_pc(h, checks=["organization", "reminders"])
    assert result["organization"]["status"] == "standalone"
    assert _reminder_ids(result) == [r[0] for r in pcm.REMINDERS]

    org = aws.client("organizations", region_name=US).create_organization(FeatureSet="ALL")["Organization"]
    h = Harness(aws, stub_services=set())
    result, _ = run_pc(h, checks=["organization"])
    f = by_check(result, "organization")[0]
    assert result["organization"]["status"] == "management" and f["resource_id"] == org["Id"]
    assert f["detail"].startswith(f"management account of org {org['Id']}")
