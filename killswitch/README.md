# killswitch: Bedrock 비상 차단 스위치 (모듈 3)

학생 계정에서 Amazon Bedrock 사용량이 갑자기 늘면 **몇 분 안에** IAM 사용자를 자동으로 막는 장치입니다.
2026년 8월 교육 계정 4개의 Bedrock 과다 사용, 9월 해킹 계정의 Bedrock 요금 같은 사고가 다시 나지 않도록 만든 범용 비상 스위치입니다.
과정별 IAM 가드레일(어떤 모델을 허용할지 등)은 과정마다 다르므로 다루지 않습니다.

| 파일 | 역할 |
|---|---|
| `killswitch.yaml` | CloudFormation 템플릿. 계정당 **리전마다 1개씩** 배포합니다(스택 이름 `edu-killswitch-<리전>`). |
| `deploy_killswitch.sh` | 현재 자격 증명의 계정에 여러 리전을 한 번에 배포·업데이트합니다. `--test`로 드라이런 점검도 합니다. |
| `reset_killswitch.py` | 조사가 끝난 뒤 차단을 푸는 운영자 도구입니다(기본 드라이런, `--apply`로 실행). |
| `lambda_src.py` | Lambda 코드 원본. 템플릿의 인라인 코드와 같아야 합니다(`python3 killswitch/sync_inline_code.py`로 동기화). |
| `tests/` | moto·cfn-lint·shellcheck 테스트 (`python3 -m pytest killswitch/tests`) |

## 1. 무엇을 하나

```
Bedrock 호출 ──► CloudWatch 지표 (AWS/Bedrock, AWS/BedrockMantle, 리전별)
                    │  N분 합계 토큰 ≥ TokenThreshold
                    ▼
               CloudWatch 알람 ──► SNS 토픽 ──► Lambda(EduKillSwitch) ──► 결과 보고 → SNS(이메일)
                                                  ▲
CloudTrail 관리 이벤트 ──► EventBridge 규칙 ─────┘  (선택, EnableApiTriggers)
```

Lambda가 실행되면 대상 IAM 사용자 각각에 대해 다음을 합니다.

1. 인라인 정책 `EduKillSwitchDeny`를 붙입니다. 거부하는 액션은 다음과 같습니다.
   - `bedrock:*`, `bedrock-mantle:*`, `aws-marketplace:Subscribe`
   - 자격 증명 발급: `iam:CreateUser`, `CreateAccessKey`, `UpdateAccessKey`, `CreateLoginProfile`, `UpdateLoginProfile`, `CreateServiceSpecificCredential`, `UpdateServiceSpecificCredential`, `ResetServiceSpecificCredential`
   - 차단 해제·권한 상승: `iam:Put*Policy`, `Attach*Policy`, `Detach*Policy`, `DeleteUserPolicy`, `Delete*PermissionsBoundary`, `CreatePolicyVersion`, `SetDefaultPolicyVersion`, `CreateRole`, `UpdateAssumeRolePolicy`, `PassRole`, `TagUser`, `UntagUser`, `sts:AssumeRole`
   - 스위치 보호: `edu-killswitch-*` 스택·Lambda·알람·규칙·토픽·로그 그룹과 `EduCleanupRole`에 대한 모든 작업
2. 사용자의 **액세스 키를 비활성화**합니다(삭제하지 않음).
3. 사용자의 **Bedrock API 키**(서비스별 자격 증명, `bedrock.amazonaws.com`)를 비활성화합니다.
4. 무엇을 비활성화했는지 사용자 태그(`edu-killswitch:keys`, `edu-killswitch:ssc`, `edu-killswitch:at`)에 적습니다. `reset_killswitch.py`가 정확히 그것만 되살릴 때 씁니다.
5. 요약을 SNS 토픽으로 보내고(이메일 구독 시 메일 수신) CloudWatch Logs에도 남깁니다.

