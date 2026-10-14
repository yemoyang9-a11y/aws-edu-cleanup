import csv
import json
import os
import socket
import subprocess
import sys
import threading

import pytest
from moto import mock_aws

import hub_test_support as hts
import orchestrate as orch

A1, A2, A3 = "111111111111", "222222222222", "333333333333"
ALIASES = {A1: "edu-2026-10-01", A2: "edu-2026-10-02", A3: "edu-2026-10-03"}


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def aws(monkeypatch):
    hts.set_fake_aws_env(monkeypatch)
    with mock_aws():
        yield


@pytest.fixture
def env(tmp_path, aws):
    """Fake repo with stub steps, a registry of 3 accounts whose (moto) aliases match, and a state dir."""
    repo = hts.make_fake_repo(tmp_path / "repo")
    reg = hts.make_registry(tmp_path / "registry.json", [(a, ALIASES[a]) for a in (A1, A2, A3)])
    for a in (A1, A2, A3):
        hts.set_alias(a, ALIASES[a])
    state = tmp_path / "state"

    def run(command, *extra, codes=None, sleep=0.0, sleeps=None, no_json=None, use_env_var=False,
            cfn_client_factory=None, monkeypatch=None):
        hts.write_stub_config(repo, codes=codes, sleep=sleep, sleeps=sleeps, no_json=no_json)
        hts.clear_calls(repo)
        argv = [command, "--registry", str(reg), "--state-dir", str(state)]
        if not use_env_var:
            argv += ["--repo-root", str(repo)]
        argv += list(extra)
        rc = orch.main(argv, cfn_client_factory=cfn_client_factory)
        return rc, hts.read_calls(repo)

    def state_of(acct):
        return json.loads((state / "2026-10" / acct / "state.json").read_text(encoding="utf-8"))

    return type("Env", (), {"repo": repo, "reg": reg, "state": state, "run": staticmethod(run),
                            "state_of": staticmethod(state_of), "tmp": tmp_path})


def calls_for(calls, acct):
    return [c for c in calls if c["account"] == acct]


def opt(argv, name):
    return argv[argv.index(name) + 1]


# ---------------------------------------------------------------------------------------------
# pure mapping helpers
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("codes, state", [
    ([0, 0, 0], "CLEANED"), ([0, 1, 0], "PARTIAL"), ([1, 1, 1], "PARTIAL"), ([0, 0, 1], "PARTIAL"),
    ([2], "FAILED"), ([0, 2], "FAILED"), ([0, 1, 2], "FAILED"), ([0, None], "FAILED"), ([0, 3, 0], "FAILED"),
])
def test_clean_state_mapping(codes, state):
    assert orch.clean_state(codes) == state


@pytest.mark.parametrize("code, state", [(0, "VERIFIED_CLEAN"), (1, "RESIDUAL"), (3, "PENDING"),
                                         (2, "VERIFY_FAILED"), (None, "VERIFY_FAILED"), (9, "VERIFY_FAILED")])
def test_verify_state_mapping(code, state):
    assert orch.verify_state(code) == state


@pytest.mark.parametrize("codes, state", [([0, 1, 1], "PLANNED"), ([0, 0, 0], "PLANNED"), ([0, 2], "PLAN_FAILED"),
                                          ([None], "PLAN_FAILED")])
def test_plan_state_mapping(codes, state):
    assert orch.plan_state(codes) == state


def test_killswitch_parameters_from_registry():
    params = orch.killswitch_parameters({"TokenThreshold": 2000000, "PeriodMinutes": 5, "TargetMode": "ALL_IAM_USERS",
                                         "TargetGroupName": "", "ExemptUserNames": ["a", "b"],
                                         "NotificationEmail": None})
    assert params == [
        {"ParameterKey": "TokenThreshold", "ParameterValue": "2000000"},
        {"ParameterKey": "PeriodMinutes", "ParameterValue": "5"},
        {"ParameterKey": "TargetMode", "ParameterValue": "ALL_IAM_USERS"},
        {"ParameterKey": "TargetGroupName", "ParameterValue": ""},
        {"ParameterKey": "ExemptUserNames", "ParameterValue": "a,b"},
        {"ParameterKey": "NotificationEmail", "ParameterValue": ""},
        {"ParameterKey": "EnableApiTriggers", "ParameterValue": "true"},
    ]
    assert orch.killswitch_parameters({"EnableApiTriggers": False})[-1]["ParameterValue"] == "false"


