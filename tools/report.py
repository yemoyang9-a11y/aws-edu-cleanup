#!/usr/bin/env python3
"""Turn the JSON results of one cleanup run into (1) a big ✅/⚠️/❌ banner and (2) a short report file.

Written for operators who are not AWS experts: the banner says in plain Korean whether they are done and
what to do next; the report file is what they hand to the person in charge (who can paste it into an AI).

    python3 tools/report.py --account 123456789012 --verify verify.json --preclose preclose.json \
        --nuke run/nuke-*.json --log ~/cleanup.log --out ~/cleanup-report-123456789012.txt

Exit code: 0 = ✅ done, 1 = ⚠️ cleaned but something to check before closing, 2 = ❌ not clean.
Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

OK, WARN, FAIL = 0, 1, 2

KOREAN_TYPES = {
    "ec2_instances": "EC2 인스턴스", "ebs_volumes": "EBS 볼륨", "ebs_snapshots": "EBS 스냅샷", "amis": "AMI 이미지",
    "elastic_ips": "탄력적 IP", "nat_gateways": "NAT 게이트웨이", "s3_buckets": "S3 버킷",
    "rds_instances": "RDS 데이터베이스", "rds_clusters": "RDS 클러스터", "lambda_functions": "Lambda 함수",
    "dynamodb_tables": "DynamoDB 테이블", "cloudformation_stacks": "CloudFormation 스택",
    "load_balancers": "로드 밸런서", "ecs_clusters": "ECS 클러스터", "eks_clusters": "EKS 클러스터",
    "sagemaker_endpoints": "SageMaker 엔드포인트", "sagemaker_notebooks": "SageMaker 노트북",
    "iam_users": "IAM 사용자", "iam_roles": "IAM 역할", "cloudfront_distributions": "CloudFront 배포",
}


def load(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def label(rtype: str) -> str:
    return KOREAN_TYPES.get(rtype, rtype)


def residuals(verify: Optional[Dict[str, Any]]) -> List[str]:
    """['서울(ap-northeast-2): EC2 인스턴스 1개 (i-0abc)', ...] from verify's inventory + examples."""
    if not verify:
        return []
    out = []
    inv = verify.get("inventory") or {}
    ex = verify.get("examples") or {}
    for region in sorted(inv):
        for rtype, n in sorted((inv.get(region) or {}).items()):
            if not n:
                continue
            ids = (ex.get(region) or {}).get(rtype) or []
            idtxt = " (" + ", ".join(ids[:3]) + (" 외" if len(ids) > 3 or n > len(ids[:3]) else "") + ")" if ids else ""
            out.append("%s: %s %d개%s" % (region, label(rtype), n, idtxt))
    return out


def nuke_problems(nukes: List[Dict[str, Any]]) -> List[str]:
    out = []
    for n in nukes:
        if n.get("status") == "ERROR":
            out.append("aws-nuke 실행 오류 (%s)" % ", ".join(r for r, v in (n.get("regions") or {}).items()
                                                          if (v or {}).get("outcome") == "error") or "?")
        for region, v in sorted((n.get("regions") or {}).items()):
            for key, what in (("failed_by_type", "삭제 실패"), ("remaining_by_type", "삭제 미완료")):
                for rtype, cnt in sorted(((v or {}).get(key) or {}).items()):
                    out.append("%s: %s %s %s개" % (region, rtype, what, cnt))
    return out


def preclose_blockers(pc: Optional[Dict[str, Any]]) -> List[str]:
    if not pc:
        return []
    return ["%s %s (%s) - %s" % (f.get("check"), f.get("resource_id"), f.get("region"), (f.get("detail") or "")[:120])
            for f in pc.get("findings") or []
            if f.get("severity") == "BLOCKER" and f.get("action") != "done"]


def root_key_warning(pc: Optional[Dict[str, Any]]) -> bool:
    return bool(pc) and any(f.get("check") == "root_access_keys" for f in pc.get("findings") or [])


