# edu-cleanup — shared contracts

Every module in this repo MUST follow these rules so the hub orchestrator can call them
and so they all work standalone in AWS CloudShell.

## Context
- A company runs AWS hands-on training. Each student gets one AWS account. The accounts are **member accounts
  of an AWS reseller's Organization** that the company does not control: no custom SCPs, and the reseller's SCPs
  block all cost/billing visibility (Cost Explorer denied, even for root); billing goes through the reseller.
  The company holds root + IAM credentials; students get IAM users. So modules rely on IAM-level controls inside
  the account (killswitch) and resource-based verification (verify inventory mode), never on SCPs or cost data.
- Courses use **ap-northeast-2 (Seoul)** and **us-east-1 (N. Virginia)**. IAM etc. = `global`.
- After a cohort, accounts are cleaned with **aws-nuke (ekristen v3.68.x)** and then **closed** — by the member's
  root in the console (unless an SCP denies `account:CloseAccount`) or by the reseller's management account
  (`CloseAccount`). Closed members stay in the org for the 90-day post-closure period. `preclose` detects membership.
- Operators like **one copy-paste block for CloudShell**. Every module's README must include one.
- Per-course IAM guardrail policies are **out of scope** (they differ per course).

## Repo layout (each module owns ONLY its folder)
```
edu-cleanup/
  CONTRACTS.md          (this file)
  common/session.py     shared helper — import it, do not modify it
  nuke/                 1) aws-nuke config template + runner
  killswitch/           3) Bedrock emergency kill switch (CloudFormation)
  cfn_predelete/        4) delete CloudFormation stacks before nuke
  preclose/             5) pre-closure checks (things that bill after closure)
  hub/                  6) hub/cleanup account: bootstrap template + orchestrator
  verify/               7) post-cleanup verification (inventory mode; cost mode where billing data is visible)
```

## Fixed names
| Thing | Value |
|---|---|
| Cleanup role in every student account | `EduCleanupRole` (AdministratorAccess, trusts hub principal + ExternalId) |
| Bootstrap stack name | `edu-bootstrap` (anything starting with `edu-bootstrap` is protected) |
| Kill switch stack name | `edu-killswitch-<region>` — NOT protected (it is fine for nuke to delete it) |
| Account alias pattern | `edu-<cohort>-<nn>` e.g. `edu-2026-10-07` (must never contain `prod`) |
| Default regions | `ap-northeast-2,us-east-1` (+ `global` for nuke only) |

Protected from deletion by every module (nuke filters, cfn_predelete skip list):
the `EduCleanupRole` role + its policy attachments/inline policies, and stacks named `edu-bootstrap*`.
If a script runs as an IAM user (e.g. in CloudShell), it must also protect that user.

## Python scripts
- Python 3.9+ compatible (CloudShell). **Only boto3/botocore** at runtime (no PyYAML, no click).
- First lines of every script:
  ```python
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
  from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING
  ```
- Use `add_common_args(parser)` → every script accepts
  `--profile --role-arn --external-id --session-name --regions --json-out --apply`.
- **Dry-run by default.** Nothing is changed unless `--apply` is passed.
- Human-readable progress → stderr (`log()`); final human summary → stdout; machine result → `--json-out` file.
- JSON result top-level keys always include: `module`, `account_id`, `generated_at`, `applied` (bool), `status`.
- Exit codes: `0` OK/clean, `1` issues remain, `2` error, `3` pending (too early to judge).
- Use `client(session, service, region)` (adaptive retries) and paginators everywhere (no first-page-only bugs).
- Unit tests in `<module>/tests/` with pytest, using moto or `botocore.stub.Stubber`. No real AWS calls.

## Bash scripts
- `#!/usr/bin/env bash` + `set -euo pipefail`, pass `shellcheck`.
- Same flag names as Python where applicable (`--role-arn`, `--external-id`, `--regions`, `--apply`, `--json-out`).

## CloudFormation
- YAML, must pass `cfn-lint` with no errors.

## Dev tooling
python3.10+, boto3/botocore 1.43.x, moto, pytest, PyYAML (tests only), cfn-lint, shellcheck, jq. Tests never call real AWS.
Some nuke tests read the aws-nuke v3.68.3 source (`docs/resources/`) to check type names: clone `https://github.com/ekristen/aws-nuke` at tag `v3.68.3` next to this repo (`../aws-nuke`) or set `AWS_NUKE_SRC`; otherwise those tests are skipped. Tests that run the real binary use `AWS_NUKE_BIN`.
