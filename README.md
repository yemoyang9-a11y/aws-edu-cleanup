# edu-cleanup — AWS 교육 계정 정리 도구

교육생마다 AWS 계정을 하나씩 주는 교육에서, 교육이 끝난 계정을 **빠르고, 빠짐없이, 해지 후 요금 없이** 정리하기 위한 도구 모음입니다. aws-nuke를 대체하지 않고 앞뒤를 감쌉니다.

## 전제: 리셀러 조직의 멤버 계정

- 교육 계정은 독립 계정이 아니라 **AWS 리셀러 조직(AWS Organizations)의 멤버 계정**입니다. 회사는 리셀러에게서 루트·IAM 자격 증명을 받아 관리하지만, 조직(관리 계정)은 회사가 통제하지 않습니다.
- 리셀러의 SCP가 비용·청구 화면을 루트까지 막고(Cost Explorer 거부), 회사용 SCP를 따로 걸어 주지도 않습니다. 청구서는 리셀러를 통해 옵니다.
- 그래서 이 도구는 SCP나 비용 데이터에 기대지 않고 **계정 안의 IAM 수준 통제**(`killswitch`)와 **리소스 기반 검증**(`verify` inventory 모드)으로 동작합니다.
- `hub`도 똑같이 동작합니다. 각 멤버 계정 안에 설치한 `EduCleanupRole`을 AssumeRole합니다(조직 기능은 쓰지 않음).
- 해지는 루트 콘솔(*Account → Close account*) 또는 관리 계정(리셀러)이 할 수 있고, 리셀러 SCP가 막으면 리셀러에 요청합니다. 해지 후 90일 동안 조직에 남고, 요금·환불 문의(예: Bedrock 과다 사용 면제)는 리셀러를 거칩니다. `preclose`가 조직 소속을 확인해 이 안내를 보여 줍니다.

## 가장 쉬운 사용법 (계정 하나, 비전공자용)

교육생 계정의 루트로 콘솔에 로그인 → CloudShell을 열고 아래 한 줄을 붙여 넣습니다.

```bash
echo ; cd ~ && (git -C aws-edu-cleanup pull -q 2>/dev/null || git clone -q https://github.com/yemoyang9-a11y/aws-edu-cleanup.git) && bash ~/aws-edu-cleanup/cleanup.sh
```

맨 앞의 `echo ;`는 붙여 넣을 때 명령 앞에 보이지 않는 글자가 붙어 `command not found`가 나는 것을 막습니다.

1. 지울 목록을 먼저 보여 줍니다(미리보기, 아무것도 지우지 않음).
2. **계정 번호 12자리**를 입력하면 삭제합니다. 그냥 Enter를 누르면 취소됩니다.
3. 남은 리소스가 있으면 5분 간격으로 최대 3번까지 다시 지웁니다.
4. 마지막에 큰 글씨로 결과가 나옵니다.
   - **✅ 정리 완료** → 끝. 창을 닫으면 됩니다.
   - **⚠️ / ❌** → 화면에 나온 보고서 파일(`~/cleanup-report-...txt`)을 담당자에게 보내 주세요. CloudShell 오른쪽 위 *작업 → 파일 다운로드*로 받을 수 있습니다.

단계별 소요 시간은 결과 바로 위에 표로 나옵니다. 이미 끝난 실행을 다시 보려면 같은 CloudShell에서 `python3 ~/aws-edu-cleanup/tools/timing.py`(가장 최근 실행) 또는 `… timing.py <계정번호>`를 실행합니다.

기존 IAM 사용자는 기본으로 **유지**합니다. 옵션: `--preview-only`(미리보기만), `--skip-preview`(미리보기 생략), `--delete-iam-users`(IAM 사용자도 삭제), `--max-passes N`, `--wait-min M`, `--cfn-wait-min M`(CloudFormation 스택을 기다리는 시간, 기본 5분. 더 오래 걸리는 스택은 AWS가 계속 지우는 동안 다음 회차에서 다시 요청).

## 원본 aws-nuke와 무엇이 다른가

aws-nuke 코드는 고치지 않고 그대로 씁니다. 바꾼 것은 **실행 방식과 앞뒤 단계**입니다.