def test_step_argv_contract(tmp_path):
    args = orch.build_parser().parse_args(["clean", "--registry", "x", "--profile", "hub",
                                           "--state-dir", str(tmp_path)])
    reg = {"cohort": "2026-10", "external_id": hts.EXTERNAL_ID, "regions": ["ap-northeast-2", "us-east-1"],
           "accounts": [], "killswitch": None}
    o = orch.Orchestrator(args, reg, tmp_path)
    acct = {"account_id": A1, "alias": ALIASES[A1]}
    nuke = o.step_argv("nuke", acct, tmp_path / "nuke.json", None)
    assert nuke[0] == "bash" and nuke[1].endswith("nuke/run_nuke.sh")
    assert opt(nuke, "--account-id") == A1
    assert opt(nuke, "--profile") == "hub"
    assert opt(nuke, "--role-arn") == f"arn:aws:iam::{A1}:role/EduCleanupRole"
    assert opt(nuke, "--external-id") == hts.EXTERNAL_ID
    assert opt(nuke, "--regions") == "ap-northeast-2,us-east-1"
    assert opt(nuke, "--json-out") == str(tmp_path / "nuke.json")
    assert "--apply" in nuke and "--session-name" not in nuke
    pre = o.step_argv("preclose", acct, tmp_path / "p.json", None)
    assert pre[1].endswith("preclose/preclose_check.py") and "--apply" not in pre
    assert "--session-name" in pre
    args.fix_preclose = True
    assert "--apply" in o.step_argv("preclose", acct, tmp_path / "p.json", None)
    o.command = "verify"
    ver = o.step_argv("verify", acct, tmp_path / "v.json", "2026-10-12")
    assert opt(ver, "--cleanup-date") == "2026-10-12" and "--apply" not in ver
    o.command = "plan"
    assert all("--apply" not in o.step_argv(s, acct, tmp_path / "x.json", None)
               for s in ("cfn_predelete", "nuke", "preclose"))


# ---------------------------------------------------------------------------------------------
# integration with stub step scripts + moto STS/IAM
# ---------------------------------------------------------------------------------------------
def test_plan_runs_all_accounts_in_parallel_with_sequential_steps(env):
    # 1.5 s per stub step: wide enough that the overlap check below stays stable when the
    # whole repo's suite runs at once (0.4 s was flaky under load).
    rc, calls = env.run("plan", sleep=1.5)
    assert rc == 0
    assert len(calls) == 9
    for a in (A1, A2, A3):
        mine = calls_for(calls, a)
        assert [c["step"] for c in mine] == ["cfn_predelete", "nuke", "preclose"]
        for prev, nxt in zip(mine, mine[1:]):  # sequential within an account
            assert prev["end"] <= nxt["start"]
        for c in mine:
            assert "--apply" not in c["argv"]
            assert opt(c["argv"], "--external-id") == hts.EXTERNAL_ID
            assert opt(c["argv"], "--role-arn") == f"arn:aws:iam::{a}:role/EduCleanupRole"
        assert env.state_of(a)["state"] == "PLANNED"
    # accounts overlap in time (parallel)
    firsts = [calls_for(calls, a)[0] for a in (A1, A2, A3)]
    assert max(c["start"] for c in firsts) < min(c["end"] for c in firsts)