지키는 원칙은 다음과 같습니다.
- **콘솔 로그인 프로필은 지우지 않습니다.** 학생은 콘솔에 들어올 수 있지만 Bedrock과 키 발급은 막힙니다.
- **IAM 역할은 건드리지 않습니다.** 특히 `EduCleanupRole`은 절대 수정하지 않습니다.
- `ExemptUserNames`의 사용자는 건드리지 않습니다(대소문자 무시). `deploy_killswitch.sh`는 실행하는 IAM 사용자를 자동으로 면제 목록에 넣습니다.
- **멱등**입니다. 두 리전의 알람이 동시에 울려도, 여러 번 실행돼도 결과가 같습니다.
- 인라인 정책 한도(사용자당 합계 2,048자)가 이미 차 있으면 관리형 정책 `/edu-killswitch/EduKillSwitchDeny`를 만들어 대신 붙입니다.

### API 트리거 (`EnableApiTriggers=true`, 기본값)

LLMjacking에서 거의 항상 나오는 관리 이벤트가 **성공**하면 알람을 기다리지 않고 바로 차단합니다.

| 규칙 | eventSource | eventName | 배포되는 리전 |
|---|---|---|---|
| `edu-killswitch-<리전>-bedrock-api` | `bedrock.amazonaws.com` | `PutUseCaseForModelAccess`, `CreateFoundationModelAgreement`, `DeleteModelInvocationLoggingConfiguration` | 모든 리전 (호출이 일어난 리전으로 전달) |
| `edu-killswitch-us-east-1-iam-api` | `iam.amazonaws.com` | `CreateUser`, `CreateAccessKey`, `CreateServiceSpecificCredential` | **us-east-1에서만** |

- IAM은 글로벌 서비스라서 CloudTrail 이벤트가 **us-east-1** 기본 이벤트 버스에만 들어옵니다. 그래서 IAM 규칙은 템플릿 조건(`AWS::Region == us-east-1`)으로 us-east-1 스택에만 만들어집니다. 서울만 배포하면 IAM 트리거는 없습니다.
- 루트, AWS 서비스, `EduCleanupRole`, 면제 사용자가 호출한 이벤트와 실패한 호출(`errorCode` 있음)은 무시합니다.
- 이벤트를 일으킨 IAM 사용자와 대상 사용자(`requestParameters.userName`, 예: 공격자가 새로 만든 사용자)도 함께 차단합니다. `GROUP` 모드에서 그룹 밖 사용자여도 마찬가지입니다.
- 강사가 수업 중에 학생 키를 발급하면 스위치가 작동합니다. 그런 과정이라면 강사 계정을 `ExemptUserNames`에 넣거나 `EnableApiTriggers=false`로 배포하세요.
- AWS 문서상 EventBridge가 `AWS API Call via CloudTrail` 이벤트를 받으려면 **로깅이 켜진 CloudTrail 트레일**이 필요합니다.

## 2. 왜 "분 단위"가 중요한가

| 장치 | 반응 시간 |
|---|---|
| AWS Budgets | 하루 최대 3회, **8~12시간** 간격 갱신 |
| Cost Anomaly Detection | 최대 **24시간**, 새 서비스는 10일 이력이 쌓여야 탐지 |
| **이 스위치 (CloudWatch 알람)** | 알람 기간(기본 5분) + 지표 게시 지연(수 분) |
| **이 스위치 (API 트리거)** | CloudTrail → EventBridge 전달 지연(보통 수 분) |

LLMjacking 피해는 하루 $46,000~$100,000 이상으로 보고됩니다(Sysdig). Budgets가 처음 반응하기 전인 8~12시간 사이에 수천~수만 달러가 나갈 수 있습니다. Bedrock 토큰 쿼터는 고객이 낮출 수 없으므로(Service Quotas는 증가만 가능), 빠른 탐지와 IAM Deny가 사실상 유일한 브레이크입니다.

## 3. 감시하는 지표 (AWS 문서로 확인함)

| 엔드포인트 | 네임스페이스 | 토큰 지표 | 요청 수 지표 |
|---|---|---|---|
| `bedrock-runtime` (InvokeModel, Converse 등) | `AWS/Bedrock` | `InputTokenCount`, `OutputTokenCount` | `Invocations` |
| `bedrock-mantle` (OpenAI 호환 Responses/Chat Completions, Anthropic Messages) | `AWS/BedrockMantle` | `TotalInputTokens`, `TotalOutputTokens` | `Inferences` |

