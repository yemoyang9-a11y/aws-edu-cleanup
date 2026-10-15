#!/usr/bin/env bash
# edu-cleanup: clean ONE training account end to end, for operators who are not AWS experts.
#
#   bash cleanup.sh                     # preview -> type the account number -> delete -> verify (retries) -> ✅/⚠️/❌
#   bash cleanup.sh --skip-preview      # no preview listing (still asks for the account number)
#   bash cleanup.sh --delete-iam-users  # also delete IAM users (default: keep every existing IAM user)
#   bash cleanup.sh --preview-only      # list what would be deleted, change nothing
#
# Options: --max-passes N (default 3)  --wait-min M (default 5, wait between passes)
#          --cfn-wait-min M (default 5, how long to wait for CloudFormation stacks before moving on;
#          slow stacks keep deleting on AWS's side and are re-sent on every pass)
# Output:  big banner at the end, full log  ~/cleanup-<account>-<time>.log,
#          report for the person in charge  ~/cleanup-report-<account>-<time>.txt
# Exit:    0 ✅ done | 1 ⚠️ cleaned, check before closing | 2 ❌ not clean / error | 3 cancelled
set -uo pipefail

KEEP_IAM_USERS=1
SKIP_PREVIEW=0
PREVIEW_ONLY=0
MAX_PASSES=3
WAIT_MIN=5
CFN_WAIT_MIN=5
while [ $# -gt 0 ]; do
  case "$1" in
    --delete-iam-users) KEEP_IAM_USERS=0 ;;
    --keep-iam-users) KEEP_IAM_USERS=1 ;;
    --skip-preview) SKIP_PREVIEW=1 ;;
    --preview-only) PREVIEW_ONLY=1 ;;
    --max-passes) MAX_PASSES="${2:?}"; shift ;;
    --wait-min) WAIT_MIN="${2:?}"; shift ;;
    --cfn-wait-min) CFN_WAIT_MIN="${2:?}"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "알 수 없는 옵션: $1"; exit 2 ;;
  esac
  shift
done

cd "$(dirname "$0")" || exit 2
ACCT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" || ACCT=""
if ! [[ "$ACCT" =~ ^[0-9]{12}$ ]]; then echo "❌ AWS에 로그인된 상태가 아니에요. CloudShell에서 실행하세요."; exit 2; fi
TS="$(date +%Y%m%d-%H%M)"
LOG="$HOME/cleanup-$ACCT-$TS.log"
REPORT="$HOME/cleanup-report-$ACCT-$TS.txt"
RUN="$HOME/.edu-cleanup-runs/$ACCT-$TS"
mkdir -p "$RUN"
mark() { printf '%s\t%s\n' "$(date +%s)" "$1" >>"$RUN/timing.tsv"; }  # read by tools/timing.py
exec > >(tee -a "$LOG") 2>&1

REGIONS="$(aws account list-regions --region-opt-status-contains ENABLED ENABLED_BY_DEFAULT \
  --query 'Regions[].RegionName' --output text 2>/dev/null | tr '\t' ',')"
[ -n "$REGIONS" ] || REGIONS="$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text | tr '\t' ',')"
RGROUPS="$(echo "$REGIONS" | tr ',' '\n' | paste -sd',,,\n')"

PROTECT=(); VPROTECT=(); KEPT_USERS=""
if [ "$KEEP_IAM_USERS" = 1 ]; then
  for U in $(aws iam list-users --query 'Users[].UserName' --output text); do
    PROTECT+=(--protect-iam-user "$U"); VPROTECT+=(--protect-prefix "$U"); KEPT_USERS="$KEPT_USERS $U"
  done
fi

nuke_all() {  # $1 = tag for result files, $2 = "" (preview) or --apply
  local tag="$1" mode="$2" i=0 G
  for G in $RGROUPS; do
    i=$((i + 1)); echo "===== $G"
    # shellcheck disable=SC2086
    bash nuke/run_nuke.sh --regions "$G" --no-global "${PROTECT[@]}" --json-out "$RUN/nuke-$tag-$i.json" $mode
  done
  echo "===== global"
  # shellcheck disable=SC2086
  bash nuke/run_nuke.sh --regions global "${PROTECT[@]}" --json-out "$RUN/nuke-$tag-z-global.json" $mode
}
recreate_default_vpcs() {
  local R
  for R in $(echo "$REGIONS" | tr ',' ' '); do
    aws ec2 create-default-vpc --region "$R" >/dev/null 2>&1 && echo "$R 기본 VPC 재생성"
  done
}
verify() {  # $1 = output json
  python3 verify/verify_cost.py --mode inventory --all-regions "${VPROTECT[@]}" --json-out "$1"
}