def test_max_parallel_one_serialises_accounts(env):
    rc, calls = env.run("plan", "--max-parallel", "1", sleep=0.05)
    assert rc == 0
    spans = []
    for a in (A1, A2, A3):
        mine = calls_for(calls, a)
        spans.append((mine[0]["start"], mine[-1]["end"]))
    spans.sort()
    for (s1, e1), (s2, e2) in zip(spans, spans[1:]):
        assert e1 <= s2


def test_repo_root_from_env_var(env, monkeypatch):
    monkeypatch.setenv(orch.REPO_ROOT_ENV, str(env.repo))
    rc, calls = env.run("plan", "--accounts", A1, use_env_var=True)
    assert rc == 0 and len(calls) == 3


def test_clean_exit_codes_map_to_states_and_stop_on_error(env):
    assert env.run("plan")[0] == 0
    codes = {f"{A2}:nuke": 1, f"{A3}:cfn_predelete": 2}
    rc, calls = env.run("clean", codes=codes)
    assert rc == 1
    assert env.state_of(A1)["state"] == "CLEANED"
    assert env.state_of(A2)["state"] == "PARTIAL"
    assert env.state_of(A3)["state"] == "FAILED"
    # exit 1 -> continue; exit 2 -> stop the account's pipeline
    assert [c["step"] for c in calls_for(calls, A2)] == ["cfn_predelete", "nuke", "preclose"]
    assert [c["step"] for c in calls_for(calls, A3)] == ["cfn_predelete"]
    for c in calls:
        if c["step"] in ("cfn_predelete", "nuke"):
            assert "--apply" in c["argv"]
        else:
            assert "--apply" not in c["argv"]  # preclose report-only without --fix-preclose
    nuke = next(c for c in calls_for(calls, A1) if c["step"] == "nuke")
    assert opt(nuke["argv"], "--account-id") == A1
    st = env.state_of(A2)
    assert [h["to"] for h in st["history"]][-2:] == ["CLEANING", "PARTIAL"]
    assert st["runs"]["clean"]["exit_codes"] == {"cfn_predelete": 0, "nuke": 1, "preclose": 0}
    assert st["steps"]["nuke"]["duration_s"] >= 0 and st["steps"]["nuke"]["started_at"]
    adir = env.state / "2026-10" / A2
    assert "stdout from" in (adir / "nuke.log").read_text(encoding="utf-8")
    assert "stderr from" in (adir / "nuke.log").read_text(encoding="utf-8")
    assert json.loads((adir / "nuke.json").read_text())["status"] == "stub-1"
    assert hts.EXTERNAL_ID not in json.dumps(st)  # secret redacted from recorded argv
    runs = list((adir / "runs").iterdir())
    assert any(r.name.endswith("-plan") for r in runs) and any(r.name.endswith("-clean") for r in runs)


def test_fix_preclose_passes_apply(env):
    env.run("plan", "--accounts", A1)
    rc, calls = env.run("clean", "--accounts", A1, "--fix-preclose", "--steps", "preclose")
    assert rc == 0
    assert [c["step"] for c in calls] == ["preclose"]
    assert "--apply" in calls[0]["argv"]


def test_steps_rerun_keeps_previous_clean_results(env):
    env.run("plan", "--accounts", A1)
    env.run("clean", "--accounts", A1, codes={f"{A1}:preclose": 1})
    assert env.state_of(A1)["state"] == "PARTIAL"
    rc, _ = env.run("clean", "--accounts", A1, "--steps", "preclose", "--fix-preclose")
    assert rc == 0
    st = env.state_of(A1)
    assert st["state"] == "CLEANED"
    assert st["runs"]["clean"]["exit_codes"] == {"cfn_predelete": 0, "nuke": 0, "preclose": 0}


def test_clean_requires_plan(env):
    rc, calls = env.run("clean", "--accounts", A1)
    assert rc == 1 and calls == []
    summary = json.loads((env.state / "2026-10" / "summary.json").read_text(encoding="utf-8"))
    assert summary["accounts"][0]["result"] == "SKIPPED_NOT_PLANNED"
    rc, calls = env.run("clean", "--accounts", A1, "--allow-unplanned")
    assert rc == 0 and len(calls) == 3


