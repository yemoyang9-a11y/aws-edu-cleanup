# nuke — aws-nuke를 빠르고 조용하게 돌리는 설정 생성기와 실행기

학생 계정 하나를 aws-nuke(ekristen v3)로 정리한다. 이 모듈은 aws-nuke를 다시 만들지 않는다. **설정과 실행 방식만** 바꿔 느림, 에러 소음, 무한 대기, 삭제 순서 문제를 줄인다.
CloudFormation 스택은 이 모듈보다 **먼저** `cfn_predelete/`가 병렬로 지운다. 그러면 aws-nuke가 만날 스택은 거의 남지 않는다.

| 파일 | 역할 |
|---|---|
| `make_config.py` | 계정 하나와 리전 목록을 받아 aws-nuke v3 YAML 설정을 만든다(표준 라이브러리 + `common/`만 사용) |
| `run_nuke.sh` | 실행기. aws-nuke 설치, 별칭 확인, 리전별 설정 생성, **리전 병렬 실행 후 global 마지막**, JSON 로그 요약, 결과 JSON |
| `cloudshell-oneshot.sh` | CloudShell에 통째로 붙여 넣는 단일 블록. 설정이 내장되어 있어 다른 파일이 필요 없다 |
| `summarize.jq`, `result.jq`, `table.jq` | 로그 요약(리전 하나), 결과 JSON 조립과 상태 판정, 사람이 읽을 표 |
| `listing_errors.json` | "listing failed"(lister 에러) 분류 규칙(정규식 목록)을 **한 곳에** 모은 파일. CloudShell 블록에도 그대로 들어 있다 |
| `tests/` | pytest. 실제 aws-nuke v3.68.3 바이너리가 만든 JSON 로그를 fixture로 쓴다 |

모든 리소스 타입 이름, 설정(setting) 이름, 필터 속성, CLI 플래그, 로그 필드는 **aws-nuke v3.68.3 소스와 libnuke v1.3.0 소스**로 확인했다. 릴리스 바이너리(`explain-config`, moto 대상 실제 실행)로도 검증했다.

## 이전 스크립트와 달라진 점

| 바뀐 것 | 이유 |
|---|---|
| 리전마다 aws-nuke 프로세스를 따로 띄워 **동시에** 실행 | aws-nuke는 리전을 하나씩 차례로 스캔하고 삭제 큐도 스레드 하나로 처리한다. 동시에 돌리면 걸리는 시간이 리전별 시간의 **합이 아니라 최댓값**이 된다 |
| `global`(IAM, CloudFront, Route 53 등)은 **리전이 모두 끝난 뒤 마지막**에 실행 | 이전에는 `global`이 먼저 돌아서 IAM 역할이 스택·Lambda보다 먼저 지워졌다. 그러면 `Role … is invalid`, Lambda ENI 고아 같은 실패가 난다 |
| `DynamoDBTableItem`, `S3Object` 제외 | Item lister는 Scan 첫 페이지와 파티션 키만 읽어 정렬 키가 있는 테이블에서는 삭제가 **반드시 실패**한다. `DeleteTable`과 `S3Bucket` 삭제가 안의 데이터를 한 번에 지운다 |
| 종료되었거나 독립 계정에서 쓸 수 없는 서비스 제외(아래 표) | `level=error listing failed` 소음과 호출당 약 30초 타임아웃이 사라져 스캔이 빨라진다 |
| `--max-wait-retries 120` | 기본값 0은 **영원히 기다린다**. 120이면 새 진척 없는 패스가 120번(약 10분 이상) 이어질 때 멈춘다 |
| `--prompt-delay 3`(최솟값) | 스캔 전과 삭제 전에 기본 10초씩 쉬던 시간을 줄인다 |
| `--parallel-queries 24`(기본 16) | 리전 안에서 동시에 도는 lister 수. 숨김 플래그라 기본값으로는 16이다. EC2 스로틀링을 고려해 32 미만으로 둔다 |
| `--log-format json` + jq 요약 | `grep -v` 대신 타입별 집계를 낸다. lister 에러를 예상됨 / SCP 의심 / 5xx / 예상 밖으로 나눠 리전마다 몇 줄로 보여 주고, LIST-ERR에는 리소스를 숨길 수 있는 것만 센다 |
| 삭제·종료 보호 해제 설정 추가 | EC2, RDS(+정지된 Aurora 클러스터 시작), CFN, DynamoDB, ELBv2, EKS, 로그 그룹, Cognito, DocDB, Neptune, QuickSight, S3 Object Lock, KMS 등 |
| 실행자(IAM 사용자 또는 역할) 자동 보호 | CloudShell에서 IAM 사용자로 돌리면 그 사용자의 정책·그룹·키·비밀번호·MFA가 지워져 **실행 도중 권한을 잃는다** |
| 별칭이 없으면 `edu-<계정ID>` 생성(삭제 단계에서만) | aws-nuke는 별칭 없는 계정을 거부한다. dry-run은 계정을 바꾸지 않도록 `--no-alias-check`로 우회한다 |
| `EduCleanupRole`, `edu-bootstrap*` 스택·역할·Lambda·로그 그룹, `EduProtectCleanupRole` 정책 보호 | 허브가 다시 들어올 수 있게 마스터키를 남긴다(CONTRACTS) |

