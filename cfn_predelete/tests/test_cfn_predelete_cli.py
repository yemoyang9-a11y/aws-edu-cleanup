"""CLI: flags, stdout table, --json-out, exit codes (no real AWS)."""
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cfn_predelete_fakes import ACCOUNT, READ_ONLY_CFN, FakeCfn, FakeClock, ps  # noqa: E402

R1, R2 = "ap-northeast-2", "us-east-1"


@pytest.fixture
def env(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        fakes = {R1: FakeCfn(R1), R2: FakeCfn(R2)}
        real_client = ps.client

        def fake_client(session, service, region=None):
            if service == "cloudformation":
                return fakes[region].client(session)
            return real_client(session, service, region)
        monkeypatch.setattr(ps, "client", fake_client)
        monkeypatch.setattr(ps.Runner, "__init__", _fake_time_init(ps.Runner.__init__))
        yield fakes


def _fake_time_init(orig):
    """Runner built by main() gets a fake clock: --timeout-min is honoured without real waiting."""
    def init(self, *a, **kw):
        orig(self, *a, **kw)
        clock = FakeClock()
        self.clock, self.sleep = clock, clock.sleep
    return init


def test_cli_defaults_and_flags():
    a = ps.build_parser().parse_args([])
    assert (a.timeout_min, a.max_parallel, a.cfn_role_arn, a.apply, a.regions) == (30.0, 8, None, False,
                                                                                    "ap-northeast-2,us-east-1")
    a = ps.build_parser().parse_args(["--timeout-min", "5", "--max-parallel", "3", "--cfn-role-arn", "arn:x",
                                      "--apply", "--role-arn", "arn:r", "--external-id", "e", "--json-out", "o"])
    assert (a.timeout_min, a.max_parallel, a.cfn_role_arn, a.apply) == (5.0, 3, "arn:x", True)


def test_dry_run_cli_writes_json_and_exits_0(env, tmp_path, capsys):
    env[R1].add("edu-bootstrap")
    env[R1].add("app")
    env[R2].add("CDKToolkit")
    out = tmp_path / "r.json"
    code = ps.main(["--json-out", str(out)])
    assert code == 0
    data = json.loads(out.read_text())
    assert data["module"] == "cfn_predelete" and data["account_id"] == ACCOUNT and data["applied"] is False
    assert data["status"] == "OK" and data["generated_at"]
    assert {r["final_status"] for r in data["results"]} == {"PLANNED", "SKIPPED_PROTECTED"}
    assert data["protected_identities"] == ["EduCleanupRole", "moto"]   # moto caller = IAM user "moto"
    stdout = capsys.readouterr().out
    assert "DRY-RUN" in stdout and "edu-bootstrap" in stdout and "CDKToolkit" in stdout
    for f in env.values():
        assert set(f.ops()) <= READ_ONLY_CFN


def test_apply_cli_exit_codes(env, tmp_path):
    env[R1].add("app")
    env[R2].add("stuck", never_finishes=True)
    out = tmp_path / "r.json"
    code = ps.main(["--apply", "--timeout-min", "2", "--json-out", str(out)])
    data = json.loads(out.read_text())
    assert code == 1 and data["status"] == "ISSUES" and data["applied"] is True
    res = {r["stack_name"]: r["final_status"] for r in data["results"]}
    assert res == {"app": "GONE", "stuck": "TIMEOUT"}


def test_apply_cli_all_gone_exit_0(env, tmp_path):
    env[R1].add("a")
    env[R2].add("b", fail_delete=True)
    assert ps.main(["--apply", "--json-out", str(tmp_path / "r.json")]) == 0


def test_global_region_is_ignored_and_bad_args_exit_2(env):
    assert ps.main(["--regions", "global"]) == 2
    assert ps.main(["--max-parallel", "0"]) == 2
    assert ps.main(["--regions", f"global,{R1}"]) == 0


def test_auth_failure_exit_2_with_json(env, tmp_path, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("no credentials")
    monkeypatch.setattr(ps, "get_session", broken)
    out = tmp_path / "r.json"
    assert ps.main(["--json-out", str(out)]) == 2
    data = json.loads(out.read_text())
    assert data["status"] == "ERROR" and data["module"] == "cfn_predelete" and "no credentials" in data["error"]


def test_interrupt_stops_run_and_cleans_temp_role(env, monkeypatch, tmp_path):
    """SIGTERM from the hub (mapped to KeyboardInterrupt) stops polling; temp role still deleted."""
    import threading
    env[R1].add("cdk-app", role=f"arn:aws:iam::{ACCOUNT}:role/gone-exec-role", never_finishes=True)
    env[R1].role_ok = lambda arn: True
    real_run = ps.Runner.run

    def interrupted_run(self):
        result = {}
        t = threading.Thread(target=lambda: result.update(r=real_run(self)))
        t.start()
        while not self.resolver.temp_created and t.is_alive():
            t.join(0.01)
        self.stop.set()                       # what the KeyboardInterrupt handler in run() does
        t.join(10)
        return result["r"]
    monkeypatch.setattr(ps.Runner, "run", interrupted_run)
    monkeypatch.setenv("MOTO_IAM_LOAD_MANAGED_POLICIES", "true")
    out = tmp_path / "r.json"
    code = ps.main(["--apply", "--regions", R1, "--json-out", str(out)])
    data = json.loads(out.read_text())
    assert code == 2 and data["status"] == "ERROR"           # interrupted run = not completed
    assert {"region": "*", "error": "interrupted by user"} in data["errors"]
    r = data["results"][0]
    assert r["final_status"] == "TIMEOUT" and r["error"].startswith("interrupted")
    assert data["role_fallback"]["temp_role_created"] and data["role_fallback"]["temp_role_deleted"]
    iam = boto3.client("iam", region_name="us-east-1")
    assert "edu-cfn-delete-temp" not in {x["RoleName"] for x in iam.list_roles()["Roles"]}


@pytest.mark.skipif(not hasattr(__import__("signal"), "SIGTERM"), reason="no SIGTERM")
def test_sigterm_handler_raises_keyboardinterrupt():
    import os
    import signal
    old = signal.getsignal(signal.SIGTERM)
    try:
        ps._sigterm_as_interrupt()
        with pytest.raises(KeyboardInterrupt):
            os.kill(os.getpid(), signal.SIGTERM)
            import time
            time.sleep(1)
    finally:
        signal.signal(signal.SIGTERM, old)
