#!/usr/bin/env python3
"""preclose_check.py - pre-closure check (edu-cleanup module 5: preclose).

Run AFTER aws-nuke and BEFORE the account is closed. It finds things that keep billing after
closure (or that aws-nuke does not handle) and, with --apply, fixes whatever can be fixed through
the API. It also detects AWS Organizations membership (training accounts are usually member
accounts of a reseller's organization) and words the closure reminders accordingly.

Dry-run by default. Exit codes: 0 = no BLOCKER left, 1 = BLOCKER(s) remain, 2 = error.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING  # noqa: E402,F401
from common.session import now_iso  # noqa: E402

import argparse  # noqa: E402
import datetime as _dt  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Tuple  # noqa: E402

from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError, UnknownServiceError  # noqa: E402

MODULE = "preclose"
BLOCKER, WARN, INFO = "BLOCKER", "WARN", "INFO"
SEVERITIES = (BLOCKER, WARN, INFO)
ACTIONS = ("none", "planned", "done", "failed")
GLOBAL = "global"
# Marketplace Agreement, Shield, Route 53 Domains, IAM, Organizations and the Account API are served from us-east-1.
HOME_REGION = "us-east-1"

COMMIT_ADVICE = "약정 기간이 끝날 때까지 해지 후에도 청구됨(API로 취소 불가) - AWS Support에 문의"

# Buyer-side search, as in the AWS Marketplace buyer Agreement API guide.
MP_CATALOG = "AWSMarketplace"
MP_FILTERS = [
    {"name": "PartyType", "values": ["Acceptor"]},
    {"name": "AgreementType", "values": ["PurchaseAgreement"]},
    {"name": "Status", "values": ["ACTIVE"]},
]
# Contract/upfront terms. AWS: cancel the ConfigurableUpfrontPricingTerm agreement first, then the
# usage one. Only usage agreements are buyer-cancellable; others need the seller.
MP_UPFRONT_TERMS = {"configurableUpfrontPricingTerm", "fixedUpfrontPricingTerm", "paymentScheduleTerm"}

RBIN_TYPES = ("EBS_SNAPSHOT", "EC2_IMAGE", "EBS_VOLUME")
PURGE_RULE_TYPES = ("EBS_SNAPSHOT", "EC2_IMAGE")
RBIN_LABEL = {"EBS_SNAPSHOT": "EBS 스냅샷", "EC2_IMAGE": "AMI", "EBS_VOLUME": "EBS 볼륨"}

ACTIVE_RESERVATION_STATES = {"active", "payment-pending", "pending-payment"}
# (check, service, method, result key, id key, count key, type key, label)
OTHER_RESERVATIONS = (
    ("rds_reserved_instance", "rds", "describe_reserved_db_instances", "ReservedDBInstances",
     "ReservedDBInstanceId", "DBInstanceCount", "DBInstanceClass", "RDS 예약 인스턴스"),
    ("elasticache_reserved_node", "elasticache", "describe_reserved_cache_nodes", "ReservedCacheNodes",
     "ReservedCacheNodeId", "CacheNodeCount", "CacheNodeType", "ElastiCache 예약 노드"),
    ("redshift_reserved_node", "redshift", "describe_reserved_nodes", "ReservedNodes",
     "ReservedNodeId", "NodeCount", "NodeType", "Redshift 예약 노드"),
)

ROOT_EMAIL_REMINDER = ("root-email", "루트 이메일은 해지 후 다른 계정에 재사용할 수 없음 - 재사용하려면 해지 전에 루트 이메일을 바꿀 것")
# Standalone account (not in any AWS Organizations organization).
REMINDERS = (
    ROOT_EMAIL_REMINDER,
    ("closure-console-only", "독립 계정 해지는 API가 없음 - 루트 사용자로 콘솔 로그인 후 Account > Close account"),
    ("final-invoice", "해지 전 사용분은 다음 달 초 최종 청구서로 청구됨(정상) - 90일 안에 재개하면 남은 리소스 과금도 재개"),
    ("support-plan", "유료 Support 플랜을 쓰고 있으면 해지 전에 Basic으로 변경(루트, 콘솔)"),
)
# Member account of someone else's organization (e.g. a reseller's). AWS docs: a member account can be
# closed by its root user (Account page) or by the management account (CloseAccount), unless an SCP denies
# account:CloseAccount; it stays in the organization (SUSPENDED) for the 90-day post-closure period; the
# management account pays all member-account charges (consolidated billing).
MEMBER_REMINDERS = (
    ROOT_EMAIL_REMINDER,
    ("closure-member", "조직 멤버 계정 해지: 루트로 콘솔 로그인 후 Account > Close account, 또는 관리 계정(리셀러)이 "
                       "Organizations CloseAccount로 해지. 조직 SCP가 account:CloseAccount를 막으면 루트로도 해지 불가 - 리셀러에 해지 요청"),
    ("post-closure-org", "해지 후 90일(post-closure period) 동안 SUSPENDED 상태로 조직에 남음 - 이 기간에만 재개 가능, "
                         "재개하면 남은 리소스 과금도 재개. 90일 뒤 영구 해지되고 조직에서 빠짐"),
    ("final-invoice-member", "해지 전 사용분은 다음 달 청구에 포함되며 멤버 계정 요금은 관리 계정(리셀러)의 청구서로 감 - "
                             "회사 청구는 리셀러 정산으로 확인"),
    ("billing-via-reseller", "이 계정에서는 SCP로 비용/청구 화면이 막혀 있을 수 있음 - 요금·환불·면제 문의(예: Bedrock 과다 사용)는 "
                             "리셀러를 통해 진행"),
    ("support-plan-member", "이 계정에서 유료 Support 플랜을 직접 가입한 경우에만: 해지 전에 Basic으로 변경(리셀러 지원 계약이면 해당 없음)"),
)
# Membership could not be determined (organizations:DescribeOrganization denied or failed).
UNKNOWN_ORG_REMINDERS = (
    ROOT_EMAIL_REMINDER,
    ("closure-unknown-org", "조직 소속 여부 확인 불가 - 루트로 콘솔 로그인 후 Account > Close account. 조직 멤버 계정이면 "
                            "관리 계정도 해지할 수 있고, SCP로 막혀 있으면 관리 계정 측에 해지 요청"),
    ("final-invoice", "해지 전 사용분은 다음 달 초 청구됨(조직 멤버면 관리 계정 청구서로 감) - 90일 안에 재개하면 남은 리소스 과금도 재개"),
    ("support-plan", "유료 Support 플랜을 직접 쓰고 있으면 해지 전에 Basic으로 변경(루트, 콘솔)"),
)

ORG_STANDALONE, ORG_MEMBER, ORG_MANAGEMENT, ORG_UNKNOWN = "standalone", "member", "management", "unknown"
# Billing-side checks that an organization SCP typically blocks in member accounts.
ORG_SENSITIVE_CHECKS = {"marketplace_agreement", "savings_plan"}
ORG_BLOCKED_DETAIL = ("blocked by organization policy (SCP) - check with the reseller | 조직 정책(SCP)으로 차단되어 "
                      "확인 불가 - 리셀러(관리 계정)에게 해당 항목 확인 요청")

ALL_CHECKS = (
    "organization", "marketplace", "cloudtrail_trails", "cloudtrail_lake", "recycle_bin", "ec2_reserved",
    "other_reserved", "savings_plans", "bedrock_provisioned", "shield", "route53_domains",
    "root_access_keys", "account_alias", "reminders",
)

# Mutating API name prefixes (used by tests and the log to tell reads from writes).
MUTATING_PREFIXES = ("Cancel", "Delete", "Deregister", "Disable", "Restore", "Unlock", "Update",
                     "Put", "Create", "Return")

_ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException",
                  "AuthorizationError", "AuthorizationErrorException", "Forbidden", "ForbiddenException"}
_REGION_DISABLED = {"OptInRequired", "InvalidClientTokenId", "UnrecognizedClientException", "AuthFailure"}
_NOT_AVAILABLE = {"UnsupportedOperation", "UnsupportedOperationException", "InvalidAction",
                  "UnknownOperationException", "NotSupported", "SubscriptionRequiredException"}


# ---------------------------------------------------------------- helpers
def _err_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code", "ClientError")
    return type(exc).__name__


def _err_text(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'ClientError')}: {err.get('Message', '')}".strip()
    return f"{type(exc).__name__}: {exc}"


def _reason(exc: BaseException) -> str:
    code = _err_code(exc)
    if code in _ACCESS_DENIED:
        return "권한 없음"
    if code in _REGION_DISABLED:
        return "리전 비활성/옵트인 필요"
    if code in _NOT_AVAILABLE or isinstance(exc, EndpointConnectionError):
        return "서비스 미지원"
    if isinstance(exc, (AttributeError, UnknownServiceError)):
        return "설치된 boto3에 이 API가 없음 - README '오래된 boto3' 참고"
    return "오류"


def _paginate(cl: Any, method: str, key: str, **kwargs: Any) -> List[Any]:
    if cl.can_paginate(method):
        out: List[Any] = []
        for page in cl.get_paginator(method).paginate(**kwargs):
            out.extend(page.get(key) or [])
        return out
    return list(getattr(cl, method)(**kwargs).get(key) or [])


def _token_loop(call: Callable[..., dict], key: str, token_in: str, token_out: str, **kwargs: Any) -> List[Any]:
    """Manual pagination for operations that have no botocore paginator."""
    out: List[Any] = []
    token = None
    while True:
        params = dict(kwargs)
        if token:
            params[token_in] = token
        resp = call(**params)
        out.extend(resp.get(key) or [])
        token = resp.get(token_out)
        if not token:
            return out


def _ts(value: Any) -> str:
    if value is None or value == "":
        return "?"
    if isinstance(value, _dt.datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def _parse_iso(value: Any) -> Optional[_dt.datetime]:
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _end_time(start: Any, duration_seconds: Any) -> str:
    begin = _parse_iso(start)
    if begin is None or not isinstance(duration_seconds, int):
        return "?"
    return _ts(begin + _dt.timedelta(seconds=duration_seconds))


def _short(ids: List[str], n: int = 10) -> str:
    head = ", ".join(ids[:n])
    return head + (f" 외 {len(ids) - n}개" if len(ids) > n else "")


def _chunks(items: List[str], size: int) -> List[List[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _finding(check: str, region: str, resource_id: str, detail: str, severity: str,
             fixable: bool = False, action: str = "none", error: Optional[str] = None) -> Dict[str, Any]:
    return {"check": check, "region": region, "resource_id": resource_id, "detail": detail,
            "severity": severity, "fixable": bool(fixable), "action": action, "error": error}


def blockers_remaining(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A BLOCKER is resolved only when its fix was actually applied (action == done)."""
    return [f for f in findings if f["severity"] == BLOCKER and f["action"] != "done"]


