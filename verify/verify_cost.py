#!/usr/bin/env python3
"""Post-cleanup verification for one AWS training account (edu-cleanup module 7: verify).

Two ways to judge "clean" (--mode):

* cost      - from the day after cleanup up to yesterday (UTC), Cost Explorer shows no *usage*
              cost line (service x usage type x day) above the threshold that is not covered by an
              allowlist rule (D+2 check). Optionally also runs the inventory (--inventory).
* inventory - never calls Cost Explorer. A live, read-only inventory of ~75 commonly creatable or
              billable resource types (per region + global) finds nothing outside the protected
              names. Runs immediately after cleanup (no settle-day wait). This is the path for member
              accounts of a reseller's AWS Organization whose SCPs deny ce:* even to root.
* auto      - (default) try Cost Explorer; if it is denied / not enabled / has no data, fall back to
              inventory mode with a note instead of failing.

Read-only: --apply is accepted but nothing is changed.
Exit codes: 0 CLEAN, 1 RESIDUAL, 2 ERROR, 3 PENDING (too early to judge; cost mode only).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING

import argparse
import datetime as dt
import fnmatch
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from common.session import now_iso, caller_arn, ROLE_NAME

MODULE = "verify"

MODE_AUTO = "auto"
MODE_COST = "cost"
MODE_INVENTORY = "inventory"

# Cost Explorer facts (verified against the botocore `ce` service model 1.43.x):
#   - endpoint lives in us-east-1
#   - Metrics valid names: AmortizedCost, BlendedCost, NetAmortizedCost, NetUnblendedCost,
#     NormalizedUsageAmount, UnblendedCost, UsageQuantity
#   - GroupBy: at most 2 groups; SERVICE and USAGE_TYPE are valid DIMENSION keys
#   - RECORD_TYPE (console: "Charge type") is a valid Filter dimension; values seen in API use:
#     Usage, Credit, Refund, Tax, Support (+ RI/Savings Plans fee types).
#     Default = exclude Credit/Refund/Tax (fail-safe: a misspelled exclude value can only cause a
#     false RESIDUAL, whereas a misspelled include value like "Usage" would cause a false CLEAN).
#     It also keeps RI/SP/Marketplace fees visible, which are real leaks. `--record-types Usage`
#     switches to an include filter.
#   - TimePeriod.End is exclusive; days are UTC calendar days.
#   - `Estimated` is per ResultByTime entry; in practice it stays true until the month's bill is
#     finalized, so it is reported but only gates the verdict with --require-final.
CE_REGION = "us-east-1"
CE_METRIC = "UnblendedCost"
DEFAULT_EXCLUDE_RECORD_TYPES = "Credit,Refund,Tax"

KST = dt.timezone(dt.timedelta(hours=9), "KST")  # Asia/Seoul, no DST

STATUS_CLEAN = "CLEAN"
STATUS_RESIDUAL = "RESIDUAL"
STATUS_PENDING = "PENDING"
STATUS_ERROR = "ERROR"
EXIT_BY_STATUS = {STATUS_CLEAN: EXIT_OK, STATUS_RESIDUAL: EXIT_ISSUES,
                  STATUS_ERROR: EXIT_ERROR, STATUS_PENDING: EXIT_PENDING}

# This script itself bills $0.01 per Cost Explorer request (usage type like USE1-APIRequest).
BUILTIN_ALLOWLIST = [{
    "service": "AWS Cost Explorer",
    "usage_type_pattern": "*APIRequest*",
    "max_usd_per_day": 1.0,
    "reason": "검증 스크립트 자신의 Cost Explorer API 호출 요금($0.01/요청) (내장 규칙)",
    "builtin": True,
}]

# Resources whose name (or any '/'- or ':'-separated part of their identifier, e.g. the function name
# inside /aws/lambda/<fn> or the topic name inside an SNS ARN) starts with one of these are listed
# under inventory_protected and never make the verdict RESIDUAL. CloudFormation stacks whose name is
# protected also protect every physical resource they own (list_stack_resources).
#   - edu-bootstrap*, EduCleanup*  : the bootstrap stack / EduCleanupRole (CONTRACTS.md)
#   - edu-killswitch*              : kill switch stacks and their resources (allowed to remain)
#   - the rest                     : created by the reseller's AWS Organization in member accounts
#                                    (Organizations access role, StackSets, Control Tower)
EDU_PROTECTED_PREFIXES = ("edu-bootstrap", "EduCleanup", "edu-killswitch")
ORG_MANAGED_PREFIXES = ("OrganizationAccountAccessRole", "StackSet-", "stacksets-exec-",
                        "AWSCloudFormationStackSetExecutionRole", "aws-controltower", "AWSControlTower")
PROTECTED_PREFIXES = EDU_PROTECTED_PREFIXES + ORG_MANAGED_PREFIXES

# A day Cost Explorer has not returned at all is "not posted yet" for this long after it ended;
# afterwards a missing day is treated as $0 (CE may omit empty days) so the run can finish.
NO_DATA_GRACE_HOURS = 72

HINT_CE_NOT_ENABLED = ("이 계정에서 Cost Explorer가 아직 활성화되지 않았습니다. 콘솔에서 Cost Explorer를 한 번 열어 활성화하세요 "
                       "(Billing and Cost Management → Cost Explorer → 'Launch Cost Explorer'. API로는 켤 수 없음). "
                       "활성화 후 데이터가 준비되기까지 약 24시간이 걸립니다.")
HINT_CE_DATA_UNAVAILABLE = ("Cost Explorer 데이터가 아직 없습니다. 콘솔에서 Cost Explorer를 한 번 열어 활성화하세요. "
                            "이미 켰다면 데이터 준비(약 24시간)를 기다린 뒤 다시 실행하세요.")
HINT_CE_ACCESS = ("ce:GetCostAndUsage 권한이 없습니다. EduCleanupRole(--role-arn) 또는 ce:GetCostAndUsage가 있는 "
                  "IAM 사용자로 실행하세요. 권한이 있는데도 거부되면 콘솔에서 Cost Explorer를 한 번 열어 활성화하세요.")
HINT_CREDENTIALS = "AWS 자격 증명을 확인하세요 (--profile / --role-arn / --external-id)."
HINT_CE_FALLBACK = ("Cost Explorer를 쓸 수 없어 인벤토리 모드로 판정했습니다 (리셀러 조직의 SCP가 ce:*를 막는 멤버 계정이면 정상). "
                    "이런 계정은 처음부터 --mode inventory 로 실행하세요. 비용 확정은 리셀러 청구서로만 할 수 있습니다.")

# Best-effort mapping of usage-type prefixes to regions (no prefix = us-east-1 or a global service).
USAGE_TYPE_REGION_PREFIX = {
    "USE1": "us-east-1", "USE2": "us-east-2", "USW1": "us-west-1", "USW2": "us-west-2",
    "APN1": "ap-northeast-1", "APN2": "ap-northeast-2", "APN3": "ap-northeast-3",
    "APS1": "ap-southeast-1", "APS2": "ap-southeast-2", "APS3": "ap-south-1",
    "CAN1": "ca-central-1", "EUC1": "eu-central-1", "EU": "eu-west-1", "EUW2": "eu-west-2",
    "EUW3": "eu-west-3", "EUN1": "eu-north-1", "EUS1": "eu-south-1", "SAE1": "sa-east-1",
    "APE1": "ap-east-1", "MES1": "me-south-1", "AFS1": "af-south-1",
}

# What to look at when a service shows residual usage (Korean hints for the summary).
SERVICE_HINTS = {
    "Amazon Elastic Compute Cloud - Compute": "EC2 인스턴스(중지 포함)",
    "EC2 - Other": "EBS 볼륨/스냅샷, NAT 게이트웨이, 탄력적 IP",
    "Amazon Virtual Private Cloud": "퍼블릭 IPv4 주소, NAT 게이트웨이, VPC 엔드포인트",
    "Elastic Load Balancing": "ALB/NLB/CLB",
    "Amazon Relational Database Service": "RDS 인스턴스/클러스터/스냅샷",
    "Amazon Simple Storage Service": "남은 S3 버킷/객체",
    "Amazon Bedrock": "프로비저닝 처리량, 커스텀/임포트 모델, 또는 유출 키로 인한 호출(kill switch 확인)",
    "Amazon SageMaker": "엔드포인트/노트북/Studio 앱",
    "Amazon OpenSearch Service": "OpenSearch 도메인/Serverless 컬렉션",
    "Amazon DynamoDB": "DynamoDB 테이블",
    "Amazon Elastic Container Service for Kubernetes": "EKS 클러스터",
    "Amazon Elastic Kubernetes Service": "EKS 클러스터",
    "AWS Key Management Service": "삭제 예약되지 않은 KMS 키",
    "Amazon Route 53": "호스팅 영역/도메인",
    "AWS CloudTrail": "추가 트레일/이벤트 데이터 스토어 (preclose 모듈)",
    "AmazonCloudWatch": "로그 그룹/알람/지표",
}


# --------------------------------------------------------------------------- args
def _date(value: str) -> Optional[dt.date]:
    if not value.strip():
        return None  # the hub passes --cleanup-date "" when it has none
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("날짜 형식은 YYYY-MM-DD 입니다: %r" % value)


def _nonneg_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("0 이상이어야 합니다")
    return n


def _pos_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("1 이상이어야 합니다")
    return n


def _nonneg_float(value: str) -> float:
    f = float(value)
    if f < 0:
        raise argparse.ArgumentTypeError("0 이상이어야 합니다")
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="정리 후 검증. inventory: 남은 리소스가 없으면 CLEAN (Cost Explorer 미사용, 정리 직후 실행). "
                    "cost: 정리 다음 날부터 어제(UTC)까지 허용 목록 밖 사용 요금이 없으면 CLEAN (D+2). "
                    "auto(기본): cost를 시도하고 Cost Explorer가 막혀 있으면 inventory로 판정.")
    add_common_args(p)
    p.add_argument("--mode", choices=(MODE_AUTO, MODE_COST, MODE_INVENTORY), default=MODE_AUTO,
                   help="inventory = Cost Explorer를 절대 호출하지 않음 / cost = Cost Explorer로만 판정 / "
                        "auto = Cost Explorer 거부·미활성화·데이터 없음이면 inventory로 대체 (기본: %(default)s)")
    p.add_argument("--cleanup-date", type=_date, default=None,
                   help="정리(aws-nuke)가 끝난 날짜 YYYY-MM-DD (cost/auto 기본: 오늘, Asia/Seoul; inventory 모드에서는 "
                        "기록용·선택). KST/UTC 어느 쪽 날짜로 적어도 검사 구간은 다음 날(UTC)부터 시작한다.")
    p.add_argument("--settle-days", type=_nonneg_int, default=2,
                   help="정리일 + N일(UTC 00:00) 전에는 PENDING (기본: 2)")
    p.add_argument("--lag-hours", type=_nonneg_float, default=12.0,
                   help="UTC 하루가 끝난 뒤 이 시간이 지나야 그날 데이터가 반영됐다고 본다. 정리 다음 날(UTC)이 반영되기 "
                        "전에는 PENDING (기본: 12 → 최초 판정 = 정리일+2 12:00 UTC = 21:00 KST)")
    p.add_argument("--days", type=_pos_int, default=None,
                   help="검사 구간을 어제(UTC)부터 거꾸로 N일로 제한 (기본: 정리일+1 ~ 어제 전체)")
    p.add_argument("--threshold-usd", type=_nonneg_float, default=0.01,
                   help="(일자, 서비스, 사용 유형)별 이 금액을 넘으면 표시 (기본: 0.01)")
    p.add_argument("--allowlist", help="허용 규칙 JSON 파일 (verify/allowlist.example.json 참고)")
    p.add_argument("--exclude-record-types", default=DEFAULT_EXCLUDE_RECORD_TYPES,
                   help="제외할 RECORD_TYPE(요금 유형), 쉼표 구분 (기본: %(default)s → 크레딧/환불/세금 제외, "
                        "약정·마켓플레이스 수수료는 잡힘)")
    p.add_argument("--record-types",
                   help="지정하면 이 RECORD_TYPE만 포함 (예: Usage). --exclude-record-types 대신 사용")
    p.add_argument("--require-final", action="store_true",
                   help="마지막 날짜가 Estimated=false(확정)가 아니면 PENDING. Estimated는 보통 월 마감까지 true라서 "
                        "월초에 지난달을 최종 확인할 때만 쓴다.")
    p.add_argument("--inventory", action="store_true",
                   help="cost 모드에서도 잔여물 인벤토리를 함께 실행 (inventory 모드와 auto 대체 시에는 항상 실행)")
    p.add_argument("--all-regions", action="store_true",
                   help="인벤토리를 활성화된 모든 리전에서 실행 (account:ListRegions, 실패 시 ec2:DescribeRegions)")
    p.add_argument("--protect-prefix", action="append", default=[],
                   help="이 접두사로 시작하는 리소스 이름은 인벤토리 잔여물로 세지 않음 (반복 가능, "
                        "기본 내장: %s)" % ", ".join(PROTECTED_PREFIXES))
    p.add_argument("--max-examples", type=_nonneg_int, default=5,
                   help="요약/JSON examples 에 유형별로 보여 줄 리소스 ID 수 (기본: %(default)s)")
    p.add_argument("--now", type=_utc_datetime, default=None,
                   help="(재현/테스트용) 현재 시각을 UTC로 고정: YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM")
    return p


def _utc_datetime(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("형식: YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM (UTC): %r" % value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- allowlist
class AllowlistError(ValueError):
    pass


def load_allowlist(path: Optional[str]) -> List[Dict[str, Any]]:
    """Load and validate allowlist rules; built-in rules are always appended."""
    rules: List[Dict[str, Any]] = []
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise AllowlistError("허용 목록 파일을 읽을 수 없습니다: %s (%s)" % (path, e))
        if not isinstance(data, list):
            raise AllowlistError("허용 목록은 JSON 배열이어야 합니다: %s" % path)
        for i, raw in enumerate(data):
            if not isinstance(raw, dict):
                raise AllowlistError("규칙 #%d 는 객체여야 합니다" % i)
            if raw and all(str(k).startswith("_") for k in raw):
                continue  # {"_comment": "..."} entries document the file
            pattern = raw.get("usage_type_pattern")
            service = raw.get("service", "*")
            cap = raw.get("max_usd_per_day")
            reason = raw.get("reason")
            if not isinstance(pattern, str) or not pattern:
                raise AllowlistError("규칙 #%d: usage_type_pattern(문자열) 필수" % i)
            if not isinstance(service, str) or not service:
                raise AllowlistError("규칙 #%d: service 는 문자열" % i)
            if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap < 0:
                raise AllowlistError("규칙 #%d: max_usd_per_day(0 이상 숫자) 필수" % i)
            if not isinstance(reason, str) or not reason.strip():
                raise AllowlistError("규칙 #%d: reason(왜 허용하는지) 필수" % i)
            rules.append({"service": service, "usage_type_pattern": pattern,
                          "max_usd_per_day": float(cap), "reason": reason.strip()})
    rules.extend(dict(r) for r in BUILTIN_ALLOWLIST)
    return rules


def _glob(value: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(value.lower(), pattern.lower())


def match_allowlist(rules: List[Dict[str, Any]], service: str, usage_type: str,
                    amount: float) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Return (rule, exceeded). exceeded=True when rules match but every cap is too low."""
    matched = [r for r in rules
               if _glob(service, r["service"]) and _glob(usage_type, r["usage_type_pattern"])]
    for r in matched:
        if amount <= r["max_usd_per_day"]:
            return r, False
    if matched:
        return matched[0], True
    return None, False


