# verify — 정리 후 검증 (모듈 7)

aws-nuke가 종료 코드 0으로 끝나도 리소스가 남을 수 있다. 예를 들면 lister 하나가 에러로 통째로 빠지거나,
목록 첫 페이지만 읽거나, Bedrock 임포트 모델처럼 지원하지 않는 타입이 있거나, 설정에 없는 리전에 리소스가 있는 경우다.
`verify_cost.py`는 이것을 **읽기 전용**으로 확인한다. 판정 방식은 `--mode`로 고른다.

| `--mode` | "정리 완료"의 뜻 | 언제 |
|---|---|---|
| `inventory` | 라이브 인벤토리(약 80개 리소스 유형, 리전별 + 글로벌)에서 보호 대상 밖 리소스가 **하나도 없다**. Cost Explorer를 **절대 호출하지 않는다** | **리셀러(리셀러) 멤버 계정의 기본 경로.** 정리 직후 바로 실행 |
| `cost` | 정리일 다음 날(UTC)부터 어제(UTC)까지 Cost Explorer에 허용 목록 밖 사용 요금이 없다 (D+2) | Cost Explorer를 쓸 수 있는 독립 계정 |
| `auto` (기본) | `cost`를 시도한다. Cost Explorer가 거부(AccessDenied, SCP 명시적 거부)·미활성화·데이터 없음이면 **exit 2로 끝내지 않고** `inventory`로 판정하고 그 사실을 요약과 JSON(`ce_fallback`)에 남긴다 | 허브 오케스트레이터처럼 계정 종류를 모를 때 |

| 판정 | exit | 뜻 |
|---|---|---|
| `CLEAN` | 0 | 보호 대상 밖 잔여물이 없다 (cost 모드: 허용 목록 밖 요금도 없다) |
| `RESIDUAL` | 1 | 인벤토리에서 리소스가 발견됐거나 (cost 모드) 허용 목록 밖 요금 줄이 있다 |
| `ERROR` | 2 | 판정할 수 없다 (자격 증명 오류, 인벤토리 검사가 하나도 성공하지 못함, cost 모드의 Cost Explorer 오류 등) |
| `PENDING` | 3 | cost 모드에서 아직 이르다 (데이터 지연). inventory 모드에는 없다 |

## 멤버 계정과 Cost Explorer (중요)

교육 계정은 독립 계정이 아니라 **리셀러(AWS 재판매 파트너)의 AWS Organization에 속한 멤버 계정**이다. 리셀러의 SCP가
**비용·결제 접근을 루트까지 전부 막는다**(`ce:*` 포함). 리셀러는 이 설정을 바꾸지 않는다. 그래서 이 계정들에서는
Cost Explorer로 "요금 0"을 확인할 수 없고, **inventory 모드가 기본 경로**다.

- 정리(aws-nuke) **직후 바로** 실행한다. settle-day 대기가 없다. `--cleanup-date`는 넣지 않아도 된다(넣으면 기록만 한다).
- 비용 안전망이 없으므로 인벤토리를 넓혔다(아래 목록). 하나라도 발견되면 `RESIDUAL`이다.
- **한계:** 목록에 없는 리소스 유형은 잡지 못한다. 실제 요금이 0인지는 **리셀러 청구서(월별 인보이스)로만** 확정할 수 있다.
  해지 전에 의심되면 콘솔에서 Tag Editor(Resource Groups → Tag Editor, 모든 리전·모든 유형)로 한 번 더 훑어본다.

## CloudShell 복사-붙여넣기 (한 블록, 멤버 계정 기본)

검증할 학생 계정의 CloudShell(아무 리전)에서 정리 직후에 실행한다. 바꿀 값은 없다.