| 원본 aws-nuke에서 겪던 일 | 이 도구에서는 |
|---|---|
| 17개 리전을 하나씩 순서대로 조회 | 4개씩 동시에 조회. 같은 로그 기준 조회 한 번에 5분 51초 → 2분 33초 (추정, 약 2.3배) |
| 삭제 중인 CloudFormation 스택을 만나면 그 리전 전체가 최대 60분씩, 최대 4번 멈춤. 화면에는 아무것도 안 나옴 | 스택은 따로 처리하고 회차마다 최대 5분만 기다림. 53분 넘게 멈췄던 리전이 같은 계정에서 153초에 끝남 (1개 계정 관찰) |
| 지워지지 않는 리소스를 끝없이 기다림 (기본값이 무한 대기) | 기다리는 횟수에 상한을 둠 |
| 안 쓰는 서비스나 권한 없는 서비스의 오류가 수백 줄 출력 | 조회 한 번에 나온 139건을 분류해서, 확인이 필요한 2건만 따로 표시 |
| DynamoDB 항목과 S3 객체를 하나씩 지우다 실패 | 테이블과 버킷째로 삭제 |
| 끝나도 다 지워졌는지 알 수 없음 | 79개 유형을 다시 조회해 ✅/❌로 표시하고, 남으면 최대 3회차까지 다시 지움 |
| 해지 후에도 나오는 요금(Marketplace 구독 등)은 범위 밖 | 14개 항목을 점검하고 구독을 해지 |

### 실제 계정 결과 (2026-10-15, 교육 계정 8개)

- **실습 스택이 없는 계정:** 13~23분 걸렸습니다.
- **실습 스택(VPC 안의 Lambda)이 있는 계정:** 수정 전 버전에서 51분 걸린 경우가 있었고, 그중 31분이 스택 대기였습니다. AWS가 Lambda의 네트워크 카드를 회수하는 시간은 어떤 도구로도 줄일 수 없습니다.
- 결과를 끝까지 확인한 **5개 계정은 모두 CLEAN**이었습니다.

### 숫자를 읽을 때 주의할 점

- **같은 계정에서 원본 aws-nuke 전체를 돌려 잰 값은 없습니다.** 원본과의 비교는 aws-nuke 소스 코드의 동작과, 이 도구의 로그로 계산한 추정입니다.
- 5개 계정이 모두 CLEAN이었다고 해서 **오류율이 0%라는 뜻은 아닙니다.** 표본이 5개뿐이라 통계적으로는 남은 리소스가 생길 확률이 최대 45%여도 이런 결과가 나올 수 있습니다.
- 확인 단계는 79개 유형만 봅니다(aws-nuke는 약 605개 유형). 최종 확인은 리셀러 청구서로 해야 합니다.
- 스택 처리 개선(`b89f784`, `4c2ea10`)은 아직 1개 계정에서만 돌려 봤습니다.

계정별 수치와 근거(측정/소스 확인/추정/보고 구분)는 [docs/RESULTS.md](docs/RESULTS.md)에 있습니다.

## 폴더별 역할

| 폴더 | 하는 일 | 해결하는 문제 |
|---|---|---|
| `nuke/` | aws-nuke 설정 생성 + 리전 병렬 실행기, CloudShell 원샷 블록 | DynamoDB 삭제 실패, 에러 로그 폭주, 끝없는 대기, 느린 순차 실행 |
| `cfn_predelete/` | nuke 전에 CloudFormation 스택을 병렬로 먼저 삭제 (Express → 실패 시 강제 삭제) | 스택 하나가 nuke 전체를 최대 60분씩 멈추는 문제, 역할이 먼저 지워져 스택 삭제 실패 |
| `killswitch/` | Bedrock 토큰 사용량이 임계치를 넘으면 몇 분 안에 교육생 Deny + 키 비활성화 | Budgets(8~12시간 지연)로는 못 막는 Bedrock 과금·해킹 |
| `preclose/` | 해지 직전 점검: 조직 소속, Marketplace 구독, CloudTrail, 휴지통 규칙, 약정 상품, 도메인 자동 갱신 | 해지 후에도 나오는 요금 |
| `verify/` | 정리 직후 남은 과금 리소스를 리전별로 조회해 판정 (`--mode inventory`; 비용 데이터를 볼 수 있는 계정이면 `cost`/`auto`) | nuke가 "성공"인데 리소스가 남는 조용한 누락 |
| `cleanup.sh`, `tools/` | 위 단계를 한 번에 실행하고 ✅/⚠️/❌ 결과와 담당자용 보고서를 만듦 | 로그를 읽을 줄 몰라도 성공 여부를 알 수 있게 |
| `hub/` | 정리 계정에서 여러 계정을 한 번에 돌리는 오케스트레이터 + 계정 생성 시 심는 부트스트랩 링크 | 계정마다 루트 로그인하는 수작업 |