def exit_code_for(findings: List[Dict[str, Any]]) -> int:
    return EXIT_ISSUES if blockers_remaining(findings) else EXIT_OK


# ---------------------------------------------------------------- checker
class PreClose:
    def __init__(self, session: Any, account: str, regions: List[str], *, apply: bool = False,
                 delete_alias: bool = False, purge_recycle_bin: bool = False,
                 client_factory: Optional[Callable[[str, str], Any]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 purge_wait_seconds: int = 600, poll_seconds: int = 5) -> None:
        self.session = session
        self.account = account
        self.regions = list(regions)
        self.apply = apply
        self.delete_alias = delete_alias
        self.purge_recycle_bin = purge_recycle_bin
        self._factory = client_factory or (lambda svc, region: client(session, svc, region))
        self._sleep = sleep
        self.purge_wait_seconds = purge_wait_seconds
        self.poll_seconds = max(1, poll_seconds)
        self._clients: Dict[Tuple[str, str], Any] = {}
        self.findings: List[Dict[str, Any]] = []
        self.checks_failed: List[str] = []
        self.checks_blocked_by_org: List[str] = []
        self.org: Optional[Dict[str, Any]] = None
        self.aliases: Optional[List[str]] = None
        self._aliases_tried = False

    # -- plumbing ---------------------------------------------------------
    def c(self, service: str, region: str) -> Any:
        key = (service, region)
        if key not in self._clients:
            self._clients[key] = self._factory(service, region)
        return self._clients[key]

    def add(self, check: str, region: str, resource_id: str, detail: str, severity: str,
            fixable: bool = False, action: str = "none", error: Optional[str] = None) -> Dict[str, Any]:
        f = _finding(check, region, resource_id, detail, severity, fixable, action, error)
        self.findings.append(f)
        return f

    def guard(self, check: str, region: str, fn: Callable[..., Any], *args: Any) -> Any:
        """Run one check; any failure becomes a WARN 'could not check' instead of crashing the run."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - one failing check must never stop the others
            if not isinstance(exc, (ClientError, BotoCoreError)):
                log(traceback.format_exc())
            if self._blocked_by_org(check, exc):
                # Not fixable from inside the account: a WARN for the reseller, not a "could not check" failure
                # (otherwise --delete-alias and ready_to_close would be held forever in member accounts).
                self.checks_blocked_by_org.append(f"{check}@{region}")
                self.add(check, region, "-", ORG_BLOCKED_DETAIL, WARN, error=_err_text(exc))
                log(f"  [{region}] {check}: 조직 정책(SCP)으로 차단 - {_err_text(exc)}")
                return None
            reason = _reason(exc)
            self.checks_failed.append(f"{check}@{region}")
            self.add(check, region, "-", f"확인 불가({reason}) - 콘솔에서 직접 확인 필요", WARN, error=_err_text(exc))
            log(f"  [{region}] {check}: 확인 불가 - {_err_text(exc)}")
            return None

    def fix(self, f: Dict[str, Any], fn: Callable[[], Any], *, enabled: bool = True,
            hint: Optional[str] = None, failure_advice: Optional[str] = None) -> bool:
        """Dry-run: mark 'planned'. --apply: run fn and record done/failed. Returns True only if done."""
        if not f["fixable"]:
            return False
        if not enabled:
            if hint:
                f["detail"] += f" | {hint}"
            return False
        if not self.apply:
            f["action"] = "planned"
            return False
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - record and continue
            if not isinstance(exc, (ClientError, BotoCoreError)):
                log(traceback.format_exc())
            f["action"] = "failed"
            f["error"] = _err_text(exc)
            if isinstance(exc, (AttributeError, UnknownServiceError)):
                f["detail"] += (" | 조치 안내: 설치된 boto3에 이 API가 없음 - 최신 boto3(Python 3.10+)로 다시 실행하거나 "
                                "콘솔에서 직접 처리(README '오래된 boto3' 참고)")
            elif failure_advice:
                f["detail"] += f" | 조치 안내: {failure_advice}"
            log(f"  실패: {f['check']} {f['resource_id']} ({f['region']}): {f['error']}")
            return False
        f["action"] = "done"
        log(f"  완료: {f['check']} {f['resource_id']} ({f['region']})")
        return True

    def _blocked_by_org(self, check: str, exc: BaseException) -> bool:
        """AccessDenied that comes from an organization SCP: the message names it, or it is a billing-side
        check in a member account (where the reseller's SCPs deny billing/cost APIs)."""
        if _err_code(exc) not in _ACCESS_DENIED:
            return False
        message = exc.response.get("Error", {}).get("Message", "") if isinstance(exc, ClientError) else ""
        if "service control policy" in str(message).lower():
            return True
        return check in ORG_SENSITIVE_CHECKS and self.org_status() == ORG_MEMBER

    def regions_for(self, service: str) -> List[str]:
        """Skip regions where botocore knows the service is not offered (avoids slow endpoint retries)."""
        try:
            available = set(self.session.get_available_regions(service)) if self.session is not None else set()
        except Exception:  # noqa: BLE001
            available = set()
        if not available:
            return list(self.regions)
        skipped = [r for r in self.regions if r not in available]
        if skipped:
            log(f"  {service}: 서비스가 없는 리전 건너뜀 {', '.join(skipped)}")
        return [r for r in self.regions if r in available]

    def discover_regions(self) -> List[str]:
        """Enabled regions: account:ListRegions, falling back to ec2:DescribeRegions."""
        try:
            items = _paginate(self.c("account", HOME_REGION), "list_regions", "Regions",
                              RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"])
            regions = sorted({r["RegionName"] for r in items
                              if r.get("RegionOptStatus") in ("ENABLED", "ENABLED_BY_DEFAULT")})
            if regions:
                return regions
            log("  account:ListRegions 결과가 비어 ec2:DescribeRegions로 대체")
        except Exception as exc:  # noqa: BLE001 - any failure falls back to EC2
            log(f"  account:ListRegions 실패({_err_text(exc)}) - ec2:DescribeRegions로 대체")
        resp = self.c("ec2", HOME_REGION).describe_regions()
        return sorted(r["RegionName"] for r in resp.get("Regions", [])
                      if r.get("OptInStatus", "opt-in-not-required") in ("opt-in-not-required", "opted-in"))

    def run(self, checks: Optional[List[str]] = None) -> None:
        selected = list(ALL_CHECKS) if checks is None else list(checks)
        for i, name in enumerate(selected, 1):
            log(f"[{i}/{len(selected)}] {name}")
            getattr(self, f"check_{name}")()

    # -- AWS Organizations membership ------------------------------------------
    def load_org(self) -> Dict[str, Any]:
        """organizations:DescribeOrganization works from member accounts too. NotInUse = standalone;
        AccessDenied (e.g. an SCP) or any other failure = unknown. Never raises."""
        info: Dict[str, Any] = {"status": ORG_UNKNOWN, "org_id": None, "management_account_id": None,
                                "management_email_domain": None, "feature_set": None, "error": None}
        try:
            org = self.c("organizations", HOME_REGION).describe_organization().get("Organization") or {}
        except Exception as exc:  # noqa: BLE001
            if _err_code(exc) == "AWSOrganizationsNotInUseException":
                info["status"] = ORG_STANDALONE
            else:
                info["error"] = _err_text(exc)
                log(f"  organizations:DescribeOrganization 실패 - 조직 소속 여부 알 수 없음 ({_err_text(exc)})")
            self.org = info
            return info
        email = str(org.get("MasterAccountEmail") or "")
        mgmt = org.get("MasterAccountId")
        info.update(org_id=org.get("Id"), management_account_id=mgmt, feature_set=org.get("FeatureSet"),
                    management_email_domain=email.rsplit("@", 1)[1] if "@" in email else None,
                    status=ORG_MANAGEMENT if mgmt and mgmt == self.account else ORG_MEMBER)
        self.org = info
        return info

    def org_status(self) -> str:
        return (self.org or self.load_org())["status"]

    def check_organization(self) -> None:
        info = self.org or self.load_org()
        status = info["status"]
        domain = f" ({info['management_email_domain']})" if info.get("management_email_domain") else ""
        if status == ORG_MEMBER:
            rid = info.get("org_id") or "?"
            detail = (f"member of org {rid}, management account {info.get('management_account_id') or '?'}{domain} - "
                      "조직 멤버 계정: SCP와 청구는 관리 계정(리셀러)이 관리. 해지·청구 안내는 아래 reminder 참고")
        elif status == ORG_MANAGEMENT:
            rid = info.get("org_id") or "?"
            detail = (f"management account of org {rid}{domain} - 이 계정이 조직의 관리 계정임. "
                      "관리 계정은 멤버 계정을 모두 정리/제거한 뒤에만 해지 가능 - 교육 계정이 맞는지 확인")
        elif status == ORG_STANDALONE:
            rid = ORG_STANDALONE
            detail = "standalone - AWS Organizations에 속하지 않은 독립 계정"
        else:
            rid = ORG_UNKNOWN
            detail = f"unknown - 조직 소속 여부 확인 불가({info.get('error') or '?'})"
        self.add("organization", GLOBAL, rid, detail, INFO)

    # -- account alias (loaded first: it is also the wrong-account guard) --
    def load_aliases(self) -> List[str]:
        self._aliases_tried = True
        self.aliases = _paginate(self.c("iam", HOME_REGION), "list_account_aliases", "AccountAliases")
        return self.aliases

    def check_account_alias(self) -> None:
        if not self._aliases_tried:
            self.guard("account_alias", GLOBAL, self.load_aliases)
        iam = self.c("iam", HOME_REGION)
        # The alias is the hub's wrong-account guard (and aws-nuke refuses accounts without one), so it is
        # deleted last and only when nothing blocks closure and every check could actually run.
        left = len(blockers_remaining(self.findings))
        unchecked = len(self.checks_failed)
        for alias in self.aliases or []:
            f = self.add("account_alias", GLOBAL, alias,
                         "계정 별칭 - 해지 전에 지워야 같은 별칭을 다른 계정에서 다시 쓸 수 있음(지우면 aws-nuke 재실행 불가)",
                         INFO, fixable=True)
            if self.delete_alias and self.apply and (left or unchecked):
                why = f"BLOCKER {left}건" if left else f"확인 불가 점검 {unchecked}건"
                f["detail"] += (f" | {why}이 남아 별칭 삭제 보류(허브 가드가 별칭을 확인하므로 맨 마지막에 삭제). "
                                "해결 후 다시 실행하거나 콘솔/CLI로 직접 삭제")
                continue
            self.fix(f, lambda a=alias: iam.delete_account_alias(AccountAlias=a),
                     enabled=self.delete_alias, hint="--apply --delete-alias 로 삭제")

    # -- Marketplace --------------------------------------------------------
    def check_marketplace(self) -> None:
        self.guard("marketplace_agreement", GLOBAL, self._marketplace)

    def _agreement_terms(self, mp: Any, agreement_id: str) -> Optional[set]:
        try:
            terms = _paginate(mp, "get_agreement_terms", "acceptedTerms", agreementId=agreement_id)
        except (ClientError, BotoCoreError) as exc:
            log(f"  {agreement_id}: 계약 조건 조회 실패({_err_text(exc)}) - 순서 정렬 없이 진행")
            return None
        kinds: set = set()
        for term in terms:
            kinds.update(k for k, v in term.items() if v)
        return kinds

    def _marketplace(self) -> None:
        mp = self.c("marketplace-agreement", HOME_REGION)
        agreements = _paginate(mp, "search_agreements", "agreementViewSummaries",
                               catalog=MP_CATALOG, filters=MP_FILTERS)
        rows = [(a, self._agreement_terms(mp, a.get("agreementId", "?"))) for a in agreements]
        rows.sort(key=lambda r: 0 if (r[1] and r[1] & MP_UPFRONT_TERMS) else 1)
        for a, terms in rows:
            aid = a.get("agreementId", "?")
            summary = a.get("proposalSummary") or {}
            products = ",".join(r.get("id", "?") for r in summary.get("resources") or []) or "?"
            seller = (a.get("proposer") or {}).get("accountId", "?")
            upfront = bool(terms and terms & MP_UPFRONT_TERMS)
            detail = (f"활성 Marketplace 구매 계약 - 계정을 해지해도 자동 취소되지 않음. product={products}, "
                      f"offer={summary.get('offerId', '?')}, seller={seller}, end={_ts(a.get('endTime'))}, "
                      f"만료 시={a.get('endTimeBehaviorType', '?')}, terms={','.join(sorted(terms)) if terms else '?'}")
            if upfront:
                detail += " | 선결제/계약형 조건 포함: 구매자 취소가 거부될 수 있음"
            f = self.add("marketplace_agreement", GLOBAL, aid, detail, BLOCKER, fixable=True)
            self.fix(f, lambda aid=aid: mp.cancel_agreement(agreementId=aid),
                     failure_advice=(f"구매자가 취소할 수 없는 계약 - 판매자(seller {seller})에게 취소를 요청하거나 "
                                     "AWS Support에 문의 (Marketplace 콘솔 > Manage subscriptions에서 상태 확인)"))

    # -- CloudTrail trails ---------------------------------------------------
    def check_cloudtrail_trails(self) -> None:
        seen: Dict[str, Dict[str, Any]] = {}
        for region in self.regions:
            self.guard("cloudtrail_trail", region, self._trails_in_region, region, seen)
        for arn, trail in seen.items():
            self._handle_trail(arn, trail)

    def _trails_in_region(self, region: str, seen: Dict[str, Dict[str, Any]]) -> None:
        # includeShadowTrails=True so a multi-region trail whose home region is not scanned is still found.
        # Shadow copies are then dropped by de-duplicating on TrailARN: each trail is reported once,
        # under (and deleted from) its home region.
        resp = self.c("cloudtrail", region).describe_trails(includeShadowTrails=True)
        for trail in resp.get("trailList", []):
            arn = trail.get("TrailARN")
            if arn and arn not in seen:
                seen[arn] = trail

    def _handle_trail(self, arn: str, trail: Dict[str, Any]) -> None:
        parts = arn.split(":")
        home = trail.get("HomeRegion") or (parts[3] if len(parts) > 3 else HOME_REGION)
        owner = parts[4] if len(parts) > 4 else self.account
        name = trail.get("Name") or arn
        if owner != self.account:
            self.add("cloudtrail_trail", home, arn,
                     f"다른 계정({owner})이 소유한 조직 트레일 '{name}' - 이 계정에서는 삭제할 수 없음", INFO)
            return
        detail = (f"트레일 '{name}' (multi_region={trail.get('IsMultiRegionTrail')}, "
                  f"bucket={trail.get('S3BucketName', '?')}) - 지우지 않으면 해지 후에도 남아 이벤트 전달/과금이 계속될 수 있음")
        f = self.add("cloudtrail_trail", home, arn, detail, BLOCKER, fixable=True)
        # DeleteTrail must be called in the trail's home region (it accepts the name or the ARN there).
        self.fix(f, lambda: self.c("cloudtrail", home).delete_trail(Name=name),
                 failure_advice="루트 또는 관리자 권한으로 콘솔에서 트레일 삭제")

    # -- CloudTrail Lake -----------------------------------------------------
    def check_cloudtrail_lake(self) -> None:
        seen: set = set()
        for region in self.regions:
            self.guard("cloudtrail_event_data_store", region, self._lake_in_region, region, seen)

    def _lake_in_region(self, region: str, seen: set) -> None:
        ct = self.c("cloudtrail", region)
        try:
            stores = _token_loop(ct.list_event_data_stores, "EventDataStores", "NextToken", "NextToken")
        except ClientError as exc:
            if _err_code(exc) == "UnsupportedOperationException":
                log(f"  [{region}] CloudTrail Lake를 쓰지 않는 계정 - 건너뜀 ({_err_text(exc)})")
                return
            raise
        for store in stores:
            arn = store.get("EventDataStoreArn")
            if not arn or arn in seen:
                continue
            seen.add(arn)
            arn_parts = arn.split(":")
            if len(arn_parts) > 3 and arn_parts[3] and arn_parts[3] != region:
                region, ct = arn_parts[3], self.c("cloudtrail", arn_parts[3])
            name = store.get("Name", arn)
            status = store.get("Status", "?")
            if status == "PENDING_DELETION":
                self.add("cloudtrail_event_data_store", region, arn,
                         f"이벤트 데이터 스토어 '{name}' 삭제 대기(PENDING_DELETION) - 7일 후 자동 삭제, 과금 없음", INFO)
                continue
            protected = bool(store.get("TerminationProtectionEnabled"))
            detail = (f"CloudTrail Lake 이벤트 데이터 스토어 '{name}' (status={status}, "
                      f"retention={store.get('RetentionPeriod', '?')}일, termination_protection={protected}) - 수집/보존 요금")
            f = self.add("cloudtrail_event_data_store", region, arn, detail, BLOCKER, fixable=True)
            self.fix(f, lambda ct=ct, arn=arn, protected=protected: self._delete_eds(ct, arn, protected))

    @staticmethod
    def _delete_eds(ct: Any, arn: str, protected: bool) -> None:
        if protected:
            ct.update_event_data_store(EventDataStore=arn, TerminationProtectionEnabled=False)
        try:
            ct.delete_event_data_store(EventDataStore=arn)
        except ClientError as exc:
            if _err_code(exc) != "EventDataStoreFederationEnabledException":
                raise
            ct.disable_federation(EventDataStore=arn)
            ct.delete_event_data_store(EventDataStore=arn)

    # -- Recycle Bin -----------------------------------------------------------
    def check_recycle_bin(self) -> None:
        for region in self.regions_for("rbin"):
            rule_state: Dict[str, Optional[Dict[str, int]]] = {}
            for rtype in RBIN_TYPES:
                rule_state[rtype] = self.guard("recycle_bin_rule", region, self._rbin_rules_of_type, region, rtype)
            self.guard("recycle_bin_contents", region, self._rbin_contents, region, rule_state)
            self.guard("recycle_bin_contents", region, self._rbin_volumes, region)

    def _rbin_rules_of_type(self, region: str, rtype: str) -> Dict[str, int]:
        rb = self.c("rbin", region)
        present = locked = deleted = 0
        for rule in _paginate(rb, "list_rules", "Rules", ResourceType=rtype):
            gone, is_locked = self._handle_rbin_rule(rb, region, rtype, rule)
            present += 0 if gone else 1
            deleted += 1 if gone else 0
            locked += 1 if is_locked else 0
        return {"present": present, "locked": locked, "deleted": deleted}

    def _rule_lock_info(self, rb: Any, rid: str) -> Tuple[Any, Any]:
        try:
            rule = rb.get_rule(Identifier=rid)
        except (ClientError, BotoCoreError) as exc:
            log(f"  rbin GetRule {rid} 실패: {_err_text(exc)}")
            return "?", None
        delay = ((rule.get("LockConfiguration") or {}).get("UnlockDelay") or {}).get("UnlockDelayValue", "?")
        return delay, rule.get("LockEndTime")

    def _handle_rbin_rule(self, rb: Any, region: str, rtype: str, rule: Dict[str, Any]) -> Tuple[bool, bool]:
        """Returns (rule is gone now, rule is locked)."""
        rid = rule.get("Identifier", "?")
        period = rule.get("RetentionPeriod") or {}
        lock = rule.get("LockState")  # None = never locked
        # Volume retention is at most 7 days, so a volume rule cannot hold billing for long: WARN, not BLOCKER.
        severity = WARN if rtype == "EBS_VOLUME" else BLOCKER
        base = (f"{RBIN_LABEL[rtype]} 보존 규칙(보존 {period.get('RetentionPeriodValue', '?')}일, "
                f"lock={lock or '없음'}) - aws-nuke가 지운 리소스가 휴지통에 남아 보존 기간 내내 과금")
        if lock in ("locked", "pending_unlock"):
            delay, lock_end = self._rule_lock_info(rb, rid)
            if lock == "pending_unlock":
                detail = (f"{base} | 잠금 해제 대기 중: {_ts(lock_end)} 이후 삭제 가능 - "
                          "그때 --apply로 다시 실행(그때까지 해지 보류)")
            else:
                detail = (f"{base} | 잠긴 규칙: 지금은 삭제 불가. UnlockRule 후 {delay}일(unlock delay) 기다린 뒤 "
                          "삭제 가능(그때까지 해지 보류)")
            self.add("recycle_bin_rule", region, rid, detail, severity, fixable=False)
            if lock == "locked":
                u = self.add("recycle_bin_rule_unlock", region, rid,
                             f"잠금 해제 요청(UnlockRule) - unlock delay {delay}일 카운트다운 시작", WARN, fixable=True)

                def _unlock(rid: str = rid, u: Dict[str, Any] = u) -> None:
                    resp = rb.unlock_rule(Identifier=rid)
                    u["detail"] += f" | 해제 예정 {_ts(resp.get('LockEndTime'))} 이후 --apply 재실행"

                self.fix(u, _unlock)
            return False, True
        f = self.add("recycle_bin_rule", region, rid, base, severity, fixable=True)
        return self.fix(f, lambda: rb.delete_rule(Identifier=rid)), False

    def _purge_gate(self, region: str) -> Tuple[bool, str]:
        rb = self.c("rbin", region)
        for rtype in PURGE_RULE_TYPES:  # fresh re-check: restoring+deleting under a live rule would restart retention
            if _paginate(rb, "list_rules", "Rules", ResourceType=rtype):
                return False, f"{rtype} 보존 규칙이 아직 조회됨 - 잠시 후 다시 실행"
        return True, ""

    def _rbin_contents(self, region: str, rule_state: Dict[str, Optional[Dict[str, int]]]) -> None:
        ec2 = self.c("ec2", region)
        snaps = _paginate(ec2, "list_snapshots_in_recycle_bin", "Snapshots")
        images = _paginate(ec2, "list_images_in_recycle_bin", "Images")
        purgeable = all(rule_state.get(t) is not None and rule_state[t]["locked"] == 0  # type: ignore[index]
                        for t in PURGE_RULE_TYPES)
        targets: List[Dict[str, Any]] = []
        fs = fi = None
        if snaps:
            ids = [s["SnapshotId"] for s in snaps]
            last = max((_ts(s.get("RecycleBinExitTime")) for s in snaps), default="?")
            fs = self.add("recycle_bin_contents", region, "snapshots",
                          f"휴지통 스냅샷 {len(snaps)}개 - 보존 만료(마지막 {last})까지 일반 스냅샷 요금으로 과금. "
                          f"크기는 API가 알려주지 않음(Cost Explorer 사용 유형 EBS:SnapshotUsage로 확인). IDs: {_short(ids)}",
                          WARN, fixable=purgeable)
            targets.append(fs)
        if images:
            ids = [i["ImageId"] for i in images]
            fi = self.add("recycle_bin_contents", region, "images",
                          f"휴지통 AMI {len(images)}개 - AMI 자체는 추가 요금 없음(연결 스냅샷은 따로 과금). IDs: {_short(ids)}",
                          INFO, fixable=purgeable)
            targets.append(fi)
        if not targets:
            return
        if not purgeable:
            for f in targets:
                f["detail"] += " | 잠겼거나 확인 못 한 보존 규칙이 있어 지금은 비울 수 없음 - 보존 만료까지 대기"
            return
        if not self.purge_recycle_bin:
            for f in targets:
                f["detail"] += " | 보존 규칙이 없는 상태에서 --apply --purge-recycle-bin 으로 복원 후 영구 삭제 가능"
            return
        # Rule-deletion propagation is not documented. Deleting a restored resource while a rule still
        # matches would send it back to the bin with a fresh retention period, so purge only in a later
        # run that starts with no retention rules at all.
        waiting = [t for t in PURGE_RULE_TYPES
                   if rule_state[t]["present"] or rule_state[t].get("deleted")]  # type: ignore[index]
        if waiting:
            for f in targets:
                f["detail"] += (f" | {'/'.join(waiting)} 보존 규칙 삭제 후(반영까지 몇 분) "
                                "--apply --purge-recycle-bin 을 다시 실행하면 비울 수 있음")
            return
        if not self.apply:
            for f in targets:
                f["action"] = "planned"
            return
        ok, why = self._purge_gate(region)
        if not ok:
            for f in targets:
                f["detail"] += f" | 비우기 보류: {why}"
            log(f"  [{region}] 휴지통 비우기 보류: {why}")
            return
        purged_snaps, purged_images, errors = self._purge(region, snaps, images)
        # Verify: anything we purged that is back in the bin means a rule still matched.
        still = {s["SnapshotId"] for s in _paginate(ec2, "list_snapshots_in_recycle_bin", "Snapshots")}
        still |= {i["ImageId"] for i in _paginate(ec2, "list_images_in_recycle_bin", "Images")}
        for rid in purged_snaps + purged_images:
            if rid in still:
                errors[rid] = "삭제 후 다시 휴지통에 들어감(보존 규칙이 아직 적용 중)"
        for f, items, key in ((fs, snaps, "SnapshotId"), (fi, images, "ImageId")):
            if f is None:
                continue
            errs = [f"{i[key]}: {errors[i[key]]}" for i in items if i[key] in errors]
            f["action"] = "failed" if errs else "done"
            f["error"] = "; ".join(errs[:5]) + (f" 외 {len(errs) - 5}건" if len(errs) > 5 else "") if errs else None

    def _wait_states(self, fetch: Callable[[List[str]], List[Dict[str, Any]]], id_key: str,
                     ids: List[str], ok_state: str, errors: Dict[str, str]) -> List[str]:
        pending, ready = list(ids), []
        attempts = max(1, self.purge_wait_seconds // self.poll_seconds)
        for attempt in range(attempts):
            items: List[Dict[str, Any]] = []
            for chunk in _chunks(pending, 100):
                try:
                    items.extend(fetch(chunk))
                except (ClientError, BotoCoreError):
                    for one in chunk if len(chunk) > 1 else []:  # one bad ID fails the whole batch
                        try:
                            items.extend(fetch([one]))
                        except (ClientError, BotoCoreError):
                            pass  # not visible yet; retry next round
            for item in items:
                state = item.get("State")
                if state == ok_state and item[id_key] not in ready:
                    ready.append(item[id_key])
                elif state in ("error", "failed", "invalid"):
                    errors[item[id_key]] = f"state={state}"
            pending = [i for i in pending if i not in ready and i not in errors]
            if not pending:
                break
            if attempt < attempts - 1:
                self._sleep(self.poll_seconds)
        for rid in pending:
            errors[rid] = f"'{ok_state}' 상태 대기 시간 초과 - 휴지통에서 복원된 채 남았을 수 있음(직접 삭제 필요)"
        return ready

    def _purge(self, region: str, snaps: List[Dict[str, Any]],
               images: List[Dict[str, Any]]) -> Tuple[List[str], List[str], Dict[str, str]]:
        """Restore from the bin, then delete. With no matching retention rule a deletion is permanent
        (EBS user guide, Recycle Bin considerations). AMIs need their snapshots restored first."""
        ec2 = self.c("ec2", region)
        errors: Dict[str, str] = {}
        restored: List[str] = []
        for snap in snaps:
            sid = snap["SnapshotId"]
            try:
                ec2.restore_snapshot_from_recycle_bin(SnapshotId=sid)
                restored.append(sid)
            except (ClientError, BotoCoreError) as exc:
                errors[sid] = f"복원 실패 {_err_text(exc)}"
        ready = self._wait_states(lambda ids: ec2.describe_snapshots(SnapshotIds=ids)["Snapshots"],
                                  "SnapshotId", restored, "completed", errors)
        restored_images: List[str] = []
        for image in images:
            iid = image["ImageId"]
            try:
                ec2.restore_image_from_recycle_bin(ImageId=iid)
                restored_images.append(iid)
            except (ClientError, BotoCoreError) as exc:
                errors[iid] = f"복원 실패 {_err_text(exc)}"
        ready_images = self._wait_states(lambda ids: ec2.describe_images(ImageIds=ids)["Images"],
                                         "ImageId", restored_images, "available", errors)
        purged_images: List[str] = []
        for iid in ready_images:
            try:
                ec2.deregister_image(ImageId=iid, DeleteAssociatedSnapshots=True)
                purged_images.append(iid)
            except (ClientError, BotoCoreError) as exc:
                errors[iid] = f"복원됐지만 등록 해제 실패(직접 삭제 필요) {_err_text(exc)}"
        purged_snaps: List[str] = []
        for sid in ready:
            try:
                ec2.delete_snapshot(SnapshotId=sid)
                purged_snaps.append(sid)
            except ClientError as exc:
                if _err_code(exc) == "InvalidSnapshot.NotFound":  # already removed with its AMI
                    purged_snaps.append(sid)
                else:
                    errors[sid] = f"복원됐지만 삭제 실패(직접 삭제 필요) {_err_text(exc)}"
        log(f"  [{region}] 휴지통 비우기: 스냅샷 {len(purged_snaps)}개, AMI {len(purged_images)}개, 실패 {len(errors)}건")
        return purged_snaps, purged_images, errors

    def _rbin_volumes(self, region: str) -> None:
        ec2 = self.c("ec2", region)
        if not hasattr(ec2, "list_volumes_in_recycle_bin"):
            log(f"  [{region}] 설치된 boto3에 ListVolumesInRecycleBin이 없어 휴지통 볼륨 확인 생략")
            return
        vols = _token_loop(ec2.list_volumes_in_recycle_bin, "Volumes", "NextToken", "NextToken")
        if not vols:
            return
        size = sum(int(v.get("Size") or 0) for v in vols)
        last = max((_ts(v.get("RecycleBinExitTime")) for v in vols), default="?")
        self.add("recycle_bin_contents", region, "volumes",
                 f"휴지통 EBS 볼륨 {len(vols)}개(총 {size} GiB) - 보존 만료(마지막 {last}, 최대 7일)까지 일반 볼륨 요금. "
                 f"자동 삭제 대기(--purge-recycle-bin 대상 아님). IDs: {_short([v['VolumeId'] for v in vols])}", WARN)

    # -- commitments ------------------------------------------------------------
    def check_ec2_reserved(self) -> None:
        for region in self.regions:
            self.guard("ec2_reserved_instance", region, self._ec2_reserved, region)

    def _ec2_reserved(self, region: str) -> None:
        resp = self.c("ec2", region).describe_reserved_instances(
            Filters=[{"Name": "state", "Values": ["active", "payment-pending"]}])
        for ri in resp.get("ReservedInstances", []):
            detail = (f"EC2 예약 인스턴스 {ri.get('InstanceCount', '?')}x {ri.get('InstanceType', '?')} "
                      f"({ri.get('OfferingType', '?')}, {ri.get('OfferingClass', '?')}, state={ri.get('State')}), "
                      f"만료 {_ts(ri.get('End'))} - {COMMIT_ADVICE}")
            self.add("ec2_reserved_instance", region, ri.get("ReservedInstancesId", "?"), detail, BLOCKER)

    def check_other_reserved(self) -> None:
        for spec in OTHER_RESERVATIONS:
            check, service = spec[0], spec[1]
            for region in self.regions_for(service):
                self.guard(check, region, self._other_reserved, region, spec)

    def _other_reserved(self, region: str, spec: Tuple[str, ...]) -> None:
        check, service, method, key, id_key, count_key, type_key, label = spec
        for r in _paginate(self.c(service, region), method, key):
            if str(r.get("State", "")).lower() not in ACTIVE_RESERVATION_STATES:
                continue
            detail = (f"{label} {r.get(count_key, '?')}x {r.get(type_key, '?')} ({r.get('OfferingType', '?')}, "
                      f"state={r.get('State')}), 만료 {_end_time(r.get('StartTime'), r.get('Duration'))} - {COMMIT_ADVICE}")
            self.add(check, region, r.get(id_key, "?"), detail, BLOCKER)

    def check_savings_plans(self) -> None:
        self.guard("savings_plan", GLOBAL, self._savings_plans)

    def _savings_plans(self) -> None:
        sp = self.c("savingsplans", HOME_REGION)
        plans = _token_loop(sp.describe_savings_plans, "savingsPlans", "nextToken", "nextToken",
                            states=["active", "queued", "payment-pending"])
        now = _dt.datetime.now(_dt.timezone.utc)
        for plan in plans:
            state = plan.get("state")
            extra = ""
            returnable = _parse_iso(plan.get("returnableUntil"))
            if state == "queued":
                extra = " | 대기(queued) 상태: DeleteQueuedSavingsPlan으로 구매 취소 가능"
            elif returnable and returnable > now:
                extra = f" | 반품 가능 기한 {_ts(returnable)} 이전: ReturnSavingsPlan으로 반품 가능"
            detail = (f"Savings Plan {plan.get('savingsPlanType', '?')} ${plan.get('commitment', '?')}/시간 "
                      f"({plan.get('paymentOption', '?')}, state={state}), 만료 {plan.get('end', '?')} - {COMMIT_ADVICE}{extra}")
            self.add("savings_plan", GLOBAL, plan.get("savingsPlanId", "?"), detail, BLOCKER)

    def check_bedrock_provisioned(self) -> None:
        for region in self.regions_for("bedrock"):
            self.guard("bedrock_provisioned_throughput", region, self._bedrock_pt, region)

    def _bedrock_pt(self, region: str) -> None:
        items = _paginate(self.c("bedrock", region), "list_provisioned_model_throughputs", "provisionedModelSummaries")
        for pt in items:
            arn = pt.get("provisionedModelArn", "?")
            name = pt.get("provisionedModelName", arn)
            if pt.get("commitmentDuration"):
                detail = (f"Bedrock 약정 Provisioned Throughput '{name}' ({pt.get('commitmentDuration')}, "
                          f"MU {pt.get('modelUnits', '?')}), 약정 만료 {_ts(pt.get('commitmentExpirationTime'))} - "
                          f"약정 중에는 삭제 불가, {COMMIT_ADVICE}")
                self.add("bedrock_provisioned_throughput", region, arn, detail, BLOCKER)
            else:
                self.add("bedrock_provisioned_throughput", region, arn,
                         f"약정 없는 Provisioned Throughput '{name}'이(가) 남아 있음 - 지울 때까지 시간당 과금. "
                         "aws-nuke를 다시 돌리거나 콘솔에서 삭제", WARN)

    def check_shield(self) -> None:
        self.guard("shield_subscription", GLOBAL, self._shield)

    def _shield(self) -> None:
        sh = self.c("shield", HOME_REGION)
        if sh.get_subscription_state().get("SubscriptionState") != "ACTIVE":
            return
        sub = sh.describe_subscription().get("Subscription") or {}
        auto = sub.get("AutoRenew", "?")
        rid = sub.get("SubscriptionArn") or "shield-advanced"
        self.add("shield_subscription", GLOBAL, rid,
                 f"Shield Advanced 구독(1년 약정, 월 $3,000) 종료 {_ts(sub.get('EndTime'))}, 자동 갱신 {auto} - {COMMIT_ADVICE}",
                 BLOCKER)
        if auto == "ENABLED":
            f = self.add("shield_auto_renew", GLOBAL, rid,
                         "자동 갱신 켜짐 - UpdateSubscription(AutoRenew=DISABLED)로 다음 1년 갱신을 막음"
                         "(구독 마지막 30일 안에서만 변경 가능)", WARN, fixable=True)
            self.fix(f, lambda: sh.update_subscription(AutoRenew="DISABLED"),
                     failure_advice="LockedSubscriptionException이면 아직 마지막 30일 전 - AWS Support에 갱신 해지 요청")

    # -- Route 53 domains / IAM ----------------------------------------------------
    def check_route53_domains(self) -> None:
        self.guard("route53_domain", GLOBAL, self._domains)

    def _domains(self) -> None:
        r53 = self.c("route53domains", HOME_REGION)
        for d in _paginate(r53, "list_domains", "Domains"):
            name = d.get("DomainName", "?")
            expiry = _ts(d.get("Expiry"))
            if d.get("AutoRenew"):
                f = self.add("route53_domain", GLOBAL, name,
                             f"등록 도메인(만료 {expiry}) 자동 갱신 켜짐 - 갱신 요금이 청구될 수 있음. "
                             "계속 쓸 도메인이면 해지 전에 다른 계정으로 이전", WARN, fixable=True)
                self.fix(f, lambda n=name: r53.disable_domain_auto_renew(DomainName=n))
            else:
                self.add("route53_domain", GLOBAL, name,
                         f"등록 도메인(만료 {expiry}) 자동 갱신 꺼짐 - 해지 후 정지 뒤 삭제/반환됨. 계속 쓸 거면 해지 전에 이전", INFO)

    def check_root_access_keys(self) -> None:
        self.guard("root_access_keys", GLOBAL, self._root_keys)

    def _root_keys(self) -> None:
        summary = self.c("iam", HOME_REGION).get_account_summary().get("SummaryMap", {})
        if summary.get("AccountAccessKeysPresent"):
            self.add("root_access_keys", GLOBAL, "root",
                     "루트 사용자에게 액세스 키가 있음 - 유출되면 무제한 과금/해킹 위험. IAM 사용자/역할로는 지울 수 없음: "
                     "루트로 로그인 > 보안 자격 증명 > 액세스 키 삭제", WARN)

    def reminders(self) -> Tuple[Tuple[str, str], ...]:
        status = self.org_status()
        if status == ORG_MEMBER:
            return MEMBER_REMINDERS
        if status == ORG_UNKNOWN:
            return UNKNOWN_ORG_REMINDERS
        return REMINDERS  # standalone (or this is the management account itself)

    def check_reminders(self) -> None:
        for rid, text in self.reminders():
            self.add("closure_reminder", GLOBAL, rid, text, INFO)


# ---------------------------------------------------------------- orchestration
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="preclose_check.py",
        description="계정 해지 직전 점검: 해지 후에도 과금되거나 aws-nuke가 처리하지 않는 항목을 찾고 --apply로 고친다.")
    add_common_args(p)
    g = p.add_argument_group("preclose")
    g.add_argument("--all-regions", action="store_true",
                   help="--regions 대신 활성화된 모든 리전 점검(account:ListRegions, 실패 시 ec2:DescribeRegions)")
    g.add_argument("--delete-alias", action="store_true", help="--apply와 함께: 계정 별칭 삭제(별칭 재사용 가능)")
    g.add_argument("--purge-recycle-bin", action="store_true",
                   help="--apply와 함께: 보존 규칙이 모두 지워진 리전에서 휴지통 스냅샷/AMI를 복원 후 영구 삭제")
    return p


def _base_result(account: Optional[str], args: argparse.Namespace, regions: List[str]) -> Dict[str, Any]:
    return {
        "module": MODULE,
        "account_id": account,
        "generated_at": now_iso(),
        "applied": bool(getattr(args, "apply", False)),
        "status": "ERROR",
        "ready_to_close": False,
        "regions": regions,
        "options": {"all_regions": bool(getattr(args, "all_regions", False)),
                    "delete_alias": bool(getattr(args, "delete_alias", False)),
                    "purge_recycle_bin": bool(getattr(args, "purge_recycle_bin", False))},
        "organization": None,
        "summary": {},
        "findings": [],
        "error": None,
    }


def error_result(account: Optional[str], args: argparse.Namespace, message: str,
                 regions: Optional[List[str]] = None) -> Dict[str, Any]:
    result = _base_result(account, args, regions or [])
    result["error"] = message
    return result


def build_result(pc: PreClose, args: argparse.Namespace, error: Optional[str] = None) -> Tuple[Dict[str, Any], int]:
    result = _base_result(pc.account, args, pc.regions)
    findings = pc.findings
    remaining = blockers_remaining(findings)
    code = EXIT_ERROR if error else exit_code_for(findings)
    result["status"] = {EXIT_OK: "OK", EXIT_ISSUES: "ISSUES"}.get(code, "ERROR")
    result["ready_to_close"] = code == EXIT_OK and not pc.checks_failed
    result["summary"] = {
        "blockers_remaining": len(remaining),
        "blockers_resolved": sum(1 for f in findings if f["severity"] == BLOCKER and f["action"] == "done"),
        "warn": sum(1 for f in findings if f["severity"] == WARN),
        "info": sum(1 for f in findings if f["severity"] == INFO),
        "actions": {a: sum(1 for f in findings if f["action"] == a) for a in ACTIONS},
        "checks_failed": list(pc.checks_failed),
        "checks_blocked_by_org": list(pc.checks_blocked_by_org),
    }
    result["organization"] = pc.org
    result["findings"] = findings
    result["error"] = error
    return result, code


def run(session: Any, account: str, args: argparse.Namespace, *,
        client_factory: Optional[Callable[[str, str], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        checks: Optional[List[str]] = None) -> Tuple[Dict[str, Any], int]:
    regions = [r for r in parse_regions(args.regions) if r != "global"]
    pc = PreClose(session, account, regions, apply=args.apply, delete_alias=args.delete_alias,
                  purge_recycle_bin=args.purge_recycle_bin, client_factory=client_factory, sleep=sleep)
    if args.all_regions:
        found = pc.guard("region_discovery", GLOBAL, pc.discover_regions)
        if found:
            pc.regions = found
        else:
            log(f"  활성 리전 조회 실패 - --regions({','.join(regions)})만 점검")
    if not pc.regions:
        return build_result(pc, args, error="점검할 리전이 없음(--regions 확인)")
    log(f"preclose: 계정 {account}, 리전 {', '.join(pc.regions)}, {'APPLY' if args.apply else 'DRY-RUN'}")

    # Wrong-account guard: CONTRACTS says training aliases never contain "prod".
    aliases = pc.guard("account_alias", GLOBAL, pc.load_aliases) or []
    prod = [a for a in aliases if "prod" in a.lower()]
    if prod:
        pc.add("account_alias", GLOBAL, ",".join(prod),
               "계정 별칭에 'prod'가 들어 있음 - 교육 계정이 아닐 수 있음. 대상 계정을 다시 확인", BLOCKER)
        if args.apply:
            msg = f"별칭 {','.join(prod)}에 'prod'가 포함되어 --apply를 중단함(아무것도 변경하지 않음)"
            log("ERROR: " + msg)
            return build_result(pc, args, error=msg)

    pc.run(checks)
    return build_result(pc, args)


SEV_LABEL = {BLOCKER: "차단(BLOCKER) - 해지 전에 반드시 해결", WARN: "경고(WARN) - 확인 권장", INFO: "정보(INFO)"}
ACTION_LABEL = {"none": "-", "planned": "예정(--apply 시 실행)", "done": "완료", "failed": "실패"}


def render_summary(result: Dict[str, Any]) -> str:
    mode = "APPLY(변경 실행)" if result.get("applied") else "DRY-RUN(변경 없음)"
    lines = [f"=== preclose 해지 전 점검 | 계정 {result.get('account_id') or '?'} | {mode} | "
             f"리전 {', '.join(result.get('regions') or []) or '-'} ==="]
    findings = result.get("findings") or []
    for sev in SEVERITIES:
        items = [f for f in findings if f["severity"] == sev]
        head = f"\n[{SEV_LABEL[sev]}] {len(items)}건"
        if sev == BLOCKER:
            left = len(blockers_remaining(items))
            head += f" (남음 {left}, 해결 {len(items) - left})"
        lines.append(head)
        for f in items:
            lines.append(f"  - [{ACTION_LABEL.get(f['action'], f['action'])}] {f['check']} | {f['region']} | {f['resource_id']}")
            lines.append(f"      {f['detail']}")
            if f.get("error"):
                lines.append(f"      오류: {f['error']}")
    summary = result.get("summary") or {}
    lines.append("")
    if result.get("error"):
        lines.append(f"판정: 오류 - {result['error']}")
    elif summary.get("blockers_remaining"):
        lines.append(f"판정: 해지 보류 - BLOCKER {summary['blockers_remaining']}건 남음"
                     + ("" if result.get("applied") else " (--apply로 고칠 수 있는 항목은 '예정' 표시)"))
    elif (result.get("organization") or {}).get("status") == ORG_MEMBER:
        lines.append("판정: BLOCKER 없음 - 수동 체크리스트를 마친 뒤 루트 콘솔에서 해지"
                     "(SCP로 막히면 관리 계정(리셀러)에 해지 요청)")
    else:
        lines.append("판정: BLOCKER 없음 - 아래 수동 체크리스트를 마친 뒤 루트 콘솔에서 해지")
    if summary.get("checks_failed"):
        lines.append(f"주의: 확인하지 못한 점검 {len(summary['checks_failed'])}건 - "
                     f"{', '.join(summary['checks_failed'])} (콘솔에서 직접 확인)")
    if summary.get("checks_blocked_by_org"):
        lines.append(f"주의: 조직 정책(SCP)으로 막힌 점검 {len(summary['checks_blocked_by_org'])}건 - "
                     f"{', '.join(summary['checks_blocked_by_org'])} (리셀러에게 확인)")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None, *,
         client_factory: Optional[Callable[[str, str], Any]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        session = get_session(args)
        acct = account_id(session)
    except Exception as exc:  # noqa: BLE001
        result = error_result(None, args, f"자격 증명/대상 계정 확인 실패: {_err_text(exc)}")
        log("ERROR: " + result["error"])
        print(render_summary(result))
        write_json(args.json_out, result)
        return EXIT_ERROR
    try:
        result, code = run(session, acct, args, client_factory=client_factory)
    except Exception as exc:  # noqa: BLE001
        log(traceback.format_exc())
        result, code = error_result(acct, args, f"예상치 못한 오류: {_err_text(exc)}"), EXIT_ERROR
    print(render_summary(result))
    write_json(args.json_out, result)
    return code


if __name__ == "__main__":
    sys.exit(main())
