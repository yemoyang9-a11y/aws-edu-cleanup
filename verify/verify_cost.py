#!/usr/bin/env python3
"""D+2 cost verification for one AWS training account (edu-cleanup module 7: verify).

"Clean" means: from the day after cleanup up to yesterday (UTC), Cost Explorer shows
no *usage* cost line (service x usage type x day) above the threshold that is not
covered by an allowlist rule. Optionally also runs a cheap read-only inventory of
commonly billable leftovers. Read-only: --apply is accepted but nothing is changed.

Exit codes: 0 CLEAN, 1 RESIDUAL, 2 ERROR, 3 PENDING (too early to judge).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING

import argparse
import datetime as dt
import fnmatch
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import BotoCoreError, ClientError

from common.session import now_iso

MODULE = "verify"

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

# Resources whose identifier starts with these belong to the protected bootstrap (CONTRACTS.md):
# they are listed under inventory_protected and never make the verdict RESIDUAL.
PROTECTED_PREFIXES = ("edu-bootstrap", "EduCleanup")

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
def _date(value: str) -> dt.date:
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
        description="D+2 Cost Explorer 검증: 정리 다음 날부터 어제(UTC)까지 허용 목록 밖 사용 요금이 없으면 CLEAN.")
    add_common_args(p)
    p.add_argument("--cleanup-date", type=_date, default=None,
                   help="정리(aws-nuke)가 끝난 날짜 YYYY-MM-DD (기본: 오늘, Asia/Seoul). "
                        "KST/UTC 어느 쪽 날짜로 적어도 검사 구간은 다음 날(UTC)부터 시작한다.")
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
                   help="과금 잔여물 빠른 인벤토리도 실행 (읽기 전용)")
    p.add_argument("--all-regions", action="store_true",
                   help="인벤토리를 활성화된 모든 리전에서 실행 (account:ListRegions)")
    p.add_argument("--protect-prefix", action="append", default=[],
                   help="이 접두사로 시작하는 리소스 이름은 인벤토리 잔여물로 세지 않음 (반복 가능, "
                        "기본 내장: %s)" % ", ".join(PROTECTED_PREFIXES))
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
    while True:
        resp = call(**params)
        for item in resp.get(key, []) or []:
            yield item
        token = resp.get("nextToken") or resp.get("NextToken")
        if not token:
            return
        params["nextToken" if "nextToken" in resp else "NextToken"] = token


def _ec2_instances(c):
    live = ["pending", "running", "shutting-down", "stopping", "stopped"]
    out = []
    for page in c.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": live}]):
        for res in page.get("Reservations", []):
            out.extend(i["InstanceId"] for i in res.get("Instances", []))
    return out


# (inventory type, boto3 service, function(client) -> list of identifiers)
REGIONAL_CHECKS = [
    ("ec2_instances", "ec2", _ec2_instances),
    ("ebs_volumes", "ec2", lambda c: [v["VolumeId"] for v in _items(c, "describe_volumes", "Volumes")]),
    ("ebs_snapshots", "ec2", lambda c: [s["SnapshotId"] for s in
                                        _items(c, "describe_snapshots", "Snapshots", OwnerIds=["self"])]),
    ("elastic_ips", "ec2", lambda c: [a.get("AllocationId") or a.get("PublicIp")
                                      for a in c.describe_addresses().get("Addresses", [])]),
    ("nat_gateways", "ec2", lambda c: [n["NatGatewayId"] for n in _items(c, "describe_nat_gateways", "NatGateways")
                                       if n.get("State") not in ("deleted", "failed")]),
    ("load_balancers_v2", "elbv2", lambda c: [lb["LoadBalancerName"] for lb in
                                              _items(c, "describe_load_balancers", "LoadBalancers")]),
    ("load_balancers_classic", "elb", lambda c: [lb["LoadBalancerName"] for lb in
                                                 _items(c, "describe_load_balancers", "LoadBalancerDescriptions")]),
    ("rds_instances", "rds", lambda c: [d["DBInstanceIdentifier"] for d in
                                        _items(c, "describe_db_instances", "DBInstances")]),
    ("rds_clusters", "rds", lambda c: [d["DBClusterIdentifier"] for d in
                                       _items(c, "describe_db_clusters", "DBClusters")]),
    ("opensearch_serverless_collections", "opensearchserverless",
     lambda c: [x.get("name") or x.get("id") for x in _items(c, "list_collections", "collectionSummaries")]),
    ("sagemaker_endpoints", "sagemaker", lambda c: [e["EndpointName"] for e in
                                                    _items(c, "list_endpoints", "Endpoints")]),
    ("sagemaker_notebook_instances", "sagemaker", lambda c: [n["NotebookInstanceName"] for n in
                                                             _items(c, "list_notebook_instances", "NotebookInstances")]),
    ("bedrock_provisioned_throughputs", "bedrock",
     lambda c: [p.get("provisionedModelName") or p.get("provisionedModelArn") for p in
                _items(c, "list_provisioned_model_throughputs", "provisionedModelSummaries")]),
    ("bedrock_custom_models", "bedrock", lambda c: [m.get("modelName") or m.get("modelArn") for m in
                                                    _items(c, "list_custom_models", "modelSummaries")]),
    ("bedrock_imported_models", "bedrock", lambda c: [m.get("modelName") or m.get("modelArn") for m in
                                                      _items(c, "list_imported_models", "modelSummaries")]),
    ("lambda_functions", "lambda", lambda c: [f["FunctionName"] for f in _items(c, "list_functions", "Functions")]),
    ("dynamodb_tables", "dynamodb", lambda c: list(_items(c, "list_tables", "TableNames"))),
    ("ecs_clusters", "ecs", lambda c: [a.split("/")[-1] for a in _items(c, "list_clusters", "clusterArns")]),
    ("eks_clusters", "eks", lambda c: list(_items(c, "list_clusters", "clusters"))),
]

REGION_DEAD_CODES = {"AuthFailure", "OptInRequired", "UnrecognizedClientException", "InvalidClientTokenId",
                     "EndpointConnectionError", "ConnectTimeoutError"}
MAX_IDS = 20


def _err(e: Exception) -> Tuple[str, str]:
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        return err.get("Code", "ClientError"), err.get("Message", str(e))
    return type(e).__name__, str(e)


class Inventory:
    def __init__(self, protected_prefixes: Tuple[str, ...] = PROTECTED_PREFIXES) -> None:
        self.protected_prefixes = tuple(protected_prefixes)
        self.counts: Dict[str, Dict[str, int]] = {}
        self.items: Dict[str, Dict[str, List[str]]] = {}
        self.protected: Dict[str, Dict[str, List[str]]] = {}
        self.unchecked: Dict[str, Dict[str, str]] = {}
        self.errors: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    def record(self, region: str, rtype: str, ids: List[Any]) -> None:
        prot = [str(i) for i in ids if isinstance(i, str) and i.startswith(self.protected_prefixes)]
        keep = [str(i) for i in ids if not (isinstance(i, str) and i.startswith(self.protected_prefixes))]
        with self._lock:
            self.counts.setdefault(region, {})
            self.counts[region][rtype] = self.counts[region].get(rtype, 0) + len(keep)
            if keep:
                lst = self.items.setdefault(region, {}).setdefault(rtype, [])
                lst.extend(keep[:max(0, MAX_IDS - len(lst))])
            if prot:
                self.protected.setdefault(region, {}).setdefault(rtype, []).extend(prot)

    def fail(self, region: str, rtype: str, code: str, message: str, is_error: bool = True) -> None:
        with self._lock:
            self.unchecked.setdefault(region, {})[rtype] = code
            if is_error:
                self.errors.append({"stage": "inventory", "region": region, "check": rtype,
                                    "code": code, "message": message[:300]})

    def total(self) -> int:
        return sum(n for per in self.counts.values() for n in per.values())


def _available(session, service: str, cache: Dict[str, List[str]]) -> List[str]:
    if service not in cache:
        try:
            cache[service] = session.get_available_regions(service) or []
        except Exception:  # metadata problems must not stop the inventory
            cache[service] = []
    return cache[service]


def inventory_region(session, region: str, inv: Inventory, avail_cache: Dict[str, List[str]]) -> None:
    dead: Optional[Tuple[str, str]] = None
    for rtype, service, fn in REGIONAL_CHECKS:
        if dead:
            inv.fail(region, rtype, dead[0], dead[1], is_error=False)
            continue
        avail = _available(session, service, avail_cache)
        if avail and region not in avail:
            inv.fail(region, rtype, "NotAvailableInRegion",
                     "botocore endpoint metadata에 이 리전이 없음", is_error=False)
            continue
        try:
            ids = fn(_client(session, service, region))
            inv.record(region, rtype, ids)
        except Exception as e:  # any failure = "could not check", never a crash
            code, msg = _err(e)
            inv.fail(region, rtype, code, msg)
            if rtype == "ec2_instances" and code in REGION_DEAD_CODES:
                dead = (code, "리전 접근 불가로 나머지 검사 생략: " + msg)


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
        code, msg = _err(e)
        inv.fail("global", "s3_buckets", code, msg)
        return
    for b in buckets:
        region = b.get("BucketRegion")
        if not region:
            try:
                loc = s3.get_bucket_location(Bucket=b["Name"]).get("LocationConstraint")
                region = {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)
            except Exception:
                region = "unknown"
        inv.record(region, "s3_buckets", [b["Name"]])


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
    inv = Inventory(protected_prefixes)
    cache: Dict[str, List[str]] = {}
    for svc in {s for _, s, _ in REGIONAL_CHECKS}:
        _available(session, svc, cache)  # warm the cache before threads start
    workers = max(1, min(8, len(regions)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda r: inventory_region(session, r, inv, cache), regions))
    inventory_s3(session, inv)
    return inv


# --------------------------------------------------------------------------- verdict & output
def decide(flagged: List[dict], inv_total: int, fatal: bool, pending: List[str]) -> str:
    """Priority: RESIDUAL (known leftovers) > ERROR > PENDING > CLEAN."""
    if flagged or inv_total > 0:
        return STATUS_RESIDUAL
    if fatal:
        return STATUS_ERROR
    if pending:
        return STATUS_PENDING
    return STATUS_CLEAN


PENDING_TEXT = {
    "settle": "정리일 + settle-days(UTC 00:00) 가 아직 지나지 않음",
    "empty-window": "정리 다음 날(UTC)이 아직 끝나지 않아 검사할 날짜가 없음",
    "lag": "정리 다음 날(UTC) 데이터가 아직 Cost Explorer에 반영되지 않았을 시간 (--lag-hours)",
    "no-data": "정리 다음 날(UTC) 데이터가 아직 Cost Explorer 응답에 없음",
    "estimated": "마지막 날 데이터가 Estimated=true (--require-final)",
}


def render_summary(res: Dict[str, Any]) -> str:
    out: List[str] = []
    w = res["window"]
    out.append("[verify] 계정 %s  판정: %s (exit %d)" % (res["account_id"], res["status"], res["exit_code"]))
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
    if res.get("inventory_ran"):
        found = {r: {t: n for t, n in per.items() if n} for r, per in res["inventory"].items()}
        found = {r: per for r, per in found.items() if per}
        if found:
            out.append("인벤토리 잔여물:")
            for region in sorted(found):
                out.append("  %s: %s" % (region, ", ".join("%s=%d" % kv for kv in sorted(found[region].items()))))
                for rtype, ids in sorted(res["inventory_items"].get(region, {}).items()):
                    out.append("      %s: %s" % (rtype, ", ".join(ids[:5]) + (" ..." if len(ids) > 5 else "")))
        else:
            out.append("인벤토리 잔여물: 없음 (%d개 리전 + S3)" % len(res["inventory_regions"]))
        unchecked = {r: {t: c for t, c in per.items() if c != "NotAvailableInRegion"}
                     for r, per in res["inventory_unchecked"].items()}
        unchecked = {r: per for r, per in unchecked.items() if per}
        if unchecked:
            out.append("확인 못 함 (권한/오류):")
            for region in sorted(unchecked):
                out.append("  %s: %s" % (region, ", ".join("%s(%s)" % kv for kv in sorted(unchecked[region].items()))))
    for wmsg in res.get("warnings", []):
        out.append("주의: " + wmsg)
    for e in res["errors"]:
        if e.get("stage") == "inventory":
            continue
        out.append("오류[%s] %s: %s" % (e.get("stage"), e.get("code"), e.get("message", "")[:200]))
        if e.get("hint"):
            out.append("  → " + e["hint"])
    if res["status"] == STATUS_RESIDUAL:
        out.append("다음 할 일: 위 리전/서비스의 리소스를 지우거나 해당 리전을 포함해 aws-nuke를 다시 돌린 뒤, "
                   "그 날짜를 --cleanup-date로 넣어 D+2에 다시 검증하세요.")
    elif res["status"] == STATUS_PENDING:
        out.append("다음 할 일: %s 이후 다시 실행하세요." % w["earliest_verdict_kst"])
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
                                   "errors": [{"stage": "internal", "code": code, "message": msg, "hint": ""}]})
        print("[verify] 판정: ERROR (exit 2) - 예상치 못한 오류: %s %s" % (code, msg))
        return EXIT_ERROR


def _run(args: argparse.Namespace) -> int:
    if args.apply:
        log("verify 는 읽기 전용입니다. --apply 는 무시됩니다.")

    now = args.now or utc_now()
    today = now.date()
    cleanup = args.cleanup_date or now.astimezone(KST).date()
    if args.record_types:
        rf = {"mode": "include", "values": parse_regions(args.record_types)}
    else:
        rf = {"mode": "exclude",
              "values": parse_regions(args.exclude_record_types) or parse_regions(DEFAULT_EXCLUDE_RECORD_TYPES)}

    res: Dict[str, Any] = {
        "module": MODULE, "account_id": None, "generated_at": now_iso(), "applied": False,
        "status": STATUS_ERROR, "exit_code": EXIT_ERROR,
        "cleanup_date": cleanup.isoformat(), "now_utc": now.replace(microsecond=0).isoformat(),
        "today_utc": today.isoformat(), "settle_days": args.settle_days, "threshold_usd": args.threshold_usd,
        "record_filter": rf, "metric": CE_METRIC, "require_final": args.require_final,
        "window": {}, "ce_requests": 0, "data_final": None, "pending_reasons": [],
        "flagged": [], "allowed": [], "daily_totals": [],
        "inventory_ran": False, "inventory_regions": [], "inventory": {}, "inventory_items": {},
        "inventory_protected": {}, "inventory_unchecked": {}, "inventory_complete": None,
        "warnings": [], "errors": [],
    }

    def finish(status: str) -> int:
        res["status"] = status
        res["exit_code"] = EXIT_BY_STATUS[status]
        write_json(args.json_out, res)
        print(render_summary(res))
        return res["exit_code"]

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
    log("계정 %s, 정리일 %s, 오늘(UTC) %s" % (res["account_id"], res["cleanup_date"], res["today_utc"]))

    fatal = False
    # Query whenever there is at least one full UTC day after cleanup: costs only grow as data
    # posts, so a line found early is a real RESIDUAL even before settle-days have passed.
    if window["end"]:
        if pending:
            log("판정 대기(%s) 중이지만 이미 지난 날짜는 조회합니다" % ", ".join(pending))
        log("Cost Explorer 조회: %s ~ %s (UTC, End 제외 %s)" % (window["start"], window["end"], window["end_exclusive"]))
        try:
            ce = client(session, "ce", CE_REGION)
            days, n = fetch_cost_and_usage(ce, window["start"], window["end_exclusive"],
                                           record_filter(rf["mode"], rf["values"]))
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
                    res["pending_reasons"].append("no-data")
                else:
                    res["warnings"].append("Cost Explorer가 %s 데이터를 돌려주지 않아 $0으로 간주" % window["start"])
            elif args.require_final and days[max(days)]["estimated"]:
                res["pending_reasons"].append("estimated")
        except (ClientError, BotoCoreError, RuntimeError) as e:
            fatal = True
            info = classify_ce_error(e)
            res["errors"].append(info)
            log("Cost Explorer 오류: %s %s" % (info["code"], info["message"]))
    else:
        log("판정 대기: %s" % ", ".join(pending))

    if args.inventory:
        regions = resolve_inventory_regions(session, args, res["errors"])
        log("인벤토리: %d개 리전 + S3" % len(regions))
        inv = run_inventory(session, regions, PROTECTED_PREFIXES + tuple(args.protect_prefix or ()))
        res.update({"inventory_ran": True, "inventory_regions": regions, "inventory": inv.counts,
                    "inventory_items": inv.items, "inventory_protected": inv.protected,
                    "inventory_unchecked": inv.unchecked})
        res["errors"].extend(inv.errors)
        res["inventory_complete"] = not any(
            c != "NotAvailableInRegion" for per in inv.unchecked.values() for c in per.values())
        inv_total = inv.total()
    else:
        inv_total = 0

    return finish(decide(res["flagged"], inv_total, fatal, res["pending_reasons"]))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_ERROR)