# --------------------------------------------------------------------------- dates
def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _midnight(day: dt.date) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)


def compute_window(cleanup: dt.date, settle_days: int, lag_hours: float, days: Optional[int],
                   now: dt.datetime) -> Dict[str, Any]:
    """Cost Explorer days are UTC calendar days ([Start, End) with End exclusive).

    Whether the operator wrote cleanup-date as a KST or a UTC date, cleanup finished before
    (cleanup+1) 00:00 UTC, so the first UTC day that is entirely post-cleanup is cleanup+1.
    The cleanup day itself is never judged (it legitimately contains pre-cleanup usage).

    A day X counts as *posted* once now >= (X+1) 00:00 UTC + lag_hours. A verdict needs at
    least one posted post-cleanup day; later, partially posted days are still queried because
    a cost line found there is real.
    """
    today = now.date()
    start = cleanup + dt.timedelta(days=1)
    end = today - dt.timedelta(days=1)  # yesterday UTC, inclusive
    if days:
        start = max(start, end - dt.timedelta(days=days - 1))
    last_posted = (now - dt.timedelta(hours=lag_hours, days=1)).date()
    settle_at = _midnight(cleanup + dt.timedelta(days=settle_days))
    first_posted_at = _midnight(start + dt.timedelta(days=1)) + dt.timedelta(hours=lag_hours)
    earliest = max(settle_at, first_posted_at)
    pending: List[str] = []
    if now < settle_at:
        pending.append("settle")
    if end < start:
        pending.append("empty-window")
    elif last_posted < start:
        pending.append("lag")
    has_days = end >= start
    return {
        "start": start.isoformat(),
        "end": end.isoformat() if has_days else None,
        "end_exclusive": (end + dt.timedelta(days=1)).isoformat() if has_days else None,
        "days": (end - start).days + 1 if has_days else 0,
        "last_posted_day": min(last_posted, end).isoformat() if has_days and last_posted >= start else None,
        "timezone": "UTC",
        "lag_hours": lag_hours,
        "earliest_verdict_utc": earliest.replace(microsecond=0).isoformat(),
        "earliest_verdict_kst": earliest.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
        "queried": False,
        "_pending": pending,
    }


# --------------------------------------------------------------------------- cost explorer
def record_filter(mode: str, values: List[str]) -> Dict[str, Any]:
    dim = {"Dimensions": {"Key": "RECORD_TYPE", "Values": list(values)}}
    return dim if mode == "include" else {"Not": dim}