```bash
REPO=~/edu-cleanup               # CloudShell에 올리거나(Actions → Upload file 후 unzip) clone 한 레포 경로
ACCT=$(aws sts get-caller-identity --query Account --output text)
python3 "$REPO/verify/verify_cost.py" --mode inventory --all-regions \
  --json-out ~/"verify-$ACCT.json"; \
echo "exit=$?  (0 CLEAN / 1 RESIDUAL / 2 ERROR)  결과: ~/verify-$ACCT.json"
```

- `--all-regions`는 `account:ListRegions`로 활성 리전을 모두 본다(막혀 있으면 `ec2:DescribeRegions`로 대체).
  빼면 기본 `--regions ap-northeast-2,us-east-1`만 본다. 리전 수에 따라 1~3분 걸린다.
- `RESIDUAL`이면 요약의 표와 예시 ID를 보고 지운 뒤(또는 그 리전/유형을 넣어 aws-nuke를 다시 돌린 뒤) **같은 명령을 바로 다시** 실행한다.
  ID 뒤에 `(deleting)`처럼 삭제 진행 중 상태가 붙어 있으면 몇 분 뒤 다시 실행하면 사라진다.
- 처음 몇 계정은 결과를 눈으로 확인한다. 리셀러가 모든 멤버 계정에 깔아 두는 리소스(StackSet 스택, 조직 접근 역할 등)가
  기본 보호 목록에 없어 매번 잡히면 `--protect-prefix <접두사>`를 추가한다(반복 가능).

허브 계정에서 학생 계정을 검증할 때는 다음과 같이 실행한다.

```bash
python3 verify/verify_cost.py --mode inventory --role-arn arn:aws:iam::<ACCOUNT_ID>:role/EduCleanupRole \
  --external-id <EXTERNAL_ID> --json-out out/<ACCOUNT_ID>-verify.json
```

허브 오케스트레이터는 `--mode`를 넘기지 않으므로 `auto`로 돈다. 멤버 계정이면 Cost Explorer 거부를 보고 inventory로 판정한다.
정리일이 오늘이라 Cost Explorer로 볼 날짜가 아직 없을 때도, `auto`는 Cost Explorer를 쓸 수 있는지 **한 번만** 확인한다($0.01).
거부되면 inventory로 판정하고, 쓸 수 있으면 `PENDING`이다.

## 보호 대상 (잔여물로 세지 않음)

- 기본 접두사: `edu-bootstrap`, `EduCleanup`(→ `EduCleanupRole`), `edu-killswitch`, 그리고 리셀러 조직이 만드는
  `OrganizationAccountAccessRole`, `StackSet-`, `stacksets-exec-`, `AWSCloudFormationStackSetExecutionRole`, `aws-controltower`, `AWSControlTower`.
  `--protect-prefix`로 추가한다.
- 이름 전체뿐 아니라 `/`·`:`로 나눈 각 부분도 본다. 예: `/aws/lambda/edu-bootstrap-account-alias` 로그 그룹, `...:edu-killswitch-...-Topic-XXXX` SNS 토픽.
- 이름이 보호 대상인 **CloudFormation 스택의 리소스 전부**(`list_stack_resources`의 물리 ID)도 보호한다. 그래서 `edu-killswitch-*` 스택이
  자동 이름으로 만든 역할·로그 그룹·토픽도 잡히지 않는다.
- 실행 주체(CloudShell의 IAM 사용자, 역할)는 보호한다. 서비스 연결 역할(`/aws-service-role/`)과 SSO 역할(`/aws-reserved/`)은 세지 않는다.
- 기본 VPC 세트(기본 VPC, 기본 서브넷, 그 IGW, DHCP 옵션, 각 VPC의 `default` 보안 그룹)는 세지 않는다. 기본이 아닌 VPC와 그 안의
  서브넷·보안 그룹·IGW는 센다.
- 보호로 제외된 리소스는 JSON `inventory_protected`에 남는다.

## 인벤토리가 보는 것 (읽기 전용, 페이지 처리, 리전별 병렬)

