#!/usr/bin/env bash
# deploy_killswitch.sh - deploy/update the EduKillSwitch stack (edu-killswitch-<region>)
# in every region of --regions for the account of the current credentials.
#
# Dry-run by default (shows the plan and validates the template); add --apply to deploy.
# --test invokes the deployed Lambda in every region with a dry_run event (changes nothing).
#
#   ./deploy_killswitch.sh --threshold 300000 --email ops@example.com --apply
#   ./deploy_killswitch.sh --test
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/killswitch.yaml"
STACK_PREFIX="edu-killswitch"

REGIONS="ap-northeast-2,us-east-1"
PROFILE=""
ROLE_ARN=""
EXTERNAL_ID=""
SESSION_NAME="edu-cleanup"
THRESHOLD=""
PERIOD_MIN="5"
INVOCATIONS="0"
TARGET_MODE="ALL_IAM_USERS"
GROUP=""
EXEMPT=""
EMAIL=""
API_TRIGGERS="true"
APPLY="false"
TEST="false"
JSON_OUT=""

usage() {
  cat <<'EOF'
Usage: deploy_killswitch.sh [options]

Trip condition
  --threshold N         Bedrock input+output tokens per period per region that trips the switch (required to deploy)
  --period-min M        Alarm period in minutes: 1 2 3 5 10 15 30 60 (default 5)
  --invocations N       Optional request-count alarm per period (0 = off, default)
  --no-api-triggers     Do not trip on CloudTrail events (PutUseCaseForModelAccess, CreateAccessKey, ...)
Targets
  --target-mode MODE    ALL_IAM_USERS (default) or GROUP
  --group NAME          IAM group whose members are locked (TargetMode GROUP)
  --exempt a,b          IAM user names that are never locked (the calling IAM user is added automatically)
  --email ADDR          E-mail for alarm / kill-switch reports (confirm the subscription mail)
Run
  --regions r1,r2       Regions (default ap-northeast-2,us-east-1; "global" is ignored)
  --apply               Actually deploy (default: dry-run = plan + template validation)
  --test                Only invoke the deployed Lambda with {"dry_run": true} in each region
  --json-out FILE       Write a machine-readable result
Credentials
  --profile P  --role-arn ARN  --external-id ID  --session-name NAME
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }
log() { echo "$*" >&2; }

# an option's value may be empty ("" for --exempt/--email/--group); required ones are checked below
need_value() { [[ $# -ge 2 ]] || die "$1 needs a value"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --regions) need_value "$@"; REGIONS="$2"; shift 2 ;;
    --profile) need_value "$@"; PROFILE="$2"; shift 2 ;;
    --role-arn) need_value "$@"; ROLE_ARN="$2"; shift 2 ;;
    --external-id) need_value "$@"; EXTERNAL_ID="$2"; shift 2 ;;
    --session-name) need_value "$@"; SESSION_NAME="$2"; shift 2 ;;
    --threshold) need_value "$@"; THRESHOLD="$2"; shift 2 ;;
    --period-min) need_value "$@"; PERIOD_MIN="$2"; shift 2 ;;
    --invocations) need_value "$@"; INVOCATIONS="$2"; shift 2 ;;
    --target-mode) need_value "$@"; TARGET_MODE="$2"; shift 2 ;;
    --group) need_value "$@"; GROUP="$2"; shift 2 ;;
    --exempt) need_value "$@"; EXEMPT="$2"; shift 2 ;;
    --email) need_value "$@"; EMAIL="$2"; shift 2 ;;
    --no-api-triggers) API_TRIGGERS="false"; shift ;;
    --apply) APPLY="true"; shift ;;
    --test) TEST="true"; shift ;;
    --json-out) need_value "$@"; JSON_OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