def test_verify_exit_codes(env):
    codes = {f"{A1}:verify": 0, f"{A2}:verify": 1, f"{A3}:verify": 3}
    rc, calls = env.run("verify", "--cleanup-date", "2026-10-12", codes=codes)
    assert rc == 1
    assert [env.state_of(a)["state"] for a in (A1, A2, A3)] == ["VERIFIED_CLEAN", "RESIDUAL", "PENDING"]
    for c in calls:
        assert c["step"] == "verify"
        assert opt(c["argv"], "--cleanup-date") == "2026-10-12"
        assert "--apply" not in c["argv"]


def test_verify_defaults_cleanup_date_to_last_clean(env):
    env.run("plan", "--accounts", A1)
    env.run("clean", "--accounts", A1)
    rc, calls = env.run("verify", "--accounts", A1)
    assert rc == 0
    assert opt(calls[0]["argv"], "--cleanup-date") == env.state_of(A1)["runs"]["clean"]["finished_at"][:10]
    rc, calls = env.run("verify", "--accounts", A2)  # never cleaned, no date -> not run
    assert rc == 1 and calls == []
    assert env.state_of(A2)["state"] == "VERIFY_FAILED"


def test_preflight_alias_mismatch_skips_account(env, tmp_path):
    reg = hts.make_registry(tmp_path / "r2.json", [(A1, ALIASES[A1]), (A2, "edu-2026-10-99")])
    hts.write_stub_config(env.repo)
    hts.clear_calls(env.repo)
    rc = orch.main(["plan", "--registry", str(reg), "--state-dir", str(env.state), "--repo-root", str(env.repo)])
    calls = hts.read_calls(env.repo)
    assert rc == 1
    assert calls_for(calls, A2) == []
    assert len(calls_for(calls, A1)) == 3
    st = env.state_of(A2)
    assert st["state"] == "FAILED_PREFLIGHT"
    assert "edu-2026-10-99" in st["preflight"]["error"]
    assert st["preflight"]["alias_actual"] == ALIASES[A2]


def test_verify_tolerates_deleted_alias_but_clean_does_not(env, tmp_path):
    reg = hts.make_registry(tmp_path / "r4.json", [(A1, ALIASES[A1])])
    # simulate preclose having deleted the alias
    import boto3
    creds = boto3.client("sts").assume_role(RoleArn=f"arn:aws:iam::{A1}:role/EduCleanupRole",
                                            RoleSessionName="tt", ExternalId=hts.EXTERNAL_ID)["Credentials"]
    boto3.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                  aws_session_token=creds["SessionToken"]).client("iam").delete_account_alias(
        AccountAlias=ALIASES[A1])
    base = ["--registry", str(reg), "--state-dir", str(env.state), "--repo-root", str(env.repo)]
    hts.write_stub_config(env.repo)
    hts.clear_calls(env.repo)
    assert orch.main(["verify", "--cleanup-date", "2026-10-12"] + base) == 0
    st = env.state_of(A1)
    assert st["state"] == "VERIFIED_CLEAN" and "gone" in st["preflight"]["warning"]
    assert orch.main(["clean", "--allow-unplanned"] + base) == 1
    assert env.state_of(A1)["state"] == "FAILED_PREFLIGHT"


def test_preflight_account_id_mismatch_skips_account(env, monkeypatch):
    real = orch.get_session

    def wrong_account(*a, **kw):  # the role ARN for A1 resolves to A3 (e.g. a typo in the registry)
        if kw.get("role_arn", "").startswith(f"arn:aws:iam::{A1}:"):
            kw["role_arn"] = f"arn:aws:iam::{A3}:role/EduCleanupRole"
        return real(*a, **kw)

    monkeypatch.setattr(orch, "get_session", wrong_account)
    rc, calls = env.run("plan", "--accounts", f"{A1},{A2}")
    assert rc == 1
    assert calls_for(calls, A1) == [] and len(calls_for(calls, A2)) == 3
    st = env.state_of(A1)
    assert st["state"] == "FAILED_PREFLIGHT" and A3 in st["preflight"]["error"]