리전별: EC2 인스턴스(terminated 제외), EBS 볼륨, EBS 스냅샷(`OwnerIds=self`), AMI(`Owners=self`), 탄력적 IP, NAT 게이트웨이,
VPC 엔드포인트(Gateway 제외), 기본이 아닌 VPC·서브넷·보안 그룹·IGW, Transit Gateway, Site-to-Site VPN, ALB/NLB, Classic LB,
Auto Scaling 그룹, RDS/Aurora 인스턴스·클러스터·수동 스냅샷(인스턴스·클러스터), DynamoDB, ElastiCache 클러스터·Serverless,
MemoryDB, Redshift 클러스터·Serverless 작업 그룹/네임스페이스, OpenSearch 도메인·Serverless 컬렉션, Kinesis Data Streams,
Firehose, MSK, Glue 작업·크롤러, EMR(활성), Kendra, DMS 복제 인스턴스, Lambda, API Gateway REST·HTTP/WebSocket, ECS 클러스터·서비스,
EKS, ECR, App Runner, Elastic Beanstalk 환경, Lightsail 인스턴스·DB, WorkSpaces, Directory Service, MWAA, Amplify,
Step Functions, Amazon MQ, EFS, FSx, SageMaker 엔드포인트·노트북·도메인·앱, Bedrock 커스텀/임포트 모델·프로비저닝 처리량·
에이전트·지식 기반, SQS, SNS, Secrets Manager, KMS 고객 관리형 키(삭제 예약 제외), CloudWatch 로그 그룹(Control Tower 것 제외)·경보,
CloudFormation 스택(DELETE_COMPLETE 제외), Route 53 Resolver 엔드포인트, Network Firewall.
글로벌: S3 버킷(버킷 리전에 집계), CloudFront 배포, Route 53 호스팅 영역, IAM 사용자, IAM 역할.

모든 호출은 무료 Describe/List API다. 호출이 실패해도 멈추지 않고 이렇게 나눠 기록한다.

| 구분 | 원인 | 판정 영향 | JSON |
|---|---|---|---|
| 해당 없음 (n/a) | 그 리전에 없는 서비스, 비활성 리전 (EndpointConnectionError, InvalidAction, OptInRequired, SubscriptionRequired, UnrecognizedClient, AuthFailure, "not supported in this region" 등) | 없음 | `not_applicable`, `not_applicable_count` |
| 정책으로 차단 | AccessDenied (SCP 명시적 거부면 코드 뒤에 `(SCP)`) | 없음. 다만 그 범위는 보지 못한 것이다 | `blocked_by_policy`, `blocked_by_policy_count` |
| 확인 못 함 | 그 밖의 오류 | 없음. `inventory_complete=false`, 경고 | `inventory_unchecked`, `errors` |

검사가 하나도 성공하지 못하면(예: 모든 호출이 거부) `ERROR`(exit 2)다.