## run_nuke.sh 사용법

필요한 것: `bash`, `aws` CLI, `jq`, `python3`(boto3는 필요 없다), `curl`/`tar`/`sha256sum`(aws-nuke 자동 설치용).

```bash
# 현재 자격 증명(CloudShell 등)으로 dry-run → 확인 후 --apply
bash nuke/run_nuke.sh
bash nuke/run_nuke.sh --apply

# 허브에서 학생 계정으로 (오케스트레이터가 이렇게 부른다)
bash nuke/run_nuke.sh --account-id 123456789012 \
  --role-arn arn:aws:iam::123456789012:role/EduCleanupRole --external-id "$EXT_ID" \
  --regions ap-northeast-2,us-east-1 --json-out state/123456789012/nuke.json [--apply]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--account-id ID` | STS로 자동 확인 | 지정하면 자격 증명의 계정과 다를 때 거부한다(exit 2) |
| `--role-arn ARN`, `--external-id X`, `--session-name N` | — | aws-nuke `--assume-role-arn`, `--assume-role-session-name`으로 넘긴다. aws-nuke가 자격 증명을 스스로 갱신한다. ExternalId는 `ps`에 보이지 않도록 환경 변수 `AWS_ASSUME_ROLE_EXTERNAL_ID`로 넘긴다 |
| `--profile P` | — | 시작 자격 증명 프로파일 |
| `--regions LIST` | `ap-northeast-2,us-east-1` | 쉼표 목록. `global`은 항상 마지막에 따로 돈다(`--no-global`로 끔) |
| `--apply` | dry-run | 실제 삭제 |
| `--log-dir DIR` | `<--json-out 폴더>/nuke-logs/<계정>-<시각>` | 리전별 설정(`config-<리전>.yaml`), 로그(`<리전>.jsonl`, `.stderr`), `result.json` |
| `--json-out FILE` | — | 결과 JSON(CONTRACTS) |
| `--protect-iam-user NAME` | 실행자가 IAM 사용자면 자동 | 반복 가능. 사용자, 관리형·인라인 정책, 그룹(+그룹 정책), 액세스 키, 로그인 프로필, MFA, SSH 키, Git·서비스 자격 증명을 남긴다 |
| `--extra-excludes LIST` | — | 추가 제외 타입(쉼표 목록, 와일드카드 가능) |
| `--blocklist LIST` | `000000000000`(자리표시) | aws-nuke가 절대 건드리지 않을 계정 ID |
| `--max-wait-retries N` | 120 | 0이면 무한 대기 |
| `--parallel-queries N` | 24 | 리전 안 동시 lister 수 |
| `--prompt-delay N` | 3 | aws-nuke 최솟값이 3이다 |

환경 변수: `AWS_NUKE_BIN`(사용할 바이너리), `AWS_NUKE_VERSION`(`v3.x.y` 또는 `latest`, 기본 `latest`), `AWS_NUKE_INSTALL_DIR`(기본 `~/.cache/edu-cleanup/bin`).
aws-nuke 탐색 순서는 `AWS_NUKE_BIN` → `PATH` → 캐시 폴더 → `~/.local/bin`(CloudShell 블록이 설치한 위치)이고, 없으면 GitHub 릴리스(linux amd64/arm64)를 받는다. 받은 파일은 `checksums.txt`로 sha256을 확인한다. 최신 버전을 확인할 수 없으면 검증한 버전 v3.68.3을 받는다. 여러 계정을 동시에 돌려도 다운로드는 `flock`으로 한 번만 한다. 바이너리는 압축 해제 후 약 300 MB다(CloudShell 홈 1 GB 중 일부를 쓴다).

