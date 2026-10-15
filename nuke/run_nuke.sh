#!/usr/bin/env bash
# edu-cleanup / nuke: run aws-nuke (ekristen v3) on ONE training account - fast and quiet.
#
#   bash nuke/run_nuke.sh                                   # dry-run, current credentials (e.g. CloudShell)
#   bash nuke/run_nuke.sh --apply                           # really delete
#   bash nuke/run_nuke.sh --account-id 123456789012 --role-arn arn:aws:iam::123456789012:role/EduCleanupRole \
#        --external-id XXX --regions ap-northeast-2,us-east-1 --json-out nuke.json [--apply]
#
# What it does (see README.md):
#   1. finds aws-nuke (PATH, $AWS_NUKE_BIN or cache) or downloads the latest ekristen release (sha256-checked)
#   2. detects account ID / caller; protects the caller if it is an IAM user
#   3. makes sure the account has an alias (aws-nuke refuses accounts without one); creates edu-<id> on --apply
#   4. one config per region (make_config.py); all regions IN PARALLEL, then `global` LAST
#   5. --log-format json -> per-region summary with jq (would-remove/removed/failed/remaining by type,
#      "listing failed" by type, classified with listing_errors.json) + JSON result (CONTRACTS.md)
# Exit: 0 nothing left (apply) / dry-run OK, 1 failed or remaining items (or an apply-run was cut short), 2 error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="nuke"
ROLE_NAME="EduCleanupRole"

ACCOUNT_ID=""
ROLE_ARN=""
EXTERNAL_ID=""
SESSION_NAME="edu-cleanup-nuke"
PROFILE=""
REGIONS="ap-northeast-2,us-east-1"
APPLY=0
LOG_DIR=""
JSON_OUT=""
PROTECT_USERS=()
PROTECT_ROLES=()
EXTRA_EXCLUDES=""
BLOCKLIST=""
MAX_WAIT_RETRIES=120
PARALLEL_QUERIES=24
PROMPT_DELAY=3
NO_GLOBAL=0
INSTALL_DIR="${AWS_NUKE_INSTALL_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/edu-cleanup/bin}"
AWS_NUKE_VERSION="${AWS_NUKE_VERSION:-latest}"
AWS_NUKE_FALLBACK_VERSION="v3.68.3"   # used when "latest" cannot be resolved (verified version for this toolkit)

# How lister errors ("listing failed") are classified: expected / maybe_scp / transient / unexpected (one place).
LISTING_ERRORS="$SCRIPT_DIR/listing_errors.json"

usage() {
  cat <<'EOF'
Usage: run_nuke.sh [options]
  --account-id ID         12-digit target account (default: detected with sts get-caller-identity)
  --role-arn ARN          assume this role (aws-nuke --assume-role-arn; auto-refreshing credentials)
  --external-id X         ExternalId for --role-arn
  --session-name NAME     STS session name (default: edu-cleanup-nuke)
  --profile P             AWS CLI profile to start from
  --regions LIST          comma-separated regions (default: ap-northeast-2,us-east-1); 'global' always runs last
  --no-global             skip the global (IAM, CloudFront, Route 53 ...) pass
  --apply                 really delete (default: dry-run)
  --log-dir DIR           configs + JSON logs (default: <dir of --json-out>/nuke-logs/<account>-<ts>, else ./nuke-logs/...)
  --json-out FILE         machine-readable result (CONTRACTS.md)
  --protect-iam-user NAME keep this IAM user (repeatable). The caller is added automatically if it is an IAM user
  --extra-excludes LIST   more resource types to exclude (comma list)
  --blocklist LIST        account IDs aws-nuke must refuse (default: placeholder 000000000000)
  --max-wait-retries N    give up after N passes with nothing new (default 120 = ~10+ min; 0 = forever)
  --parallel-queries N    listers per region at once (default 24; aws-nuke default 16)
  --prompt-delay N        seconds aws-nuke sleeps before scan/delete (min 3, default 3)
  -h, --help
Env: AWS_NUKE_BIN (binary to use), AWS_NUKE_VERSION (vX.Y.Z or latest), AWS_NUKE_INSTALL_DIR
EOF
}