# ---- validation -------------------------------------------------------------
command -v aws >/dev/null 2>&1 || die "aws CLI not found (run this in AWS CloudShell)"
[[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"
if [[ "$TEST" != "true" ]]; then
  [[ "$THRESHOLD" =~ ^[1-9][0-9]*$ ]] || die "--threshold must be a positive integer (tokens per period)"
fi
case "$PERIOD_MIN" in 1|2|3|5|10|15|30|60) ;; *) die "--period-min must be one of 1 2 3 5 10 15 30 60" ;; esac
[[ "$INVOCATIONS" =~ ^[0-9]+$ ]] || die "--invocations must be a non-negative integer"
case "$TARGET_MODE" in
  ALL_IAM_USERS) ;;
  GROUP) [[ -n "$GROUP" ]] || die "--target-mode GROUP needs --group" ;;
  *) die "--target-mode must be ALL_IAM_USERS or GROUP" ;;
esac
[[ -z "$EMAIL" || "$EMAIL" == *@* ]] || die "--email does not look like an e-mail address"
[[ "$EXEMPT" != *" "* ]] || die "--exempt must be comma-separated without spaces"
[[ -n "${REGIONS//[[:space:],]/}" ]] || die "--regions is empty"

AWS_BASE=(aws)
[[ -n "$PROFILE" ]] && AWS_BASE+=(--profile "$PROFILE")

