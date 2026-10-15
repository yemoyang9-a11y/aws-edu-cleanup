#!/usr/bin/env bash
set +H  # FIRST command on purpose: an interactive shell would otherwise treat "!" in the pasted text as history
# =====================================================================================================
# edu-cleanup / nuke - CloudShell ONE-SHOT for ONE training account. Paste this whole file into
# AWS CloudShell (signed in to the student account) - or run it with `bash cloudshell-oneshot.sh`.
#   1) installs aws-nuke (latest ekristen v3 release, sha256-checked) into ~/.local/bin if missing
#   2) account ID from STS; alias edu-<id> is created only if the account has none (aws-nuke needs one)
#   3) DRY-RUN: ap-northeast-2 + us-east-1 IN PARALLEL, then global LAST  -> summary table
#   4) type "yes" to really delete (same order), then optionally recreate the default VPCs
# The caller is protected if it is an IAM user (user, policies, groups, keys, password, MFA) or a role.
# Same excludes / settings / filters as nuke/make_config.py (tests check that they stay identical).
# Run nuke/../cfn_predelete first if the account has CloudFormation stacks (faster, no hangs).
# =====================================================================================================
(
set -uo pipefail
REGIONS="${EDU_REGIONS:-ap-northeast-2 us-east-1}"   # course regions; 'global' is always added last
MAX_WAIT_RETRIES=120          # aws-nuke default 0 = wait forever
PARALLEL_QUERIES=24           # aws-nuke default 16
W="$HOME/edu-nuke"; BIN="$HOME/.local/bin"; mkdir -p "$W" "$BIN"; export PATH="$BIN:$PATH"
say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31mERROR: %s\033[0m\n' "$*"; exit 2; }

# ---- 1. aws-nuke --------------------------------------------------------------------------------
if ! command -v aws-nuke >/dev/null 2>&1; then
  case "$(uname -m)" in x86_64) A=amd64 ;; aarch64|arm64) A=arm64 ;; *) fail "unsupported CPU $(uname -m)" ;; esac
  TAG="$(curl -fsSLI -o /dev/null -w '%{url_effective}' https://github.com/ekristen/aws-nuke/releases/latest 2>/dev/null)"; TAG="${TAG##*/}"
  case "$TAG" in v3.*) ;; *) TAG=v3.68.3; say "latest release not resolvable - using $TAG" ;; esac
  U="https://github.com/ekristen/aws-nuke/releases/download/$TAG"; F="aws-nuke-$TAG-linux-$A.tar.gz"
  say "installing aws-nuke $TAG ..."
  curl -fsSL -o "$W/$F" "$U/$F" || fail "download failed"
  curl -fsSL -o "$W/checksums.txt" "$U/checksums.txt" || fail "checksums.txt download failed"
  (cd "$W" && grep " $F\$" checksums.txt | sha256sum -c --quiet -) || fail "sha256 mismatch"
  tar -xzf "$W/$F" -C "$BIN" aws-nuke || fail "extract failed"
  chmod +x "$BIN/aws-nuke"; rm -f "$W/$F"
fi
command -v jq >/dev/null 2>&1 || fail "jq not found"
say "$(aws-nuke version)"

