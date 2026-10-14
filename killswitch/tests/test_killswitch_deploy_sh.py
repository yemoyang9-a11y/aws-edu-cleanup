"""deploy_killswitch.sh: shellcheck + behaviour with a fake `aws` CLI on PATH."""
import json
import os
import shutil
import stat
import subprocess

import pytest

from killswitch_test_support import KS

SCRIPT = KS / "deploy_killswitch.sh"

FAKE_AWS = r'''#!/usr/bin/env bash
# fake aws CLI: logs every call, answers the few calls deploy_killswitch.sh makes
printf '%s\n' "$*" >> "$FAKE_AWS_LOG"
args=" $* "
case "$args" in
  *" sts assume-role "*)
    printf 'ASIAFAKE\tsecret\ttoken\n' ;;
  *" sts get-caller-identity "*)
    printf '123456789012\t%s\n' "${FAKE_CALLER:-arn:aws:iam::123456789012:user/operator}" ;;
  *" cloudformation describe-stacks "*"StackStatus"*)
    if [[ -n "${FAKE_STACK_STATUS:-}" ]]; then echo "$FAKE_STACK_STATUS"; else echo "Stack does not exist" >&2; exit 254; fi ;;
  *" cloudformation describe-stacks "*"FunctionName"*)
    if [[ -n "${FAKE_STACK_STATUS:-}" ]]; then echo "edu-killswitch-fn"; else exit 254; fi ;;
  *" cloudformation describe-stacks "*)
    printf 'AlarmName\tedu-killswitch-x-bedrock-tokens\n' ;;
  *" cloudformation validate-template "*) echo '{}' ;;
  *" cloudformation deploy "*) [[ "${FAKE_DEPLOY_FAIL:-}" == "1" ]] && exit 255; echo "deployed" ;;
  *" cloudformation delete-stack "*|*" cloudformation wait "*) ;;
  *" lambda invoke "*)
    out="${*: -1}"
    if [[ "${FAKE_LAMBDA_ERROR:-}" == "1" ]]; then
      echo '{"errorMessage":"boom"}' > "$out"; echo '{"StatusCode":200,"FunctionError":"Unhandled"}'
    else
      echo '{"status": "dry-run", "users": []}' > "$out"; echo '{"StatusCode":200}'
    fi ;;
  *) echo "unexpected aws call: $*" >&2; exit 99 ;;
esac
'''


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "aws.log"
    log.write_text("")
    e = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    e.update(PATH="%s:%s" % (bindir, os.environ["PATH"]), FAKE_AWS_LOG=str(log))

    class Env:
        pass

    x = Env()
    x.env, x.log, x.tmp = e, log, tmp_path

    def run(*args, **extra):
        en = dict(e)
        en.update(extra)
        r = subprocess.run(["bash", str(SCRIPT)] + list(args), capture_output=True, text=True, env=en)
        r.calls = log.read_text().splitlines()
        return r

    x.run = run
    return x