# ---- credentials ------------------------------------------------------------
if [[ -n "$ROLE_ARN" ]]; then
  log "assuming $ROLE_ARN"
  assume=("${AWS_BASE[@]}" sts assume-role --role-arn "$ROLE_ARN" --role-session-name "${SESSION_NAME:0:64}"
          --duration-seconds 3600 --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
  [[ -n "$EXTERNAL_ID" ]] && assume+=(--external-id "$EXTERNAL_ID")
  read -r AK SK ST < <("${assume[@]}") || die "assume-role failed"
  [[ -n "${ST:-}" ]] || die "assume-role returned no credentials"
  export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
  unset AWS_PROFILE
  AWS_BASE=(aws)
fi

read -r ACCOUNT_ID CALLER_ARN < <("${AWS_BASE[@]}" sts get-caller-identity --query '[Account,Arn]' --output text) \
  || die "cannot call sts get-caller-identity (credentials?)"
log "account ${ACCOUNT_ID} as ${CALLER_ARN}"

# The calling IAM user must never lock itself out (CONTRACTS.md).
if [[ "$CALLER_ARN" == arn:*:iam::*:user/* ]]; then
  CALLER_USER="${CALLER_ARN##*/}"
  if [[ ",${EXEMPT}," != *",${CALLER_USER},"* ]]; then
    EXEMPT="${EXEMPT:+${EXEMPT},}${CALLER_USER}"
    log "adding calling IAM user '${CALLER_USER}' to the exempt list"
  fi
fi

IFS=',' read -r -a REGION_LIST <<< "$REGIONS"
RESULTS=()
FAILED=0

record() {  # region stack status detail
  RESULTS+=("$1|$2|$3|$4")
}

# ---- test mode --------------------------------------------------------------
run_test() {
  local region="$1" stack="$2" fn out
  fn="$("${AWS_BASE[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" \
        --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text 2>/dev/null)" || fn=""
  if [[ -z "$fn" || "$fn" == "None" ]]; then
    log "[$region] stack $stack not found - deploy it first"
    record "$region" "$stack" "missing" "stack not deployed"
    return 1
  fi
  out="$(mktemp)"
  if ! meta="$("${AWS_BASE[@]}" lambda invoke --region "$region" --function-name "$fn" \
      --cli-binary-format raw-in-base64-out \
      --payload '{"trigger":"manual","reason":"deploy_killswitch.sh --test","dry_run":true}' \
      "$out")"; then
    rm -f "$out"
    record "$region" "$stack" "test-failed" "lambda invoke failed"
    return 1
  fi
  log "[$region] dry-run result of $fn:"
  cat "$out" >&2
  echo >&2
  if [[ "$meta" == *FunctionError* ]] || grep -q '"errorMessage"' "$out"; then
    record "$region" "$stack" "test-failed" "lambda returned an error"
    rm -f "$out"
    return 1
  fi
  record "$region" "$stack" "tested" "$(grep -o '"status": *"[^"]*"' "$out" | head -n1 | tr -d '"' || true)"
  rm -f "$out"
}

# ---- deploy -----------------------------------------------------------------
deploy_region() {
  local region="$1" stack="$2" status
  status="$("${AWS_BASE[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" \
            --query 'Stacks[0].StackStatus' --output text 2>/dev/null)" || status="NONE"
  log "[$region] stack $stack: current status $status"
  if [[ "$APPLY" != "true" ]]; then
    "${AWS_BASE[@]}" cloudformation validate-template --region "$region" \
      --template-body "file://${TEMPLATE}" >/dev/null || { record "$region" "$stack" "invalid" "validate-template failed"; return 1; }
    log "[$region] dry-run: would deploy $stack with ${PARAMS[*]}"
    record "$region" "$stack" "planned" "$status"
    return 0
  fi
  if [[ "$status" == "ROLLBACK_COMPLETE" ]]; then
    log "[$region] deleting $stack (ROLLBACK_COMPLETE cannot be updated)"
    "${AWS_BASE[@]}" cloudformation delete-stack --region "$region" --stack-name "$stack"
    "${AWS_BASE[@]}" cloudformation wait stack-delete-complete --region "$region" --stack-name "$stack"
  fi
  if ! "${AWS_BASE[@]}" cloudformation deploy --region "$region" --stack-name "$stack" \
      --template-file "$TEMPLATE" --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
      --tags edu-cleanup=killswitch --parameter-overrides "${PARAMS[@]}"; then
    record "$region" "$stack" "failed" "cloudformation deploy failed"
    return 1
  fi
  "${AWS_BASE[@]}" cloudformation describe-stacks --region "$region" --stack-name "$stack" \
    --query 'Stacks[0].Outputs[].[OutputKey,OutputValue]' --output text >&2 || true
  record "$region" "$stack" "deployed" "ok"
}

PARAMS=(
  "TokenThreshold=${THRESHOLD:-0}"
  "PeriodMinutes=${PERIOD_MIN}"
  "InvocationThreshold=${INVOCATIONS}"
  "TargetMode=${TARGET_MODE}"
  "TargetGroupName=${GROUP}"
  "ExemptUserNames=${EXEMPT}"
  "NotificationEmail=${EMAIL}"
  "EnableApiTriggers=${API_TRIGGERS}"
)

for raw in "${REGION_LIST[@]}"; do
  region="${raw//[[:space:]]/}"
  [[ -z "$region" || "$region" == "global" ]] && continue
  stack="${STACK_PREFIX}-${region}"
  if [[ "$TEST" == "true" ]]; then
    run_test "$region" "$stack" || FAILED=1
  else
    deploy_region "$region" "$stack" || FAILED=1
  fi
done

# ---- summary ----------------------------------------------------------------
if [[ "$TEST" == "true" ]]; then MODE="test"; elif [[ "$APPLY" == "true" ]]; then MODE="apply"; else MODE="dry-run"; fi
if [[ "$FAILED" -eq 0 ]]; then STATUS="ok"; else STATUS="failed"; fi
echo "killswitch ${MODE} [${STATUS}] account=${ACCOUNT_ID} exempt=${EXEMPT:--}"
for r in ${RESULTS[@]+"${RESULTS[@]}"}; do
  IFS='|' read -r r_region r_stack r_status r_detail <<< "$r"
  printf '  %-16s %-32s %-12s %s\n' "$r_region" "$r_stack" "$r_status" "$r_detail"
done

if [[ -n "$JSON_OUT" ]]; then
  APPLIED="false"
  [[ "$MODE" == "apply" ]] && APPLIED="true"
  printf '%s\n' ${RESULTS[@]+"${RESULTS[@]}"} | python3 -c '
import json, sys, datetime
acct, applied, status, mode, path = sys.argv[1:6]
rows = [l.rstrip("\n").split("|", 3) for l in sys.stdin if l.strip()]
out = {"module": "killswitch-deploy", "account_id": acct, "applied": applied == "true",
       "generated_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
       "status": status, "mode": mode,
       "stacks": [dict(zip(("region", "stack", "status", "detail"), r)) for r in rows]}
with open(path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
' "$ACCOUNT_ID" "$APPLIED" "$STATUS" "$MODE" "$JSON_OUT"
fi

if [[ "$FAILED" -ne 0 ]]; then exit 1; fi
exit 0