**종료 코드**: `0` dry-run 정상 완료 또는 apply로 모두 삭제, `1` 실패하거나 끝나지 않은 항목이 남음(apply), `2` 실행 오류(인증 실패, 별칭에 `prod` 포함, aws-nuke 비정상 종료, 계정 불일치 등).
"listing failed"는 종료 코드를 바꾸지 않는다. 요약과 JSON에만 나온다.

## CloudShell에서 한 번에 실행하기

학생 계정에 로그인한 CloudShell에 `cloudshell-oneshot.sh`의 **내용 전체**를 붙여 넣는다(`bash cloudshell-oneshot.sh`로 실행해도 된다).

1. aws-nuke가 없으면 최신 ekristen 릴리스를 `~/.local/bin`에 설치한다(sha256 확인).
2. 계정 ID를 STS로 읽는다. 실행자가 IAM 사용자면 그 사용자와 정책·그룹·키·MFA를, 역할이면 그 역할을 보호한다.
3. **dry-run**: 서울·버지니아를 동시에 돌리고 `global`을 마지막에 돈 뒤 요약표를 보여 준다. 계정은 바꾸지 않는다.
4. `yes`를 입력하면 같은 순서로 실제 삭제한다. 별칭이 없으면 이때 `edu-<계정ID>`를 만든다.
5. 과정 리전의 기본 VPC를 다시 만들지 묻는다. aws-nuke는 기본 VPC도 지운다.

- 첫 줄 `set +H`는 꼭 남겨 둔다. 대화형 셸은 붙여 넣은 글의 `!`를 히스토리 명령으로 해석하는데, 이 줄이 그걸 끈다.
- 블록은 `( ... )` 서브셸 안에서 돈다. 중간에 실패해도 CloudShell 창은 닫히지 않는다.
- 로그와 설정은 `~/edu-nuke/`에 남는다(`dry-*.jsonl`, `apply-*.jsonl`, `config-*.yaml`).
- 리전을 바꾸려면 붙여 넣기 전에 `export EDU_REGIONS="ap-northeast-2 us-east-1 ap-northeast-1"`을 실행한다.
- CloudShell은 키보드 입력이 20~30분 없으면 세션을 끝낸다. 실행 중인 프로세스는 입력으로 치지 않는다. 오래 걸리면 가끔 Enter를 누른다.
- 설정 내용(제외 목록, settings, 필터)은 `make_config.py`와 **완전히 같다**. 테스트가 두 결과를 YAML 단위로 비교한다.

아래 블록은 `cloudshell-oneshot.sh`와 같다(테스트가 같은지 확인한다).

<!-- BEGIN CLOUDSHELL BLOCK (= cloudshell-oneshot.sh) -->
```bash
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
```
<!-- END CLOUDSHELL BLOCK -->

## 제외 목록과 근거

**제외한 타입은 절대 삭제되지 않는다.** 그래서 학생이 새 계정에서 **만들 수 없는** 것과 부모 리소스가 함께 지우는 것만 제외한다.
Q Business, Glue(`GlueDevEndpoint`만 예외), Timestream for InfluxDB, Inspector2, Redshift Serverless, CodeStar Connections처럼 **지금도 만들 수 있고 돈이 드는 서비스는 제외하지 않는다.** 이런 서비스는 lister 에러가 나도 그대로 둔다(아래 "알려진 한계").
일부 리전에서만 안 되는 서비스(Bedrock 모델 커스터마이징, Verified Access, us-west-1의 DocumentDB 등)도 제외하지 않는다. 다른 리전에서는 만들 수 있기 때문이다. 이런 에러는 요약에서 "예상됨"으로 분류한다(아래 "요약 읽는 법").
와일드카드는 libnuke가 등록된 타입 이름에 glob으로 펼친다. 실제로 펼친 결과는 `aws-nuke explain-config --with-excluded`로 확인했다(605개 중 51개 제외).

