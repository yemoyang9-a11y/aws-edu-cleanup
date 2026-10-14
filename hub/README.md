# hub — 정리 계정(허브)과 다계정 오케스트레이터

교육이 끝난 학생 계정 수십 개를 한 곳에서 dry-run → 검토 → 정리 → D+2 검증까지 돌리는 모듈입니다.
AWS Organizations 없이 standalone 계정만 쓰는 환경을 전제로 합니다.

## 개념: 정리 계정 = 관리실 마스터키

- 회사 소유 standalone 계정 하나를 **정리 계정(허브)** 으로 정합니다. 오케스트레이터는 여기서만 돕니다.
- 각 학생 계정에는 처음 한 번 **자물쇠**를 달아 둡니다. 이 자물쇠가 `EduCleanupRole`(AdministratorAccess)입니다.
  이 역할은 허브의 지정된 역할/사용자 하나가 정해진 `ExternalId`를 낼 때만 열립니다.
- 허브는 이 마스터키로 각 학생 계정에 들어가 `cfn_predelete → nuke → preclose`를 차례로 실행합니다.
  여러 계정은 동시에 처리합니다.
- Organizations가 없으면 바깥에서 자물쇠를 달 방법이 없습니다. 그래서 **계정마다 루트로 한 번** quick-create 링크를 열어야 합니다.
  계정당 1분 정도 걸리고, 학생 IAM 사용자를 만들려고 루트로 로그인하는 그 세션에서 같이 하면 됩니다.
- **registry.json = 허용 목록**입니다. 여기에 없는 계정은 절대 건드리지 않습니다.
  계정마다 실행 전에 세 가지를 확인하고(preflight), 하나라도 다르면 `FAILED_PREFLIGHT`로 건너뜁니다.
  - AssumeRole로 들어간 계정 ID가 registry의 ID와 같은지
  - 계정 별칭이 registry의 별칭과 같은지
  - 대상이 허브 계정 자신이 아닌지

```
[허브 계정] orchestrate.py ── AssumeRole(EduCleanupRole, ExternalId) ──▶ [학생 계정 N개, 병렬]
   registry.json (허용 목록)                                          ├ cfn_predelete/predelete_stacks.py
   state/<기수>/<계정>/ (상태·로그)                                    ├ nuke/run_nuke.sh
                                                                     ├ preclose/preclose_check.py
                                                                     └ (D+2) verify/verify_cost.py
```

## 파일

| 파일 | 내용 |
|---|---|
| `bootstrap.yaml` | 학생 계정에 설치하는 CloudFormation(스택 이름 **`edu-bootstrap`**). 설치 항목: `EduCleanupRole`, 학생 그룹에 붙일 수 있는 보호 정책 `EduProtectCleanupRole`(부착하지 않은 상태로 만듦), 선택 사항인 계정 별칭 |
| `make_quick_create_link.py` | 계정별 quick-create 링크와 같은 동작의 `aws cloudformation create-stack` 명령을 출력 |
| `hub-policy.json` | 허브 역할/사용자에 붙일 정책(`sts:AssumeRole` on `arn:aws:iam::*:role/EduCleanupRole`) |
| `registry.example.json` | 기수 registry 예시 |
| `orchestrate.py` | `plan`/`clean`/`verify`/`status`/`killswitch` 오케스트레이터 |
| `hub_registry.py` | registry 검증(12자리 ID·중복 금지, 별칭에 `prod` 금지, ExternalId 8자 이상) |

## 1. 허브 1회 준비

1. **허브 역할(또는 사용자) 만들기** — 예: 역할 `EduHubOrchestrator`.
   - 이 역할에 `hub-policy.json` 정책을 붙입니다.
   - 오케스트레이터를 EC2·CodeBuild에서 돌린다면 그 서비스가 이 역할을 쓰게 합니다. 노트북에서 돌린다면 이 역할을 맡는 프로필을 만듭니다.
   - 학생 계정 쪽 신뢰 정책은 `arn:aws:iam::<허브ID>:root` + `aws:PrincipalArn` 조건으로 걸려 있습니다. 그래서 허브 역할을 같은 이름으로 다시 만들어도 신뢰가 끊기지 않습니다.
   - 역할 체이닝(역할에서 다시 역할을 맡는 것)이면 세션이 1시간으로 제한됩니다. 그래도 `common/session.py`와 aws-nuke(`--assume-role-arn`)가 자격 증명을 스스로 갱신합니다.