def fetch_cost_and_usage(ce, start: str, end_exclusive: str,
                         cost_filter: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """Return ({day: {"estimated": bool, "groups": {(service, usage_type): [amount, unit]}}}, requests)."""
    kwargs: Dict[str, Any] = {
        "TimePeriod": {"Start": start, "End": end_exclusive},
        "Granularity": "DAILY",
        "Metrics": [CE_METRIC],
        "Filter": cost_filter,
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"},
                    {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    }
    days: Dict[str, Dict[str, Any]] = {}
    requests = 0
    seen = set()
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        requests += 1
        for r in resp.get("ResultsByTime", []):
            day = r["TimePeriod"]["Start"]
            d = days.setdefault(day, {"estimated": False, "groups": {}})
            # a day can be split over several pages; missing flag = treat as estimated
            d["estimated"] = d["estimated"] or bool(r.get("Estimated", True))
            for g in r.get("Groups", []):
                keys = list(g.get("Keys", [])) + ["?", "?"]
                metric = g.get("Metrics", {}).get(CE_METRIC, {})
                amount = float(metric.get("Amount", "0") or 0)
                unit = metric.get("Unit", "USD")
                slot = d["groups"].setdefault((keys[0], keys[1]), [0.0, unit])
                slot[0] += amount
        token = resp.get("NextPageToken")
        if not token:
            break
        if token in seen:
            raise RuntimeError("Cost Explorer NextPageToken loop")
        seen.add(token)
        kwargs["NextPageToken"] = token
    return days, requests


def region_hint(usage_type: str) -> str:
    head = usage_type.split("-", 1)[0] if "-" in usage_type else ""
    if head in USAGE_TYPE_REGION_PREFIX:
        return USAGE_TYPE_REGION_PREFIX[head]
    if head.isupper() and any(ch.isdigit() for ch in head) and len(head) <= 5:
        return "unknown(%s)" % head
    return "us-east-1/global"


def analyze(days: Dict[str, Dict[str, Any]], threshold: float,
            rules: List[Dict[str, Any]]) -> Tuple[List[dict], List[dict], List[dict]]:
    flagged: List[dict] = []
    allowed: List[dict] = []
    daily: List[dict] = []
    for day in sorted(days):
        info = days[day]
        total = 0.0
        flagged_sum = 0.0
        for (service, usage_type), (amount, unit) in sorted(info["groups"].items()):
            total += amount
            if amount <= threshold:
                continue
            line = {"date": day, "service": service, "usage_type": usage_type,
                    "amount_usd": round(amount, 6), "unit": unit,
                    "estimated": info["estimated"], "region_hint": region_hint(usage_type)}
            rule, exceeded = match_allowlist(rules, service, usage_type, amount)
            if rule and not exceeded:
                line["reason"] = rule["reason"]
                allowed.append(line)
                continue
            if rule and exceeded:
                line["allowlist_exceeded"] = {"max_usd_per_day": rule["max_usd_per_day"],
                                              "reason": rule["reason"]}
            line["hint"] = SERVICE_HINTS.get(service, "")
            flagged.append(line)
            flagged_sum += amount
        daily.append({"date": day, "amount_usd": round(total, 6),
                      "flagged_usd": round(flagged_sum, 6), "estimated": info["estimated"]})
    flagged.sort(key=lambda x: (-x["amount_usd"], x["date"], x["service"], x["usage_type"]))
    return flagged, allowed, daily


def classify_ce_error(e: Exception) -> Dict[str, Any]:
    if isinstance(e, ClientError):
        code = e.response.get("Error", {}).get("Code", "ClientError")
        msg = e.response.get("Error", {}).get("Message", str(e))
        low = msg.lower()
        if code == "DataUnavailableException":
            hint = HINT_CE_DATA_UNAVAILABLE
        elif "not enabled" in low or "not activated" in low or ("cost explorer" in low and "enable" in low):
            hint = HINT_CE_NOT_ENABLED
        elif code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
            hint = HINT_CE_ACCESS
        elif code in ("UnrecognizedClientException", "InvalidClientTokenId", "ExpiredToken",
                      "ExpiredTokenException"):
            hint = HINT_CREDENTIALS
        else:
            hint = ""
        return {"stage": "cost", "code": code, "message": msg, "hint": hint}
    return {"stage": "cost", "code": type(e).__name__, "message": str(e),
            "hint": HINT_CREDENTIALS if "credential" in str(e).lower() else ""}


CE_FALLBACK_CODES = {"AccessDeniedException", "AccessDenied", "UnauthorizedOperation", "UnauthorizedException",
                     "DataUnavailableException"}


def ce_fallback_reason(e: Exception) -> Optional[str]:
    """auto mode: return why Cost Explorer is unusable for this account (-> inventory), or None.

    Covers SCP explicit denies (reseller member accounts), plain AccessDenied, Cost Explorer never
    enabled, and DataUnavailable. Credential, throttling and other errors stay errors (exit 2).
    """
    if not isinstance(e, ClientError):
        return None
    code = e.response.get("Error", {}).get("Code", "")
    low = str(e.response.get("Error", {}).get("Message", "")).lower()
    if "service control policy" in low or "explicit deny" in low:
        return "scp-deny"
    if "not enabled" in low or "not activated" in low:
        return "not-enabled"
    if code == "DataUnavailableException":
        return "data-unavailable"
    if code in CE_FALLBACK_CODES or "accessdenied" in code.lower():
        return "access-denied"
    return None


def probe_cost_explorer(ce, today: dt.date) -> int:
    """One cheap request (yesterday, no grouping) just to learn whether CE is usable. Returns requests."""
    ce.get_cost_and_usage(TimePeriod={"Start": (today - dt.timedelta(days=1)).isoformat(), "End": today.isoformat()},
                          Granularity="DAILY", Metrics=[CE_METRIC])
    return 1


# --------------------------------------------------------------------------- inventory
_client_lock = threading.Lock()


def _client(session, service: str, region: str):
    with _client_lock:  # boto3 Session is not thread-safe; clients are
        return client(session, service, region)


def _items(c, op: str, key: str, **kwargs):
    """Yield every item of a list/describe call, paginating when possible."""
    if c.can_paginate(op):
        for page in c.get_paginator(op).paginate(**kwargs):
            for item in page.get(key, []) or []:
                yield item
        return
    call = getattr(c, op)
    params = dict(kwargs)
    seen = set()
    while True:
        resp = call(**params)
        for item in resp.get(key, []) or []:
            yield item
        token = resp.get("nextToken") or resp.get("NextToken")
        if not token or token in seen:
            return
        seen.add(token)
        params["nextToken" if "nextToken" in resp else "NextToken"] = token


def _get(item: Any, path: Optional[str]) -> Any:
    if path is None:
        return item
    cur = item
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# States after which a resource is gone (still listed for a while by some APIs).
GONE_STATES = {"deleted", "terminated", "deregistered", "delete_complete"}


def _gone(state: Any) -> bool:
    return isinstance(state, str) and state.lower() in GONE_STATES


def _is_deleting(state: Any) -> bool:
    """Transient 'going away' states: still listed as residual, but flagged so the operator re-runs."""
    if not isinstance(state, str):
        return False
    s = state.lower()
    if "fail" in s or "unsuccess" in s:
        return False
    return "delet" in s or "terminat" in s or s == "shutting-down"


def _list(op: str, key: str, ident: Any = None, state: Optional[str] = None,
          skip: Any = None, **kwargs):
    """Declarative check: list `op`, take `ident` (field name or callable) of every item in `key`.

    Items whose `state` is gone (deleted/terminated/...) or for which skip(item) is true are ignored.
    Returns a check function(client, inventory) -> list of ids or (id, state) tuples.
    """
    def fn(c, inv=None):
        out = []
        for it in _items(c, op, key, **kwargs):
            st = _get(it, state) if state else None
            if _gone(st) or (skip is not None and skip(it)):
                continue
            ident_val = ident(it) if callable(ident) else _get(it, ident)
            if ident_val is None:
                continue
            out.append((str(ident_val), st) if st else str(ident_val))
        return out
    fn.spec = {"op": op, "key": key, "kwargs": kwargs,
               "fields": [f.split(".")[0] for f in (ident, state) if isinstance(f, str)]}
    fn.ops = [op]
    return fn


def _uses(*ops):
    """Decorator: declare the client operations a custom check calls (checked against botocore in tests)."""
    def deco(fn):
        fn.ops = list(ops)
        return fn
    return deco


@_uses("describe_instances")
def _ec2_instances(c, inv=None):
    live = ["pending", "running", "shutting-down", "stopping", "stopped"]
    out = []
    for page in c.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": live}]):
        for res in page.get("Reservations", []):
            out.extend((i["InstanceId"], _get(i, "State.Name")) for i in res.get("Instances", []))
    return out


def _default_vpc_ids(c) -> set:
    return {v["VpcId"] for v in _items(c, "describe_vpcs", "Vpcs",
                                       Filters=[{"Name": "is-default", "Values": ["true"]}])}


@_uses("describe_vpcs", "describe_internet_gateways")
def _internet_gateways(c, inv=None):
    """IGWs except the one attached to the default VPC (detached IGWs count)."""
    default = _default_vpc_ids(c)
    out = []
    for g in _items(c, "describe_internet_gateways", "InternetGateways"):
        attached = [a.get("VpcId") for a in g.get("Attachments", []) or [] if a.get("VpcId")]
        if attached and all(v in default for v in attached):
            continue
        out.append(g["InternetGatewayId"])
    return out


@_uses("describe_addresses")
def _elastic_ips(c, inv=None):
    return [a.get("AllocationId") or a.get("PublicIp") for a in c.describe_addresses().get("Addresses", [])]


@_uses("describe_vpn_connections")
def _vpn_connections(c, inv=None):
    return [(v["VpnConnectionId"], v.get("State")) for v in c.describe_vpn_connections().get("VpnConnections", [])
            if not _gone(v.get("State"))]


@_uses("list_clusters", "list_services")
def _ecs_services(c, inv=None):
    out = []
    for arn in _items(c, "list_clusters", "clusterArns"):
        cluster = arn.rsplit("/", 1)[-1]
        for svc in _items(c, "list_services", "serviceArns", cluster=arn):
            out.append("%s/%s" % (cluster, svc.rsplit("/", 1)[-1]))
    return out


@_uses("list_delivery_streams")
def _firehose_streams(c, inv=None):
    out: List[str] = []
    params: Dict[str, Any] = {}
    while True:
        resp = c.list_delivery_streams(**params)
        names = resp.get("DeliveryStreamNames", []) or []
        out.extend(names)
        if not resp.get("HasMoreDeliveryStreams") or not names:
            return out
        params["ExclusiveStartDeliveryStreamName"] = names[-1]


@_uses("list_keys", "describe_key")
def _kms_keys(c, inv=None):
    """Customer-managed keys that are not already scheduled for deletion."""
    out = []
    for k in _items(c, "list_keys", "Keys"):
        try:
            md = c.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
        except ClientError as e:  # unreadable key: not provably AWS-managed -> report it
            out.append((k["KeyId"], "describe-failed:%s" % _err(e)[0]))
            continue
        if md.get("KeyManager") != "CUSTOMER" or md.get("KeyState") in ("PendingDeletion", "PendingReplicaDeletion"):
            continue
        out.append(md["KeyId"])
    return out


@_uses("describe_alarms")
def _cloudwatch_alarms(c, inv=None):
    out = []
    types = ["MetricAlarm", "CompositeAlarm", "LogAlarm"]
    for page in c.get_paginator("describe_alarms").paginate(AlarmTypes=types):
        for key in ("MetricAlarms", "CompositeAlarms", "LogAlarms"):
            out.extend(a["AlarmName"] for a in page.get(key, []) or [])
    return out


# Log groups created by the reseller's Organization / Control Tower, not by students.
AWS_MANAGED_LOG_GROUP_PREFIXES = ("aws-controltower/", "/aws/controltower/", "/aws/lambda/aws-controltower-")


@_uses("list_stacks", "list_stack_resources")
def _cloudformation_stacks(c, inv=None):
    """Every stack except DELETE_COMPLETE. Physical resources of protected stacks become protected."""
    statuses = [s for s in c.meta.service_model.shape_for("StackStatus").enum if s != "DELETE_COMPLETE"]
    out = []
    for s in _items(c, "list_stacks", "StackSummaries", StackStatusFilter=statuses):
        name = s["StackName"]
        if inv is not None and inv.is_protected(name):
            try:
                inv.add_protected_ids(r.get("PhysicalResourceId") for r in _items(
                    c, "list_stack_resources", "StackResourceSummaries", StackName=s.get("StackId") or name))
            except Exception as e:  # the stack itself is still protected
                log("보호 스택 %s 리소스 목록 실패: %s" % (name, _err(e)[0]))
        out.append((name, s.get("StackStatus")))
    return out


def _sagemaker_app_id(a: dict) -> str:
    parts = (a.get("DomainId"), a.get("UserProfileName") or a.get("SpaceName"), a.get("AppType"), a.get("AppName"))
    return "/".join(p for p in parts if p)


EMR_ACTIVE = ["STARTING", "BOOTSTRAPPING", "RUNNING", "WAITING", "TERMINATING"]

# (inventory type, boto3 service, check(client, inventory) -> list of ids or (id, state)).
# ec2_instances first (it detects dead/disabled regions), cloudformation_stacks second (it collects
# the physical IDs of protected stacks before the other checks of the same region run).
REGIONAL_CHECKS = [
    ("ec2_instances", "ec2", _ec2_instances),
    ("cloudformation_stacks", "cloudformation", _cloudformation_stacks),
    # --- EC2 / VPC
    ("ebs_volumes", "ec2", _list("describe_volumes", "Volumes", "VolumeId", "State")),
    ("ebs_snapshots", "ec2", _list("describe_snapshots", "Snapshots", "SnapshotId", OwnerIds=["self"])),
    ("amis", "ec2", _list("describe_images", "Images", "ImageId", "State", Owners=["self"])),
    ("elastic_ips", "ec2", _elastic_ips),
    ("nat_gateways", "ec2", _list("describe_nat_gateways", "NatGateways", "NatGatewayId", "State",
                                  skip=lambda n: n.get("State") == "failed")),
    ("vpc_endpoints", "ec2", _list("describe_vpc_endpoints", "VpcEndpoints", "VpcEndpointId", "State",
                                   skip=lambda e: e.get("VpcEndpointType") == "Gateway"
                                   or str(e.get("State", "")).lower() in ("rejected", "failed", "expired"))),
    ("vpcs", "ec2", _list("describe_vpcs", "Vpcs", "VpcId", skip=lambda v: v.get("IsDefault"))),
    ("subnets", "ec2", _list("describe_subnets", "Subnets", "SubnetId", skip=lambda s: s.get("DefaultForAz"))),
    ("security_groups", "ec2", _list("describe_security_groups", "SecurityGroups",
                                     lambda g: "%s/%s" % (g["GroupId"], g.get("GroupName", "")),
                                     skip=lambda g: g.get("GroupName") == "default")),
    ("internet_gateways", "ec2", _internet_gateways),
    ("transit_gateways", "ec2", _list("describe_transit_gateways", "TransitGateways", "TransitGatewayId", "State")),
    ("vpn_connections", "ec2", _vpn_connections),
    ("load_balancers_v2", "elbv2", _list("describe_load_balancers", "LoadBalancers", "LoadBalancerName", "State.Code")),
    ("load_balancers_classic", "elb", _list("describe_load_balancers", "LoadBalancerDescriptions", "LoadBalancerName")),
    ("autoscaling_groups", "autoscaling", _list("describe_auto_scaling_groups", "AutoScalingGroups",
                                                "AutoScalingGroupName", "Status")),
    # --- databases / caches / analytics
    ("rds_instances", "rds", _list("describe_db_instances", "DBInstances", "DBInstanceIdentifier", "DBInstanceStatus")),
    ("rds_clusters", "rds", _list("describe_db_clusters", "DBClusters", "DBClusterIdentifier", "Status")),
    ("rds_snapshots_manual", "rds", _list("describe_db_snapshots", "DBSnapshots", "DBSnapshotIdentifier",
                                          SnapshotType="manual")),
    ("rds_cluster_snapshots_manual", "rds", _list("describe_db_cluster_snapshots", "DBClusterSnapshots",
                                                  "DBClusterSnapshotIdentifier", SnapshotType="manual")),
    ("dynamodb_tables", "dynamodb", _list("list_tables", "TableNames")),
    ("elasticache_clusters", "elasticache", _list("describe_cache_clusters", "CacheClusters", "CacheClusterId",
                                                  "CacheClusterStatus")),
    ("elasticache_serverless_caches", "elasticache", _list("describe_serverless_caches", "ServerlessCaches",
                                                           "ServerlessCacheName", "Status")),
    ("memorydb_clusters", "memorydb", _list("describe_clusters", "Clusters", "Name", "Status")),
    ("redshift_clusters", "redshift", _list("describe_clusters", "Clusters", "ClusterIdentifier", "ClusterStatus")),
    ("redshift_serverless_workgroups", "redshift-serverless", _list("list_workgroups", "workgroups",
                                                                    "workgroupName", "status")),
    ("redshift_serverless_namespaces", "redshift-serverless", _list("list_namespaces", "namespaces",
                                                                    "namespaceName", "status")),
    ("opensearch_domains", "opensearch", _list("list_domain_names", "DomainNames", "DomainName")),
    ("opensearch_serverless_collections", "opensearchserverless",
     _list("list_collections", "collectionSummaries", lambda x: x.get("name") or x.get("id"), "status")),
    ("kinesis_streams", "kinesis", _list("list_streams", "StreamSummaries", "StreamName", "StreamStatus")),
    ("firehose_streams", "firehose", _firehose_streams),
    ("msk_clusters", "kafka", _list("list_clusters_v2", "ClusterInfoList", "ClusterName", "State")),
    ("glue_jobs", "glue", _list("get_jobs", "Jobs", "Name")),
    ("glue_crawlers", "glue", _list("get_crawlers", "Crawlers", "Name")),
    ("emr_clusters", "emr", _list("list_clusters", "Clusters", lambda x: "%s/%s" % (x["Id"], x.get("Name", "")),
                                  ClusterStates=EMR_ACTIVE)),
    ("kendra_indexes", "kendra", _list("list_indices", "IndexConfigurationSummaryItems",
                                       lambda x: x.get("Name") or x.get("Id"), "Status")),
    ("dms_replication_instances", "dms", _list("describe_replication_instances", "ReplicationInstances",
                                               "ReplicationInstanceIdentifier", "ReplicationInstanceStatus")),
    # --- compute / containers / serverless
    ("lambda_functions", "lambda", _list("list_functions", "Functions", "FunctionName")),
    ("apigateway_rest_apis", "apigateway", _list("get_rest_apis", "items",
                                                 lambda x: "%s/%s" % (x["id"], x.get("name", "")))),
    ("apigateway_v2_apis", "apigatewayv2", _list("get_apis", "Items", lambda x: "%s/%s" % (x["ApiId"], x.get("Name", "")))),
    ("ecs_clusters", "ecs", _list("list_clusters", "clusterArns", lambda a: a.rsplit("/", 1)[-1])),
    ("ecs_services", "ecs", _ecs_services),
    ("eks_clusters", "eks", _list("list_clusters", "clusters")),
    ("ecr_repositories", "ecr", _list("describe_repositories", "repositories", "repositoryName")),
    ("apprunner_services", "apprunner", _list("list_services", "ServiceSummaryList", "ServiceName", "Status")),
    ("elasticbeanstalk_environments", "elasticbeanstalk", _list("describe_environments", "Environments",
                                                                "EnvironmentName", "Status", IncludeDeleted=False)),
    ("lightsail_instances", "lightsail", _list("get_instances", "instances", "name", "state.name")),
    ("lightsail_databases", "lightsail", _list("get_relational_databases", "relationalDatabases", "name", "state")),
    ("workspaces", "workspaces", _list("describe_workspaces", "Workspaces", "WorkspaceId", "State")),
    ("directory_service_directories", "ds", _list("describe_directories", "DirectoryDescriptions",
                                                  lambda d: "%s/%s" % (d["DirectoryId"], d.get("Name", "")), "Stage")),
    ("mwaa_environments", "mwaa", _list("list_environments", "Environments")),
    ("amplify_apps", "amplify", _list("list_apps", "apps", lambda a: "%s/%s" % (a["appId"], a.get("name", "")))),
    ("stepfunctions_state_machines", "stepfunctions", _list("list_state_machines", "stateMachines", "name")),
    ("mq_brokers", "mq", _list("list_brokers", "BrokerSummaries", "BrokerName", "BrokerState")),
    # --- storage
    ("efs_file_systems", "efs", _list("describe_file_systems", "FileSystems", "FileSystemId", "LifeCycleState")),
    ("fsx_file_systems", "fsx", _list("describe_file_systems", "FileSystems", "FileSystemId", "Lifecycle")),
    # --- ML / AI
    ("sagemaker_endpoints", "sagemaker", _list("list_endpoints", "Endpoints", "EndpointName", "EndpointStatus")),
    ("sagemaker_notebook_instances", "sagemaker", _list("list_notebook_instances", "NotebookInstances",
                                                        "NotebookInstanceName", "NotebookInstanceStatus")),
    ("sagemaker_domains", "sagemaker", _list("list_domains", "Domains",
                                             lambda d: "%s/%s" % (d["DomainId"], d.get("DomainName", "")), "Status")),
    ("sagemaker_apps", "sagemaker", _list("list_apps", "Apps", _sagemaker_app_id, "Status")),
    ("bedrock_custom_models", "bedrock", _list("list_custom_models", "modelSummaries",
                                               lambda m: m.get("modelName") or m.get("modelArn"))),
    ("bedrock_imported_models", "bedrock", _list("list_imported_models", "modelSummaries",
                                                 lambda m: m.get("modelName") or m.get("modelArn"))),
    ("bedrock_provisioned_throughputs", "bedrock", _list("list_provisioned_model_throughputs", "provisionedModelSummaries",
                                                         lambda p: p.get("provisionedModelName") or p.get("provisionedModelArn"),
                                                         "status")),
    ("bedrock_agents", "bedrock-agent", _list("list_agents", "agentSummaries",
                                              lambda a: "%s/%s" % (a["agentId"], a.get("agentName", "")), "agentStatus")),
    ("bedrock_knowledge_bases", "bedrock-agent", _list("list_knowledge_bases", "knowledgeBaseSummaries",
                                                       lambda k: "%s/%s" % (k["knowledgeBaseId"], k.get("name", "")),
                                                       "status")),
    # --- integration / security / ops
    ("sqs_queues", "sqs", _list("list_queues", "QueueUrls", lambda u: u.rstrip("/").rsplit("/", 1)[-1])),
    ("sns_topics", "sns", _list("list_topics", "Topics", lambda t: t["TopicArn"].rsplit(":", 1)[-1])),
    ("secretsmanager_secrets", "secretsmanager", _list("list_secrets", "SecretList", "Name")),
    ("kms_customer_keys", "kms", _kms_keys),
    ("cloudwatch_log_groups", "logs", _list("describe_log_groups", "logGroups", "logGroupName",
                                            skip=lambda g: str(g.get("logGroupName", "")).startswith(
                                                AWS_MANAGED_LOG_GROUP_PREFIXES))),
    ("cloudwatch_alarms", "cloudwatch", _cloudwatch_alarms),
    ("route53_resolver_endpoints", "route53resolver", _list("list_resolver_endpoints", "ResolverEndpoints",
                                                            lambda r: "%s/%s" % (r["Id"], r.get("Name", "")), "Status")),
    ("network_firewalls", "network-firewall", _list("list_firewalls", "Firewalls", "FirewallName")),
]


@_uses("list_distributions")
def _cloudfront_distributions(c, inv=None):
    out = []
    for page in c.get_paginator("list_distributions").paginate():
        for d in (page.get("DistributionList") or {}).get("Items", []) or []:
            out.append(("%s/%s" % (d["Id"], d.get("DomainName", "")), d.get("Status")))
    return out


@_uses("list_users")
def _iam_users(c, inv=None):
    return [u["UserName"] for u in _items(c, "list_users", "Users")]


SERVICE_ROLE_PATHS = ("/aws-service-role/", "/aws-reserved/")


@_uses("list_roles")
def _iam_roles(c, inv=None):
    """Roles except service-linked (/aws-service-role/) and SSO-reserved (/aws-reserved/) ones."""
    return [r["RoleName"] for r in _items(c, "list_roles", "Roles")
            if not str(r.get("Path", "/")).startswith(SERVICE_ROLE_PATHS) and r["RoleName"] != ROLE_NAME]


# Global services (queried once via us-east-1, recorded under "global"); S3 is separate (per bucket region).
GLOBAL_CHECKS = [
    ("cloudfront_distributions", "cloudfront", _cloudfront_distributions),
    ("route53_hosted_zones", "route53", _list("list_hosted_zones", "HostedZones",
                                              lambda z: "%s/%s" % (z["Name"], z["Id"].rsplit("/", 1)[-1]))),
    ("iam_users", "iam", _iam_users),
    ("iam_roles", "iam", _iam_roles),
]

MAX_IDS = 20

# --- error classification -----------------------------------------------------------------
KIND_NA = "not_applicable"
KIND_BLOCKED = "blocked_by_policy"
KIND_ERROR = "error"

# Service/API not offered (or region not enabled) -> not applicable, never a failure.
NA_CODES = {"InvalidAction", "UnrecognizedClientException", "InvalidClientTokenId", "AuthFailure", "OptInRequired",
            "SubscriptionRequiredException", "SubscriptionRequired", "UnknownOperationException",
            "UnsupportedOperation", "UnsupportedOperationException"}
NA_EXCEPTIONS = {"EndpointConnectionError", "UnknownEndpointError", "EndpointResolutionError", "UnknownRegionError",
                 "NoRegionError", "UnknownEndpoint"}
NA_MESSAGES = ("not authorized for region", "not authorized to use this region", "not supported in this region",
               "not supported in the region", "not supported in region", "not available in this region",
               "not available in the region", "not available in region", "region is not enabled",
               "region not enabled", "needs a subscription",
               # Bedrock features missing in a region answer ValidationException "Unknown operation" or
               # AccessDenied "...not authorized to invoke this API operation" (seen live, 2026-10-15).
               "unknown operation", "not authorized to invoke this api operation")
# Checks whose features only exist in some regions: a plain AccessDenied there (no SCP wording) is region
# gating, not a policy. Seen live: bedrock imported/custom models answered AccessDenied in exactly the
# regions without Custom Model Import.
REGION_GATED_CHECK_PREFIXES = ("bedrock_",)
# Denied by a policy (with AdministratorAccess this is an SCP or permission boundary) -> counted separately.
BLOCKED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "UnauthorizedException",
                 "AuthorizationError", "AuthorizationErrorException", "Forbidden", "ForbiddenException",
                 "NotAuthorized"}
CREDENTIAL_CODES = {"ExpiredToken", "ExpiredTokenException", "RequestExpired", "SignatureDoesNotMatch",
                    "InvalidSignatureException", "InvalidAccessKeyId"}
# A dead or disabled region: after ec2_instances fails like this, the rest of the region is n/a.
REGION_DEAD_CODES = {"AuthFailure", "OptInRequired", "UnrecognizedClientException", "InvalidClientTokenId",
                     "EndpointConnectionError"}


def _err(e: Exception) -> Tuple[str, str]:
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        return err.get("Code", "ClientError"), err.get("Message", str(e))
    return type(e).__name__, str(e)


def classify_check_error(e: Exception, rtype: str = "") -> Tuple[str, str, str]:
    """-> (kind, code, message). kind: not_applicable | blocked_by_policy | error."""
    code, msg = _err(e)
    low = str(msg).lower()
    if type(e).__name__ in NA_EXCEPTIONS or code in NA_CODES:
        return KIND_NA, code, msg
    if code in CREDENTIAL_CODES:
        return KIND_ERROR, code, msg
    if "service control policy" in low or "explicit deny" in low:
        return KIND_BLOCKED, code + " (SCP)", msg
    if any(m in low for m in NA_MESSAGES):
        return KIND_NA, code, msg
    status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(e, ClientError) else None
    if code in BLOCKED_CODES or "accessdenied" in code.lower() or status == 403:
        if rtype.startswith(REGION_GATED_CHECK_PREFIXES):
            return KIND_NA, code, msg
        return KIND_BLOCKED, code, msg
    return KIND_ERROR, code, msg


class Inventory:
    def __init__(self, protected_prefixes: Tuple[str, ...] = PROTECTED_PREFIXES,
                 protected_names: Tuple[str, ...] = ()) -> None:
        self.protected_prefixes = tuple(protected_prefixes)
        self.protected_names = set(protected_names)  # exact names (e.g. the caller's IAM user)
        self.protected_ids: set = set()               # physical IDs of protected CloudFormation stacks
        self.counts: Dict[str, Dict[str, int]] = {}
        self.items: Dict[str, Dict[str, List[str]]] = {}
        self.protected: Dict[str, Dict[str, List[str]]] = {}
        self.deleting: Dict[str, Dict[str, int]] = {}
        self.unchecked: Dict[str, Dict[str, str]] = {}
        self.not_applicable: Dict[str, Dict[str, str]] = {}
        self.blocked: Dict[str, Dict[str, str]] = {}
        self.errors: List[Dict[str, Any]] = []
        self.checked = 0
        self._lock = threading.Lock()

    # -- protection
    def add_protected_ids(self, ids) -> None:
        """Physical IDs come as names, ARNs or URLs (SQS): keep the full ID and its last segment."""
        new = set()
        for i in ids:
            if not i:
                continue
            s = str(i)
            new.add(s)
            new.add(re.split(r"[/:]", s.rstrip("/"))[-1])
        with self._lock:
            self.protected_ids.update(x for x in new if len(x) >= 8)

    def is_protected(self, ident: str) -> bool:
        s = str(ident)
        if s in self.protected_names or s.startswith(self.protected_prefixes) or s in self.protected_ids:
            return True
        for seg in re.split(r"[/:]", s):
            if seg and (seg.startswith(self.protected_prefixes) or seg in self.protected_ids
                        or seg in self.protected_names):
                return True
        return False

    # -- results
    def record(self, region: str, rtype: str, found: List[Any]) -> None:
        keep: List[str] = []
        prot: List[str] = []
        deleting = 0
        for x in found:
            ident, st = x if isinstance(x, tuple) else (x, None)
            if ident is None:
                continue
            ident = str(ident)
            if self.is_protected(ident):
                prot.append(ident)
                continue
            if _is_deleting(st):
                deleting += 1
                ident = "%s (%s)" % (ident, st)
            keep.append(ident)
        with self._lock:
            self.checked += 1
            self.counts.setdefault(region, {})
            self.counts[region][rtype] = self.counts[region].get(rtype, 0) + len(keep)
            if keep:
                lst = self.items.setdefault(region, {}).setdefault(rtype, [])
                lst.extend(keep[:max(0, MAX_IDS - len(lst))])
            if deleting:
                per = self.deleting.setdefault(region, {})
                per[rtype] = per.get(rtype, 0) + deleting
            if prot:
                self.protected.setdefault(region, {}).setdefault(rtype, []).extend(prot)

    def fail(self, region: str, rtype: str, code: str, message: str, kind: str = KIND_ERROR) -> None:
        with self._lock:
            if kind == KIND_NA:
                self.not_applicable.setdefault(region, {})[rtype] = code
            elif kind == KIND_BLOCKED:
                self.blocked.setdefault(region, {})[rtype] = code
            else:
                self.unchecked.setdefault(region, {})[rtype] = code
                self.errors.append({"stage": "inventory", "region": region, "check": rtype,
                                    "code": code, "message": str(message)[:300]})

    def fail_exc(self, region: str, rtype: str, e: Exception) -> str:
        kind, code, msg = classify_check_error(e, rtype)
        self.fail(region, rtype, code, msg, kind)
        return kind

    def total(self) -> int:
        return sum(n for per in self.counts.values() for n in per.values())

    @staticmethod
    def _n(d: Dict[str, Dict[str, Any]]) -> int:
        return sum(len(per) for per in d.values())

    def blocked_count(self) -> int:
        return self._n(self.blocked)

    def not_applicable_count(self) -> int:
        return self._n(self.not_applicable)

    def examples(self, n: int) -> Dict[str, Dict[str, List[str]]]:
        return {r: {t: ids[:n] for t, ids in per.items() if ids} for r, per in self.items.items()}


def _available(session, service: str, cache: Dict[str, List[str]]) -> List[str]:
    if service not in cache:
        try:
            cache[service] = session.get_available_regions(service) or []
        except Exception:  # metadata problems must not stop the inventory
            cache[service] = []
    return cache[service]


def inventory_region(session, region: str, inv: Inventory, avail_cache: Dict[str, List[str]]) -> None:
    dead: Optional[Tuple[str, str]] = None
    clients: Dict[str, Any] = {}
    for rtype, service, fn in REGIONAL_CHECKS:
        if dead:
            inv.fail(region, rtype, dead[0], dead[1], KIND_NA)
            continue
        avail = _available(session, service, avail_cache)
        if avail and region not in avail:
            inv.fail(region, rtype, "NotAvailableInRegion", "botocore endpoint metadata에 이 리전이 없음", KIND_NA)
            continue
        try:
            if service not in clients:
                clients[service] = _client(session, service, region)
            inv.record(region, rtype, fn(clients[service], inv))
        except Exception as e:  # any failure = n/a, blocked or "could not check" - never a crash
            kind = inv.fail_exc(region, rtype, e)
            code = _err(e)[0]
            if rtype == "ec2_instances" and kind == KIND_NA and code in REGION_DEAD_CODES:
                dead = (code, "리전 사용 불가로 나머지 검사 생략: " + str(_err(e)[1])[:200])


def inventory_s3(session, inv: Inventory) -> None:
    try:
        s3 = _client(session, "s3", "us-east-1")
        if s3.can_paginate("list_buckets"):
            # PageSize sends MaxBuckets, which makes S3 include BucketRegion (no per-bucket lookups)
            pages = s3.get_paginator("list_buckets").paginate(PaginationConfig={"PageSize": 1000})
            buckets = [b for page in pages for b in page.get("Buckets", [])]
        else:
            buckets = s3.list_buckets().get("Buckets", [])
    except Exception as e:
        inv.fail_exc("global", "s3_buckets", e)
        return
    by_region: Dict[str, List[str]] = {}
    for b in buckets:
        region = b.get("BucketRegion")
        if not region:
            try:
                loc = s3.get_bucket_location(Bucket=b["Name"]).get("LocationConstraint")
                region = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)
            except Exception:
                region = "unknown"
        by_region.setdefault(region, []).append(b["Name"])
    if not by_region:
        inv.record("global", "s3_buckets", [])
    for region, names in by_region.items():
        inv.record(region, "s3_buckets", names)