- 두 네임스페이스 모두 **차원 없는 계정 단위 시계열**을 게시합니다. `AWS/Bedrock` 문서는 "To account for all requests, query these metrics without specifying a dimension"이라고 안내합니다. 그래서 `ModelId`별 합산이나 `SEARCH`가 필요 없습니다(`SEARCH`는 알람에 쓸 수도 없습니다).
- 알람 식: `SUM([rt_in, rt_out, mt_in, mt_out])` ≥ `TokenThreshold`, 통계 `Sum`, 기간 `PeriodMinutes`, 1개 데이터포인트로 판정, **누락 데이터는 notBreaching**(사용이 없으면 OK).
- `InvocationThreshold`(기본 0 = 끔)를 주면 요청 수 알람을 하나 더 만듭니다. 토큰이 없는 이미지·영상 모델 남용을 잡을 때 씁니다.
- mantle을 포함한 이유는 `bedrock-mantle`이 별도 IAM 접두사와 별도 네임스페이스를 써서, `AWS/Bedrock`만 보는 알람은 이 경로를 놓치기 때문입니다.

## 4. 배포: 계정 하나 (CloudShell에 한 번에 붙여넣기)

준비: 회사 루트 또는 관리자 IAM 사용자로 학생 계정 콘솔에 로그인하고 CloudShell을 엽니다. `edu-cleanup` 폴더를 올려 둡니다(Actions → Upload file로 zip 업로드 후 `unzip`, 또는 `git clone`).

```bash
# ===== 값만 고쳐서 통째로 붙여넣기 =====
THRESHOLD=2000000          # 5분 동안 리전별 입력+출력 토큰 합계 (아래 6절 참고)
EMAIL="ops@example.com"    # 알림 받을 주소 ("" 이면 메일 없음, 구독 확인 메일을 꼭 눌러야 함)
EXEMPT=""                  # 절대 막지 않을 IAM 사용자 (쉼표 구분, 실행자 본인은 자동 포함)
REGIONS="ap-northeast-2,us-east-1"

cd ~/edu-cleanup/killswitch && chmod +x deploy_killswitch.sh && \
./deploy_killswitch.sh --regions "$REGIONS" --threshold "$THRESHOLD" --period-min 5 \
  --target-mode ALL_IAM_USERS --exempt "$EXEMPT" --email "$EMAIL" --apply && \
./deploy_killswitch.sh --regions "$REGIONS" --test
```

- 학생만 막으려면 `--target-mode GROUP --group <학생그룹>`을 씁니다.
- 다른 계정에서 역할을 넘겨받아 배포할 때는 `--role-arn arn:aws:iam::<ID>:role/EduCleanupRole --external-id <ID>`를 씁니다. 이 경우 실행자가 역할이므로 면제 목록에 자동으로 추가되는 사용자는 없습니다.
- `--apply`가 없으면 드라이런입니다. 템플릿 검증과 계획만 출력합니다.
- 결과를 기계가 읽을 형식으로 남기려면 `--json-out out.json`을 붙입니다. 종료 코드는 0(성공), 1(일부 리전 실패), 2(인자·인증 오류)입니다.
- Bedrock을 쓸 수 있는 리전마다 배포해야 합니다. 학생 리전 제한 정책이 없다면 도쿄 등 다른 리전도 `--regions`에 넣으세요.

### 허브 오케스트레이터에서 배포할 때

`hub/orchestrate.py killswitch`는 계정×리전마다 다음 값으로 배포합니다.
- 스택 이름: `edu-killswitch-<region>`
- Capabilities: `CAPABILITY_IAM`만 사용합니다(이름을 고정한 IAM 리소스가 없으므로 `NAMED_IAM` 불필요).
- 파라미터 키: `TokenThreshold`(필수), `PeriodMinutes`, `TargetMode`, `TargetGroupName`, `ExemptUserNames`, `NotificationEmail`, `EnableApiTriggers`, `InvocationThreshold`(선택). 모든 값은 문자열로 넘깁니다. 예: `"2000000"`, `"true"`, `"ops1,ops2"`.
- 템플릿 크기는 약 31KB로, `TemplateBody` 한도(51,200바이트)보다 작아 S3 업로드가 필요 없습니다.