def test_preflight_refuses_hub_account(env, tmp_path):
    reg = hts.make_registry(tmp_path / "r3.json", [(hts.HUB_ACCOUNT, "")])
    hts.clear_calls(env.repo)
    rc = orch.main(["plan", "--registry", str(reg), "--state-dir", str(env.state), "--repo-root", str(env.repo)])
    assert rc == 1 and hts.read_calls(env.repo) == []
    assert "hub account" in env.state_of(hts.HUB_ACCOUNT)["preflight"]["error"]


def test_existing_lock_prevents_run(env):
    lock = env.state / "2026-10" / A1 / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                "started_at": orch.now_iso(), "command": "clean", "token": "x"}))
    rc, calls = env.run("plan")
    assert rc == 1
    assert calls_for(calls, A1) == []
    assert len(calls) == 6
    summary = json.loads((env.state / "2026-10" / "summary.json").read_text(encoding="utf-8"))
    row = next(a for a in summary["accounts"] if a["account_id"] == A1)
    assert row["result"] == "SKIPPED_LOCKED"
    assert lock.exists()  # someone else's lock is never removed


def test_stale_lock_is_replaced(env):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = env.state / "2026-10" / A1 / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname(),
                                "started_at": orch.now_iso(), "command": "clean", "token": "x"}))
    rc, calls = env.run("plan", "--accounts", A1)
    assert rc == 0 and len(calls) == 3
    assert not lock.exists()  # released after the run
    assert list(lock.parent.glob(".lock.stale-*"))


def test_old_lock_from_other_host_is_stale(env):
    lock = env.state / "2026-10" / A1 / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 1, "host": "other-host", "started_at": "2020-01-01T00:00:00+00:00",
                                "command": "clean", "token": "x"}))
    rc, calls = env.run("plan", "--accounts", A1)
    assert rc == 0 and len(calls) == 3


def test_concurrent_runs_do_not_double_run(env):
    hts.write_stub_config(env.repo, sleep=0.6)
    hts.clear_calls(env.repo)
    argv = ["plan", "--registry", str(env.reg), "--state-dir", str(env.state), "--repo-root", str(env.repo),
            "--accounts", A1]
    rcs = []
    threads = [threading.Thread(target=lambda: rcs.append(orch.main(list(argv)))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    calls = hts.read_calls(env.repo)
    assert sorted(rcs) == [0, 1]
    assert [c["step"] for c in calls] == ["cfn_predelete", "nuke", "preclose"]  # ran exactly once


def test_step_timeout_and_missing_json(env):
    env.run("plan", "--accounts", A1)
    rc, calls = env.run("clean", "--accounts", A1, "--step-timeout", "0.01",
                        sleeps={f"{A1}:cfn_predelete": 5}, no_json=[f"{A1}:cfn_predelete"])
    assert rc == 1
    st = env.state_of(A1)
    assert st["state"] == "FAILED"
    rec = st["steps"]["cfn_predelete"]
    assert rec["exit_code"] is None and "timeout" in rec["error"] and rec["json_missing"]
    assert "nuke" not in st["runs"]["clean"]["exit_codes"]


def test_summary_files_and_status(env, capsys):
    env.run("plan")
    rc, _ = env.run("clean", codes={f"{A2}:nuke": 1})
    out = capsys.readouterr().out
    assert rc == 1
    assert "ACCOUNT" in out and "CLEANED" in out and "PARTIAL" in out and ALIASES[A2] in out
    cdir = env.state / "2026-10"
    summary = json.loads((cdir / "summary.json").read_text(encoding="utf-8"))
    assert summary["module"] == "hub" and summary["command"] == "clean"
    assert summary["state_totals"] == {"CLEANED": 2, "PARTIAL": 1}
    by = {a["account_id"]: a for a in summary["accounts"]}
    assert by[A2]["steps"]["nuke"]["exit_code"] == 1 and by[A2]["steps"]["nuke"]["mode"] == "apply"
    raw = (cdir / "summary.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    assert [r["account_id"] for r in rows] == [A1, A2, A3]
    assert rows[1]["state"] == "PARTIAL" and rows[1]["nuke_exit"] == "1"

    # status reads the state files without running anything
    hts.clear_calls(env.repo)
    rc = orch.main(["status", "--registry", str(env.reg), "--state-dir", str(env.state)])
    out = capsys.readouterr().out
    assert rc == 1  # PARTIAL is not OK
    assert hts.read_calls(env.repo) == []
    assert "PARTIAL" in out and "1" in out


def test_status_ok_and_registry_error(env, tmp_path, capsys):
    assert orch.main(["status", "--registry", str(env.reg), "--state-dir", str(tmp_path / "empty")]) == 0
    assert "NEW" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"cohort": "x", "external_id": "short", "regions": [], "accounts": []}))
    assert orch.main(["plan", "--registry", str(bad), "--state-dir", str(tmp_path)]) == 2
    assert orch.main(["plan", "--registry", str(env.reg), "--state-dir", str(tmp_path),
                      "--repo-root", str(tmp_path / "nowhere")]) == 2
    assert orch.main(["plan", "--registry", str(env.reg), "--accounts", "999999999999"]) == 2
    assert orch.main(["verify", "--registry", str(env.reg), "--cleanup-date", "12-10-2026",
                      "--repo-root", str(env.repo)]) == 2