# ---- 2. account, caller, alias ------------------------------------------------------------------
ACCT="$(aws sts get-caller-identity --query Account --output text)" || fail "no AWS credentials"
CALLER="$(aws sts get-caller-identity --query Arn --output text)"
PUSER=""; PROLE=""
case "$CALLER" in
  *:user/*) PUSER="${CALLER##*/}" ;;
  *:assumed-role/*) PROLE="${CALLER#*:assumed-role/}"; PROLE="${PROLE%%/*}" ;;
esac
ALIAS="$(aws iam list-account-aliases --query 'AccountAliases[0]' --output text)"; [ "$ALIAS" = None ] && ALIAS=""
case "$(printf '%s' "$ALIAS" | tr '[:upper:]' '[:lower:]')" in *prod*) fail "alias '$ALIAS' contains 'prod' - refusing" ;; esac
NOALIAS=""   # aws-nuke refuses accounts without an alias: dry-run bypasses the check, apply creates edu-<id>
if [ -z "$ALIAS" ]; then NOALIAS=1; say "no account alias yet - dry-run uses --no-alias-check; 'yes' creates edu-$ACCT"; fi
say "account $ACCT (${ALIAS:-no alias})  caller $CALLER"

# ---- 3. config (one file per region; same content as nuke/make_config.py) ------------------------
q() { printf '"%s"' "$1"; }
f_exact() { printf '        - property: "%s"\n          type: "exact"\n          value: "%s"\n' "$1" "$2"; }
f_glob() { printf '        - property: "%s"\n          type: "glob"\n          value: "%s"\n' "$1" "$2"; }
PROT_FILTERS=""
if [ -n "$PUSER" ]; then
  PPOL=""; PGRP=""
  for a in $(aws iam list-attached-user-policies --user-name "$PUSER" --query 'AttachedPolicies[].PolicyArn' --output text); do
    case "$a" in arn:aws*:iam::aws:policy/*) ;; *) PPOL="$PPOL $a" ;; esac; done
  for g in $(aws iam list-groups-for-user --user-name "$PUSER" --query 'Groups[].GroupName' --output text); do
    PGRP="$PGRP $g"
    for a in $(aws iam list-attached-group-policies --group-name "$g" --query 'AttachedPolicies[].PolicyArn' --output text); do
      case "$a" in arn:aws*:iam::aws:policy/*) ;; *) PPOL="$PPOL $a" ;; esac; done
  done
  PMFA="$(aws iam list-mfa-devices --user-name "$PUSER" --query 'MFADevices[].SerialNumber' --output text)"
  PROT_FILTERS="$(
    echo "      IAMUser:"; f_exact Name "$PUSER"
    for t in IAMUserPolicyAttachment IAMUserGroupAttachment IAMUserAccessKey IAMLoginProfile IAMUserMFADevice \
             IAMUserSSHPublicKey IAMUserHTTPSGitCredential IAMSigningCertificate IAMServiceSpecificCredential; do
      echo "      $t:"; f_exact UserName "$PUSER"; done
    echo "      IAMUserPolicy:"; printf '        - type: "glob"\n          value: "%s -> *"\n' "$PUSER"
    if [ -n "$PMFA" ]; then echo "      IAMVirtualMFADevice:"; for s in $PMFA; do f_exact SerialNumber "$s"; done; fi
    if [ -n "$PGRP" ]; then
      echo "      IAMGroup:"; for g in $PGRP; do f_exact Name "$g"; done
      echo "      IAMGroupPolicyAttachment:"; for g in $PGRP; do f_exact GroupName "$g"; done
      echo "      IAMGroupPolicy:"; for g in $PGRP; do printf '        - type: "glob"\n          value: "%s -> *"\n' "$g"; done
    fi
  )"
  say "protecting IAM user '$PUSER'${PGRP:+ (groups:$PGRP)}"
fi
[ -n "$PROLE" ] && [ "$PROLE" != EduCleanupRole ] && say "protecting caller role '$PROLE'"
ROLES="EduCleanupRole${PROLE:+ $PROLE}"

mkcfg() {  # $1 = region or global
cat <<EOF
regions:
  - $(q "$1")
blocklist:
  - "000000000000"
$([ -n "$NOALIAS" ] && printf 'bypass-alias-check-accounts:\n  - "%s"\n' "$ACCT")
settings:
  EC2Instance:
    DisableStopProtection: true
    DisableDeletionProtection: true
  EC2Image:
    IncludeDisabled: true
    IncludeDeprecated: true
    DisableDeregistrationProtection: true
  RDSInstance:
    DisableDeletionProtection: true
    StartClusterToDelete: true
  CloudFormationStack:
    DisableDeletionProtection: true
    CreateRoleToDeleteStack: true
  DynamoDBTable:
    DisableDeletionProtection: true
  ELBv2:
    DisableDeletionProtection: true
  EKSCluster:
    DisableDeletionProtection: true
  CloudWatchLogsLogGroup:
    DisableDeletionProtection: true
  CognitoUserPool:
    DisableDeletionProtection: true
  DocDBCluster:
    DisableDeletionProtection: true
  DSQLCluster:
    DisableDeletionProtection: true
  NeptuneCluster:
    DisableDeletionProtection: true
  NeptuneInstance:
    DisableClusterDeletionProtection: true
    DisableDeletionProtection: true
  NeptuneGraph:
    DisableDeletionProtection: true
  PinpointPhoneNumber:
    DisableDeletionProtection: true
  QuickSightSubscription:
    DisableTerminationProtection: true
  S3Bucket:
    BypassGovernanceRetention: true
    RemoveObjectLegalHold: true
  LightsailInstance:
    ForceDeleteAddOns: true
  KMSKey:
    DeleteInUseKeys: true
resource-types:
  excludes:
    - "S3Object"
    - "DynamoDBTableItem"
    - "OpsWorks*"
    - "CodeStarProject"
    - "RoboMaker*"
    - "QLDBLedger"
    - "ElasticTranscoder*"
    - "MediaStore*"
    - "InspectorAssessment*"
    - "AppMesh*"
    - "LexBot"
    - "LexIntent"
    - "LexSlotType"
    - "LexModelBuildingServiceBotAlias"
    - "GlueDevEndpoint"
    - "Cloud9Environment"
    - "CloudSearchDomain"
    - "DataPipelinePipeline"
    - "MachineLearning*"
    - "CloudDirectoryDirectory"
    - "CloudDirectorySchema"
    - "AWS::Timestream::*"
    - "FMS*"
    - "ShieldProtection*"
    - "ServiceCatalogTagOption*"
accounts:
  $(q "$ACCT"):
    filters:
      IAMRole:
$(for r in $ROLES; do f_exact Name "$r"; done; f_glob Name "edu-bootstrap*")
      IAMRolePolicyAttachment:
$(for r in $ROLES; do f_exact RoleName "$r"; done; f_glob RoleName "edu-bootstrap*")
      IAMRolePolicy:
$(for r in $ROLES; do f_exact role:RoleName "$r"; done; f_glob role:RoleName "edu-bootstrap*")
      IAMPolicy:
$(f_exact Name EduProtectCleanupRole; for a in ${PPOL:-}; do f_exact ARN "$a"; done)
      CloudFormationStack:
$(f_glob Name "edu-bootstrap*")
      LambdaFunction:
$(f_glob Name "edu-bootstrap*")
      CloudWatchLogsLogGroup:
$(f_glob Name "/aws/lambda/edu-bootstrap*")
$PROT_FILTERS
EOF
}
mkcfgs() { for r in $REGIONS global; do mkcfg "$r" > "$W/config-$r.yaml"; done; }
mkcfgs

# ---- runner: regions in parallel, then global; JSON logs ---------------------------------------
nuke() {  # $1 = dry|apply
  local mode="$1" r; local -a extra=()
  [ "$mode" = apply ] && extra+=(--no-dry-run)
  [ -n "$NOALIAS" ] && extra+=(--no-alias-check)
  for r in $REGIONS; do
    ( aws-nuke run -c "$W/config-$r.yaml" --default-region "$r" --no-prompt --prompt-delay 3 "${extra[@]}" \
        --max-wait-retries "$MAX_WAIT_RETRIES" --parallel-queries "$PARALLEL_QUERIES" --log-format json \
        > "$W/$mode-$r.jsonl" 2> "$W/$mode-$r.stderr" < /dev/null; echo $? > "$W/$mode-$r.rc" ) &
  done
  say "[$mode] $REGIONS running in parallel ... (logs: $W/$mode-*.jsonl)"; wait
  say "[$mode] global (IAM, CloudFront, Route 53 ...) last ..."
  aws-nuke run -c "$W/config-global.yaml" --default-region us-east-1 --no-prompt --prompt-delay 3 "${extra[@]}" \
    --max-wait-retries "$MAX_WAIT_RETRIES" --parallel-queries "$PARALLEL_QUERIES" --log-format json \
    > "$W/$mode-global.jsonl" 2> "$W/$mode-global.stderr" < /dev/null; echo $? > "$W/$mode-global.rc"
}
# listing-error rules (= nuke/listing_errors.json; a test checks that the copies are identical)
IFS= read -r -d '' LF_RULES <<'JSON' || true
{
  "_doc": [
    "How summarize.jq (and the inlined copy in cloudshell-oneshot.sh) classifies aws-nuke listing-failed (lister) errors.",
    "One lister error = {resource_type, error}. First match wins, in this order:",
    "  known_noise_types (exact type name) -> expected",
    "  expected   regex on the raw error -> the API does not exist / is not offered for this account in this region: nothing can hide there",
    "  maybe_scp  regex -> access denied WITHOUT a reason: an SCP of the reseller Organization OR regional gating. Shown on one quiet line",
    "  transient  regex -> 5xx / throttling: a real failure (counted in LIST-ERR) but listed on one line - re-run to confirm",
    "  anything else -> unexpected: one LISTING FAILED line per type, counted in LIST-ERR (resources of that type were NOT scanned)",
    "Regexes are jq/Oniguruma (case-sensitive unless (?i:...)); $ = end of string, so use (?:\\n|$) for end of line.",
    "Samples: real CloudShell dry-run, aws-nuke v3.68.3, 17 regions, 2026-10-15 (tests/fixtures/real-cloudshell-v3.68.3-listing-failed.jsonl).",
    "cloudshell-oneshot.sh embeds this file verbatim in a quoted heredoc - keep the copies identical (a test compares them)."
  ],
  "known_noise_types": [
    "FMSPolicy", "FMSNotificationChannel", "ShieldProtection", "ShieldProtectionGroup",
    "ServiceCatalogTagOption", "ServiceCatalogTagOptionPortfolioAttachment",
    "QBusinessApplication", "QBusinessIndex", "QBusinessDataSource", "QBusinessPlugin", "QBusinessRetriever",
    "QBusinessWebExperience"
  ],
  "expected": [
    {"re": "no such host|DNS lookup failed",
     "why": "the service has no endpoint in this region"},
    {"re": "UnknownOperationException|(?i:unknown operation)",
     "why": "API not offered in this region (Bedrock custom models / model customization: 404 UnknownOperationException or ValidationException Unknown operation)"},
    {"re": "InvalidAction: The action \\w+ is not valid for this web service",
     "why": "EC2 API not offered in this region (Verified Access in ap-northeast-3, eu-west-3)"},
    {"re": "Unrecognized engine name",
     "why": "DocumentDB is not offered in this region (us-west-1: DescribeDBClusters -> Unrecognized engine name: docdb)"},
    {"re": "is not authorized for region",
     "why": "account not onboarded for the service in this region (Cloud Directory: Account N is not authorized for region R)"},
    {"re": "(?i:account is(?:n.t| not) authorized to (?:call this operation|make this call|invoke this API operation))",
     "why": "service-side regional/feature gating, not IAM and not an SCP (those say is not authorized to perform ...): Comprehend NotAuthorizedException, Transcribe language models, Bedrock evaluation jobs"},
    {"re": "operation is currently disabled",
     "why": "API switched off by AWS (Glue GetDevEndpoints)"}
  ],
  "maybe_scp": [
    {"re": "service control policy",
     "why": "explicit deny by an SCP of the Organization the account belongs to - we cannot list, students usually cannot create either"},
    {"re": "AccessDenied(?:Exception)?:?[ \\t]*(?:\\n|$)",
     "why": "AccessDenied with an EMPTY message (Rekognition, Textract, Timestream for InfluxDB): an SCP deny or a region where the account is not enabled - cannot tell which"},
    {"re": "Unable to determine service/operation name to be authorized",
     "why": "403 from the authorization layer (SSM Quick Setup in ap-northeast-3): usually a region without the service, possibly an SCP"}
  ],
  "transient": [
    {"re": "(?i:status ?code: ?5[0-9][0-9])",
     "why": "HTTP 5xx from the service (Bedrock prompts/flow aliases in us-west-1 returned 500)"},
    {"re": "InternalServerError|InternalFailure|ServiceUnavailable",
     "why": "server-side error"},
    {"re": "Throttling|Rate exceeded|TooManyRequests|RequestLimitExceeded|SlowDown",
     "why": "throttled - lower --parallel-queries or re-run"}
  ]
}
JSON
# summary: last state per item (type+owner+name); "listing failed" by resource_type, classified with LF_RULES:
#   expected (not offered here) / maybe SCP (empty AccessDenied) / 5xx+throttling / unexpected (one line each)
summary() {  # $1 = dry|apply
  local mode="$1" r
  if [ "$mode" = apply ]; then printf '\n%-16s %4s %8s %7s %6s %6s %9s %8s\n' REGION EXIT REMOVED FAILED LEFT KEPT LIST-ERR EXP/SCP
  else printf '\n%-16s %4s %13s %6s %9s %8s\n' REGION EXIT WOULD-DELETE KEPT LIST-ERR EXP/SCP; fi
  for r in $REGIONS global; do
    jq -R -s -r --arg r "$r" --arg mode "$mode" --arg rc "$(cat "$W/$mode-$r.rc" 2>/dev/null || echo 2)" --argjson rules "$LF_RULES" '
      # BEGIN listing-error classification (cloudshell-oneshot.sh carries an identical copy - a test compares them)
      # Rules: listing_errors.json. Input {type, error(raw)} -> "expected" | "maybe_scp" | "transient" | "unexpected"
      def lf_bucket($rules):
        (.error // "" | tostring) as $e | (.type // "") as $t
        | if any(($rules.known_noise_types // [])[]; . == $t) then "expected"
          elif any(($rules.expected // [])[]; .re as $re | $e | test($re)) then "expected"
          elif any(($rules.maybe_scp // [])[]; .re as $re | $e | test($re)) then "maybe_scp"
          elif any(($rules.transient // [])[]; .re as $re | $e | test($re)) then "transient"
          else "unexpected" end;
      def lf_rank: {"expected": 0, "maybe_scp": 1, "transient": 2, "unexpected": 3}[.] // 3;
      # END listing-error classification
      [splits("\n") | select(length>0) | (try fromjson catch null) | select(type=="object")] as $ev
      | ([$ev[] | select((.type|type)=="string" and (.state|type)=="string")]
         | reduce .[] as $e ({}; .[$e.type+"|"+($e.owner//"")+"|"+($e.name//"")] = $e) | [.[]]) as $it
      | def n(s): [$it[] | select(.state|IN(s))] | length;
        def by(s): [$it[] | select(.state|IN(s)) | .type] | group_by(.) | map("\(.[0]) \(length)") | join(", ");
      ([$ev[] | select(.level=="error" and ((.msg//"")|startswith("listing failed")))
        | {type: (.resource_type // "?"), error: ((.error // .msg) | tostring)} | . + {bucket: lf_bucket($rules)}]
       | group_by(.type) | map((map(.bucket|lf_rank)|max) as $m | map(select((.bucket|lf_rank)==$m)) | .[0])) as $lf
      | def lb(b): [$lf[] | select(.bucket==b) | .type];
      ((lb("transient")+lb("unexpected"))|length) as $nerr
      | ([(lb("expected")|length),(lb("maybe_scp")|length)] | if add==0 then "-" else "\(.[0])/\(.[1])" end) as $exp
      | (if $mode=="apply"
         then ["ROW",$r,$rc,n("finished"),n("failed"),n("new","new-dependency","pending","waiting","hold","pending-dependency"),n("filtered"),$nerr,$exp]
         else ["ROW",$r,$rc,n("new","new-dependency"),n("filtered"),$nerr,$exp] end | map(tostring) | join("\t")),
        (if $mode=="dry" and by("new","new-dependency")!="" then "    would delete: \(by("new","new-dependency"))" else empty end),
        (if $mode=="apply" and by("failed")!="" then "    FAILED: \(by("failed"))" else empty end),
        (if $mode=="apply" and by("new","pending","waiting","hold")!="" then "    NOT FINISHED: \(by("new","pending","waiting","hold"))" else empty end),
        (lb("expected") | if length>0 then "    listing failed (expected - service not used/subscribed): \(join(", "))" else empty end),
        (lb("maybe_scp") | if length>0 then "    access denied (maybe SCP): \(join(", "))" else empty end),
        (lb("transient") | if length>0 then "    LISTING FAILED (5xx/throttling - re-run to confirm): \(join(", "))" else empty end),
        ($lf[] | select(.bucket=="unexpected")
         | "    LISTING FAILED \(.type): \(.error | split("\n") | map(gsub("^\\s+";"")|select(length>0)) | .[0:3] | join(" | ") | .[0:300])"),
        ([$ev[] | select(.level=="fatal") | .msg] | unique | .[] | "    aws-nuke stopped: \(.)")
    ' "$W/$mode-$r.jsonl" | while IFS= read -r line; do
      case "$line" in
        ROW*) IFS=$'\t' read -r _ a b c d e f g h <<<"$line"
              if [ "$mode" = apply ]; then printf '%-16s %4s %8s %7s %6s %6s %9s %8s\n' "$a" "$b" "$c" "$d" "$e" "$f" "$g" "$h"
              else printf '%-16s %4s %13s %6s %9s %8s\n' "$a" "$b" "$c" "$d" "$e" "$f"; fi ;;
        *) printf '%s\n' "$line" ;;
      esac
    done
    [ "$(cat "$W/$mode-$r.rc" 2>/dev/null)" = 0 ] || [ ! -s "$W/$mode-$r.stderr" ] || { echo "    stderr:"; tail -n 3 "$W/$mode-$r.stderr" | sed 's/^/      /'; }
  done
  echo "LIST-ERR = lister failures that may hide resources (re-run / check them); EXP/SCP = not offered here / access denied (maybe SCP)"
}

T0=$SECONDS; nuke dry; say "== DRY-RUN done in $((SECONDS - T0))s - nothing was deleted =="; summary dry
for r in $REGIONS global; do [ "$(cat "$W/dry-$r.rc")" = 0 ] || fail "dry-run failed for $r - see $W/dry-$r.jsonl / .stderr"; done

# ---- 4. confirm + apply ------------------------------------------------------------------------
echo
read -r -p "Delete EVERYTHING above in account $ACCT (${ALIAS:-no alias})? type yes: " OK < /dev/tty
[ "$OK" = yes ] || { say "cancelled - nothing deleted"; exit 0; }
if [ -n "$NOALIAS" ]; then
  ALIAS="edu-$ACCT"; say "creating account alias '$ALIAS' (aws-nuke requires one to delete)"
  aws iam create-account-alias --account-alias "$ALIAS" || fail "could not create alias $ALIAS"
  NOALIAS=""; mkcfgs
fi
T0=$SECONDS; nuke apply; say "== APPLY done in $((SECONDS - T0))s =="; summary apply
RC=0; for r in $REGIONS global; do [ "$(cat "$W/apply-$r.rc")" = 0 ] || RC=1; done
if [ "$RC" = 0 ]; then say "RESULT: clean"
else say "RESULT: something is left - run this block again (a 2nd pass usually finishes it)"; fi

# ---- 5. default VPCs (aws-nuke deletes them; students expect one) -----------------------------
read -r -p "Recreate the default VPC in: $REGIONS ? [y/N] " DV < /dev/tty
if [ "$DV" = y ] || [ "$DV" = Y ]; then
  for r in $REGIONS; do
    if [ "$(aws ec2 describe-vpcs --region "$r" --filters Name=isDefault,Values=true --query 'length(Vpcs)' --output text)" = 0 ]; then
      aws ec2 create-default-vpc --region "$r" --query 'Vpc.VpcId' --output text | sed "s/^/  $r default VPC: /"
    else echo "  $r: default VPC already exists"; fi
  done
fi
exit "$RC"
)