log() { printf '%s [nuke] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; write_error_json "$*"; exit 2; }

write_error_json() {
  [ -n "${JSON_OUT:-}" ] || return 0
  command -v jq >/dev/null 2>&1 || { printf '{"module":"nuke","status":"ERROR","exit_code":2}\n' >"$JSON_OUT"; return 0; }
  jq -n --arg m "$MODULE" --arg a "${ACCOUNT_ID:-}" --arg t "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" \
    --argjson applied "$([ "$APPLY" = 1 ] && echo true || echo false)" --arg e "$1" \
    '{module:$m, account_id:$a, generated_at:$t, applied:$applied, status:"ERROR", exit_code:2, error:$e, regions:{}}' \
    >"$JSON_OUT" || true
}

need_val() { [ $# -ge 2 ] && [ -n "$2" ] || { usage >&2; die "option $1 needs a value"; }; }

while [ $# -gt 0 ]; do
  case "$1" in
    --account-id) need_val "$@"; ACCOUNT_ID="$2"; shift 2 ;;
    --role-arn) need_val "$@"; ROLE_ARN="$2"; shift 2 ;;
    --external-id) need_val "$@"; EXTERNAL_ID="$2"; shift 2 ;;
    --session-name) need_val "$@"; SESSION_NAME="$2"; shift 2 ;;
    --profile) need_val "$@"; PROFILE="$2"; shift 2 ;;
    --regions) need_val "$@"; REGIONS="$2"; shift 2 ;;
    --no-global) NO_GLOBAL=1; shift ;;
    --apply) APPLY=1; shift ;;
    --log-dir) need_val "$@"; LOG_DIR="$2"; shift 2 ;;
    --json-out) need_val "$@"; JSON_OUT="$2"; shift 2 ;;
    --protect-iam-user) need_val "$@"; PROTECT_USERS+=("$2"); shift 2 ;;
    --extra-excludes) need_val "$@"; EXTRA_EXCLUDES="$2"; shift 2 ;;
    --blocklist) need_val "$@"; BLOCKLIST="$2"; shift 2 ;;
    --max-wait-retries) need_val "$@"; MAX_WAIT_RETRIES="$2"; shift 2 ;;
    --parallel-queries) need_val "$@"; PARALLEL_QUERIES="$2"; shift 2 ;;
    --prompt-delay) need_val "$@"; PROMPT_DELAY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

for n in MAX_WAIT_RETRIES PARALLEL_QUERIES PROMPT_DELAY; do
  [[ "${!n}" =~ ^[0-9]+$ ]] || die "$n must be a non-negative integer"
