"""End-to-end tests of cleanup.sh + tools/report.py with stub step scripts and a fake `aws`."""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCT = "123456789012"

FAKE_AWS = r"""#!/usr/bin/env bash
case "$*" in
  *"sts get-caller-identity"*) echo "123456789012" ;;
  *"account list-regions"*) printf 'ap-northeast-2\tus-east-1\n' ;;
  *"iam list-users"*) printf 'teacher\tstudent01\n' ;;
  *"create-default-vpc"*) exit 0 ;;
  *) exit 0 ;;
esac
"""

# One stub serves every step; behaviour comes from $STUB_DIR/plan.json and the call counter.
STUB = r'''#!/usr/bin/env python3
import json, os, sys
d = os.environ["STUB_DIR"]; step = os.environ["STEP"]
plan = json.load(open(os.path.join(d, "plan.json")))
args = sys.argv[1:]
with open(os.path.join(d, "calls.log"), "a") as f:
    f.write(step + " " + " ".join(args) + "\n")
out = args[args.index("--json-out") + 1] if "--json-out" in args else None
cnt_file = os.path.join(d, step + ".count")
n = int(open(cnt_file).read()) + 1 if os.path.exists(cnt_file) else 1
open(cnt_file, "w").write(str(n))
if step == "verify":
    seq = plan.get("verify", ["CLEAN"])
    st = seq[min(n, len(seq)) - 1]
    res = {"status": st, "inventory": {}, "examples": {}}
    if st == "RESIDUAL":
        res["inventory"] = {"ap-northeast-2": {"ec2_instances": 1}}
        res["examples"] = {"ap-northeast-2": {"ec2_instances": ["i-0abc"]}}
    if out: json.dump(res, open(out, "w"))
    print("[verify] 판정:", st)
    sys.exit({"CLEAN": 0, "RESIDUAL": 1}.get(st, 2))
if step == "preclose":
    f = plan.get("preclose_findings", [])
    if out: json.dump({"findings": f}, open(out, "w"))
    sys.exit(1 if any(x["severity"] == "BLOCKER" and x["action"] != "done" for x in f) else 0)
if step == "nuke" and out:
    json.dump({"status": plan.get("nuke_status", "OK"), "regions": {}}, open(out, "w"))
if step == "cfn" and out:
    json.dump({"status": "OK"}, open(out, "w"))
sys.exit(0)
'''


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "cleanup.sh", r / "cleanup.sh")
    shutil.copy(ROOT / "tools" / "report.py", r / "tools" / "report.py")
    stub = tmp_path / "stub.py"
    stub.write_text(STUB)
    for rel, step, kind in (("nuke/run_nuke.sh", "nuke", "sh"), ("cfn_predelete/predelete_stacks.py", "cfn", "py"),
                            ("preclose/preclose_check.py", "preclose", "py"), ("verify/verify_cost.py", "verify", "py")):
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if kind == "sh":
            p.write_text(f'#!/usr/bin/env bash\nSTEP={step} exec "{sys.executable}" "{stub}" "$@"\n')
        else:
            p.write_text(f'import os, runpy, sys\nos.environ["STEP"]="{step}"\nsys.argv[0]="{stub}"\nrunpy.run_path("{stub}", run_name="__main__")\n')
    b = tmp_path / "bin"
    b.mkdir()
    (b / "aws").write_text(FAKE_AWS)
    (b / "aws").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return r, b, home, tmp_path


def run(repo, plan, typed, *args):
    r, b, home, tmp = repo
    (tmp / "plan.json").write_text(json.dumps(plan))
    tty = tmp / "tty.txt"
    tty.write_text(typed)
    for c in tmp.glob("*.count"):
        c.unlink()
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", HOME=str(home), STUB_DIR=str(tmp),
               EDU_CLEANUP_TTY=str(tty))
    p = subprocess.run(["bash", str(r / "cleanup.sh"), "--wait-min", "0", *args], capture_output=True, text=True,
                       env=env, timeout=120)
    calls = (tmp / "calls.log").read_text() if (tmp / "calls.log").exists() else ""
    (tmp / "calls.log").unlink(missing_ok=True)
    reports = sorted(home.glob("cleanup-report-*.txt"))
    return p, calls, (reports[-1].read_text() if reports else "")