## 주요 옵션

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--mode` | `auto` | `inventory` / `cost` / `auto` (위 표 참고) |
| `--cleanup-date` | 오늘(KST), inventory 모드는 없음 | 정리일. cost 모드의 검사 구간은 다음 날(UTC)부터 시작한다. inventory 모드에서는 기록용 |
| `--settle-days` | 2 | 정리일+N일 00:00 UTC 전에는 `PENDING` |
| `--lag-hours` | 12 | UTC 하루가 끝나고 이 시간이 지나야 그날 데이터가 반영됐다고 본다 |
| `--days` | 전체 | 검사 구간을 어제부터 거꾸로 N일로 줄인다 |
| `--threshold-usd` | 0.01 | (날짜, 서비스, 사용 유형) 한 줄이 이 금액을 넘으면 검사 대상이 된다 |
| `--allowlist` | 없음 | 허용 규칙 JSON (`allowlist.example.json` 참고) |
| `--exclude-record-types` | `Credit,Refund,Tax` | 제외할 요금 유형(RECORD_TYPE) |
| `--record-types` | – | 지정하면 이 유형만 포함한다 (예: `Usage`). 제외 방식 대신 쓴다 |
| `--require-final` | 끔 | 마지막 날짜가 `Estimated=false`일 때만 판정한다 (월초의 지난달 최종 확인용) |
| `--inventory` | 끔 | cost 모드에서도 인벤토리를 함께 돌린다 (inventory 모드와 auto 대체 시에는 항상 돈다) |
| `--all-regions` | 끔 | 인벤토리를 활성화된 모든 리전에서 돌린다 (`account:ListRegions`, 실패하면 `ec2:DescribeRegions`) |
| `--protect-prefix` | 위 "보호 대상" 참고 | 이 접두사로 시작하는 이름(또는 `/`·`:`로 나눈 부분)은 잔여물로 세지 않는다. 반복 가능 |
| `--max-examples` | 5 | 요약과 JSON `examples`에 유형별로 보여 줄 ID 수 |
| 공통 | | `--profile --role-arn --external-id --session-name --regions --json-out --apply` (`--apply`는 무시된다. 읽기 전용) |

**RECORD_TYPE 필터.** 기본값은 크레딧, 환불, 세금을 **빼는** 방식이다. 값 철자가 틀려도 결과는 거짓 `RESIDUAL`에 그친다.
반대로 `Usage`만 **포함**하는 방식에서 철자가 틀리면 거짓 `CLEAN`이 나온다. 제외 방식은 또 RI·Savings Plans 같은 약정 수수료와
Marketplace 수수료도 잡아낸다. 이런 요금은 해지한 뒤에도 계속 청구된다.

## cost 모드 (Cost Explorer를 쓸 수 있는 계정)

`--mode cost`(또는 `auto`에서 Cost Explorer가 동작할 때)의 정의는 다음과 같다.

> **정리일 다음 날(UTC)부터 어제(UTC)까지 Cost Explorer에 허용 목록 밖의 사용 요금이 없다.**

`--inventory`를 주면 인벤토리도 함께 돌리고, 하나라도 발견되면 비용이 0이어도 `RESIDUAL`이다.

```bash
python3 "$REPO/verify/verify_cost.py" --mode cost --cleanup-date 2026-10-14 \
  --allowlist "$REPO/verify/allowlist.example.json" --inventory --all-regions --json-out ~/"verify-$ACCT.json"