done
[ "$PROMPT_DELAY" -ge 3 ] || die "--prompt-delay must be >= 3 (aws-nuke refuses less)"
[ "$PARALLEL_QUERIES" -ge 1 ] || die "--parallel-queries must be >= 1"
[ -z "$ACCOUNT_ID" ] || [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || die "--account-id must be 12 digits"
[ -z "$EXTERNAL_ID" ] || [ -n "$ROLE_ARN" ] || die "--external-id needs --role-arn"

for tool in aws jq python3; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found in PATH"
done
LF_RULES="$(jq -c . "$LISTING_ERRORS")" || die "cannot read $LISTING_ERRORS"

# ---------------------------------------------------------------------------------------------
# AWS CLI calls run as the TARGET account: assume --role-arn once for the CLI (aws-nuke itself
# gets --assume-role-arn and refreshes its own credentials).
# ---------------------------------------------------------------------------------------------
AWS_BASE=(aws)
[ -z "$PROFILE" ] || AWS_BASE+=(--profile "$PROFILE")

if [ -n "$ROLE_ARN" ]; then
  log "assuming $ROLE_ARN for account checks"
  assume_args=(sts assume-role --role-arn "$ROLE_ARN" --role-session-name "${SESSION_NAME:0:64}" --duration-seconds 3600 --output json)
  [ -z "$EXTERNAL_ID" ] || assume_args+=(--external-id "$EXTERNAL_ID")
  creds_json="$("${AWS_BASE[@]}" "${assume_args[@]}")" || die "sts assume-role failed for $ROLE_ARN"
  AWS_ACCESS_KEY_ID="$(jq -r .Credentials.AccessKeyId <<<"$creds_json")"
  AWS_SECRET_ACCESS_KEY="$(jq -r .Credentials.SecretAccessKey <<<"$creds_json")"
  AWS_SESSION_TOKEN="$(jq -r .Credentials.SessionToken <<<"$creds_json")"
  unset creds_json
  # exported only into the CLI calls below (not into aws-nuke, which assumes the role itself)
  AWS_T=(env -u AWS_PROFILE "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID" "AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY"
         "AWS_SESSION_TOKEN=$AWS_SESSION_TOKEN" aws)
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
else
  AWS_T=("${AWS_BASE[@]}")
fi

ident="$("${AWS_T[@]}" sts get-caller-identity --output json)" || die "sts get-caller-identity failed (no credentials?)"
CALLER_ACCOUNT="$(jq -r .Account <<<"$ident")"
CALLER_ARN="$(jq -r .Arn <<<"$ident")"
if [ -z "$ACCOUNT_ID" ]; then
  ACCOUNT_ID="$CALLER_ACCOUNT"
elif [ "$ACCOUNT_ID" != "$CALLER_ACCOUNT" ]; then
  die "credentials are for account $CALLER_ACCOUNT but --account-id is $ACCOUNT_ID - refusing"
fi
log "account $ACCOUNT_ID, caller $CALLER_ARN"

CALLER_ROLE=""
case "$CALLER_ARN" in
  *":root") log "WARNING: running as the account ROOT user (works, but prefer an IAM role/user)" ;;
  *":user/"*)
    caller_user="${CALLER_ARN##*/}"
    if [[ ! " ${PROTECT_USERS[*]-} " =~ [[:space:]]${caller_user}[[:space:]] ]]; then
      PROTECT_USERS+=("$caller_user")
      log "caller is IAM user '$caller_user' -> protected (its policies, groups, keys, login profile, MFA)"
    fi ;;
  *":assumed-role/"*)
    CALLER_ROLE="${CALLER_ARN#*:assumed-role/}"; CALLER_ROLE="${CALLER_ROLE%%/*}"
    if [ "$CALLER_ROLE" != "$ROLE_NAME" ]; then
      PROTECT_ROLES+=("$CALLER_ROLE")
      log "caller is role '$CALLER_ROLE' -> protected (with its policies)"
    fi ;;
esac