echo "################################################################"
echo " 계정 $ACCT 정리 시작 ($(date '+%Y-%m-%d %H:%M'))"
echo " 리전 $(echo "$REGIONS" | tr ',' '\n' | wc -l)개 | IAM 사용자: $([ "$KEEP_IAM_USERS" = 1 ] && echo "유지 ($KEPT_USERS )" || echo "삭제")"
echo "################################################################"

if [ "$SKIP_PREVIEW" = 0 ]; then
  echo; echo "########## [1/3] 미리보기 (아무것도 지우지 않음) ##########"
  mark "미리보기: 해지 전 점검"
  python3 preclose/preclose_check.py --all-regions
  mark "미리보기: 스택 조회"
  python3 cfn_predelete/predelete_stacks.py --regions "$REGIONS"
  mark "미리보기: nuke 조회 (지우지 않음)"
  nuke_all preview ""
  if [ "$PREVIEW_ONLY" = 1 ]; then mark end; echo; echo "미리보기만 했어요. 아무것도 지우지 않았어요."; exit 0; fi
fi

echo
echo "########## 확인 ##########"
mark "계정 번호 입력 대기"
echo " 계정 번호 $ACCT 의 리소스를 삭제합니다. (영문/숫자 입력 상태인지 확인하세요)"
OK_INPUT=""
exec 3<"${EDU_CLEANUP_TTY:-/dev/tty}" || { echo " 입력을 받을 수 없어요(터미널 아님). 아무것도 지우지 않았어요."; exit 3; }
# Drop anything already waiting on the terminal (e.g. leftover lines of a pasted block), so it can't
# answer the prompt by accident. Only for a real terminal; tests feed a file through EDU_CLEANUP_TTY.
if [ -z "${EDU_CLEANUP_TTY:-}" ]; then while read -r -t 0.3 -u 3 _junk; do :; done; fi
for _ in 1 2 3; do
  read -r -u 3 -p " 삭제하려면 계정 번호 12자리를 입력 (그만두려면 그냥 Enter): " OK_INPUT || OK_INPUT=""
  OK_INPUT="${OK_INPUT//[^0-9]/}"
  [ -z "$OK_INPUT" ] && { echo " 취소했어요. 아무것도 지우지 않았어요."; exit 3; }
  [ "$OK_INPUT" = "$ACCT" ] && break
  echo " 번호가 달라요. 다시 입력해 주세요."
done
exec 3<&-
[ "$OK_INPUT" = "$ACCT" ] || { echo " 세 번 틀려서 취소했어요. 아무것도 지우지 않았어요."; exit 3; }

echo; echo "########## [2/3] 삭제 ##########"

PASS=0
LAST_TAG=""
while :; do
  PASS=$((PASS + 1)); LAST_TAG="pass$PASS"
  echo; echo "---------- 삭제 ${PASS}회차 ----------"
  # Stacks first, but only wait a few minutes: a stack held by VPC Lambda ENIs takes 20-40 min on AWS's side.
  # It keeps deleting in the background while nuke works; the next pass re-sends it (FORCE if DELETE_FAILED).
  mark "스택 삭제 (${PASS}회차)"
  python3 cfn_predelete/predelete_stacks.py --regions "$REGIONS" --apply --timeout-min "$CFN_WAIT_MIN" \
    --json-out "$RUN/cfn-$LAST_TAG.json" \
    || echo " 아직 지워지는 중인 스택이 있어요(TIMEOUT은 정상). AWS가 계속 지우는 동안 다음 단계로 넘어갑니다."
  mark "nuke 삭제 (${PASS}회차)"
  nuke_all "$LAST_TAG" --apply
  mark "기본 VPC 재생성 (${PASS}회차)"
  recreate_default_vpcs
  echo; echo "########## [3/3] 남은 리소스 확인 (${PASS}회차) ##########"
  mark "남은 리소스 확인 (${PASS}회차)"
  if verify "$RUN/verify.json"; then break; fi
  if [ "$PASS" -ge "$MAX_PASSES" ]; then break; fi
  echo " 아직 남은 리소스가 있어요. 지우는 중일 수 있으니 ${WAIT_MIN}분 기다렸다가 다시 시도합니다..."
  mark "회차 사이 대기 (${WAIT_MIN}분)"
  sleep $((WAIT_MIN * 60))
done

mark "해지 전 점검 (적용)"
python3 preclose/preclose_check.py --all-regions --apply --json-out "$RUN/preclose.json"
mark end

echo; echo "########## 단계별 소요 시간 ##########"
python3 tools/timing.py "$RUN" || true

echo
python3 tools/report.py --account "$ACCT" --verify "$RUN/verify.json" --preclose "$RUN/preclose.json" \
  --nuke "$RUN/nuke-$LAST_TAG-*.json" --log "$LOG" --out "$REPORT" --passes "$PASS"
RC=$?
echo " 전체 로그: $LOG"
exit "$RC"