## 5. 동작 확인 (`--test`)

```bash
./deploy_killswitch.sh --test                 # 리전마다 Lambda를 {"dry_run": true}로 호출
```

드라이런은 **아무것도 바꾸지 않고**, 누구를 어떻게 막을지 목록만 반환합니다. 요약은 SNS로도 보내므로 메일 수신까지 함께 확인할 수 있습니다. 직접 호출하려면 다음을 씁니다.

```bash
aws lambda invoke --region ap-northeast-2 --function-name edu-killswitch-ap-northeast-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"trigger":"manual","reason":"점검","dry_run":true}' /dev/stdout
```

알람 경로까지 실제로 확인하려면, 테스트용 학생 사용자 하나만 있는 계정에서 알람 상태를 강제로 바꿉니다. **이 명령은 실제로 차단합니다.** 확인이 끝나면 7절의 reset으로 되돌리세요.

```bash
aws cloudwatch set-alarm-state --region ap-northeast-2 \
  --alarm-name edu-killswitch-ap-northeast-2-bedrock-tokens --state-value ALARM --state-reason "drill"
```

## 6. 임계값(TokenThreshold) 정하기

임계값은 **리전별, 기간(PeriodMinutes)당** 입력+출력 토큰 합계입니다. 정상 수업의 최고치보다 넉넉히 높고, 공격 규모보다는 훨씬 낮게 잡습니다.

| 상황 | 예시 값 (5분 기준) | 메모 |
|---|---|---|
| Bedrock을 쓰지 않는 과정 | `1` | 토큰이 하나라도 나오면 차단 |
| 가벼운 챗봇 실습 (학생 20~30명) | `500000` | 학생 1명이 5분에 약 1~2만 토큰 |
| RAG·에이전트 집중 과정 | `2000000` | 에이전트는 호출당 컨텍스트가 큼 |
| 강사 데모만 | `200000` | |

지난 수업의 실제 최고치를 보고 3~5배로 잡는 방법을 권장합니다.

```bash
for M in InputTokenCount OutputTokenCount; do
  aws cloudwatch get-metric-statistics --region ap-northeast-2 --namespace AWS/Bedrock --metric-name $M \
    --start-time "$(date -u -d '-14 days' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)" \
    --period 300 --statistics Sum --query 'max_by(Datapoints,&Sum).Sum'
done
```

비용으로 환산하면 `기간당 최대 비용 ≈ TokenThreshold × 모델 토큰 단가`입니다. 임계값 바로 아래로 **계속** 쓰면 탐지되지 않으므로(하루 = 5분 × 288회), Budgets 작업(최초 2개 무료)과 Cost Anomaly Detection을 느린 백스톱으로 함께 두세요.

## 7. 차단 풀기 (`reset_killswitch.py`)

먼저 SNS 메일과 CloudTrail을 보고 원인을 확인합니다. 해킹이 의심되면 키를 되살리지 말고 해당 키는 삭제하세요. 차단된 사용자 본인은 정책을 지울 수 없으므로 **루트, 면제 사용자, 또는 EduCleanupRole**로 실행합니다.

```bash
cd ~/edu-cleanup
python3 killswitch/reset_killswitch.py                                  # 드라이런: 무엇을 풀지 보기
python3 killswitch/reset_killswitch.py --apply                          # Deny 정책만 제거 + 알람 재무장
python3 killswitch/reset_killswitch.py --apply --reactivate-keys        # 스위치가 끈 키까지 다시 켜기
python3 killswitch/reset_killswitch.py --apply --users alice,bob        # 특정 사용자만
```

- `EduKillSwitchDeny` 인라인 정책을 지우고, 대체 관리형 정책이 붙어 있으면 분리합니다.
- `--reactivate-keys`를 주면 태그에 기록된 키, 즉 **스위치가 비활성화한 키만** 다시 켭니다. 원래부터 꺼져 있던 키는 그대로 둡니다. 모두 성공하면 태그를 지웁니다.
- `edu-killswitch-*` 알람이 OK가 아니면 OK로 되돌려 다시 울릴 수 있게 합니다(`--no-rearm`으로 생략). 사용량이 여전히 임계값 이상이면 곧 다시 차단됩니다. 의도된 동작입니다.
- 공통 인자(`--profile --role-arn --external-id --regions --json-out --apply`)를 지원합니다. 종료 코드는 0(풀 것 없음 또는 모두 해제), 1(드라이런에서 풀 대상 있음, 또는 일부 실패), 2(오류)입니다.