# Details of protected users: customer-managed policies, groups, MFA devices (so the user keeps its permissions)
EXTRA_CFG_ARGS=()
for u in "${PROTECT_USERS[@]+"${PROTECT_USERS[@]}"}"; do
  EXTRA_CFG_ARGS+=(--protect-iam-user "$u")
  if ! "${AWS_T[@]}" iam get-user --user-name "$u" >/dev/null 2>&1; then
    log "WARNING: IAM user '$u' not found - filtering by name only"; continue
  fi
  while read -r arn; do
    case "$arn" in ""|arn:aws*:iam::aws:policy/*) ;; *) EXTRA_CFG_ARGS+=(--protect-iam-policy-arn "$arn") ;; esac
  done < <("${AWS_T[@]}" iam list-attached-user-policies --user-name "$u" --query 'AttachedPolicies[].PolicyArn' --output text | tr '\t' '\n')
  while read -r g; do
    [ -z "$g" ] && continue
    EXTRA_CFG_ARGS+=(--protect-iam-group "$g")
    while read -r arn; do
      case "$arn" in ""|arn:aws*:iam::aws:policy/*) ;; *) EXTRA_CFG_ARGS+=(--protect-iam-policy-arn "$arn") ;; esac
    done < <("${AWS_T[@]}" iam list-attached-group-policies --group-name "$g" --query 'AttachedPolicies[].PolicyArn' --output text | tr '\t' '\n')
  done < <("${AWS_T[@]}" iam list-groups-for-user --user-name "$u" --query 'Groups[].GroupName' --output text | tr '\t' '\n')
  while read -r s; do
    [ -n "$s" ] && EXTRA_CFG_ARGS+=(--protect-mfa-serial "$s")
  done < <("${AWS_T[@]}" iam list-mfa-devices --user-name "$u" --query 'MFADevices[].SerialNumber' --output text | tr '\t' '\n')
done
for r in "${PROTECT_ROLES[@]+"${PROTECT_ROLES[@]}"}"; do EXTRA_CFG_ARGS+=(--protect-iam-role "$r"); done
[ -z "$EXTRA_EXCLUDES" ] || EXTRA_CFG_ARGS+=(--extra-excludes "$EXTRA_EXCLUDES")
[ -z "$BLOCKLIST" ] || EXTRA_CFG_ARGS+=(--blocklist "$BLOCKLIST")
# DeleteStack can pass EduCleanupRole (it trusts cloudformation.amazonaws.com) when a stack's own role is gone
[ "$CALLER_ROLE" != "$ROLE_NAME" ] || EXTRA_CFG_ARGS+=(--cfn-use-current-role)

# ---------------------------------------------------------------------------------------------
# Account alias: aws-nuke refuses an account without one, or one containing "prod".
# ---------------------------------------------------------------------------------------------
ALIAS="$("${AWS_T[@]}" iam list-account-aliases --query 'AccountAliases[0]' --output text 2>/dev/null || true)"
[ "$ALIAS" != "None" ] || ALIAS=""
ALIAS_CREATED=false
NO_ALIAS_CHECK=0
if [ -n "$ALIAS" ]; then
  case "$(tr '[:upper:]' '[:lower:]' <<<"$ALIAS")" in
    *prod*) die "account alias '$ALIAS' contains 'prod' - refusing" ;;
  esac
  log "account alias: $ALIAS"
elif [ "$APPLY" = 1 ]; then
  ALIAS="edu-$ACCOUNT_ID"
  log "account has no alias -> creating '$ALIAS' (aws-nuke requires one)"
  "${AWS_T[@]}" iam create-account-alias --account-alias "$ALIAS" >/dev/null || die "could not create account alias $ALIAS"
  ALIAS_CREATED=true
else
  log "account has no alias: dry-run uses --no-alias-check (+ bypass-alias-check-accounts); --apply will create edu-$ACCOUNT_ID"
  NO_ALIAS_CHECK=1
  EXTRA_CFG_ARGS+=(--bypass-alias-check)
fi

# ---------------------------------------------------------------------------------------------
# aws-nuke binary
# ---------------------------------------------------------------------------------------------
install_aws_nuke() {
  local arch tag base tmp sum want
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) die "unsupported CPU $(uname -m) - install aws-nuke manually and set AWS_NUKE_BIN" ;;
  esac
  tag="$AWS_NUKE_VERSION"
  if [ "$tag" = latest ]; then
    # the /releases/latest redirect needs no GitHub API token
    tag="$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/ekristen/aws-nuke/releases/latest 2>/dev/null)" || tag=""
    tag="${tag##*/}"
    if [[ ! "$tag" =~ ^v3\.[0-9]+\.[0-9]+$ ]]; then
      log "WARNING: could not resolve the latest aws-nuke release - using $AWS_NUKE_FALLBACK_VERSION"
      tag="$AWS_NUKE_FALLBACK_VERSION"
    fi
  fi
  [[ "$tag" =~ ^v3\.[0-9]+\.[0-9]+$ ]] || die "unexpected aws-nuke version '$tag' (need v3.x.y)"
  base="https://github.com/ekristen/aws-nuke/releases/download/$tag"
  tmp="$(mktemp -d)"
  log "downloading aws-nuke $tag (linux-$arch)"
  curl -fsSL -o "$tmp/an.tgz" "$base/aws-nuke-$tag-linux-$arch.tar.gz" || { rm -rf "$tmp"; die "download failed"; }
  if curl -fsSL -o "$tmp/checksums.txt" "$base/checksums.txt"; then
    want="$(awk -v f="aws-nuke-$tag-linux-$arch.tar.gz" '$2==f {print $1}' "$tmp/checksums.txt")"
    sum="$(sha256sum "$tmp/an.tgz" | awk '{print $1}')"
    [ -n "$want" ] && [ "$want" = "$sum" ] || { rm -rf "$tmp"; die "sha256 mismatch for aws-nuke $tag"; }
  else
    log "WARNING: checksums.txt not available - skipping sha256 check"
  fi
  tar -xzf "$tmp/an.tgz" -C "$tmp" aws-nuke || { rm -rf "$tmp"; die "unexpected archive layout"; }
  mkdir -p "$INSTALL_DIR"
  [ -x "$INSTALL_DIR/aws-nuke" ] && { rm -rf "$tmp"; return 0; }   # installed meanwhile by a parallel run
  install -m 0755 "$tmp/aws-nuke" "$INSTALL_DIR/aws-nuke.$$" && mv -f "$INSTALL_DIR/aws-nuke.$$" "$INSTALL_DIR/aws-nuke"
  rm -rf "$tmp"
}