2. **ExternalId 만들기** — 기수마다 하나씩. 예:
   `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`.
   AWS는 ExternalId를 비밀로 취급하지 않습니다. 그래도 계정을 잘못 고르는 실수를 막아 줍니다.
3. **템플릿을 S3에 올리기** — quick-create 링크는 **학생 계정의 콘솔**이 S3에서 템플릿을 읽어 옵니다.
   그래서 학생 계정이 그 객체를 읽을 수 있어야 합니다. 방법은 둘 중 하나입니다.
   - (권장) 버킷 정책으로 학생 계정 ID들에 `s3:GetObject` 권한을 줍니다(템플릿 객체 하나만).
     `make_quick_create_link.py --registry … --bucket-policy`가 정책 JSON을 출력합니다. 버킷은 SSE-S3로 둡니다.
     KMS 고객 관리형 키를 쓰면 학생 계정에 KMS 권한도 줘야 해서 번거롭습니다.
   - 또는 템플릿 객체 하나만 공개로 둡니다. 템플릿에는 비밀이 없지만(ExternalId는 파라미터로 넘깁니다), 버킷의 퍼블릭 액세스 차단을 풀어야 합니다.
   ```bash
   aws s3 cp hub/bootstrap.yaml s3://<버킷>/edu/bootstrap.yaml
   # 템플릿 URL: https://<버킷>.s3.<리전>.amazonaws.com/edu/bootstrap.yaml
   ```
4. **Cost Explorer는 학생 계정마다 콘솔에서 한 번 켜야 합니다.** API로는 켤 수 없습니다.
   부트스트랩을 하는 루트 세션에서 *Billing and Cost Management → Cost Explorer*를 한 번 열어 두세요.
   D+2 `verify`가 Cost Explorer API를 쓰고, 데이터는 켠 뒤 약 24시간 후부터 쌓입니다.

## 2. 기수마다 하는 일

### ① registry 작성
```bash
cp hub/registry.example.json registry-2026-10.json   # external_id, accounts 채우기
```
- `account_id`는 반드시 **따옴표가 있는 12자리 문자열**이어야 합니다. 숫자로 쓰면 앞자리 0이 사라집니다.
- `alias`는 `edu-<기수>-<번호>` 형식으로 쓰고 `prod`가 들어가면 안 됩니다. 비워 두면 별칭 검사를 건너뜁니다(권장하지 않음).
- `killswitch`의 키는 `killswitch/killswitch.yaml` 파라미터 이름과 같아야 합니다: `TokenThreshold`, `PeriodMinutes`, `TargetMode`, `TargetGroupName`, `ExemptUserNames`, `NotificationEmail`. `EnableApiTriggers`는 생략하면 `"true"`입니다.

### ② 링크 생성 → 계정마다 루트로 열기
```bash
python3 hub/make_quick_create_link.py --registry registry-2026-10.json \
  --template-url https://<버킷>.s3.ap-northeast-2.amazonaws.com/edu/bootstrap.yaml \
  --hub-principal-arn arn:aws:iam::<허브ID>:role/EduHubOrchestrator \
  --csv links-2026-10.csv
```
계정마다 할 일은 다음과 같습니다.
1. **해당 계정의 루트로 로그인**한 브라우저에서 링크를 엽니다.
2. ExternalId 칸을 확인합니다.
3. *"AWS CloudFormation이 사용자 지정 이름으로 IAM 리소스를 생성할 수 있음을 승인합니다"*를 체크하고 **스택 생성**을 누릅니다.

> ⚠️ `ExternalId`는 NoEcho 파라미터입니다. AWS 문서에 따르면 quick-create 링크는 **NoEcho 파라미터를 무시**합니다.
> 그래서 칸이 비어 있을 수 있습니다. 비어 있으면 registry의 `external_id`를 붙여넣으세요.
> 또는 그 계정의 CloudShell(루트)에서 함께 출력된 `aws cloudformation create-stack …` 줄을 실행하세요.

생성이 끝나면 학생 그룹에 `EduProtectCleanupRole` 정책을 붙이세요(출력 `ProtectPolicyArn`).
이 정책이 없으면 학생이 정리 역할을 지우거나 고칠 수 있습니다. `iam:*Role*`에는 `iam:PassRole`도 포함되므로, 학생이 이 관리자 역할을 서비스에 넘겨 권한을 올리는 것도 막습니다.