def inventory_global(session, inv: Inventory) -> None:
    for rtype, service, fn in GLOBAL_CHECKS:
        try:
            inv.record("global", rtype, fn(_client(session, service, "us-east-1"), inv))
        except Exception as e:
            inv.fail_exc("global", rtype, e)
    inventory_s3(session, inv)


def _caller_names(session) -> Tuple[str, ...]:
    """IAM user / role name of whoever runs this (protected: CONTRACTS 'protect the running user')."""
    try:
        arn = caller_arn(session)
    except Exception:
        return ()
    kind, _, rest = str(arn).split(":", 5)[-1].partition("/")  # user/<path>/name | assumed-role/Role/sess | root
    if kind == "user" and rest:
        return (rest.rsplit("/", 1)[-1],)
    if kind == "assumed-role" and rest:
        return (rest.split("/", 1)[0],)
    return ()


def resolve_inventory_regions(session, args, errors: List[Dict[str, Any]]) -> List[str]:
    if not args.all_regions:
        return [r for r in parse_regions(args.regions) if r != "global"]
    try:
        acct = client(session, "account", "us-east-1")
        regions = [r["RegionName"] for r in _items(acct, "list_regions", "Regions",
                                                   RegionOptStatusContains=["ENABLED", "ENABLED_BY_DEFAULT"])]
        if regions:
            return sorted(regions)
        raise RuntimeError("account:ListRegions returned no regions")
    except Exception as e:
        code, msg = _err(e)
        errors.append({"stage": "regions", "code": code, "message": msg,
                       "hint": "account:ListRegions 실패 → ec2:DescribeRegions(활성 리전)로 대체"})
        log("account:ListRegions 실패(%s) → ec2:DescribeRegions로 대체" % code)
    try:
        ec2 = client(session, "ec2", "us-east-1")
        return sorted(r["RegionName"] for r in ec2.describe_regions().get("Regions", []))
    except Exception as e:
        code, msg = _err(e)
        errors.append({"stage": "regions", "code": code, "message": msg,
                       "hint": "리전 목록을 못 가져와 --regions 값을 사용"})
        return [r for r in parse_regions(args.regions) if r != "global"]