자세한 사용법은 각 폴더의 README에 있고, 모든 README에 CloudShell에 그대로 붙여 넣는 블록이 들어 있습니다.

## 교육 한 기수 기준 순서

```
교육 전   hub: 계정 생성 → 루트로 부트스트랩 링크 1회 클릭 (정리 역할 설치)
          killswitch: 계정·리전마다 차단 스위치 배포 (hub의 killswitch 명령으로 일괄)
교육 후   hub plan   → 결과 확인 (cfn_predelete → nuke → preclose, 모두 dry-run)
          hub clean  → 실제 삭제
          hub verify → 바로 이어서 남은 리소스 확인 (inventory 모드, CLEAN 확인)
해지 전   preclose --apply 로 남은 구독·트레일 정리 → 루트 콘솔에서 해지 (막히면 리셀러에 요청)
```

계정 한두 개라면 hub 없이 CloudShell에서 `cfn_predelete` → `nuke/cloudshell-oneshot.sh` → `preclose` → `verify` 순서로 각 README의 블록을 붙여 넣으면 됩니다.
verify는 기다릴 필요 없이 정리 직후 실행합니다: `python3 verify/verify_cost.py --mode inventory --all-regions`

## 실행 팁

- 셸에서 변수 이름을 `GROUPS`로 짓지 마세요. bash 내장 변수라 값이 덮어써지지 않습니다. `RGROUPS`처럼 다른 이름을 씁니다.
- 모든 리전을 nuke할 때는(`nuke/run_nuke.sh`) 리전을 4개씩 묶어 `--no-global`로 실행하고, IAM 같은 전역 리소스는 마지막에 `--regions global`로 한 번만 실행합니다.
  ```bash
  RGROUPS=("ap-northeast-2,us-east-1,us-east-2,us-west-2" "ap-northeast-1,ap-southeast-1,eu-west-1,eu-central-1")
  for g in "${RGROUPS[@]}"; do bash nuke/run_nuke.sh --regions "$g" --no-global; done   # 확인 후 --apply
  bash nuke/run_nuke.sh --regions global
  ```

## 공통 규칙

- 모든 스크립트는 **기본이 dry-run**이고, `--apply`를 줄 때만 실제로 바꿉니다.
- 종료 코드: `0` 정상/깨끗함, `1` 문제 남음, `2` 실행 오류, `3` 아직 판정 이르음(verify).
- 보호 대상: `EduCleanupRole` 역할과 `edu-bootstrap*` 스택은 어떤 모듈도 지우지 않습니다.
- 모듈 간 규칙은 `CONTRACTS.md`에 있습니다.

## 검증 상태

- 단위·통합 테스트 500여 개 (moto 모의 AWS, 실제 aws-nuke v3.68.3 바이너리 포함), cfn-lint, shellcheck 통과.
- 모의 AWS 위에서 hub → 다른 계정 역할 전환 → 스택 병렬 삭제 → nuke까지 end-to-end로 확인했습니다.
- 2026-10-15 실제 교육 계정 8개(리셀러 조직 멤버)에서 17개 리전 조회·삭제·검증을 실행했습니다. 결과를 끝까지 확인한 5개는 모두 CLEAN이었습니다.
  - 실습 스택이 없는 계정은 13~23분, 스택이 있는 계정은 스택 삭제 대기 때문에 50분 이상 걸린 경우가 있었습니다.
  - 원본 aws-nuke와의 비교, 계정별 측정값, 정확도의 한계는 [docs/RESULTS.md](docs/RESULTS.md)에 정리했습니다.
- 계정 해지는 회사 쪽에서는 루트 콘솔로 합니다. 해지 API(`CloseAccount`)는 조직 관리 계정(리셀러)만 쓸 수 있습니다.

## 라이선스

MIT. aws-nuke(MIT)를 실행할 뿐 코드를 포함하지 않습니다.