def decide(verify, nukes, pc) -> int:
    vstatus = (verify or {}).get("status")
    if vstatus != "CLEAN" or any(n.get("status") == "ERROR" for n in nukes):
        return FAIL
    if preclose_blockers(pc):
        return WARN
    return OK


def build(account: str, verify, nukes, pc, log: str, report_path: str, passes: int) -> Dict[str, str]:
    level = decide(verify, nukes, pc)
    res, nprob, blk = residuals(verify), nuke_problems(nukes), preclose_blockers(pc)
    bar = "=" * 62
    lines = [bar]
    if level == OK:
        lines += [" ✅ 정리 완료 - 계정 %s" % account, " 남은 리소스 없음. 이 창을 닫아도 됩니다."]
    elif level == WARN:
        lines += [" ⚠️  리소스 정리는 완료 - 계정 %s" % account,
                  " 다만 해지 전에 확인할 항목이 %d건 있어요." % len(blk),
                  " 할 일: 아래 보고서 파일을 담당자에게 보내 주세요."]
    else:
        lines += [" ❌ 정리가 끝나지 않았어요 - 계정 %s" % account]
        for r in (res or nprob)[:6]:
            lines.append("   - " + r)
        if len(res or nprob) > 6:
            lines.append("   - ... 외 %d건 (보고서 참고)" % (len(res or nprob) - 6))
        if not res and not nprob:
            lines.append("   - 확인 단계가 실행되지 못했어요 (로그 참고)")
        lines += [" 할 일: 아래 보고서 파일을 담당자에게 보내 주세요."]
    if root_key_warning(pc):
        lines.append(" 🔑 루트 액세스 키가 있어요 - 루트로 로그인해 보안 자격 증명에서 삭제하세요.")
    if level != OK:
        lines.append("   보고서: %s" % report_path)
    lines.append(bar)
    banner = "\n".join(lines)

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rep = ["# edu-cleanup 보고서", "", "계정: %s" % account, "시각: %s" % now,
           "결과: %s" % {OK: "정리 완료", WARN: "정리 완료 (해지 전 확인 필요)", FAIL: "정리 안 됨"}[level],
           "삭제 시도 횟수: %d" % passes, "verify 판정: %s" % ((verify or {}).get("status") or "실행 안 됨"), ""]
    rep += ["## 남은 리소스 (verify)"] + (["- " + r for r in res] or ["- 없음"]) + [""]
    rep += ["## 마지막 삭제에서 실패/미완료 (aws-nuke)"] + (["- " + r for r in nprob] or ["- 없음"]) + [""]
    rep += ["## 해지 전 확인 항목 (preclose BLOCKER)"] + (["- " + r for r in blk] or ["- 없음"]) + [""]
    unchecked = (verify or {}).get("inventory_unchecked") or {}
    if unchecked:
        rep += ["## 확인 못 한 범위 (오류)"] + ["- %s: %s" % (r, ", ".join(sorted(v))) for r, v in sorted(unchecked.items())] + [""]
    if root_key_warning(pc):
        rep += ["## 보안", "- 루트 액세스 키가 있음: 루트 로그인 > 보안 자격 증명 > 액세스 키 삭제", ""]
    rep += ["## 전체 로그", "- %s" % log, "",
            "(담당자 참고: 이 파일과 전체 로그를 AI에 붙여 넣으면 원인 분석에 충분합니다.)"]
    return {"banner": banner, "report": "\n".join(rep) + "\n", "level": str(level)}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", required=True)
    p.add_argument("--verify")
    p.add_argument("--preclose")
    p.add_argument("--nuke", nargs="*", default=[], help="nuke result JSON files or globs, in run order")
    p.add_argument("--log", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--passes", type=int, default=1)
    a = p.parse_args(argv)
    files: List[str] = []
    for pat in a.nuke:
        files += sorted(glob.glob(pat)) or [pat]
    nukes = [d for d in (load(f) for f in files) if d]
    r = build(a.account, load(a.verify), nukes, load(a.preclose), a.log, a.out, a.passes)
    Path(a.out).write_text(r["report"], encoding="utf-8")
    print(r["banner"])
    return int(r["level"])


if __name__ == "__main__":
    sys.exit(main())