def run_inventory(session, regions: List[str],
                  protected_prefixes: Tuple[str, ...] = PROTECTED_PREFIXES) -> Inventory:
    inv = Inventory(protected_prefixes, _caller_names(session))
    cache: Dict[str, List[str]] = {}
    for svc in {s for _, s, _ in REGIONAL_CHECKS}:
        _available(session, svc, cache)  # warm the cache before threads start
    workers = max(1, min(8, len(regions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda r: inventory_region(session, r, inv, cache), regions))
    # after the regions: protected stacks of every region are known (IAM roles, buckets they own)
    inventory_global(session, inv)
    return inv


# --------------------------------------------------------------------------- verdict & output
def decide(flagged: List[dict], inv_total: int, fatal: bool, pending: List[str]) -> str:
    """Cost mode. Priority: RESIDUAL (known leftovers) > ERROR > PENDING > CLEAN."""
    if flagged or inv_total > 0:
        return STATUS_RESIDUAL
    if fatal:
        return STATUS_ERROR
    if pending:
        return STATUS_PENDING
    return STATUS_CLEAN


def decide_inventory(inv_total: int, checked: int) -> str:
    """Inventory mode: RESIDUAL if anything unprotected was found; ERROR only if nothing could be checked."""
    if inv_total > 0:
        return STATUS_RESIDUAL
    if checked == 0:
        return STATUS_ERROR
    return STATUS_CLEAN


PENDING_TEXT = {
    "settle": "정리일 + settle-days(UTC 00:00) 가 아직 지나지 않음",
    "empty-window": "정리 다음 날(UTC)이 아직 끝나지 않아 검사할 날짜가 없음",
    "lag": "정리 다음 날(UTC) 데이터가 아직 Cost Explorer에 반영되지 않았을 시간 (--lag-hours)",
    "no-data": "정리 다음 날(UTC) 데이터가 아직 Cost Explorer 응답에 없음",
    "estimated": "마지막 날 데이터가 Estimated=true (--require-final)",
}


def _render_cost(res: Dict[str, Any], out: List[str]) -> None:
    w = res["window"]
    span = "%s ~ %s (UTC, %d일)" % (w["start"], w["end"], w["days"]) if w.get("end") else "%s ~ (아직 없음)" % w["start"]
    out.append("정리일: %s   검사 구간: %s   최초 판정 가능: %s" % (
        res["cleanup_date"], span, w["earliest_verdict_kst"]))
    out.append("Cost Explorer 요청: %d회 (≈ $%.2f)" % (res["ce_requests"], res["ce_requests"] * 0.01))
    for reason in res["pending_reasons"]:
        out.append("  대기 사유: " + PENDING_TEXT.get(reason, reason))
    if res["daily_totals"]:
        rf = res["record_filter"]
        out.append("일별 합계 (%s, RECORD_TYPE %s %s):" % (
            CE_METRIC, "포함" if rf["mode"] == "include" else "제외", ",".join(rf["values"])))
        for d in res["daily_totals"]:
            out.append("  %s  $%.4f  (허용 목록 밖 $%.4f)%s" % (
                d["date"], d["amount_usd"], d["flagged_usd"], "  [추정치]" if d["estimated"] else ""))
        if res.get("data_final") is False:
            out.append("  ※ Estimated=true: 월 마감 전 추정치라 금액이 조금 바뀔 수 있음 (판정에는 영향 없음)")
    if res["flagged"]:
        out.append("허용 목록 밖 사용 요금 (금액순, 최대 20건 / 전체 %d건):" % len(res["flagged"]))
        for f in res["flagged"][:20]:
            extra = " ← %s" % f["hint"] if f.get("hint") else ""
            if f.get("allowlist_exceeded"):
                extra += " (허용 한도 $%.2f 초과)" % f["allowlist_exceeded"]["max_usd_per_day"]
            out.append("  %s  $%-9.4f %s | %s [%s]%s" % (
                f["date"], f["amount_usd"], f["service"], f["usage_type"], f["region_hint"], extra))
    if res["allowed"]:
        out.append("허용 목록으로 통과: %d건 (합계 $%.4f)" % (
            len(res["allowed"]), sum(a["amount_usd"] for a in res["allowed"])))


def _render_inventory(res: Dict[str, Any], out: List[str]) -> None:
    found = {r: {t: n for t, n in per.items() if n} for r, per in res["inventory"].items()}
    found = {r: per for r, per in found.items() if per}
    scope = "%d개 리전 + global" % len(res["inventory_regions"])
    if found:
        regions = sorted(found, key=lambda r: (r == "global", r))
        types = sorted({t for per in found.values() for t in per})
        w0 = max(len("유형"), max(len(t) for t in types))
        widths = [max(len(r), 3) for r in regions]
        out.append("인벤토리 잔여물 (%s, 0이 아닌 것만):" % scope)
        out.append("  " + "유형".ljust(w0 - 2) + "  " + "  ".join(r.rjust(wd) for r, wd in zip(regions, widths)))
        for t in types:
            cells = [str(found[r].get(t, "-")).rjust(wd) for r, wd in zip(regions, widths)]
            out.append("  " + t.ljust(w0) + "  " + "  ".join(cells))
        n = res.get("max_examples", 5)
        if n:
            out.append("예시 ID (유형별 최대 %d개):" % n)
            for region in regions:
                for rtype, ids in sorted(res["examples"].get(region, {}).items()):
                    more = found[region].get(rtype, 0) - len(ids)
                    out.append("  [%s] %s: %s%s" % (region, rtype, ", ".join(ids), " 외 %d개" % more if more > 0 else ""))
        deleting = sum(n for per in res.get("inventory_deleting", {}).values() for n in per.values())
        if deleting:
            out.append("  ※ 이 중 %d개는 삭제/종료 진행 중 상태 → 몇 분 뒤 다시 실행하면 사라질 수 있음" % deleting)
    else:
        out.append("인벤토리 잔여물: 없음 (%s)" % scope)
    prot = sum(len(ids) for per in res["inventory_protected"].values() for ids in per.values())
    if prot:
        out.append("보호 대상으로 제외: %d개 (edu-bootstrap/EduCleanupRole/edu-killswitch/조직 관리 리소스 등)" % prot)
    if res["blocked_by_policy_count"]:
        out.append("정책(SCP 등)으로 조회 차단: %d건 (잔여물로 세지 않음, 이 범위는 확인 못 함)" % res["blocked_by_policy_count"])
        for region in sorted(res["blocked_by_policy"]):
            per = res["blocked_by_policy"][region]
            out.append("  %s: %s" % (region, ", ".join(sorted(per))))
    if res["not_applicable_count"]:
        out.append("해당 없음(n/a, 리전에 없는 서비스·비활성 리전): %d건" % res["not_applicable_count"])
    if res["inventory_unchecked"]:
        out.append("확인 못 함 (오류):")
        for region in sorted(res["inventory_unchecked"]):
            per = res["inventory_unchecked"][region]
            out.append("  %s: %s" % (region, ", ".join("%s(%s)" % kv for kv in sorted(per.items()))))


def render_summary(res: Dict[str, Any]) -> str:
    out: List[str] = []
    mode_txt = {MODE_INVENTORY: "인벤토리(Cost Explorer 미사용)", MODE_COST: "비용(Cost Explorer)"}.get(res.get("mode"), "?")
    if res.get("ce_fallback"):
        mode_txt += " ← auto: Cost Explorer 사용 불가로 대체"
    out.append("[verify] 계정 %s  판정: %s (exit %d)  모드: %s" % (
        res["account_id"], res["status"], res["exit_code"], mode_txt))
    if res.get("mode") == MODE_COST:
        _render_cost(res, out)
    elif res.get("cleanup_date"):
        out.append("정리일: %s (인벤토리 모드는 대기 없이 바로 판정)" % res["cleanup_date"])
    if res.get("ce_fallback"):
        fb = res["ce_fallback"]
        out.append("Cost Explorer: %s %s" % (fb.get("code"), str(fb.get("message", ""))[:160]))
    if res.get("inventory_ran"):
        _render_inventory(res, out)
    for wmsg in res.get("warnings", []):
        out.append("주의: " + wmsg)
    for e in res["errors"]:
        if e.get("stage") == "inventory":
            continue
        out.append("오류[%s] %s: %s" % (e.get("stage"), e.get("code"), e.get("message", "")[:200]))
        if e.get("hint"):
            out.append("  → " + e["hint"])
    if res["status"] == STATUS_RESIDUAL:
        if res.get("mode") == MODE_INVENTORY:
            out.append("다음 할 일: 위 리소스를 지우거나(필요하면 해당 리전/유형을 넣어 aws-nuke 재실행) "
                       "바로 다시 --mode inventory 로 검증하세요.")
        else:
            out.append("다음 할 일: 위 리전/서비스의 리소스를 지우거나 해당 리전을 포함해 aws-nuke를 다시 돌린 뒤, "
                       "그 날짜를 --cleanup-date로 넣어 D+2에 다시 검증하세요.")
    elif res["status"] == STATUS_PENDING:
        out.append("다음 할 일: %s 이후 다시 실행하세요." % res["window"]["earliest_verdict_kst"])
    elif res["status"] == STATUS_CLEAN and res.get("mode") == MODE_INVENTORY:
        out.append("참고: 목록에 없는 리소스 유형은 잡지 못합니다. 비용 확정은 리셀러 청구서로 확인하세요.")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as e:  # never let a traceback turn into exit 1 (= RESIDUAL)
        code, msg = _err(e)
        log("예상치 못한 오류: %s %s" % (code, msg))
        write_json(args.json_out, {"module": MODULE, "account_id": None, "generated_at": now_iso(),
                                   "applied": False, "status": STATUS_ERROR, "exit_code": EXIT_ERROR,
                                   "mode": None, "mode_requested": args.mode,
                                   "errors": [{"stage": "internal", "code": code, "message": msg, "hint": ""}]})
        print("[verify] 판정: ERROR (exit 2) - 예상치 못한 오류: %s %s" % (code, msg))
        return EXIT_ERROR


def _run(args: argparse.Namespace) -> int:
    if args.apply:
        log("verify 는 읽기 전용입니다. --apply 는 무시됩니다.")

    now = args.now or utc_now()
    today = now.date()
    inventory_only = args.mode == MODE_INVENTORY
    cleanup = args.cleanup_date or (None if inventory_only else now.astimezone(KST).date())
    if args.record_types:
        rf = {"mode": "include", "values": parse_regions(args.record_types)}
    else:
        rf = {"mode": "exclude",
              "values": parse_regions(args.exclude_record_types) or parse_regions(DEFAULT_EXCLUDE_RECORD_TYPES)}

    res: Dict[str, Any] = {
        "module": MODULE, "account_id": None, "generated_at": now_iso(), "applied": False,
        "status": STATUS_ERROR, "exit_code": EXIT_ERROR,
        "mode": MODE_INVENTORY if inventory_only else MODE_COST, "mode_requested": args.mode, "ce_fallback": None,
        "cleanup_date": cleanup.isoformat() if cleanup else None, "now_utc": now.replace(microsecond=0).isoformat(),
        "today_utc": today.isoformat(), "settle_days": args.settle_days, "threshold_usd": args.threshold_usd,
        "record_filter": rf, "metric": CE_METRIC, "require_final": args.require_final,
        "window": {}, "ce_requests": 0, "data_final": None, "pending_reasons": [],
        "flagged": [], "allowed": [], "daily_totals": [],
        "inventory_ran": False, "inventory_regions": [], "inventory": {}, "inventory_items": {},
        "examples": {}, "max_examples": args.max_examples, "inventory_deleting": {},
        "inventory_protected": {}, "inventory_unchecked": {}, "inventory_complete": None,
        "not_applicable": {}, "not_applicable_count": 0, "blocked_by_policy": {}, "blocked_by_policy_count": 0,
        "warnings": [], "errors": [],
    }

    def finish(status: str) -> int:
        res["status"] = status
        res["exit_code"] = EXIT_BY_STATUS[status]
        write_json(args.json_out, res)
        print(render_summary(res))
        return res["exit_code"]

    pending: List[str] = []
    window: Dict[str, Any] = {}
    rules: List[Dict[str, Any]] = []
    if not inventory_only:
        window = compute_window(cleanup, args.settle_days, args.lag_hours, args.days, now)
        pending = window.pop("_pending")
        res["window"] = window
        res["pending_reasons"] = list(pending)
        try:
            rules = load_allowlist(args.allowlist)
        except AllowlistError as e:
            res["errors"].append({"stage": "args", "code": "InvalidAllowlist", "message": str(e), "hint": ""})
            return finish(STATUS_ERROR)
        res["allowlist_rules"] = len(rules)

    try:
        session = get_session(args)
        res["account_id"] = account_id(session)
    except Exception as e:
        code, msg = _err(e)
        res["errors"].append({"stage": "auth", "code": code, "message": msg, "hint": HINT_CREDENTIALS})
        return finish(STATUS_ERROR)
    log("계정 %s, 모드 %s, 정리일 %s, 오늘(UTC) %s" % (res["account_id"], args.mode, res["cleanup_date"], res["today_utc"]))

    fatal = False
    if not inventory_only:
        fatal = _cost_step(args, session, res, window, rules, now, today)

    if res["mode"] == MODE_INVENTORY or args.inventory:
        regions = resolve_inventory_regions(session, args, res["errors"])
        log("인벤토리: %d개 리전 + global (%d개 리전 유형 + %d개 global 유형)" % (
            len(regions), len(REGIONAL_CHECKS), len(GLOBAL_CHECKS) + 1))
        inv = run_inventory(session, regions, PROTECTED_PREFIXES + tuple(args.protect_prefix or ()))
        res.update({"inventory_ran": True, "inventory_regions": regions, "inventory": inv.counts,
                    "inventory_items": inv.items, "examples": inv.examples(args.max_examples),
                    "inventory_deleting": inv.deleting, "inventory_protected": inv.protected,
                    "inventory_unchecked": inv.unchecked, "not_applicable": inv.not_applicable,
                    "not_applicable_count": inv.not_applicable_count(), "blocked_by_policy": inv.blocked,
                    "blocked_by_policy_count": inv.blocked_count(), "inventory_checked": inv.checked})
        res["errors"].extend(inv.errors)
        res["inventory_complete"] = not inv.unchecked and not inv.blocked
        if not res["inventory_complete"] and res["mode"] == MODE_INVENTORY:
            res["warnings"].append("일부 유형/리전을 확인하지 못했습니다(권한 차단 또는 오류). 해당 범위는 콘솔에서 확인하세요.")
        inv_total, checked = inv.total(), inv.checked
    else:
        inv_total, checked = 0, 0

    if res["mode"] == MODE_INVENTORY:
        return finish(decide_inventory(inv_total, checked))
    return finish(decide(res["flagged"], inv_total, fatal, res["pending_reasons"]))


def _fallback_to_inventory(res: Dict[str, Any], e: Exception, reason: str) -> None:
    info = classify_ce_error(e)
    info["reason"] = reason
    res["ce_fallback"] = info
    res["mode"] = MODE_INVENTORY
    res["pending_reasons"] = []
    res["ce_requests"] = 0
    res["warnings"].append(HINT_CE_FALLBACK)
    log("Cost Explorer 사용 불가(%s: %s) → 인벤토리 모드로 판정" % (info["code"], reason))


def _cost_step(args, session, res: Dict[str, Any], window: Dict[str, Any], rules: List[Dict[str, Any]],
               now: dt.datetime, today: dt.date) -> bool:
    """Run the Cost Explorer part. Returns fatal (True = CE error that is not a fallback case)."""
    pending = res["pending_reasons"]
    if not window["end"]:
        log("판정 대기: %s" % ", ".join(pending))
        if args.mode != MODE_AUTO:
            return False
        # auto: nothing to judge yet, but learn now whether CE is usable at all (1 request, $0.01),
        # so accounts that can never use CE get an inventory verdict instead of an endless PENDING.
        try:
            res["ce_requests"] = probe_cost_explorer(client(session, "ce", CE_REGION), today)
            res["ce_probe"] = "ok"
        except (ClientError, BotoCoreError) as e:
            reason = ce_fallback_reason(e)
            if reason:
                _fallback_to_inventory(res, e, reason)
            else:
                res["ce_probe"] = "error:%s" % _err(e)[0]
                res["warnings"].append("Cost Explorer 사용 가능 여부 확인 실패: %s" % _err(e)[0])
        return False

    # Query whenever there is at least one full UTC day after cleanup: costs only grow as data
    # posts, so a line found early is a real RESIDUAL even before settle-days have passed.
    if pending:
        log("판정 대기(%s) 중이지만 이미 지난 날짜는 조회합니다" % ", ".join(pending))
    log("Cost Explorer 조회: %s ~ %s (UTC, End 제외 %s)" % (window["start"], window["end"], window["end_exclusive"]))
    try:
        ce = client(session, "ce", CE_REGION)
        rf = res["record_filter"]
        days, n = fetch_cost_and_usage(ce, window["start"], window["end_exclusive"],
                                       record_filter(rf["mode"], rf["values"]))
    except (ClientError, BotoCoreError, RuntimeError) as e:
        reason = ce_fallback_reason(e) if args.mode == MODE_AUTO else None
        if reason:
            _fallback_to_inventory(res, e, reason)
            return False
        info = classify_ce_error(e)
        res["errors"].append(info)
        log("Cost Explorer 오류: %s %s" % (info["code"], info["message"]))
        return True

    res["ce_requests"] = n
    window["queried"] = True
    flagged, allowed, daily = analyze(days, args.threshold_usd, rules)
    res["flagged"], res["allowed"], res["daily_totals"] = flagged, allowed, daily
    if daily:
        res["data_final"] = not any(d["estimated"] for d in daily)
    # CE omits days it has not posted yet: the first post-cleanup day should be there.
    start_day = dt.date.fromisoformat(window["start"])
    if window["start"] not in days:
        if now < _midnight(start_day + dt.timedelta(days=1)) + dt.timedelta(hours=NO_DATA_GRACE_HOURS):
            pending.append("no-data")
        else:
            res["warnings"].append("Cost Explorer가 %s 데이터를 돌려주지 않아 $0으로 간주" % window["start"])
    elif args.require_final and days[max(days)]["estimated"]:
        pending.append("estimated")
    return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)