## 8. 한계

- **루트 사용자는 IAM으로 막을 수 없습니다.** 루트에 액세스 키가 없어야 하고, MFA를 걸어야 합니다.
- **IAM 역할은 막지 않습니다.** 학생이 만든 EC2·Lambda 등이 자기 역할로 Bedrock을 호출하면 계속 과금됩니다. 차단된 사용자는 새 역할 생성·AssumeRole·PassRole이 막히지만, 이미 돌고 있는 워크로드는 멈추지 않습니다. 알람 메일을 받으면 CloudTrail에서 `AssumedRole` 호출자를 확인하세요.
- **지표는 리전 단위입니다.** Bedrock을 쓸 수 있는 리전마다 배포해야 합니다. 교차 리전 추론(CRIS)은 요청을 받은 리전 기준으로 집계되는 것으로 보이나, 검증하지는 않았습니다.
- **수 분의 지연이 있습니다.** 지표 게시 지연, 알람 평가 주기, CloudTrail → EventBridge 전달, IAM 정책 전파(최종 일관성)가 더해집니다. 이 시간 동안 나온 요금은 막지 못합니다. 지연 값은 AWS 문서에 명시돼 있지 않아 추정입니다.
- 임계값 바로 아래로 오래 쓰는 "느린 누수"는 잡지 못합니다(6절 백스톱 참고).
- 면제 목록의 사용자와 `EduCleanupRole`이 유출되면 막을 수 없습니다.
- API 트리거에는 CloudTrail 트레일이 필요합니다(문서 기준). aws-nuke가 트레일과 이 스택을 지우는 것은 괜찮습니다. 계정을 해지하기 직전 단계이기 때문입니다(스택 이름 `edu-killswitch-*`는 보호 대상이 아닙니다).
- 알람이 계속 ALARM 상태이면 새로 생긴 사용자는 자동으로 막히지 않습니다(알람은 상태가 바뀔 때만 동작). 단, us-east-1의 IAM 트리거가 `CreateUser`를 잡습니다.

## 9. 개발

```bash
python3 killswitch/sync_inline_code.py          # lambda_src.py → killswitch.yaml 인라인 코드 반영
python3 -m pytest killswitch/tests -q            # moto, cfn-lint, shellcheck 테스트
cfn-lint killswitch/killswitch.yaml && shellcheck killswitch/deploy_killswitch.sh
```

- 인라인 코드(`Code.ZipFile`)는 CloudFormation이 `index`라는 파일로 zip하며(핸들러 `index.handler`), zip 한도는 4MB입니다. 실제로 먼저 걸리는 한도는 `TemplateBody` 51,200바이트이며, 현재 템플릿은 약 31KB입니다.
- 런타임은 `python3.13`입니다(Amazon Linux 2023, 지원 종료 예정 2029-06-30).
- moto는 서비스별 자격 증명(Bedrock API 키)을 구현하지 않으므로, 테스트는 botocore `before-call` 훅으로 이 두 API를 흉내 냅니다.

### 근거 문서
- Bedrock 런타임 지표: <https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html>
- bedrock-mantle 지표: <https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-mantle-metrics.html>
- Bedrock API 키 생성·폐기: <https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-generate.html>, <https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-revoke.html>
- CloudTrail 글로벌 서비스 이벤트(us-east-1): <https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-concepts.html>, <https://repost.aws/knowledge-center/eventbridge-rule-triggers-iam-single-region>
- EventBridge와 CloudTrail(트레일 필요): <https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-service-event-cloudtrail.html>
- PutMetricAlarm(Period, TreatMissingData), 지표 수학(SEARCH는 알람 불가): <https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_PutMetricAlarm.html>, <https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/using-metric-math.html>
- Lambda Code.ZipFile(4MB): <https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-properties-lambda-function-code.html>
- IAM 쿼터(사용자 인라인 정책 2,048자): <https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html>