def test_plan_after_clean_keeps_state(env):
    env.run("plan", "--accounts", A1)
    env.run("clean", "--accounts", A1)
    env.run("plan", "--accounts", A1)
    st = env.state_of(A1)
    assert st["state"] == "CLEANED"
    assert st["runs"]["plan"]["result"] == "PLANNED"


# ---------------------------------------------------------------------------------------------
# killswitch deployment (botocore Stubber on real CloudFormation clients)
# ---------------------------------------------------------------------------------------------
def _missing(stubber, stack):
    stubber.add_client_error("describe_stacks", service_error_code="ValidationError",
                             service_message=f"Stack with id {stack} does not exist", http_status_code=400,
                             expected_params={"StackName": stack})


def _status(stubber, stack, status):
    stubber.add_response("describe_stacks", {"Stacks": [{
        "StackName": stack, "StackId": f"arn:aws:cloudformation:us-east-1:111111111111:stack/{stack}/1",
        "CreationTime": "2026-10-14T00:00:00Z", "StackStatus": status}]}, {"StackName": stack})


def test_killswitch_create_update_nochange_and_failure(env):
    import boto3
    from botocore.stub import Stubber

    template = (env.repo / "killswitch" / "killswitch.yaml").read_text(encoding="utf-8")
    expected_params = [
        {"ParameterKey": "TokenThreshold", "ParameterValue": "2000000"},
        {"ParameterKey": "PeriodMinutes", "ParameterValue": "5"},
        {"ParameterKey": "TargetMode", "ParameterValue": "ALL_IAM_USERS"},
        {"ParameterKey": "TargetGroupName", "ParameterValue": ""},
        {"ParameterKey": "ExemptUserNames", "ParameterValue": ""},
        {"ParameterKey": "NotificationEmail", "ParameterValue": ""},
        {"ParameterKey": "EnableApiTriggers", "ParameterValue": "true"},
    ]

    def call_params(stack):
        return {"StackName": stack, "TemplateBody": template, "Parameters": expected_params,
                "Capabilities": ["CAPABILITY_IAM"],
                "Tags": [{"Key": "managed-by", "Value": "edu-cleanup"}, {"Key": "edu-cohort", "Value": "2026-10"}]}

    stubs = {}
    regions = ("ap-northeast-2", "us-east-1")
    for acct in (A1, A2, A3):
        for region in regions:
            c = boto3.client("cloudformation", region_name=region)
            stubs[(acct, region)] = (c, Stubber(c))
    stack = {r: f"edu-killswitch-{r}" for r in regions}

    # A1: both regions new -> create
    for r in regions:
        s = stubs[(A1, r)][1]
        _missing(s, stack[r])
        s.add_response("create_stack", {"StackId": "id"}, call_params(stack[r]))
        _status(s, stack[r], "CREATE_COMPLETE")
    # A2: seoul exists unchanged -> NO_CHANGE ; virginia exists -> update
    s = stubs[(A2, "ap-northeast-2")][1]
    _status(s, stack["ap-northeast-2"], "CREATE_COMPLETE")
    s.add_client_error("update_stack", service_error_code="ValidationError",
                       service_message="No updates are to be performed.", http_status_code=400,
                       expected_params=call_params(stack["ap-northeast-2"]))
    s = stubs[(A2, "us-east-1")][1]
    _status(s, stack["us-east-1"], "UPDATE_COMPLETE")
    s.add_response("update_stack", {"StackId": "id"}, call_params(stack["us-east-1"]))
    _status(s, stack["us-east-1"], "UPDATE_COMPLETE")
    # A3: seoul create rolls back -> FAILED ; virginia stuck in progress -> BUSY
    s = stubs[(A3, "ap-northeast-2")][1]
    _missing(s, stack["ap-northeast-2"])
    s.add_response("create_stack", {"StackId": "id"}, call_params(stack["ap-northeast-2"]))
    _status(s, stack["ap-northeast-2"], "ROLLBACK_COMPLETE")
    s = stubs[(A3, "us-east-1")][1]
    _status(s, stack["us-east-1"], "UPDATE_IN_PROGRESS")

    for _, st in stubs.values():
        st.activate()
    seen = []

    def factory(session, region, account_id):
        seen.append((account_id, region))
        return stubs[(account_id, region)][0]

    rc, calls = env.run("killswitch", "--yes", cfn_client_factory=factory)
    assert rc == 1
    assert calls == []  # killswitch never runs step scripts
    assert sorted(seen) == sorted(stubs)
    for _, st in stubs.values():
        st.assert_no_pending_responses()

    def ks(acct):
        return env.state_of(acct)["runs"]["killswitch"]

    assert ks(A1)["result"] == "KILLSWITCH_DEPLOYED"
    assert {r: v["action"] for r, v in ks(A1)["regions"].items()} == {r: "CREATED" for r in regions}
    assert ks(A2)["result"] == "KILLSWITCH_DEPLOYED"
    assert {r: v["action"] for r, v in ks(A2)["regions"].items()} == {"ap-northeast-2": "NO_CHANGE",
                                                                        "us-east-1": "UPDATED"}
    assert ks(A3)["result"] == "KILLSWITCH_FAILED"
    assert {r: v["action"] for r, v in ks(A3)["regions"].items()} == {"ap-northeast-2": "FAILED",
                                                                        "us-east-1": "BUSY"}
    # killswitch does not move the cleanup state machine
    assert env.state_of(A1)["state"] == "NEW"
    summary = json.loads((env.state / "2026-10" / "summary.json").read_text(encoding="utf-8"))
    assert {a["account_id"]: a["result"] for a in summary["accounts"]} == {
        A1: "KILLSWITCH_DEPLOYED", A2: "KILLSWITCH_DEPLOYED", A3: "KILLSWITCH_FAILED"}