| 패턴 | 펼친 타입 | 근거 |
|---|---|---|
| `S3Object` | 1 | `S3Bucket` 삭제가 1000개 단위 `DeleteObjects`로 버킷을 비운다(공식 starter config도 제외) |
| `DynamoDBTableItem` | 1 | Scan 첫 페이지와 파티션 키만 읽어 정렬 키 테이블에서 실패하고, 패스마다 다시 스캔한다. `DeleteTable`이 아이템을 함께 지운다 |
| `OpsWorks*` | 7 | OpsWorks Stacks/Chef/Puppet 완전 종료(2024-05-01). 엔드포인트 DNS 실패 또는 타임아웃 |
| `CodeStarProject` | 1 | CodeStar 완전 종료(2024-07-25). `CodeStar*`로 넓히지 않는다. `CodeStarConnection`(CodeConnections)과 `CodeStarNotificationRule`은 살아 있다 |
| `RoboMaker*` | 3 | 완전 종료(2025-09-10) |
| `QLDBLedger` | 1 | 완전 종료(2025-07-31) |
| `ElasticTranscoder*` | 2 | 완전 종료(2025-11-13) |
| `MediaStore*` | 2 | 완전 종료(2025-11-12) |
| `InspectorAssessment*` | 3 | Inspector Classic 완전 종료(2026-05-20). `Inspector2`는 제외하지 않는다 |
| `AppMesh*` | 7 | App Mesh 지원 종료(2026-09-30) |
| `LexBot`, `LexIntent`, `LexSlotType`, `LexModelBuildingServiceBotAlias` | 4 | Lex **V1**(lexmodelbuildingservice API) 지원 종료(2025-09-15). 실제 dry-run(2026-10-15)에서 모든 리전 503. v3.68.3에는 Lex V2 타입이 없고, 새 타입이 생겨도 걸리지 않게 `Lex*` 대신 정확한 이름으로 적었다 |
| `GlueDevEndpoint` | 1 | 개발 엔드포인트는 Glue 0.9/1.0 전용인데 두 버전이 2026-04-01에 EOL. 실제 dry-run(2026-10-15)에서 17개 리전 모두 `GetDevEndpoints operation is currently disabled`. 다른 Glue 타입은 그대로 지운다 |
| `Cloud9Environment` | 1 | 신규 고객 차단(2024-07-25) |
| `CloudSearchDomain` | 1 | 신규 고객 차단(2024-07-25) |
| `DataPipelinePipeline` | 1 | 신규 고객 차단 |
| `MachineLearning*` | 4 | Amazon Machine Learning 신규 사용자 받지 않음 |
| `CloudDirectoryDirectory`, `CloudDirectorySchema` | 2 | Cloud Directory 신규 고객 차단(지원 종료 2027-07-24, 2026-10-15 문서 확인). 실제 dry-run에서 모든 리전 `Account … is not authorized for region` |
| `AWS::Timestream::*` | 3 | Timestream for LiveAnalytics 신규 고객 차단(2025-06-20). Cloud Control 타입 Database/Table/ScheduledQuery가 기본 등록되어 있다. `TimestreamInfluxDBDbInstance`는 남긴다 |
| `FMS*` | 2 | Firewall Manager는 Organizations와 관리자 계정이 필요하다. 독립 계정에서는 List가 항상 실패한다 |
| `ShieldProtection*` | 2 | Shield Advanced 구독이 있어야 한다. 보호 항목을 지워도 구독(1년 약정)은 끝나지 않으므로 해지 전 점검에서 따로 본다 |
| `ServiceCatalogTagOption*` | 2 | 무료 리소스. TagOptions를 쓴 적 없는 계정에서는 `TagOptionNotMigratedException`이 난다(공식 starter config도 제외) |

추가로 제외하려면 `--extra-excludes "GlueJob,Glue*"`처럼 지정한다. 단, 그 서비스를 학생이 못 쓰게 IAM으로 막았을 때만 그렇게 한다.

### settings(보호 해제)