### ③ (선택) Bedrock 차단 스위치 배포
```bash
python3 hub/orchestrate.py killswitch --registry registry-2026-10.json
```
계정×리전마다 스택 `edu-killswitch-<리전>`을 만들거나 업데이트합니다(`CAPABILITY_IAM`). 변경 사항이 없으면 `NO_CHANGE`로 표시합니다.

### ④ plan(dry-run) → 검토
```bash
python3 hub/orchestrate.py plan --registry registry-2026-10.json --max-parallel 10
```
- `state/2026-10/<계정>/cfn_predelete.log`, `nuke.log`, `preclose.log`, `*.json`에서 삭제 예정 목록과 해지 전 차단 요소를 검토합니다.
- 실행할 때마다 `runs/<run-id>/`에 사본이 남으므로, 나중에 실행해도 검토한 plan 결과가 지워지지 않습니다.

### ⑤ clean(실제 삭제)
```bash
python3 hub/orchestrate.py clean --registry registry-2026-10.json
```
- 터미널에서는 기수 이름을 직접 입력해야 진행됩니다. 자동화 환경에서는 `--yes`를 씁니다.
- `plan`이 성공적으로 기록되지 않은 계정은 건너뜁니다. 그래도 실행하려면 `--allow-unplanned`를 씁니다.
- 단계별 처리: 종료 코드 2(오류)가 나면 그 계정의 파이프라인을 멈춥니다. 1(잔여 있음)이면 기록만 하고 다음 단계로 넘어갑니다.
- `preclose`는 기본적으로 보고만 합니다. 수정까지 하려면 `--fix-preclose`를 붙입니다.
  예: `clean --steps preclose --fix-preclose`. 이렇게 일부 단계만 다시 돌리면 나머지 단계의 마지막 결과는 그대로 유지됩니다.
- 특정 계정만: `--accounts 111111111111,222222222222`

### ⑥ D+2 verify
```bash
python3 hub/orchestrate.py verify --registry registry-2026-10.json   # --cleanup-date 생략 시 마지막 clean 날짜
```
결과는 `VERIFIED_CLEAN`(0), `RESIDUAL`(1), `PENDING`(3, 아직 판단하기 이른 시점)입니다. `PENDING`이면 하루 뒤에 다시 실행합니다.

### ⑦ 해지 전 수정 → 루트 콘솔에서 해지
- `preclose` 보고서의 차단 요소를 처리합니다(`--fix-preclose` 또는 수동). 차단 요소의 예: Marketplace 구독, RI/SP, Route 53 도메인, CloudTrail 트레일, 별칭.
- **standalone 계정 해지는 API가 없고 루트 콘솔에서만 할 수 있습니다.** 계정마다 루트로 로그인해 *계정 → 계정 해지*를 진행합니다.
- 별칭이 이미 지워졌다면 `verify`는 별칭이 *없는* 상태만 허용합니다. 다른 별칭이 붙어 있으면 여전히 거부합니다.

### 상태 확인
```bash
python3 hub/orchestrate.py status --registry registry-2026-10.json
```

## 상태 머신과 결과 파일

```
NEW → PLANNED → CLEANING → CLEANED | PARTIAL | FAILED → VERIFIED_CLEAN | RESIDUAL | PENDING
                                     (그 밖에) FAILED_PREFLIGHT, PLAN_FAILED, VERIFY_FAILED
```

| 위치 | 내용 |
|---|---|
| `state/<기수>/<계정>/state.json` | 현재 상태, 전이 이력(시각·사유), 단계별 종료 코드·소요 시간, preflight 결과 |
| `state/<기수>/<계정>/<단계>.log` / `.json` | 단계의 stdout+stderr / `--json-out` 결과 |
| `state/<기수>/<계정>/.lock` | 계정 잠금. 같은 계정을 동시에 두 번 돌리지 못하게 합니다 |
| `state/<기수>/summary.json`, `summary.csv` | 기수 전체 요약. CSV는 엑셀에서 바로 열리도록 utf-8-sig로 저장합니다 |

- 종료 코드: 0 = 선택한 계정이 모두 정상, 1 = 하나라도 문제, 2 = registry나 설정 오류.
- 잠금이 오래된 것(stale)으로 판단되는 경우는 두 가지입니다. 이때는 자동으로 치우고 진행합니다.
  - 같은 호스트에서 잠금을 잡은 프로세스가 이미 죽었을 때
  - 잠금을 잡은 지 `--lock-ttl-hours`(기본 24시간)가 지났을 때
