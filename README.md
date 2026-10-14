# edu-cleanup — AWS 교육 계정 정리 도구

교육생마다 독립 AWS 계정(Organizations 없음)을 주는 교육에서, 교육이 끝난 계정을 **빠르고, 빠짐없이, 해지 후 요금 없이** 정리하기 위한 도구 모음입니다. aws-nuke를 대체하지 않고 앞뒤를 감쌉니다.

## 폴더별 역할

| 폴더 | 하는 일 | 해결하는 문제 |
|---|---|---|
| `nuke/` | aws-nuke 설정 생성 + 리전 병렬 실행기, CloudShell 원샷 블록 | DynamoDB 삭제 실패, 에러 로그 폭주, 끝없는 대기, 느린 순차 실행 |
| `cfn_predelete/` | nuke 전에 CloudFormation 스택을 병렬로 먼저 삭제 (Express → 실패 시 강제 삭제) | 스택 하나가 nuke 전체를 최대 60분씩 멈추는 문제, 역할이 먼저 지워져 스택 삭제 실패 |
| `killswitch/` | Bedrock 토큰 사용량이 임계치를 넘으면 몇 분 안에 교육생 Deny + 키 비활성화 | Budgets(8~12시간 지연)로는 못 막는 Bedrock 과금·해킹 |
| `preclose/` | 해지 직전 점검: Marketplace 구독, CloudTrail, 휴지통 규칙, 약정 상품, 도메인 자동 갱신 | 해지 후에도 나오는 요금 |
| `verify/` | 정리 2일 뒤 Cost Explorer로 서비스별 비용이 0인지 판정 (CLEAN / RESIDUAL / PENDING) | nuke가 "성공"인데 리소스가 남는 조용한 누락 |
| `hub/` | 정리 계정에서 여러 계정을 한 번에 돌리는 오케스트레이터 + 계정 생성 시 심는 부트스트랩 링크 | 계정마다 루트 로그인하는 수작업 |

자세한 사용법은 각 폴더의 README에 있고, 모든 README에 CloudShell에 그대로 붙여 넣는 블록이 들어 있습니다.

## 교육 한 기수 기준 순서

```
교육 전   hub: 계정 생성 → 루트로 부트스트랩 링크 1회 클릭 (정리 역할 설치)
          killswitch: 계정·리전마다 차단 스위치 배포 (hub의 killswitch 명령으로 일괄)
교육 후   hub plan   → 결과 확인 (cfn_predelete → nuke → preclose, 모두 dry-run)
          hub clean  → 실제 삭제
D+2       hub verify → CLEAN 확인
해지 전   preclose --apply 로 남은 구독·트레일 정리 → 루트 콘솔에서 해지
```

계정 한두 개라면 hub 없이 CloudShell에서 `cfn_predelete` → `nuke/cloudshell-oneshot.sh` → `preclose` → (2일 뒤) `verify` 순서로 각 README의 블록을 붙여 넣으면 됩니다.

## 공통 규칙

- 모든 스크립트는 **기본이 dry-run**이고, `--apply`를 줄 때만 실제로 바꿉니다.
- 종료 코드: `0` 정상/깨끗함, `1` 문제 남음, `2` 실행 오류, `3` 아직 판정 이르음(verify).
- 보호 대상: `EduCleanupRole` 역할과 `edu-bootstrap*` 스택은 어떤 모듈도 지우지 않습니다.
- 모듈 간 규칙은 `CONTRACTS.md`에 있습니다.

## 검증 상태

- 단위·통합 테스트 360여 개 (moto 모의 AWS, 실제 aws-nuke v3.68.3 바이너리 포함), cfn-lint, shellcheck 통과.
- 모의 AWS 위에서 hub → 다른 계정 역할 전환 → 스택 병렬 삭제 → nuke까지 end-to-end로 확인했습니다.
- **실제 AWS 계정에서는 아직 돌려보지 않았습니다.** 첫 적용은 테스트용 계정 하나에서 dry-run부터 해 보세요.
- 계정 해지는 AWS가 API를 제공하지 않아 루트 콘솔에서 직접 해야 합니다.