`EC2Instance`(DisableStopProtection, DisableDeletionProtection), `EC2Image`(IncludeDisabled, IncludeDeprecated, DisableDeregistrationProtection), `RDSInstance`(DisableDeletionProtection, StartClusterToDelete), `CloudFormationStack`(DisableDeletionProtection, CreateRoleToDeleteStack. `--role-arn`이 EduCleanupRole이면 UseCurrentRoleToDeleteStack도 켠다), `DynamoDBTable`, `ELBv2`, `EKSCluster`, `CloudWatchLogsLogGroup`, `CognitoUserPool`, `DocDBCluster`, `DSQLCluster`, `NeptuneCluster`, `NeptuneInstance`(+DisableClusterDeletionProtection), `NeptuneGraph`, `PinpointPhoneNumber`(DisableDeletionProtection), `QuickSightSubscription`(DisableTerminationProtection), `S3Bucket`(BypassGovernanceRetention, RemoveObjectLegalHold), `LightsailInstance`(ForceDeleteAddOns), `KMSKey`(DeleteInUseKeys).

- `KMSKey.DeleteInUseKeys`: 이 설정이 없으면 서비스 grant가 남은 키는 `hold` 상태로 기다리다 `--max-wait-retries`에 걸려 exit 1이 된다. 키는 7일 대기 후 삭제된다.
- `CreateRoleToDeleteStack`: v3.68.1(#1033)부터 리전 스택에서도 다시 동작한다. 다만 만들어지는 역할에 권한 정책이 없어 대개 `DELETE_FAILED`를 거쳐 retain으로 끝난다. 그래서 스택은 `cfn_predelete`가 먼저 지우는 것이 기본이다.

### 필터(보호 대상)

| 타입 | 조건 |
|---|---|
| `IAMRole` | `Name` = `EduCleanupRole`(+ 실행자 역할), `Name` glob `edu-bootstrap*` |
| `IAMRolePolicyAttachment` | `RoleName` 같은 조건 |
| `IAMRolePolicy` | `role:RoleName` 같은 조건 |
| `IAMPolicy` | `Name` = `EduProtectCleanupRole`, 보호 사용자·그룹에 붙은 고객 관리형 정책 `ARN` |
| `CloudFormationStack` | `Name` glob `edu-bootstrap*` |
| `LambdaFunction`, `CloudWatchLogsLogGroup` | 부트스트랩 별칭 함수 `edu-bootstrap*`, `/aws/lambda/edu-bootstrap*` |
| `IAMUser` 외 사용자 소유 타입 | `IAMUser.Name`. `IAMUserPolicyAttachment`, `IAMUserGroupAttachment`, `IAMUserAccessKey`, `IAMLoginProfile`, `IAMUserMFADevice`, `IAMUserSSHPublicKey`, `IAMUserHTTPSGitCredential`, `IAMSigningCertificate`, `IAMServiceSpecificCredential`는 `UserName`. `IAMUserPolicy`는 속성이 없어 문자열 `"<user> -> *"` glob. `IAMVirtualMFADevice`는 `SerialNumber` |
| `IAMGroup`, `IAMGroupPolicyAttachment`, `IAMGroupPolicy` | 보호 사용자가 속한 그룹(관리자 권한이 그룹에서 오는 경우가 많다) |

계정 별칭: aws-nuke에는 별칭을 지우는 리소스 타입이 **없다**(별칭은 읽기만 한다). 그래서 따로 보호할 필요가 없다. 해지 전 별칭 삭제는 `preclose/`가 맡는다.

## 요약 읽는 법

```
== aws-nuke APPLY  account 123456789012 (edu-123456789012)  aws-nuke v3.68.3  412s ==
REGION          EXIT  TIME     REMOVED  FAILED  LEFT  KEPT  LIST-ERR  EXP/SCP
ap-northeast-2  1     388s          41       1     -     5         -      2/0
us-west-1       0     120s           6       -     -     5         3      8/3
global          0     24s           18       -     -    21         -        -

[ap-northeast-2] removed: EC2SecurityGroup 9, EC2Subnet 6, ...
[ap-northeast-2] FAILED: EC2VPC 1
[ap-northeast-2]   x EC2VPC vpc-0abc...
[ap-northeast-2] listing failed (expected - service not used/subscribed): BedrockCustomModel, BedrockModelCustomizationJob
[us-west-1] listing failed (expected - service not used/subscribed): BedrockCustomModel, ..., DocDBCluster, DocDBInstance, ...
[us-west-1] access denied (maybe SCP): RekognitionDataset, RekognitionProject, TimestreamInfluxDBDbInstance
[us-west-1] LISTING FAILED (5xx/throttling - re-run to confirm): BedrockFlowAlias, BedrockPrompt
[us-west-1] LISTING FAILED GlueJob: AccessDeniedException: User: arn:aws:sts::…:assumed-role/… is not authorized to perform: glue:GetJobs …
RESULT: ISSUES (exit 1)  removed 65, failed 1, not finished 0
LIST-ERR: 3 lister failure(s) - resources of those types were NOT scanned (see LISTING FAILED above)
```

- **WOULD-DELETE / REMOVED**: 지울 예정이거나 지운 항목 수. 아래 줄에 타입별 개수가 나온다.
- **FAILED**: 마지막 상태가 `failed`인 항목. aws-nuke(libnuke v1.3.0)는 실패한 항목만 남은 패스가 몇 번(2~3패스) 이어지면 포기한다. **한 번 더 돌리면** 대개 끝난다(의존성 순서 문제).
- **LEFT**: apply에서 끝나지 않은 항목(`new`/`pending`/`waiting`/`hold`). 대개 `--max-wait-retries`에 걸린 경우다.
- **KEPT**: 필터나 리소스 자체 규칙(기본 VPC 아님, AWS 관리형 등)으로 남긴 항목.
- **LIST-ERR / EXP/SCP**: lister 에러("listing failed")를 `listing_errors.json`의 정규식으로 네 갈래로 나눈다. **LIST-ERR는 리소스를 숨길 수 있는 것(아래 3, 4)만 센다.** EXP/SCP는 1과 2의 타입 수다(`-`는 0). 리전마다 1~3은 한 줄씩, 4는 타입마다 한 줄이다.
  1. `listing failed (expected - service not used/subscribed)`: 그 리전에 API가 없거나 이 계정에 열려 있지 않다(`UnknownOperationException`, `InvalidAction … not valid for this web service`, `Unrecognized engine name`, `not authorized for region`, Comprehend·Transcribe·Bedrock의 `account is not authorized to …`, `is currently disabled`, DNS 실패). 알려진 소음(FMS, Shield, ServiceCatalog TagOption, Q Business)도 여기 들어간다. **무시해도 된다.** 그 리전에서는 만들 수 없으니 숨은 리소스도 없다.
  2. `access denied (maybe SCP)`: 이유 없는 `AccessDeniedException`(Rekognition, Textract, Timestream for InfluxDB), SSM Quick Setup의 403, `service control policy` 문구가 있는 거부. 학생 계정은 리셀러 Organization의 멤버라 **우리가 모르는 SCP**가 있을 수 있고, 리전이 막힌 경우와 구분이 안 된다. 종료 코드와 LIST-ERR에는 넣지 않는다. 대개 무시해도 되지만, 그 리전에서 학생이 해당 서비스를 썼을 수 있으면 콘솔에서 한 번 보거나 `verify/`(D+2 비용)로 확인한다.
  3. `LISTING FAILED (5xx/throttling - re-run to confirm)`: 5xx·스로틀링. 일시적인 경우가 많지만 이번 스캔에서 그 타입은 **확인되지 않았다**. 다시 돌려서 사라지는지 본다.
  4. `LISTING FAILED <타입>: <에러>`: 어떤 규칙에도 안 맞는 실패(예: 역할에 권한이 없는 `… is not authorized to perform …`). **이 타입의 리소스는 한 개도 발견되지 않았다는 뜻이다**(조용한 누락). 원인을 고쳐 다시 돌리고, 계속되면 `verify/`로 확인한다. 반복해서 나오는데 리전 제한이 확실한 에러라면 `listing_errors.json`의 `expected`에 정규식과 근거(`why`)를 추가한다(CloudShell 블록의 사본도 같이 고친다. 테스트가 비교한다).
- **EXIT**: 리전별 aws-nuke 종료 코드. `aws-nuke stopped: failed`는 실패 포기, `max wait retries of 120 exceeded`는 대기 포기다. 그 밖의 fatal(예: `account is in blocklist`)은 오류다.
- 결과 JSON의 `regions.<리전>`에는 `outcome`(`completed`/`gave_up_failed`/`gave_up_wait`/`error`), `*_by_type`, `failed_items`, `listing_failed_by_type`(타입별 `count`, 대표 에러, `bucket`), `listing_failed_buckets`(버킷별 타입 목록), `listing_failed_by_bucket`(버킷별 타입 수), `listing_failed_unexpected`(LIST-ERR에 들어간 타입), `log`, `config`가 들어 있다.

## 알려진 한계

- **"listing failed"는 남는다.** aws-nuke는 엔드포인트가 아예 없는 서비스만 조용히 건너뛰고, 그 밖의 lister 에러는 모두 ERROR로 남긴다. 구독하지 않은 Q Business, 리전에 따라 안 되는 서비스(다른 리전에서는 돈이 든다), 권한이나 스로틀링 오류 같은 에러는 제외하지 않았으므로 계속 보인다. 이 모듈은 이것을 네 갈래(예상됨 / SCP 의심 / 5xx·스로틀링 / 예상 밖)로 나눠 보여 줄 뿐이다. 분류는 에러 문구 기준이라 AWS가 문구를 바꾸면 "예상 밖"으로 떨어진다(안전한 쪽).
- **조용한 누락**: lister가 실패한 타입, 설정 밖 리전, aws-nuke가 지원하지 않는 리소스(예: Bedrock imported model)는 종료 코드 0이어도 남을 수 있다. 완료 판정은 `verify/`(D+2 비용 확인)로 한다.
- 실패한 항목만 남으면 aws-nuke(libnuke v1.3.0)는 2~3패스 만에 포기한다. CloudFront 배포처럼 비활성화 전파가 필요한 리소스는 **두 번째 실행**이 필요할 수 있다.
- CloudFormation 스택 삭제는 aws-nuke 안에서 여전히 블로킹이다(최대 60분). 스택은 `cfn_predelete`로 먼저 지운다.
- 리전을 병렬로 돌리므로 리전 간 의존성(예: 다른 리전 리소스가 쓰는 IAM)은 보장하지 않는다. IAM은 마지막 `global`에서만 지운다.
- 기본 VPC도 지운다. 다음 기수가 계정을 다시 쓴다면 CloudShell 블록의 기본 VPC 재생성 단계를 쓰거나 `aws ec2 create-default-vpc`를 실행한다.
- `--parallel-queries`를 32 이상으로 올리면 EC2 `RequestLimitExceeded`가 날 수 있다.
- 실행자가 루트이면 보호할 사용자가 없다. 동작은 하지만 IAM 사용자나 역할로 실행하는 편이 낫다.

## 테스트

```bash
python3 -m pytest nuke/tests -q
# 실제 aws-nuke 바이너리가 있으면 explain-config 검증도 돈다
AWS_NUKE_BIN=~/.cache/edu-cleanup/bin/aws-nuke python3 -m pytest nuke/tests -q
```

- `test_nuke_make_config.py`: YAML 구조, 제외·필터·설정 이름이 `aws-nuke/docs/resources`에 있는지(와일드카드 전개 포함), 학생이 쓸 수 있는 서비스를 제외하지 않는지 확인한다. 실제 바이너리의 `explain-config --account-id`(자격 증명 불필요)로 설정이 파싱되는지도 본다.
- `test_nuke_summary.py`: 실제 v3.68.3 바이너리를 moto 서버에 돌려 얻은 JSON 로그(`tests/fixtures/real-*.jsonl`)와 합성 로그로 jq 요약과 상태 판정을 확인한다. `real-cloudshell-v3.68.3-listing-failed.jsonl`은 실제 CloudShell 17개 리전 dry-run(2026-10-15)의 LISTING FAILED 줄을 에러 문구 그대로 JSON 로그 형식으로 되돌린 것으로, 버킷 분류·한 줄 요약·LIST-ERR를 확인한다.
- `test_nuke_shell.py`: shellcheck, 가짜 `aws`/`aws-nuke`로 `run_nuke.sh`를 끝까지 실행한다(리전 병렬 후 global, 별칭, 보호, 종료 코드). CloudShell 블록이 만드는 설정이 `make_config.py`와 같은지, 블록에 든 분류 규칙·jq 코드가 `listing_errors.json`·`summarize.jq`와 같은지 비교한다. 대화형 bash(pty)에 블록을 붙여 넣어 dry-run → yes → apply → 기본 VPC까지 확인한다.