def test_clean_first_pass(repo):
    p, calls, rep = run(repo, {"verify": ["CLEAN"]}, ACCT + "\n")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "✅ 정리 완료" in p.stdout
    assert calls.count("verify ") == 1
    # IAM users kept by default: nuke and verify get them as protected
    assert "--protect-iam-user teacher" in calls and "--protect-prefix student01" in calls
    assert "결과: 정리 완료" in rep


def test_retry_then_clean(repo):
    p, calls, rep = run(repo, {"verify": ["RESIDUAL", "CLEAN"]}, ACCT + "\n")
    assert p.returncode == 0
    assert "삭제 2회차" in p.stdout and "✅" in p.stdout
    assert calls.count("verify ") == 2
    assert "삭제 시도 횟수: 2" in rep


def test_still_dirty_after_max_passes(repo):
    p, calls, rep = run(repo, {"verify": ["RESIDUAL"]}, ACCT + "\n", "--max-passes", "3")
    assert p.returncode == 2
    assert "❌ 정리가 끝나지 않았어요" in p.stdout and "EC2 인스턴스 1개 (i-0abc)" in p.stdout
    assert calls.count("verify ") == 3
    assert "ap-northeast-2: EC2 인스턴스 1개" in rep and "cleanup-report-" in p.stdout


def test_preclose_blocker_is_warning(repo):
    f = [{"check": "marketplace_agreement", "region": "global", "resource_id": "agmt-1", "detail": "x",
          "severity": "BLOCKER", "action": "failed"}]
    p, _, rep = run(repo, {"verify": ["CLEAN"], "preclose_findings": f}, ACCT + "\n")
    assert p.returncode == 1 and "⚠️" in p.stdout
    assert "marketplace_agreement agmt-1" in rep


def test_root_key_warning_shown(repo):
    f = [{"check": "root_access_keys", "region": "global", "resource_id": "root", "detail": "",
          "severity": "WARN", "action": "none"}]
    p, _, _ = run(repo, {"verify": ["CLEAN"], "preclose_findings": f}, ACCT + "\n")
    assert p.returncode == 0 and "🔑 루트 액세스 키" in p.stdout


def test_blank_input_cancels_without_deleting(repo):
    p, calls, _ = run(repo, {}, "\n")
    assert p.returncode == 3 and "취소" in p.stdout
    assert "--apply" not in calls


def test_three_wrong_inputs_cancel(repo):
    p, calls, _ = run(repo, {}, "1\n2\n3\n")
    assert p.returncode == 3 and "세 번 틀려서" in p.stdout
    assert "--apply" not in calls


def test_wrong_then_right_input(repo):
    p, calls, _ = run(repo, {"verify": ["CLEAN"]}, "999\n" + ACCT + "\n")
    assert p.returncode == 0 and "번호가 달라요" in p.stdout and "--apply" in calls


def test_input_with_spaces_or_hangul_garbage_is_cleaned(repo):
    p, _, _ = run(repo, {"verify": ["CLEAN"]}, "호 " + ACCT + " \n")
    assert p.returncode == 0


def test_preview_only_never_applies(repo):
    p, calls, _ = run(repo, {}, "", "--preview-only")
    assert p.returncode == 0 and "--apply" not in calls and "nuke " in calls


def test_skip_preview_and_delete_iam_users(repo):
    p, calls, _ = run(repo, {"verify": ["CLEAN"]}, ACCT + "\n", "--skip-preview", "--delete-iam-users")
    assert p.returncode == 0
    assert "--protect-iam-user" not in calls
    assert calls.splitlines()[0].startswith("cfn ") and "--apply" in calls.splitlines()[0]


def test_nuke_error_is_failure(repo):
    p, _, _ = run(repo, {"verify": ["CLEAN"], "nuke_status": "ERROR"}, ACCT + "\n")
    assert p.returncode == 2 and "❌" in p.stdout


def test_shellcheck_clean():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck not installed")
    assert subprocess.run(["shellcheck", str(ROOT / "cleanup.sh")]).returncode == 0