if [ -n "${AWS_NUKE_BIN:-}" ]; then
  NUKE="$AWS_NUKE_BIN"
elif command -v aws-nuke >/dev/null 2>&1; then
  NUKE="$(command -v aws-nuke)"
elif [ -x "$INSTALL_DIR/aws-nuke" ]; then
  NUKE="$INSTALL_DIR/aws-nuke"
elif [ -x "$HOME/.local/bin/aws-nuke" ]; then      # installed by cloudshell-oneshot.sh (binary is ~300 MB: reuse it)
  NUKE="$HOME/.local/bin/aws-nuke"
else
  mkdir -p "$INSTALL_DIR"
  if command -v flock >/dev/null 2>&1; then   # the hub runs several accounts at once: download only once
    exec 9>"$INSTALL_DIR/.install.lock"; flock 9
  fi
  [ -x "$INSTALL_DIR/aws-nuke" ] || install_aws_nuke
  if command -v flock >/dev/null 2>&1; then flock -u 9; exec 9>&-; fi
  NUKE="$INSTALL_DIR/aws-nuke"
fi
NUKE_VERSION="$("$NUKE" version 2>/dev/null | awk '{print $NF}')" || die "cannot run $NUKE"
case "$NUKE_VERSION" in v3.*) ;; *) die "need aws-nuke v3 (ekristen), found '$NUKE_VERSION' at $NUKE" ;; esac
log "aws-nuke $NUKE_VERSION ($NUKE)"

# ---------------------------------------------------------------------------------------------
# Configs: one per region, global last
# ---------------------------------------------------------------------------------------------
IFS=',' read -r -a REG_ARR <<<"$REGIONS"
REGIONAL=()
for r in "${REG_ARR[@]}"; do
  r="${r//[[:space:]]/}"
  [ -n "$r" ] && [ "$r" != global ] || continue
  [[ "$r" =~ ^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-[0-9]+$ ]] || die "invalid region '$r'"
  [[ " ${REGIONAL[*]-} " =~ [[:space:]]${r}[[:space:]] ]] || REGIONAL+=("$r")
done
[ "${#REGIONAL[@]}" -gt 0 ] || [ "$NO_GLOBAL" = 0 ] || die "no regions to run"
ALL_OWNERS=("${REGIONAL[@]+"${REGIONAL[@]}"}")
[ "$NO_GLOBAL" = 1 ] || ALL_OWNERS+=(global)

TS="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -z "$LOG_DIR" ]; then
  if [ -n "$JSON_OUT" ]; then LOG_DIR="$(dirname "$JSON_OUT")/nuke-logs/$ACCOUNT_ID-$TS"
  else LOG_DIR="./nuke-logs/$ACCOUNT_ID-$TS"; fi
fi
mkdir -p "$LOG_DIR"
log "logs: $LOG_DIR"

for owner in "${ALL_OWNERS[@]}"; do
  python3 "$SCRIPT_DIR/make_config.py" --account-id "$ACCOUNT_ID" --regions "$owner" --no-global \
    "${EXTRA_CFG_ARGS[@]+"${EXTRA_CFG_ARGS[@]}"}" --out "$LOG_DIR/config-$owner.yaml" 2>/dev/null \
    || die "make_config.py failed for $owner"
done

NUKE_ARGS=(run --no-prompt --prompt-delay "$PROMPT_DELAY" --log-format json
           --max-wait-retries "$MAX_WAIT_RETRIES" --parallel-queries "$PARALLEL_QUERIES")
[ "$APPLY" = 0 ] || NUKE_ARGS+=(--no-dry-run)
[ "$NO_ALIAS_CHECK" = 0 ] || NUKE_ARGS+=(--no-alias-check)
[ -z "$PROFILE" ] || NUKE_ARGS+=(--profile "$PROFILE")
NUKE_ENV=(env)
if [ -n "$ROLE_ARN" ]; then
  NUKE_ARGS+=(--assume-role-arn "$ROLE_ARN" --assume-role-session-name "${SESSION_NAME:0:64}")
  # = --assume-role-external-id, but via its env source (pkg/commands/nuke/nuke.go) so it is not visible in `ps`
  [ -z "$EXTERNAL_ID" ] || NUKE_ENV+=("AWS_ASSUME_ROLE_EXTERNAL_ID=$EXTERNAL_ID")
fi