def test_killswitch_skips_preflight_failures_and_filters_unknown_params(env, tmp_path):
    import boto3
    from botocore.stub import Stubber

    # template without EnableApiTriggers -> that parameter must not be sent
    tpl = env.repo / "killswitch" / "killswitch.yaml"
    tpl.write_text(tpl.read_text(encoding="utf-8").replace(
        '  EnableApiTriggers:\n    Type: String\n    Default: "true"\n', ""), encoding="utf-8")
    reg = hts.make_registry(tmp_path / "r.json", [(A1, ALIASES[A1]), (A2, "edu-2026-10-77")],
                            regions=("ap-northeast-2",), killswitch={"TokenThreshold": 10})
    c = boto3.client("cloudformation", region_name="ap-northeast-2")
    s = Stubber(c)
    _missing(s, "edu-killswitch-ap-northeast-2")
    s.add_response("create_stack", {"StackId": "id"}, {
        "StackName": "edu-killswitch-ap-northeast-2", "TemplateBody": tpl.read_text(encoding="utf-8"),
        "Parameters": [{"ParameterKey": "TokenThreshold", "ParameterValue": "10"}],
        "Capabilities": ["CAPABILITY_IAM"],
        "Tags": [{"Key": "managed-by", "Value": "edu-cleanup"}, {"Key": "edu-cohort", "Value": "2026-10"}]})
    _status(s, "edu-killswitch-ap-northeast-2", "CREATE_COMPLETE")
    s.activate()
    seen = []

    def factory(session, region, account_id):
        seen.append(account_id)
        return c

    rc = orch.main(["killswitch", "--registry", str(reg), "--state-dir", str(env.state),
                    "--repo-root", str(env.repo), "--yes"], cfn_client_factory=factory)
    assert rc == 1
    assert seen == [A1]
    s.assert_no_pending_responses()
    assert env.state_of(A2)["runs"]["killswitch"]["result"] == "FAILED_PREFLIGHT"


