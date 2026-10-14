"""Shared helpers for the hub tests (not collected by pytest: name does not start with test_)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HUB_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = HUB_DIR.parent
for p in (str(REPO_ROOT), str(HUB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

HUB_ACCOUNT = "123456789012"  # moto's default account = the hub
EXTERNAL_ID = "test-external-id-0123456789"

# Fake step: records its argv/timing, writes a CONTRACTS-shaped JSON result and exits with a chosen code.
STUB_STEP = r'''
import json, os, sys, time
step, argv = sys.argv[1], sys.argv[2:]
def opt(name):
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else ""
    return None
root = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(root, "stub_config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
role = opt("--role-arn") or ""
acct = role.split(":")[4] if role.count(":") >= 5 else (opt("--account-id") or "")
key = acct + ":" + step
code = cfg.get("codes", {}).get(key, cfg.get("default_code", 0))
t0 = time.time()
time.sleep(cfg.get("sleeps", {}).get(key, cfg.get("sleep", 0)))
t1 = time.time()
print("stdout from " + key)
print("stderr from " + key, file=sys.stderr)
out = opt("--json-out")
if out and key not in cfg.get("no_json", []):
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"module": step, "account_id": acct, "generated_at": "2026-10-14T00:00:00+00:00",
                   "applied": "--apply" in argv, "status": "stub-%d" % code}, f)
calls = os.path.join(root, "calls")
os.makedirs(calls, exist_ok=True)
with open(os.path.join(calls, "%s-%s-%d.json" % (acct, step, os.getpid())), "w", encoding="utf-8") as f:
    json.dump({"step": step, "account": acct, "argv": argv, "start": t0, "end": t1, "code": code}, f)
sys.exit(code)
'''

NUKE_SH = '''#!/usr/bin/env bash
set -euo pipefail
exec "{python}" "$(dirname "$0")/../stub_step.py" nuke "$@"
'''

PY_WRAPPER = '''import os, runpy, sys
sys.argv = [sys.argv[0], "{step}"] + sys.argv[1:]
runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "stub_step.py"), run_name="__main__")
'''

KILLSWITCH_YAML = """AWSTemplateFormatVersion: "2010-09-09"
Parameters:
  TokenThreshold:
    Type: Number
  PeriodMinutes:
    Type: Number
  TargetMode:
    Type: String
  TargetGroupName:
    Type: String
  ExemptUserNames:
    Type: String
  NotificationEmail:
    Type: String
  EnableApiTriggers:
    Type: String
    Default: "true"
Resources:
  Topic:
    Type: AWS::SNS::Topic
"""


def make_fake_repo(root: Path, codes=None, sleep=0.0, sleeps=None, no_json=None) -> Path:
    """Create a temp repo root with stub step scripts and killswitch/killswitch.yaml."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "stub_step.py").write_text(STUB_STEP, encoding="utf-8")
    write_stub_config(root, codes=codes, sleep=sleep, sleeps=sleeps, no_json=no_json)
    for rel, step in (("cfn_predelete/predelete_stacks.py", "cfn_predelete"),
                      ("preclose/preclose_check.py", "preclose"),
                      ("verify/verify_cost.py", "verify")):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(PY_WRAPPER.replace("{step}", step), encoding="utf-8")
    nuke = root / "nuke" / "run_nuke.sh"
    nuke.parent.mkdir(parents=True, exist_ok=True)
    nuke.write_text(NUKE_SH.replace("{python}", sys.executable), encoding="utf-8")
    ks = root / "killswitch" / "killswitch.yaml"
    ks.parent.mkdir(parents=True, exist_ok=True)
    ks.write_text(KILLSWITCH_YAML, encoding="utf-8")
    return root


def write_stub_config(root: Path, codes=None, sleep=0.0, sleeps=None, no_json=None) -> None:
    (root / "stub_config.json").write_text(json.dumps({
        "codes": codes or {}, "sleep": sleep, "sleeps": sleeps or {}, "no_json": no_json or []}),
        encoding="utf-8")


def read_calls(root: Path) -> list:
    calls_dir = root / "calls"
    if not calls_dir.exists():
        return []
    out = []
    for p in calls_dir.glob("*.json"):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return sorted(out, key=lambda c: c["start"])


def clear_calls(root: Path) -> None:
    for p in (root / "calls").glob("*.json") if (root / "calls").exists() else []:
        p.unlink()


def make_registry(path: Path, accounts, cohort="2026-10", regions=("ap-northeast-2", "us-east-1"),
                  max_parallel=10, killswitch=None) -> Path:
    data = {
        "cohort": cohort,
        "external_id": EXTERNAL_ID,
        "regions": list(regions),
        "max_parallel": max_parallel,
        "killswitch": killswitch if killswitch is not None else {
            "TokenThreshold": 2000000, "PeriodMinutes": 5, "TargetMode": "ALL_IAM_USERS",
            "TargetGroupName": "", "ExemptUserNames": "", "NotificationEmail": ""},
        "accounts": [{"account_id": a, "alias": al, "note": ""} for a, al in accounts],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def set_fake_aws_env(monkeypatch) -> None:
    for k in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)


def set_alias(account_id: str, alias: str) -> None:
    """Inside an active moto mock: give a (moto) account an IAM alias."""
    import boto3
    creds = boto3.client("sts").assume_role(
        RoleArn=f"arn:aws:iam::{account_id}:role/EduCleanupRole", RoleSessionName="test-setup",
        ExternalId=EXTERNAL_ID)["Credentials"]
    s = boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                      aws_session_token=creds["SessionToken"], region_name="us-east-1")
    s.client("iam").create_account_alias(AccountAlias=alias)