def test_shellcheck_clean():
    exe = shutil.which("shellcheck")
    if not exe:
        pytest.skip("shellcheck not installed")
    r = subprocess.run([exe, "-x", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_script_header():
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text


def test_dry_run_default_validates_only(env):
    r = env.run("--threshold", "500000")
    assert r.returncode == 0, r.stderr
    assert not [c for c in r.calls if "cloudformation deploy" in c]
    assert len([c for c in r.calls if "validate-template" in c]) == 2
    assert "killswitch dry-run [ok]" in r.stdout
    assert "edu-killswitch-ap-northeast-2" in r.stdout and "edu-killswitch-us-east-1" in r.stdout


def test_apply_deploys_each_region_with_parameters(env):
    r = env.run("--threshold", "500000", "--period-min", "10", "--target-mode", "GROUP", "--group", "students",
                "--exempt", "ops1,ops2", "--email", "ops@example.com", "--regions", "ap-northeast-2,us-east-1,global",
                "--apply", "--json-out", str(env.tmp / "o.json"))
    assert r.returncode == 0, r.stderr
    deploys = [c for c in r.calls if "cloudformation deploy" in c]
    assert len(deploys) == 2
    d = deploys[0]
    assert "--region ap-northeast-2" in d and "--stack-name edu-killswitch-ap-northeast-2" in d
    assert "--capabilities CAPABILITY_IAM" in d and "CAPABILITY_NAMED_IAM" not in d
    for p in ("TokenThreshold=500000", "PeriodMinutes=10", "TargetMode=GROUP", "TargetGroupName=students",
              "ExemptUserNames=ops1,ops2,operator", "NotificationEmail=ops@example.com", "EnableApiTriggers=true"):
        assert p in d, p
    assert "--stack-name edu-killswitch-us-east-1" in deploys[1]
    res = json.loads((env.tmp / "o.json").read_text())
    assert res["module"] == "killswitch-deploy" and res["applied"] is True and res["status"] == "ok"
    assert res["account_id"] == "123456789012" and res["generated_at"]
    assert [s["stack"] for s in res["stacks"]] == ["edu-killswitch-ap-northeast-2", "edu-killswitch-us-east-1"]


def test_readme_paste_block_empty_values_accepted(env):
    """README CloudShell block passes --exempt "" and --email "" when unused."""
    r = env.run("--regions", "ap-northeast-2,us-east-1", "--threshold", "2000000", "--period-min", "5",
                "--target-mode", "ALL_IAM_USERS", "--exempt", "", "--email", "", "--apply")
    assert r.returncode == 0, r.stderr
    d = next(c for c in r.calls if "cloudformation deploy" in c)
    assert "ExemptUserNames=operator" in d and "NotificationEmail= " in d + " "


def test_calling_user_already_exempt_not_duplicated(env):
    r = env.run("--threshold", "1", "--exempt", "operator", "--apply", "--regions", "us-east-1")
    d = next(c for c in r.calls if "cloudformation deploy" in c)
    assert "ExemptUserNames=operator " in d + " "


def test_role_caller_not_added_to_exempt(env):
    r = env.run("--threshold", "1", "--apply", "--regions", "us-east-1",
                "--role-arn", "arn:aws:iam::123456789012:role/EduCleanupRole", "--external-id", "xyz",
                FAKE_CALLER="arn:aws:sts::123456789012:assumed-role/EduCleanupRole/edu-cleanup")
    assert r.returncode == 0, r.stderr
    assume = next(c for c in r.calls if "sts assume-role" in c)
    assert "--external-id xyz" in assume and "--role-session-name edu-cleanup" in assume
    d = next(c for c in r.calls if "cloudformation deploy" in c)
    assert "ExemptUserNames= " in d + " "
    assert "--no-api-triggers" not in d


def test_profile_and_no_api_triggers(env):
    r = env.run("--threshold", "1", "--apply", "--regions", "us-east-1", "--profile", "edu", "--no-api-triggers")
    assert r.returncode == 0, r.stderr
    assert all("--profile edu" in c for c in r.calls)
    assert "EnableApiTriggers=false" in next(c for c in r.calls if "cloudformation deploy" in c)


def test_rollback_complete_is_deleted_first(env):
    r = env.run("--threshold", "1", "--apply", "--regions", "us-east-1", FAKE_STACK_STATUS="ROLLBACK_COMPLETE")
    assert r.returncode == 0, r.stderr
    i_del = next(i for i, c in enumerate(r.calls) if "delete-stack" in c)
    i_dep = next(i for i, c in enumerate(r.calls) if "cloudformation deploy" in c)
    assert i_del < i_dep


def test_deploy_failure_exit_1(env):
    r = env.run("--threshold", "1", "--apply", FAKE_DEPLOY_FAIL="1")
    assert r.returncode == 1
    assert "[failed]" in r.stdout


def test_test_mode_invokes_dry_run(env):
    r = env.run("--test", FAKE_STACK_STATUS="CREATE_COMPLETE")
    assert r.returncode == 0, r.stderr
    inv = [c for c in r.calls if "lambda invoke" in c]
    assert len(inv) == 2
    assert '"dry_run":true' in inv[0] and "--function-name edu-killswitch-fn" in inv[0]
    assert not [c for c in r.calls if "cloudformation deploy" in c]
    assert "tested" in r.stdout and "status: dry-run" in r.stdout


def test_test_mode_lambda_error_and_missing_stack(env):
    assert env.run("--test", FAKE_STACK_STATUS="CREATE_COMPLETE", FAKE_LAMBDA_ERROR="1").returncode == 1
    r = env.run("--test")
    assert r.returncode == 1 and "missing" in r.stdout


@pytest.mark.parametrize("args,msg", [
    ([], "--threshold"),
    (["--threshold", "abc"], "--threshold"),
    (["--threshold", "5", "--period-min", "7"], "--period-min"),
    (["--threshold", "5", "--target-mode", "GROUP"], "--group"),
    (["--threshold", "5", "--target-mode", "SOME"], "--target-mode"),
    (["--threshold", "5", "--email", "nope"], "--email"),
    (["--threshold", "5", "--bogus"], "unknown option"),
    (["--threshold"], "needs a value"),
    (["--threshold", "5", "--regions", ""], "--regions"),
])
def test_argument_validation_exit_2(env, args, msg):
    r = env.run(*args)
    assert r.returncode == 2
    assert msg in r.stderr
    assert not [c for c in r.calls if "cloudformation" in c]