- Ctrl-C나 SIGTERM을 받으면 실행 중인 단계를 종료하고 잠금을 풉니다.

## 어디서 돌릴까

- **CloudShell은 긴 실행에 쓰지 마세요.** 키보드나 마우스 입력이 20~30분 없으면 세션이 끝납니다. 출력이 계속 흘러도 입력으로 치지 않습니다. 계정 1~2개를 빠르게 확인할 때만 쓰세요.
- 권장하는 실행 환경:
  - 노트북: `tmux`나 `screen` 안에서 실행합니다.
  - 허브 계정의 작은 EC2: 인스턴스 프로파일로 허브 역할을 씁니다. 역할 체이닝이 아니므로 1시간 제한이 없습니다.
  - CodeBuild: 동시 빌드 쿼터를 먼저 확인합니다.
- 필요한 것: Python 3.9 이상과 boto3, bash, aws-nuke 바이너리(`nuke/` 모듈 README 참고), 그리고 이 저장소 전체.

```bash
# 허브 EC2/노트북 복사-붙여넣기 블록
git clone <이 저장소> edu-cleanup && cd edu-cleanup
export AWS_PROFILE=edu-hub            # 허브 역할 프로필 (EC2 인스턴스 프로파일이면 생략)
REG=registry-2026-10.json
python3 hub/orchestrate.py plan   --registry "$REG"
python3 hub/orchestrate.py status --registry "$REG"
# 로그 검토 후
python3 hub/orchestrate.py clean  --registry "$REG"
# D+2
python3 hub/orchestrate.py verify --registry "$REG"
```

## 안전 장치 요약

- registry가 허용 목록입니다. `--accounts`에 registry 밖의 ID를 넣으면 실행 전에 오류로 끝납니다(종료 코드 2).
- preflight 검사:
  - AssumeRole 뒤 `GetCallerIdentity` 계정이 registry ID와 같은지
  - 별칭이 registry와 같은지(`prod`가 들어 있으면 거부)
  - 대상이 허브 계정 자신이 아닌지
- 기본은 dry-run입니다. `clean`만 `--apply`를 넘기고, `preclose`는 `--fix-preclose`를 줄 때만 넘깁니다.
- `clean` 전에 `plan`이 있어야 하고, 터미널에서는 기수 이름을 입력해 확인합니다.
- ExternalId는 로그와 state에 `***`로 가려서 기록합니다(단계 스크립트에는 그대로 넘깁니다).
- 모든 모듈은 `EduCleanupRole`(정책 포함)과 `edu-bootstrap*` 스택을 지우지 않습니다(CONTRACTS.md).
  - 부트스트랩의 별칭용 Lambda·역할·로그 그룹 이름도 모두 `edu-bootstrap-`으로 시작합니다.
  - 이것들은 `edu-bootstrap` 스택이 소유하므로, 스택을 지우면 함께 지워집니다.
- 신뢰 정책의 두 번째 구문(`cloudformation.amazonaws.com`)은 `cfn_predelete`를 위한 것입니다. 스택의 서비스 역할이 먼저 지워졌을 때 `EduCleanupRole`을 `RoleARN`으로 넘겨 `DeleteStack`을 실행하는 데 씁니다.
  - 이 구문에는 소스 조건이 없습니다. 그래서 그 계정에서 `iam:PassRole` 권한이 있는 주체라면 이 역할을 CloudFormation에 넘길 수 있습니다.
  - 학생 그룹에 `EduProtectCleanupRole`을 꼭 붙이세요. 이 정책이 `iam:PassRole`도 막습니다.
- `EduCleanupRole`의 최대 세션은 12시간입니다. 다만 허브에서 역할 체이닝으로 맡으면 1시간이 상한이고, 각 모듈이 자격 증명을 자동으로 갱신해서 이어 갑니다.

## 테스트

```bash
python3 -m pytest hub/tests -q     # moto + 가짜 단계 스크립트, 실제 AWS 호출 없음
cfn-lint hub/bootstrap.yaml
```
단계 스크립트 위치는 `--repo-root` 또는 환경 변수 `EDU_CLEANUP_REPO_ROOT`로 바꿀 수 있습니다. 테스트는 이 방법으로 가짜 스크립트를 씁니다.