```

### 왜 D+2인가

- **Cost Explorer는 하루 늦다.** 비용은 전날 사용분까지만 반영되고, 이번 달 데이터는 24시간보다 더 늦게 갱신되기도 한다
  ([Cost Explorer 데이터](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-exploring-data.html)).
- **하루 단위는 UTC다.** `DAILY` 결과의 날짜는 UTC 자정 기준이고 `TimePeriod.End`는 포함되지 않는다.
  서울(KST)은 UTC+9라서 정리일 KST 날짜에는 정리 전 사용량이 섞인다. 그래서 정리일 당일은 판정하지 않고
  **정리일+1(UTC)부터** 본다. `--cleanup-date`에는 KST 날짜와 UTC 날짜 중 무엇을 넣어도 된다
  (기본값은 오늘의 KST 날짜). 어느 쪽이든 정리는 정리일+1 00:00 UTC 전에 끝났기 때문이다.
- **기본 타이밍:** `--settle-days 2`와 `--lag-hours 12`를 쓰면 최초 판정 시각은
  **정리일+2 12:00 UTC (= 정리일+2 21:00 KST)**다. 그 전에 실행하면 `PENDING`이고 다시 실행할 시각을 알려 준다.
  가장 확실한 운영 방법은 **정리일+3 오전(KST)에 실행**하는 것이다.
  - 데이터가 이미 나온 날짜는 PENDING이어도 조회한다. 그 날짜에 허용 목록 밖 요금이 있으면 기다리지 않고
    바로 `RESIDUAL`로 판정한다. 요금은 나중에 늘어날 수는 있어도 줄지는 않는다.
  - 정리 다음 날 데이터가 아직 응답에 없으면 `PENDING`(no-data)이다. 그날이 끝나고 72시간이 지나도 없으면
    $0으로 간주하고 경고를 남긴다.
- **`Estimated` 플래그:** 응답의 날짜마다 붙는 `Estimated`는 실제로는 **그 달 청구서가 확정될 때까지 계속 `true`**다.
  따라서 판정 기준으로 쓰지 않고 요약에 `[추정치]`로만 표시한다. 금액은 조금 바뀔 수 있다.
  월초에 지난달 계정을 최종 확인할 때는 `--require-final`을 쓴다. 이 옵션을 주면 마지막 날짜가
  `Estimated=false`가 될 때까지 `PENDING`이다.

### Cost Explorer를 한 번 켜기 (독립 계정만, API 없음)

리셀러 멤버 계정은 SCP 때문에 켤 수도 쓸 수도 없다. 이 절은 Cost Explorer를 쓸 수 있는 독립 계정에만 해당한다.
Cost Explorer는 **콘솔에서 처음 열 때 활성화되고, API로는 켤 수 없다**
([ce-enable](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-enable.html)).
켠 뒤 이번 달 데이터가 보이기까지 약 24시간이 걸린다. 따라서 **계정을 만들 때(부트스트랩 시점)** 켜 두어야 한다.

1. 루트(또는 결제 접근이 허용된 관리자)로 로그인한다.
2. **Billing and Cost Management** → 왼쪽 **Cost Explorer**를 연다.
3. "Welcome to Cost Explorer" 화면이 나오면 **Launch Cost Explorer**를 누른다. 화면이 열리면 끝이다.

참고:
- 켜지 않은 계정에서 `--mode cost`는 `ERROR`(exit 2)와 함께 "콘솔에서 Cost Explorer를 한 번 열어 활성화하세요" 안내를 낸다.
  `auto`는 이때 inventory로 판정한다.
- Cost Explorer를 켜면 Cost Anomaly Detection 모니터도 자동으로 생긴다. 요금은 없다.
- API(`ce:GetCostAndUsage`)는 IAM 정책만으로 호출된다(SCP가 막지 않는 한). 콘솔용 "IAM 사용자/역할의 결제 정보 액세스" 설정은
  콘솔 화면에만 적용된다. `EduCleanupRole`(AdministratorAccess)이면 충분하다.

### 허용 목록 (`--allowlist`, cost 모드)

```json
[{"service": "Amazon Route 53", "usage_type_pattern": "HostedZone", "max_usd_per_day": 0.5, "reason": "..."}]
```

- `service`는 Cost Explorer의 SERVICE 값이고, `usage_type_pattern`은 USAGE_TYPE에 대한 glob(`*`, `?`)이다. 대소문자는 구분하지 않는다.
- 규칙에 맞더라도 `max_usd_per_day`를 넘으면 그 줄은 **RESIDUAL**이 된다. 상한 없는 허용은 없다.
- `reason`은 필수다. `"_comment"`처럼 `_`로 시작하는 키만 있는 항목은 주석으로 보고 건너뛴다.
- 이 스크립트가 쓰는 Cost Explorer API 요금(`AWS Cost Explorer` / `*APIRequest*`, 하루 $1까지)은 내장 규칙으로 항상 허용된다.
- 예시 파일은 일부러 보수적으로 만들었다. 규칙 하나가 진짜 잔여물을 가릴 수 있으므로 넓은 패턴(`*`)은 쓰지 않는다.

### cost 모드의 RESIDUAL 줄 읽는 법과 할 일

```
허용 목록 밖 사용 요금 (금액순, 최대 20건 / 전체 3건):
  2026-10-15  $1.9000    Amazon Bedrock | USE1-CustomModelStorage [us-east-1] ← 프로비저닝 처리량, 커스텀/임포트 모델, ...
  2026-10-15  $0.2500    EC2 - Other | APN2-EBS:VolumeUsage.gp3 [ap-northeast-2] ← EBS 볼륨/스냅샷, NAT 게이트웨이, 탄력적 IP
  2026-10-16  $0.2500    Amazon Elastic Compute Cloud - Compute | USW2-BoxUsage:t3.micro [us-west-2] ← EC2 인스턴스(중지 포함)
