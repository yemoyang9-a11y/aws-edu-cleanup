"""run_nuke.sh + cloudshell-oneshot.sh: shellcheck, end-to-end with fake `aws`/`aws-nuke`, config parity."""
import json
import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from nuke_test_support import NUKE_DIR

import make_config as mc

RUN = NUKE_DIR / "run_nuke.sh"
ONESHOT = NUKE_DIR / "cloudshell-oneshot.sh"
ACCT = "123456789012"
needs_tools = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("jq")), reason="needs bash + jq")


# ------------------------------------------------------------------ shellcheck
@pytest.mark.parametrize("script", [RUN, ONESHOT], ids=lambda p: p.name)
def test_shellcheck(script):
    sc = shutil.which("shellcheck")
    if not sc:
        pytest.skip("shellcheck not installed")
    p = subprocess.run([sc, "-x", str(script)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_bash_syntax():
    for s in (RUN, ONESHOT):
        subprocess.run(["bash", "-n", str(s)], check=True)


def test_oneshot_starts_with_set_plus_h():
    """Pasted into an interactive shell, '!' would trigger history expansion unless `set +H` runs first."""
    lines = ONESHOT.read_text().splitlines()
    assert lines[0] == "#!/usr/bin/env bash" and lines[1].startswith("set +H")
    body = "\n".join(lines[2:])
    assert body.lstrip().split("\n")[0].startswith("#") and "\n(\n" in body and body.rstrip().endswith(")")


# ------------------------------------------------------------------ fakes
FAKE_AWS = r'''#!/usr/bin/env bash
echo "aws $*" >> "$FAKE_LOG"
case "$*" in
  *"sts assume-role"*) echo '{"Credentials":{"AccessKeyId":"AK","SecretAccessKey":"SK","SessionToken":"TK"}}' ;;
  *"sts get-caller-identity --query Account"*) echo 123456789012 ;;
  *"sts get-caller-identity --query Arn"*) echo "$FAKE_CALLER" ;;
  *"sts get-caller-identity"*)
    if [ -n "${AWS_SESSION_TOKEN:-}" ]; then echo '{"Account":"123456789012","Arn":"arn:aws:sts::123456789012:assumed-role/EduCleanupRole/s"}'
    else printf '{"Account":"123456789012","Arn":"%s"}\n' "$FAKE_CALLER"; fi ;;
  *"iam get-user"*) echo '{}' ;;
  *"list-attached-user-policies"*) printf 'arn:aws:iam::aws:policy/ReadOnlyAccess\tarn:aws:iam::123456789012:policy/ops\n' ;;
  *"list-groups-for-user"*) echo admins ;;
  *"list-attached-group-policies"*) echo arn:aws:iam::123456789012:policy/grp ;;
  *"list-mfa-devices"*) echo arn:aws:iam::123456789012:mfa/ops-kim ;;
  *"list-account-aliases"*) echo "${FAKE_ALIAS:-None}" ;;
  *"create-account-alias"*) echo "ALIAS-CREATED" >> "$FAKE_LOG" ;;
  *"describe-vpcs"*) echo 0 ;;
  *"create-default-vpc"*) echo vpc-new ;;
  *) echo "unexpected aws call: $*" >&2; exit 1 ;;
esac
'''

# Emits lines in the exact aws-nuke v3.68.3 JSON shape (see fixtures/real-*.jsonl)
FAKE_NUKE = r'''#!/usr/bin/env bash
if [ "$1" = version ]; then echo "aws-nuke version v3.68.3"; exit 0; fi
args="$*"; cfg=""; apply=0
while [ $# -gt 0 ]; do case "$1" in -c) cfg="$2"; shift 2 ;; --no-dry-run) apply=1; shift ;; *) shift ;; esac; done
owner=$(python3 -c 'import sys,yaml; print(yaml.safe_load(open(sys.argv[1]))["regions"][0])' "$cfg")
echo "$(date +%s.%N) START $owner EXT=${AWS_ASSUME_ROLE_EXTERNAL_ID:-} $args" >> "$FAKE_LOG"
sleep "${FAKE_SLEEP:-0.5}"
j() { printf '%s\n' "$1"; }
j '{"component":"libnuke","level":"info","msg":"aws-nuke - v3.68.3 - d7e7","time":"t"}'
j '{"error":"AccessDeniedException","level":"error","msg":"listing failed:\n    AccessDeniedException","owner":"'"$owner"'","resource_type":"FMSPolicy","time":"t"}'
j '{"level":"info","msg":"would remove","name":"x-1","owner":"'"$owner"'","state":"new","state_code":0,"type":"EC2Instance","time":"t"}'
j '{"level":"info","msg":"filtered: filtered by config","name":"EduCleanupRole","owner":"'"$owner"'","state":"filtered","state_code":7,"type":"IAMRole","time":"t"}'
j '{"component":"libnuke","filtered":1,"level":"info","msg":"Scan complete: 2 total, 1 nukeable, 1 filtered.\n","nukeable":1,"time":"t","total":2}'
if [ "$apply" = 0 ]; then j '{"component":"libnuke","level":"info","msg":"The above resources would be deleted with the supplied configuration. Provide --no-dry-run to actually destroy resources.","time":"t"}'; echo "$(date +%s.%N) END $owner" >> "$FAKE_LOG"; exit 0; fi
j '{"level":"info","msg":"triggered remove","name":"x-1","owner":"'"$owner"'","state":"pending","state_code":3,"type":"EC2Instance","time":"t"}'
if [ "$owner" = "${FAKE_FAIL_OWNER:-none}" ]; then
  j '{"level":"info","msg":"failed","name":"x-1","owner":"'"$owner"'","state":"failed","state_code":6,"type":"EC2Instance","time":"t"}'
  j '{"component":"libnuke","level":"error","msg":"There are resources in failed state, but none are ready for deletion, anymore.","time":"t"}'
  j '{"level":"fatal","msg":"failed","time":"t"}'; echo "$(date +%s.%N) END $owner" >> "$FAKE_LOG"; exit 1
fi
if [ "$owner" = "${FAKE_CRASH_OWNER:-none}" ]; then echo "panic: boom"; echo "goroutine 1 [running]:"; exit 2; fi
j '{"level":"info","msg":"removed","name":"x-1","owner":"'"$owner"'","state":"finished","state_code":8,"type":"EC2Instance","time":"t"}'
j '{"component":"libnuke","failed":0,"finished":1,"level":"info","msg":"Nuke complete: 0 failed, 1 skipped, 1 finished.\n","skipped":1,"time":"t"}'
echo "$(date +%s.%N) END $owner" >> "$FAKE_LOG"
'''


@pytest.fixture
def fakes(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    for name, body in (("aws", FAKE_AWS), ("aws-nuke", FAKE_NUKE)):
        p = b / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", FAKE_LOG=str(tmp_path / "calls.log"),
               FAKE_CALLER="arn:aws:iam::123456789012:user/ops-kim", HOME=str(tmp_path / "home"))
    env.pop("AWS_NUKE_BIN", None)
    # Other modules' conftests export fake AWS_* credentials at import time; the fake `aws`
    # treats AWS_SESSION_TOKEN as "running under an assumed role", so start from a clean slate.
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        env.pop(k, None)
    (tmp_path / "home").mkdir()
    return tmp_path, env


def run_nuke(tmp_path, env, *args):
    out = tmp_path / "r.json"
    p = subprocess.run(["bash", str(RUN), "--json-out", str(out), "--log-dir", str(tmp_path / "logs"), *args],
                       capture_output=True, text=True, env=env, timeout=120)
    res = json.loads(out.read_text()) if out.exists() else None
    return p, res


def calls(tmp_path):
    return (tmp_path / "calls.log").read_text()


def starts(tmp_path):
    rows = [ln.split() for ln in calls(tmp_path).splitlines() if " START " in ln or " END " in ln]
    return [(float(r[0]), r[1], r[2]) for r in rows]


# ------------------------------------------------------------------ run_nuke.sh
@needs_tools
def test_dry_run_parallel_then_global(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, env)
    assert p.returncode == 0, p.stderr
    assert res["module"] == "nuke" and res["applied"] is False and res["status"] == "OK"
    assert list(res["regions"]) == ["ap-northeast-2", "us-east-1", "global"]
    assert res["totals"]["would_remove"] == 3
    ev = starts(tmp)
    t = {(kind, owner): ts for ts, kind, owner in ev}
    # both regions start before either ends (parallel); global starts after both ended
    assert t[("START", "ap-northeast-2")] < t[("END", "us-east-1")]
    assert t[("START", "us-east-1")] < t[("END", "ap-northeast-2")]
    assert t[("START", "global")] >= max(t[("END", "ap-northeast-2")], t[("END", "us-east-1")])
    log = calls(tmp)
    assert "ALIAS-CREATED" not in log                       # dry-run never changes the account
    assert "--no-dry-run" not in log and "--no-alias-check" in log
    for flag in ("--no-prompt", "--prompt-delay 3", "--log-format json", "--max-wait-retries 120",
                 "--parallel-queries 24"):
        assert flag in log, flag
    assert "WOULD-DELETE" in p.stdout and "RESULT: OK" in p.stdout
    # lister errors are classified with listing_errors.json (FMSPolicy = known noise -> expected, not LIST-ERR)
    us = res["regions"]["us-east-1"]
    assert us["listing_failed_by_bucket"] == {"expected": 1, "maybe_scp": 0, "transient": 0, "unexpected": 0}
    assert us["listing_failed_unexpected"] == [] and res["totals"]["listing_failed_unexpected"] == 0
    assert res["totals"]["listing_failed_by_bucket"]["expected"] == 3
    assert "listing failed (expected - service not used/subscribed): FMSPolicy" in p.stdout


@needs_tools
def test_caller_iam_user_is_protected_with_policies_groups_mfa(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, env)
    assert res["protected_users"] == ["ops-kim"]
    cfg = yaml.safe_load((tmp / "logs" / "config-global.yaml").read_text())
    f = cfg["accounts"][ACCT]["filters"]
    assert {"property": "Name", "type": "exact", "value": "ops-kim"} in f["IAMUser"]
    assert {"property": "ARN", "type": "exact", "value": "arn:aws:iam::123456789012:policy/ops"} in f["IAMPolicy"]
    assert {"property": "ARN", "type": "exact", "value": "arn:aws:iam::123456789012:policy/grp"} in f["IAMPolicy"]
    assert not [x for x in f["IAMPolicy"] if "ReadOnlyAccess" in x["value"]]     # AWS-managed: not ours to keep
    assert f["IAMGroup"] == [{"property": "Name", "type": "exact", "value": "admins"}]
    assert f["IAMVirtualMFADevice"][0]["value"].endswith(":mfa/ops-kim")
    assert cfg["bypass-alias-check-accounts"] == [ACCT]
    assert cfg["regions"] == ["global"]
    assert yaml.safe_load((tmp / "logs" / "config-us-east-1.yaml").read_text())["regions"] == ["us-east-1"]


@needs_tools
def test_apply_creates_alias_and_reports_failures(fakes):
    tmp, env = fakes
    env = dict(env, FAKE_FAIL_OWNER="ap-northeast-2")
    p, res = run_nuke(tmp, env, "--apply")
    assert p.returncode == 1, p.stderr
    assert res["status"] == "ISSUES" and res["applied"] is True
    assert res["alias_created"] is True and res["account_alias"] == f"edu-{ACCT}"
    assert "ALIAS-CREATED" in calls(tmp) and "--no-dry-run" in calls(tmp) and "--no-alias-check" not in calls(tmp)
    assert res["regions"]["ap-northeast-2"]["failed_by_type"] == {"EC2Instance": 1}
    assert res["regions"]["us-east-1"]["removed_by_type"] == {"EC2Instance": 1}
    assert res["totals"]["failed"] == 1
    assert "RESULT: ISSUES" in p.stdout and "x EC2Instance x-1" in p.stdout


@needs_tools
def test_apply_clean_exit_0(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, dict(env, FAKE_ALIAS="edu-2026-10-01"), "--apply", "--regions", "ap-northeast-2")
    assert p.returncode == 0, p.stderr
    assert res["status"] == "OK" and res["totals"]["removed"] == 2 and res["alias_created"] is False
    assert list(res["regions"]) == ["ap-northeast-2", "global"]


@needs_tools
def test_crash_is_exit_2(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, dict(env, FAKE_CRASH_OWNER="global", FAKE_ALIAS="a"), "--apply")
    assert p.returncode == 2 and res["status"] == "ERROR"
    assert res["regions"]["global"]["outcome"] == "error"


@needs_tools
def test_role_arn_mode(fakes):
    tmp, env = fakes
    arn = f"arn:aws:iam::{ACCT}:role/EduCleanupRole"
    p, res = run_nuke(tmp, dict(env, FAKE_ALIAS="edu-x"), "--account-id", ACCT, "--role-arn", arn,
                      "--external-id", "S3CR3T", "--regions", "us-east-1", "--session-name", "edu-2026-nuke")
    assert p.returncode == 0, p.stderr
    log = calls(tmp)
    assert f"--assume-role-arn {arn}" in log and "--assume-role-session-name edu-2026-nuke" in log
    assert "EXT=S3CR3T" in log                                   # via AWS_ASSUME_ROLE_EXTERNAL_ID ...
    assert "--assume-role-external-id" not in log                # ... not on the command line (ps)
    assert "sts assume-role" in log and "--external-id S3CR3T" in log
    assert res["protected_users"] == []                          # assumed role: no IAM user to protect
    cfg = yaml.safe_load((tmp / "logs" / "config-us-east-1.yaml").read_text())
    assert cfg["settings"]["CloudFormationStack"]["UseCurrentRoleToDeleteStack"] is True
    assert "S3CR3T" not in json.dumps(res)


@needs_tools
def test_account_mismatch_and_bad_args(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, env, "--account-id", "999999999999")
    assert p.returncode == 2 and res["status"] == "ERROR" and "refusing" in res["error"]
    p, _ = run_nuke(tmp, env, "--prompt-delay", "1")
    assert p.returncode == 2
    p, _ = run_nuke(tmp, env, "--bogus")
    assert p.returncode == 2


@needs_tools
def test_prod_alias_refused(fakes):
    tmp, env = fakes
    p, res = run_nuke(tmp, dict(env, FAKE_ALIAS="My-PROD-account"))
    assert p.returncode == 2 and "prod" in res["error"]
    assert " START " not in calls(tmp)


# ------------------------------------------------------------------ cloudshell-oneshot.sh
def oneshot_config(tmp_path, env, region, caller):
    """Run only the config part of the oneshot block (up to mkcfgs) and return the YAML for `region`."""
    text = ONESHOT.read_text()
    head = text.split("\n(\n", 1)[1].split("# ---- runner:", 1)[0]
    head = head.replace('if ! command -v aws-nuke', 'if false && ! command -v aws-nuke')
    script = tmp_path / "part.sh"
    script.write_text("set -uo pipefail\n" + head)
    e = dict(env, FAKE_CALLER=caller)
    subprocess.run(["bash", str(script)], check=True, env=e, capture_output=True, text=True, timeout=60)
    return yaml.safe_load((Path(env["HOME"]) / "edu-nuke" / f"config-{region}.yaml").read_text())


@needs_tools
@pytest.mark.parametrize("caller,users,roles", [
    ("arn:aws:iam::123456789012:user/ops-kim", ["ops-kim"], []),
    ("arn:aws:sts::123456789012:assumed-role/OpsRole/me", [], ["OpsRole"]),
    ("arn:aws:iam::123456789012:root", [], []),
])
def test_oneshot_config_matches_make_config(fakes, caller, users, roles):
    """The inlined CloudShell config must be identical to make_config.py's (same excludes/settings/filters)."""
    tmp, env = fakes
    for region in ("ap-northeast-2", "global"):
        got = oneshot_config(tmp, env, region, caller)
        kw = dict(include_global=False, protect_users=users, protect_roles=roles, bypass_alias_check=True)
        if users:
            kw.update(protect_groups=["admins"],
                      protect_policy_arns=["arn:aws:iam::123456789012:policy/ops", "arn:aws:iam::123456789012:policy/grp"],
                      protect_mfa_serials=["arn:aws:iam::123456789012:mfa/ops-kim"])
        want = mc.build_config(ACCT, [region], **kw)
        assert got == want, region


@needs_tools
def test_oneshot_full_paste_flow(fakes):
    """Whole block through an interactive bash on a pty: dry-run, 'yes', apply, default VPCs."""
    pty = pytest.importorskip("pty")
    import select
    import time
    tmp, env = fakes
    env = dict(env, FAKE_SLEEP="0.2", TERM="dumb")
    nb = Path(env["HOME"]) / ".local" / "bin"
    nb.mkdir(parents=True)
    shutil.copy(tmp / "bin" / "aws-nuke", nb / "aws-nuke")
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("bash", ["bash", "--norc", "-i"], env)
    out = b""

    def pump(seconds, until=None):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(fd, 65536)
                except OSError:
                    return
            if until and until in out:
                return
    os.write(fd, b"\x1b[200~" + ONESHOT.read_bytes() + b"\x1b[201~")
    pump(1)
    os.write(fd, b"\n")
    pump(60, b"type yes:")
    os.write(fd, b"yes\n")
    pump(60, b"[y/N]")
    os.write(fd, b"y\n")
    pump(10, b"default VPC: vpc-new")
    os.write(fd, b"echo EXIT=$?\nexit\n")
    pump(5)
    os.waitpid(pid, 0)
    txt = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", out.decode(errors="replace")).replace("\r", "")
    assert "event not found" not in txt
    assert "DRY-RUN done" in txt and "APPLY done" in txt and "RESULT: clean" in txt
    assert "us-east-1 default VPC: vpc-new" in txt and "EXIT=0" in txt
    log = calls(tmp)
    assert log.index("--no-alias-check") < log.index("ALIAS-CREATED") < log.index("--no-dry-run")
    ev = starts(tmp)
    apply_ev = [e for e in ev if e[0] > min(ts for ts, k, o in ev if k == "START" and o == "global")]
    assert apply_ev                                            # second (apply) round happened


def test_readme_contains_the_cloudshell_block_verbatim():
    readme = (NUKE_DIR / "README.md").read_text()
    begin, end = "<!-- BEGIN CLOUDSHELL BLOCK (= cloudshell-oneshot.sh) -->\n```bash\n", "```\n<!-- END CLOUDSHELL BLOCK -->"
    assert begin in readme and end in readme
    embedded = readme.split(begin, 1)[1].split(end, 1)[0]
    assert embedded == ONESHOT.read_text(), "README block is stale - re-copy cloudshell-oneshot.sh into README.md"


# ------------------------------------------------------------------ listing-error classification in the oneshot
LF_BEGIN, LF_END = "# BEGIN listing-error classification", "# END listing-error classification"


def _lf_block(text):
    block = text[text.index(LF_BEGIN):text.index(LF_END) + len(LF_END)]
    return [ln.strip() for ln in block.splitlines()]


def test_oneshot_embeds_the_listing_error_rules_verbatim():
    text = ONESHOT.read_text()
    embedded = text.split("<<'JSON' || true\n", 1)[1].split("\nJSON\n", 1)[0] + "\n"
    assert embedded == (NUKE_DIR / "listing_errors.json").read_text(), \
        "re-copy nuke/listing_errors.json into cloudshell-oneshot.sh (LF_RULES heredoc)"
    assert "\nJSON\n" not in (NUKE_DIR / "listing_errors.json").read_text()


def test_oneshot_classification_is_the_same_jq_as_summarize():
    assert _lf_block(ONESHOT.read_text()) == _lf_block((NUKE_DIR / "summarize.jq").read_text())
    # the jq program is single-quoted in bash: the shared block must not contain a single quote
    assert "'" not in "\n".join(_lf_block(ONESHOT.read_text()))


@needs_tools
def test_oneshot_summary_buckets(tmp_path):
    """The oneshot summary() on real lister errors: compact lines, LIST-ERR = real failures only."""
    from nuke_test_support import FIXTURES
    w = tmp_path / "w"
    w.mkdir()
    evs = [json.loads(ln) for ln in (FIXTURES / "real-cloudshell-v3.68.3-listing-failed.jsonl").read_text().splitlines()]
    regions = ["us-west-1", "ap-northeast-3", "us-east-1"]
    for r in regions + ["global"]:
        lines = [json.dumps(e) for e in evs if e["owner"] == r]
        if r == "us-east-1":   # plus one error no rule knows -> its own line
            lines.append(json.dumps({"error": "ResourceNotFoundException: odd", "level": "error", "owner": r,
                                     "msg": "listing failed:\n    ResourceNotFoundException: odd",
                                     "resource_type": "GlueJob", "time": "t"}))
        (w / f"dry-{r}.jsonl").write_text("\n".join(lines) + "\n")
        (w / f"dry-{r}.rc").write_text("0\n")
    text = ONESHOT.read_text()
    part = text[text.index("# listing-error rules"):text.index("T0=$SECONDS; nuke dry;")]
    script = tmp_path / "s.sh"
    script.write_text(f'set -uo pipefail\nW="{w}"\nREGIONS="{" ".join(regions)}"\n{part}summary dry\n')
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True, timeout=60).stdout
    rows = {ln.split()[0]: ln.split() for ln in out.splitlines() if ln[:1].isalpha() and ln.split()[0] in regions + ["global"]}
    assert rows["us-west-1"][-2:] == ["2", "9/3"]          # 2 x HTTP 500; DocDB/Bedrock/Comprehend/Glue expected
    assert rows["ap-northeast-3"][-2:] == ["0", "8/1"]
    assert rows["us-east-1"][-2:] == ["5", "3/0"]          # 4 x Lex V1 503 + GlueJob
    assert rows["global"][-2:] == ["0", "-"]
    assert "    access denied (maybe SCP): RekognitionDataset, RekognitionProject, TimestreamInfluxDBDbInstance" in out
    assert "    LISTING FAILED (5xx/throttling - re-run to confirm): BedrockFlowAlias, BedrockPrompt" in out
    assert "    LISTING FAILED GlueJob: ResourceNotFoundException: odd" in out
    assert out.count("LISTING FAILED ") == 3                # 2 collapsed 5xx lines + 1 unexpected