PIDS=()
# shellcheck disable=SC2329  # invoked by the trap below
on_interrupt() {
  local p
  log "interrupted - stopping aws-nuke"
  for p in "${PIDS[@]+"${PIDS[@]}"}"; do kill "$p" 2>/dev/null || true; done
  pkill -TERM -P $$ 2>/dev/null || true
  write_error_json "interrupted"
  exit 2
}
trap on_interrupt INT TERM

run_one() {  # $1 = owner (region or global)
  local owner="$1" dr
  dr="$owner"; [ "$owner" != global ] || dr="us-east-1"   # aws-nuke's own default for the global pass
  "${NUKE_ENV[@]}" "$NUKE" "${NUKE_ARGS[@]}" -c "$LOG_DIR/config-$owner.yaml" --default-region "$dr" \
    >"$LOG_DIR/$owner.jsonl" 2>"$LOG_DIR/$owner.stderr" </dev/null
}

MODE="dry-run"; [ "$APPLY" = 0 ] || MODE="APPLY"
T0=$SECONDS
if [ "${#REGIONAL[@]}" -gt 0 ]; then
  log "$MODE: ${REGIONAL[*]} in parallel ..."
  for owner in "${REGIONAL[@]}"; do
    ( s=$SECONDS; rc=0; run_one "$owner" || rc=$?; echo "$rc $((SECONDS - s))" >"$LOG_DIR/$owner.rc" ) &
    PIDS+=("$!")
  done
  wait "${PIDS[@]}" || true
  PIDS=()
fi
if [ "$NO_GLOBAL" = 0 ]; then
  log "$MODE: global (IAM, CloudFront, Route 53 ...) last ..."
  s=$SECONDS; rc=0; run_one global || rc=$?; echo "$rc $((SECONDS - s))" >"$LOG_DIR/global.rc"
fi
TOTAL_S=$((SECONDS - T0))
trap - INT TERM

# ---------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------
APPLIED_STR=false; [ "$APPLY" = 0 ] || APPLIED_STR=true
REGIONS_JSON='{}'
for owner in "${ALL_OWNERS[@]}"; do
  read -r rc dur <"$LOG_DIR/$owner.rc" || { rc=2; dur=0; }
  one="$(jq -R -s -f "$SCRIPT_DIR/summarize.jq" --arg applied "$APPLIED_STR" \
          --argjson exit_code "$rc" --argjson rules "$LF_RULES" "$LOG_DIR/$owner.jsonl")" \
    || one='{"exit_code":2,"outcome":"error","errors":["could not parse the aws-nuke log"],"counts":{}}'
  stderr_tail="$(tail -c 2000 "$LOG_DIR/$owner.stderr" 2>/dev/null || true)"
  REGIONS_JSON="$(jq --arg o "$owner" --argjson one "$one" --argjson d "$dur" --arg st "$stderr_tail" \
      --arg log "$LOG_DIR/$owner.jsonl" --arg cfg "$LOG_DIR/config-$owner.yaml" \
      '.[$o] = ($one + {duration_s:$d, log:$log, config:$cfg} + (if $st == "" then {} else {stderr_tail:$st} end))' \
      <<<"$REGIONS_JSON")"
done
PROTECTED_JSON="$(printf '%s\n' "${PROTECT_USERS[@]+"${PROTECT_USERS[@]}"}" | jq -R . | jq -cs 'map(select(length > 0))')"
RESULT="$(jq -n -f "$SCRIPT_DIR/result.jq" --argjson regions "$REGIONS_JSON" --arg account_id "$ACCOUNT_ID" \
  --arg applied "$APPLIED_STR" --arg generated_at "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" --arg alias "$ALIAS" \
  --arg alias_created "$ALIAS_CREATED" --arg caller_arn "$CALLER_ARN" --arg aws_nuke_version "$NUKE_VERSION" \
  --arg log_dir "$LOG_DIR" --arg duration_s "$TOTAL_S" --argjson protected_users "$PROTECTED_JSON")" \
  || die "could not build the result JSON"
[ -z "$JSON_OUT" ] || printf '%s\n' "$RESULT" >"$JSON_OUT"
printf '%s\n' "$RESULT" >"$LOG_DIR/result.json"

# Human summary (stdout)
jq -r -f "$SCRIPT_DIR/table.jq" <<<"$RESULT"

exit "$(jq -r .exit_code <<<"$RESULT")"