def test_relative_state_dir_is_resolved_for_step_scripts(env, monkeypatch, tmp_path):
    """Steps run with cwd=repo_root, so the default relative --state-dir must become absolute."""
    monkeypatch.chdir(tmp_path)
    rc, calls = env.run("plan", "--state-dir", "state-rel")  # argparse: last --state-dir wins
    assert len(calls) == 9          # every step actually ran (a broken path crashes stubs silently)
    assert rc == 0
    for a in (A1, A2, A3):
        for step in ("cfn_predelete", "nuke", "preclose"):
            assert (tmp_path / "state-rel" / "2026-10" / a / f"{step}.json").is_file()
    for c in calls:
        out = opt(c["argv"], "--json-out")
        assert os.path.isabs(out), out
        assert out.startswith(str(tmp_path / "state-rel"))


def test_crashed_step_without_json_is_an_error_not_issues(env):
    """A step that exits 1 without writing --json-out crashed (uncaught exception), it did not
    'find issues': plan must become PLAN_FAILED and clean must stop the pipeline as FAILED."""
    rc, _ = env.run("plan", "--accounts", A1, codes={f"{A1}:preclose": 1}, no_json=[f"{A1}:preclose"])
    assert rc == 1
    st = env.state_of(A1)
    assert st["state"] == "PLAN_FAILED"
    rec = st["steps"]["preclose"]
    assert rec["exit_code"] is None and rec["raw_exit_code"] == 1 and rec["json_missing"]
    assert "no result JSON" in rec["error"]

    env.run("plan", "--accounts", A1)                                  # healthy plan again
    rc, calls = env.run("clean", "--accounts", A1, codes={f"{A1}:cfn_predelete": 1},
                        no_json=[f"{A1}:cfn_predelete"])
    assert rc == 1
    st = env.state_of(A1)
    assert st["state"] == "FAILED"
    assert [c["step"] for c in calls] == ["cfn_predelete"]             # pipeline stopped


def test_exit_1_with_json_is_still_issues(env):
    """The normal 'issues found' path is unchanged: exit 1 + JSON written."""
    env.run("plan", "--accounts", A1)
    rc, calls = env.run("clean", "--accounts", A1, codes={f"{A1}:nuke": 1})
    assert env.state_of(A1)["state"] == "PARTIAL"
    assert [c["step"] for c in calls] == ["cfn_predelete", "nuke", "preclose"]