```

- **[리전]**은 사용 유형 접두사로 추정한 값이다(`APN2`=서울, `USE1`/접두사 없음=버지니아 또는 글로벌, `USW2`=오리건 …).
  **설정에 없는 리전**(위 예의 `us-west-2`)이 보이면 aws-nuke 설정 밖에 리소스가 있다는 뜻이다.
- 같은 서비스와 사용 유형이 **날마다 반복되면** 남아 있는 리소스다. **하루만 보이면** 정리 당일의 꼬리일 수 있다.
  그런 줄은 다음 날 다시 확인한다.
- 할 일:
  1. 인벤토리(`--inventory --all-regions`)로 그 리전과 서비스의 리소스 이름을 확인한다(요약의 "인벤토리 잔여물").
  2. 직접 지우거나, 그 리전과 리소스 타입을 포함해 aws-nuke를 다시 돌린다. Bedrock 임포트 모델, 프로비저닝 처리량, 커스텀 모델은 aws-nuke가
     놓칠 수 있으니 콘솔이나 API로 직접 삭제한다.
  3. **다시 지운 날짜를 `--cleanup-date`로** 넣고 D+2에 다시 검증한다. `CLEAN`이 나와야 해지 단계(preclose)로 넘어간다.
- 인벤토리에서 "정책으로 차단"이나 "확인 못 함"으로 나온 리전과 타입은 보지 못한 것이다. JSON의 `inventory_complete=false`로 표시된다.
  판정은 그대로 두지만, 그 범위는 콘솔에서 한 번 직접 확인한다.

## 비용

- Cost Explorer API는 **요청 1건당 $0.01**이다 ([요금](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/)).
  이 스크립트는 `GetCostAndUsage`를 **보통 1회** 호출한다. 결과가 길어 `NextPageToken`이 오면 페이지마다 1회씩 더 호출한다.
  정리된 계정이라면 거의 항상 1회, 계정 100개를 검증해도 약 $1이다. 요약의 "Cost Explorer 요청: N회"에 실제 횟수가 나온다.
- `--mode inventory`는 Cost Explorer를 **한 번도 호출하지 않는다**($0).
- 검사할 UTC 날짜가 아직 없으면(정리 당일에 실행한 경우) `--mode cost`는 Cost Explorer를 **호출하지 않는다**(0회, $0).
  `auto`는 Cost Explorer를 쓸 수 있는지 알아보려고 1회($0.01) 호출한다.
- 인벤토리는 무료 Describe/List API만 쓴다(리전당 약 80회 + 클러스터·키 수만큼, 글로벌 약 5회).

## JSON 결과 (`--json-out`, 허브 오케스트레이터용)

```json
{
  "module": "verify", "account_id": "123456789012", "generated_at": "2026-10-17T00:10:00+00:00",
  "applied": false, "status": "CLEAN|RESIDUAL|PENDING|ERROR", "exit_code": 0,
  "mode": "inventory|cost", "mode_requested": "auto|inventory|cost",
  "ce_fallback": {"code": "AccessDeniedException", "message": "... explicit deny in a service control policy", "reason": "scp-deny", "...": "..."},
  "cleanup_date": "2026-10-14 (inventory 모드에서 생략 시 null)", "now_utc": "...",
  "window": {"start": "2026-10-15", "end": "2026-10-16", "end_exclusive": "2026-10-17", "days": 2,
             "timezone": "UTC", "earliest_verdict_utc": "...", "earliest_verdict_kst": "2026-10-16 21:00 KST", "queried": true},
  "pending_reasons": [], "ce_requests": 1, "data_final": false,
  "flagged": [{"date": "...", "service": "...", "usage_type": "...", "amount_usd": 0.25, "region_hint": "ap-northeast-2",
               "estimated": true, "hint": "...", "allowlist_exceeded": {"max_usd_per_day": 1.0, "reason": "..."}}],
  "allowed": [{"date": "...", "service": "...", "usage_type": "...", "amount_usd": 0.5, "reason": "..."}],
  "daily_totals": [{"date": "...", "amount_usd": 0.75, "flagged_usd": 0.25, "estimated": true}],
  "inventory_ran": true, "inventory_regions": ["ap-northeast-2", "us-east-1"], "inventory_checked": 156,
  "inventory": {"ap-northeast-2": {"ec2_instances": 2, "vpcs": 0, "...": 0}, "global": {"iam_roles": 0}},
  "examples": {"ap-northeast-2": {"ec2_instances": ["i-0abc (shutting-down)", "i-0def"]}}, "max_examples": 5,
  "inventory_items": {"...": "유형별 최대 20개 ID"}, "inventory_deleting": {"ap-northeast-2": {"ec2_instances": 1}},
  "inventory_protected": {"global": {"iam_roles": ["OrganizationAccountAccessRole"]}},
  "not_applicable": {"ap-northeast-2": {"kendra_indexes": "NotAvailableInRegion"}}, "not_applicable_count": 1,
  "blocked_by_policy": {"us-east-1": {"bedrock_agents": "AccessDeniedException (SCP)"}}, "blocked_by_policy_count": 1,
  "inventory_unchecked": {"us-east-1": {"glue_jobs": "InternalFailure"}},
  "inventory_complete": false, "warnings": [], "errors": [{"stage": "cost|auth|regions|inventory|args|internal", "code": "...", "message": "...", "hint": "..."}]
}
```

cost 모드의 판정 우선순위는 `RESIDUAL`(발견된 잔여물) > `ERROR` > `PENDING` > `CLEAN`이다. inventory 모드는 잔여물이 있으면 `RESIDUAL`,
검사가 하나도 성공하지 못했으면 `ERROR`, 아니면 `CLEAN`이다. 예상치 못한 예외도 exit 2로 끝나며 1로 끝나는 일은 없다.

## 테스트

```bash
cd edu-cleanup && python3 -m pytest verify/tests -q
```

Cost Explorer는 `botocore.stub.Stubber`로 테스트한다(moto의 CE 지원이 제한적이다). 다루는 경우는 CLEAN, RESIDUAL, PENDING(날짜, lag, no-data,
`Estimated` + `--require-final`), 허용 목록 glob과 상한, 페이지 처리, `--mode cost`의 CE 미활성화·DataUnavailable·권한 오류(exit 2)다.
모드 테스트(`test_verify_modes.py`): inventory 모드는 CE를 호출하지 않음, auto는 AccessDenied(SCP)·미활성화·DataUnavailable에서 inventory로
대체(그 밖의 오류는 exit 2), 판정 날짜가 없을 때 auto의 1회 확인, 기본 VPC 세트 무시·기본 아닌 VPC 계산, 보호 이름·보호 스택의 리소스·
`--protect-prefix`·실행 주체 제외, n/a 오류는 RESIDUAL이 아님, SCP AccessDenied는 `blocked_by_policy`, exit 0/1/2.
인벤토리는 moto로 테스트하고(moto가 아직 구현하지 않은 몇 API는 "확인 못 함"으로 남는다), 모든 검사의 boto3 메서드·결과 키·필드·입력 인자·
페이지네이터 이름을 설치된 botocore 모델과 대조하는 테스트가 있다.
