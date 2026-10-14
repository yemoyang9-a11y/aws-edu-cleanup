#!/usr/bin/env python3
"""edu-cleanup hub orchestrator: run the cleanup pipeline across many standalone student accounts.

    orchestrate.py plan       --registry registry.json   # dry-run: cfn_predelete, nuke, preclose (no --apply)
    orchestrate.py clean      --registry registry.json   # same steps with --apply (preclose: report-only
                                                         #   unless --fix-preclose)
    orchestrate.py verify     --registry registry.json   # D+2 cost verification (verify/verify_cost.py)
    orchestrate.py status     --registry registry.json   # print the state table from <state-dir>
    orchestrate.py killswitch --registry registry.json   # deploy killswitch/killswitch.yaml to every account x region

Common options: [--accounts id1,id2] [--max-parallel N] [--profile P] [--state-dir state]
                [--cleanup-date YYYY-MM-DD] [--steps nuke,preclose] [--fix-preclose] [--yes]

How it works
  * The registry is the allowlist. Nothing outside it is ever touched.
  * Per account: lock (<state-dir>/<cohort>/<id>/.lock) -> preflight guard (AssumeRole EduCleanupRole with
    ExternalId, GetCallerIdentity must equal the registry id, account alias must equal the registry alias,
    target must not be the hub account) -> steps run sequentially as subprocesses.
  * Accounts run in parallel (ThreadPoolExecutor, max_parallel).
  * State per account in <state-dir>/<cohort>/<id>/state.json:
        PLANNED -> CLEANING -> CLEANED | PARTIAL | FAILED -> VERIFIED_CLEAN | RESIDUAL | PENDING
    plus FAILED_PREFLIGHT, PLAN_FAILED, VERIFY_FAILED.
  * Each step's stdout+stderr goes to <step>.log, its JSON result to <step>.json; copies of both are kept in
    <id>/runs/<run-id>/ so a later run never erases the plan you reviewed.
  * summary.json / summary.csv (utf-8-sig for Excel) in <state-dir>/<cohort>/.

Exit codes: 0 every selected account OK, 1 otherwise, 2 registry/config error.
Runtime dependencies: boto3/botocore + common/ only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import csv  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Tuple  # noqa: E402

from botocore.config import Config  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, WaiterError  # noqa: E402

from common.session import (  # noqa: E402
    EXIT_ERROR, EXIT_ISSUES, EXIT_OK, EXIT_PENDING, KILLSWITCH_STACK_PREFIX, ROLE_NAME,
    client, get_session, log, now_iso,
)
from hub_registry import KILLSWITCH_KEYS, RegistryError, contains_prod, load_registry, select_accounts  # noqa: E402

# ---------------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------------
COMMANDS = ("plan", "clean", "verify", "status", "killswitch")
REPO_ROOT_ENV = "EDU_CLEANUP_REPO_ROOT"
DEFAULT_STEP_TIMEOUT_MIN = 360
DEFAULT_LOCK_TTL_HOURS = 24
TEMPLATE_BODY_LIMIT = 51200  # CloudFormation TemplateBody limit (bytes)
WAITER_DELAY_S = 10           # killswitch stack waiter poll interval
WAITER_MAX_ATTEMPTS = 120     # x WAITER_DELAY_S = 20 min per stack

STEP_DEFS: Dict[str, Dict[str, str]] = {
    "cfn_predelete": {"kind": "python", "path": "cfn_predelete/predelete_stacks.py"},
    "nuke": {"kind": "bash", "path": "nuke/run_nuke.sh"},
    "preclose": {"kind": "python", "path": "preclose/preclose_check.py"},
    "verify": {"kind": "python", "path": "verify/verify_cost.py"},
}
COMMAND_STEPS: Dict[str, List[str]] = {
    "plan": ["cfn_predelete", "nuke", "preclose"],
    "clean": ["cfn_predelete", "nuke", "preclose"],
    "verify": ["verify"],
}
STEP_COLUMNS = ["cfn_predelete", "nuke", "preclose", "verify"]
STEP_SHORT = {"cfn_predelete": "CFN", "nuke": "NUKE", "preclose": "PRECLOSE", "verify": "VERIFY"}

# States (state.json "state")
NEW = "NEW"
PLANNED = "PLANNED"
PLAN_FAILED = "PLAN_FAILED"
CLEANING = "CLEANING"
CLEANED = "CLEANED"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
VERIFIED_CLEAN = "VERIFIED_CLEAN"
RESIDUAL = "RESIDUAL"
PENDING = "PENDING"
VERIFY_FAILED = "VERIFY_FAILED"
FAILED_PREFLIGHT = "FAILED_PREFLIGHT"
# Run results that do not change the state machine
SKIPPED_LOCKED = "SKIPPED_LOCKED"
SKIPPED_NOT_PLANNED = "SKIPPED_NOT_PLANNED"
INTERRUPTED = "INTERRUPTED"
KILLSWITCH_DEPLOYED = "KILLSWITCH_DEPLOYED"
KILLSWITCH_FAILED = "KILLSWITCH_FAILED"

# `plan` never moves an account backwards out of these states (it only records the dry-run).
POST_CLEAN_STATES = {CLEANING, CLEANED, PARTIAL, FAILED, VERIFIED_CLEAN, RESIDUAL, PENDING, VERIFY_FAILED}

OK_RESULTS = {
    "plan": {PLANNED},
    "clean": {CLEANED},
    "verify": {VERIFIED_CLEAN},
    "killswitch": {KILLSWITCH_DEPLOYED},
}
STATUS_BAD_STATES = {FAILED_PREFLIGHT, PLAN_FAILED, PARTIAL, FAILED, RESIDUAL, VERIFY_FAILED}

KS_OK_ACTIONS = {"CREATED", "UPDATED", "NO_CHANGE"}
_CFN_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


# ---------------------------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------------------------
def role_arn_for(account_id: str) -> str:
    return f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"


def is_error_exit(code: Optional[int]) -> bool:
    return code is None or code not in (EXIT_OK, EXIT_ISSUES)


def plan_state(exit_codes: List[Optional[int]]) -> str:
    """Dry-run: exit 1 just means 'there is something to delete' -> still PLANNED."""
    return PLAN_FAILED if any(is_error_exit(c) for c in exit_codes) else PLANNED


def clean_state(exit_codes: List[Optional[int]]) -> str:
    """0 everywhere -> CLEANED; any 1 -> PARTIAL; any 2/unknown/timeout -> FAILED."""
    if any(is_error_exit(c) for c in exit_codes):
        return FAILED
    if any(c == EXIT_ISSUES for c in exit_codes):
        return PARTIAL
    return CLEANED


def verify_state(code: Optional[int]) -> str:
    return {EXIT_OK: VERIFIED_CLEAN, EXIT_ISSUES: RESIDUAL, EXIT_PENDING: PENDING}.get(code, VERIFY_FAILED)


def killswitch_parameters(cfg: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
    """registry['killswitch'] -> CloudFormation Parameters (values as strings, booleans lower-case)."""
    cfg = dict(cfg or {})
    cfg.setdefault("EnableApiTriggers", "true")
    params = []
    for key in KILLSWITCH_KEYS:
        if key not in cfg:
            continue
        val = cfg[key]
        if isinstance(val, bool):
            sval = "true" if val else "false"
        elif val is None:
            sval = ""
        elif isinstance(val, (list, tuple)):
            sval = ",".join(str(v) for v in val)
        else:
            sval = str(val)
        params.append({"ParameterKey": key, "ParameterValue": sval})
    return params


def template_parameter_names(body: str) -> Optional[List[str]]:
    """Top-level parameter names of a YAML/JSON CloudFormation template (no PyYAML available).

    Returns None when the Parameters section cannot be found, so callers can fall back to sending all.
    """
    text = body.lstrip()
    if text.startswith("{"):
        try:
            return list((json.loads(text).get("Parameters") or {}).keys())
        except ValueError:
            return None
    names: List[str] = []
    in_params, indent = False, None
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if re.match(r"^Parameters\s*:\s*(#.*)?$", line):
            in_params, indent = True, None
            continue
        if in_params:
            if not line[:1].isspace():
                break
            cur = len(line) - len(line.lstrip())
            if indent is None:
                indent = cur
            if cur == indent:
                m = re.match(r"""^\s*["']?([A-Za-z0-9]+)["']?\s*:""", line)
                if m:
                    names.append(m.group(1))
    return names if in_params else None


def session_name_for(*parts: str) -> str:
    raw = "-".join(p for p in parts if p)
    return re.sub(r"[^\w+=,.@-]", "-", raw)[:64]


def redact(argv: List[str], secret: str) -> List[str]:
    return ["***" if (secret and a == secret) else a for a in argv]


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _parse_iso(value: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------------------------
# Per-account lock (O_EXCL file with pid/host/start; stale if the pid is dead on this host or too old)
# ---------------------------------------------------------------------------------------------
class AccountLock:
    def __init__(self, path: Path, command: str, ttl_hours: float = DEFAULT_LOCK_TTL_HOURS):
        self.path = path
        self.command = command
        self.ttl = dt.timedelta(hours=ttl_hours)
        self.token = uuid.uuid4().hex
        self.held = False

    def _info(self) -> Dict[str, Any]:
        return {"pid": os.getpid(), "host": socket.gethostname(), "started_at": now_iso(),
                "command": self.command, "token": self.token}

    def _stale_reason(self, info: Optional[Dict[str, Any]]) -> Optional[str]:
        if info is None:
            try:
                age = time.time() - self.path.stat().st_mtime
            except FileNotFoundError:
                return "vanished"
            return "unreadable lock older than 60s" if age > 60 else None
        try:
            pid = int(info.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if info.get("host") == socket.gethostname() and not _pid_alive(pid):
            return f"pid {info.get('pid')} is not running"
        started = _parse_iso(info.get("started_at", ""))
        if started and dt.datetime.now(dt.timezone.utc) - started > self.ttl:
            return f"older than {self.ttl}"
        return None

    def acquire(self) -> Tuple[bool, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                info = read_json(self.path)
                why = self._stale_reason(info)
                if why is None:
                    info = info or {}
                    return False, (f"locked by pid {info.get('pid')} on {info.get('host')} since "
                                   f"{info.get('started_at')} ({info.get('command')})")
                # Only remove the stale lock if it is still the same file content we judged.
                if read_json(self.path) == info:
                    stale = self.path.with_name(f".lock.stale-{int(time.time())}-{uuid.uuid4().hex[:6]}")
                    try:
                        os.replace(self.path, stale)
                        log(f"[{self.path.parent.name}] removed stale lock ({why}) -> {stale.name}")
                    except FileNotFoundError:
                        pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._info(), f)
            self.held = True
            return True, ""
        return False, "could not acquire lock (contention)"

    def release(self) -> None:
        if not self.held:
            return
        info = read_json(self.path)
        if info and info.get("token") == self.token:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.held = False


# ---------------------------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------------------------
class Orchestrator:
    def __init__(self, args: argparse.Namespace, registry: Dict[str, Any], repo_root: Path,
                 cfn_client_factory: Optional[Callable[..., Any]] = None):
        self.args = args
        self.command: str = args.command
        self.registry = registry
        self.cohort: str = registry["cohort"]
        self.external_id: str = registry["external_id"]
        self.regions: List[str] = registry["regions"]
        self.repo_root = repo_root
        # Absolute: step scripts run with cwd=repo_root, so a relative --state-dir (the default
        # "state") would make their --json-out paths point somewhere else.
        self.cohort_dir = Path(args.state_dir).expanduser().resolve() / self.cohort
        self.run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{self.command}"
        self.abort = threading.Event()
        self._procs: Dict[int, subprocess.Popen] = {}
        self._procs_lock = threading.Lock()
        self.hub_account: Optional[str] = None
        self.cfn_client_factory = cfn_client_factory or (lambda session, region, account_id:
                                                         session.client("cloudformation", region_name=region,
                                                                        config=_CFN_CONFIG))
        self.ks_template_body: Optional[str] = None
        self.ks_template_params: Optional[List[str]] = None
        self.ks_template_url: Optional[str] = getattr(args, "killswitch_template_url", None)
        self.waiter_delay = WAITER_DELAY_S
        self.waiter_max_attempts = WAITER_MAX_ATTEMPTS

    # ---- paths & state ----------------------------------------------------------------------
    def account_dir(self, account_id: str) -> Path:
        return self.cohort_dir / account_id

    def load_state(self, acct: Dict[str, str]) -> Dict[str, Any]:
        st = read_json(self.account_dir(acct["account_id"]) / "state.json")
        if st is None:
            st = {"account_id": acct["account_id"], "alias": acct["alias"], "cohort": self.cohort,
                  "state": NEW, "history": [], "runs": {}, "steps": {}, "created_at": now_iso()}
        st["alias"] = acct["alias"]
        st.setdefault("history", [])
        st.setdefault("runs", {})
        st.setdefault("steps", {})
        return st

    def save_state(self, st: Dict[str, Any]) -> None:
        st["updated_at"] = now_iso()
        atomic_write_json(self.account_dir(st["account_id"]) / "state.json", st)

    def set_state(self, st: Dict[str, Any], new_state: str, reason: str = "") -> None:
        prev = st.get("state")
        st["state"] = new_state
        st["history"].append({"at": now_iso(), "command": self.command, "run_id": self.run_id,
                              "from": prev, "to": new_state, "reason": reason})
        st["history"] = st["history"][-200:]
        self.save_state(st)

    # ---- preflight ----------------------------------------------------------------------------
    def preflight(self, acct: Dict[str, str]) -> Tuple[Dict[str, Any], Any]:
        """AssumeRole + identity/alias guard. Returns (record, session or None)."""
        aid, want_alias = acct["account_id"], acct["alias"]
        rec: Dict[str, Any] = {"at": now_iso(), "ok": False, "role_arn": role_arn_for(aid)}
        if self.hub_account and aid == self.hub_account:
            rec["error"] = f"target account {aid} is the hub account itself - refusing"
            return rec, None
        try:
            session = get_session(profile=self.args.profile, role_arn=role_arn_for(aid),
                                  external_id=self.external_id,
                                  session_name=session_name_for("edu-hub", self.cohort, self.command))
            ident = client(session, "sts", "us-east-1").get_caller_identity()
            rec["caller_account"] = ident.get("Account")
            rec["caller_arn"] = ident.get("Arn")
            if ident.get("Account") != aid:
                rec["error"] = f"GetCallerIdentity returned account {ident.get('Account')}, registry says {aid}"
                return rec, None
            aliases = client(session, "iam", "us-east-1").list_account_aliases().get("AccountAliases") or []
            actual = aliases[0] if aliases else ""
            rec["alias_actual"] = actual
            if contains_prod(actual):
                rec["error"] = f"account alias {actual!r} contains 'prod' - refusing"
                return rec, None
            if want_alias and actual != want_alias:
                if self.command == "verify" and not actual:
                    # preclose --apply may delete the alias before closure; verify is read-only and the
                    # account id already matched, so a *missing* alias is tolerated here (never a different one).
                    rec["warning"] = f"account alias is gone (registry says {want_alias!r}); continuing for verify"
                else:
                    rec["error"] = f"account alias is {actual or '(none)'!r}, registry says {want_alias!r}"
                    return rec, None
        except (ClientError, BotoCoreError) as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            return rec, None
        except Exception as e:  # never let one account crash the whole run
            rec["error"] = f"unexpected {type(e).__name__}: {e}"
            return rec, None
        rec["ok"] = True
        return rec, session

    # ---- subprocess steps -------------------------------------------------------------------
    def step_argv(self, step: str, acct: Dict[str, str], json_out: Path, cleanup_date: Optional[str]) -> List[str]:
        d = STEP_DEFS[step]
        script = str(self.repo_root / d["path"])
        argv = [sys.executable, script] if d["kind"] == "python" else ["bash", script]
        if step == "nuke":
            argv += ["--account-id", acct["account_id"]]
        if self.args.profile:
            argv += ["--profile", self.args.profile]
        argv += ["--role-arn", role_arn_for(acct["account_id"]),
                 "--external-id", self.external_id,
                 "--regions", ",".join(self.regions),
                 "--json-out", str(json_out)]
        if d["kind"] == "python":  # add_common_args guarantees --session-name for Python modules
            argv += ["--session-name", session_name_for("edu", self.cohort, step)]
        if step == "verify":
            argv += ["--cleanup-date", cleanup_date or ""]
        if self.command == "clean":
            if step in ("cfn_predelete", "nuke") or (step == "preclose" and self.args.fix_preclose):
                argv.append("--apply")
        return argv

    def _register(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            self._procs[proc.pid] = proc

    def _unregister(self, proc: subprocess.Popen) -> None:
        with self._procs_lock:
            self._procs.pop(proc.pid, None)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def kill_all(self) -> None:
        with self._procs_lock:
            procs = list(self._procs.values())
        for p in procs:
            self._kill(p)

    def run_step(self, step: str, acct: Dict[str, str], run_dir: Path, cleanup_date: Optional[str]) -> Dict[str, Any]:
        adir = self.account_dir(acct["account_id"])
        json_out = adir / f"{step}.json"
        log_path = adir / f"{step}.log"
        try:
            json_out.unlink()  # never read a previous run's result by mistake
        except FileNotFoundError:
            pass
        argv = self.step_argv(step, acct, json_out, cleanup_date)
        rec: Dict[str, Any] = {"step": step, "command": self.command, "run_id": self.run_id,
                               "mode": "apply" if "--apply" in argv else "dry-run",
                               "started_at": now_iso(), "argv": redact(argv, self.external_id),
                               "json_out": str(json_out), "log": str(log_path)}
        t0 = time.monotonic()
        timeout_s = max(1.0, float(self.args.step_timeout) * 60.0)
        code: Optional[int] = None
        env = dict(os.environ, PYTHONUNBUFFERED="1", EDU_CLEANUP_COHORT=self.cohort,
                   EDU_CLEANUP_RUN_ID=self.run_id)
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"# {rec['started_at']} run={self.run_id} account={acct['account_id']} step={step}\n")
            lf.write("# " + " ".join(rec["argv"]) + "\n")
            lf.flush()
            try:
                proc = subprocess.Popen(argv, cwd=str(self.repo_root), stdout=lf, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            except OSError as e:
                rec["error"] = f"could not start: {e}"
                lf.write(f"# could not start: {e}\n")
            else:
                self._register(proc)
                try:
                    deadline = time.monotonic() + timeout_s
                    while True:
                        try:
                            code = proc.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            if self.abort.is_set():
                                self._kill(proc)
                                rec["error"] = "interrupted"
                                code = None
                                break
                            if time.monotonic() > deadline:
                                self._kill(proc)
                                rec["error"] = f"timeout after {self.args.step_timeout} min"
                                code = None
                                break
                finally:
                    self._unregister(proc)
            lf.write(f"\n# exit={code} error={rec.get('error', '')} at {now_iso()}\n")
        rec["exit_code"] = code
        rec["finished_at"] = now_iso()
        rec["duration_s"] = round(time.monotonic() - t0, 1)
        result = read_json(json_out)
        if result is None:
            rec["json_missing"] = True
            # Every step writes --json-out on a normal finish (OK or ISSUES). A missing result means it
            # crashed (Python exits 1 on an uncaught exception, which would otherwise read as "issues
            # found"), so treat it as an error for state purposes; keep the real code for the log.
            if code is not None and code != EXIT_ERROR:
                rec["raw_exit_code"] = code
                rec["error"] = (rec.get("error", "") + " no result JSON written (step crashed?)").strip()
                code = None
            rec["exit_code"] = code
        else:
            rec["status"] = result.get("status")
        for p in (json_out, log_path):
            if p.exists():
                shutil.copy2(p, run_dir / p.name)
        log(f"[{acct['account_id']}] {step} ({rec['mode']}) exit={code} "
            f"{rec.get('error', '')} {rec['duration_s']}s")
        return rec

    # ---- killswitch -------------------------------------------------------------------------
    def _deploy_stack(self, cfn: Any, stack: str, params: List[Dict[str, str]]) -> Tuple[str, str]:
        src: Dict[str, Any] = ({"TemplateURL": self.ks_template_url} if self.ks_template_url
                               else {"TemplateBody": self.ks_template_body})
        common_kw: Dict[str, Any] = dict(StackName=stack, Parameters=params, Capabilities=["CAPABILITY_IAM"],
                                         Tags=[{"Key": "managed-by", "Value": "edu-cleanup"},
                                               {"Key": "edu-cohort", "Value": self.cohort}], **src)
        wcfg = {"Delay": self.waiter_delay, "MaxAttempts": self.waiter_max_attempts}
        status: Optional[str] = None
        try:
            status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except ClientError as e:
            if "does not exist" not in str(e):
                raise
        if status == "ROLLBACK_COMPLETE":  # a failed create can only be deleted, then recreated
            cfn.delete_stack(StackName=stack)
            cfn.get_waiter("stack_delete_complete").wait(StackName=stack, WaiterConfig=wcfg)
            status = None
        if status and status.endswith("_IN_PROGRESS"):
            return "BUSY", f"stack is {status}; try again later"
        if status is None or status == "DELETE_COMPLETE":
            cfn.create_stack(**common_kw)
            waiter, action = "stack_create_complete", "CREATED"
        else:
            try:
                cfn.update_stack(**common_kw)
            except ClientError as e:
                if "No updates are to be performed" in str(e):
                    return "NO_CHANGE", ""
                raise
            waiter, action = "stack_update_complete", "UPDATED"
        try:
            cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig=wcfg)
        except WaiterError as e:
            last = getattr(e, "last_response", None) or {}
            stacks = last.get("Stacks") or [{}]
            return "FAILED", f"{stacks[0].get('StackStatus', '')} {stacks[0].get('StackStatusReason', '') or e}"
        return action, ""

    def deploy_killswitch(self, acct: Dict[str, str], session: Any) -> Dict[str, Any]:
        params = killswitch_parameters(self.registry.get("killswitch"))
        if self.ks_template_params is not None:
            params = [p for p in params if p["ParameterKey"] in self.ks_template_params]
        out: Dict[str, Any] = {}
        for region in self.regions:
            if self.abort.is_set():
                out[region] = {"action": "INTERRUPTED"}
                continue
            stack = f"{KILLSWITCH_STACK_PREFIX}-{region}"
            t0 = time.monotonic()
            try:
                cfn = self.cfn_client_factory(session, region, acct["account_id"])
                action, detail = self._deploy_stack(cfn, stack, params)
            except (ClientError, BotoCoreError) as e:
                action, detail = "ERROR", f"{type(e).__name__}: {e}"
            except Exception as e:
                action, detail = "ERROR", f"unexpected {type(e).__name__}: {e}"
            out[region] = {"stack": stack, "action": action, "detail": detail,
                           "duration_s": round(time.monotonic() - t0, 1)}
            log(f"[{acct['account_id']}] killswitch {region}: {action} {detail}")
        return out

    # ---- per-account worker -----------------------------------------------------------------
    def _cleanup_date_for(self, st: Dict[str, Any]) -> Optional[str]:
        if self.args.cleanup_date:
            return self.args.cleanup_date
        fin = (st.get("runs", {}).get("clean") or {}).get("finished_at")
        return fin[:10] if fin else None

    def _was_planned(self, st: Dict[str, Any]) -> bool:
        runs = st.get("runs", {})
        return (runs.get("plan") or {}).get("result") == PLANNED or bool(runs.get("clean"))

    def run_account(self, acct: Dict[str, str]) -> Dict[str, Any]:
        aid = acct["account_id"]
        adir = self.account_dir(aid)
        adir.mkdir(parents=True, exist_ok=True)
        started, t0 = now_iso(), time.monotonic()
        res: Dict[str, Any] = {"account_id": aid, "alias": acct["alias"], "command": self.command,
                               "run_id": self.run_id, "started_at": started, "steps": {}, "reason": ""}

        def finish(result: str, reason: str = "") -> Dict[str, Any]:
            res["result"] = result
            res["reason"] = reason or res.get("reason", "")
            res["ok"] = result in OK_RESULTS.get(self.command, set())
            res["finished_at"] = now_iso()
            res["duration_s"] = round(time.monotonic() - t0, 1)
            return res

        if self.abort.is_set():
            return finish(INTERRUPTED, "run interrupted before start")
        lock = AccountLock(adir / ".lock", self.command, self.args.lock_ttl_hours)
        ok, why = lock.acquire()
        if not ok:
            log(f"[{aid}] skipped: {why}")
            return finish(SKIPPED_LOCKED, why)
        try:
            st = self.load_state(acct)
            if self.command == "clean" and not self.args.allow_unplanned and not self._was_planned(st):
                return finish(SKIPPED_NOT_PLANNED, "no successful 'plan' recorded; run plan first "
                                                   "(or pass --allow-unplanned)")
            pf, session = self.preflight(acct)
            st["preflight"] = pf
            if not pf["ok"]:
                log(f"[{aid}] FAILED_PREFLIGHT: {pf.get('error')}")
                if self.command == "killswitch":
                    st["runs"]["killswitch"] = {"run_id": self.run_id, "result": FAILED_PREFLIGHT,
                                                "started_at": started, "finished_at": now_iso(),
                                                "reason": pf.get("error", "")}
                    self.save_state(st)
                else:
                    self.set_state(st, FAILED_PREFLIGHT, pf.get("error", ""))
                return finish(FAILED_PREFLIGHT, pf.get("error", ""))

            if self.command == "killswitch":
                regions = self.deploy_killswitch(acct, session)
                good = all(r.get("action") in KS_OK_ACTIONS for r in regions.values())
                result = KILLSWITCH_DEPLOYED if good else KILLSWITCH_FAILED
                res["killswitch"] = regions
                st["runs"]["killswitch"] = {"run_id": self.run_id, "result": result, "started_at": started,
                                            "finished_at": now_iso(), "regions": regions}
                self.save_state(st)
                return finish(result, "" if good else "; ".join(
                    f"{r}: {v.get('action')} {v.get('detail', '')}".strip()
                    for r, v in regions.items() if v.get("action") not in KS_OK_ACTIONS))

            cleanup_date = None
            if self.command == "verify":
                cleanup_date = self._cleanup_date_for(st)
                if not cleanup_date:
                    reason = "no --cleanup-date given and no recorded 'clean' run for this account"
                    self.set_state(st, VERIFY_FAILED, reason)
                    return finish(VERIFY_FAILED, reason)
                res["cleanup_date"] = cleanup_date

            if self.command == "clean":
                self.set_state(st, CLEANING)

            run_dir = adir / "runs" / self.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            codes: List[Optional[int]] = []
            for step in self.selected_steps():
                if self.abort.is_set():
                    res["reason"] = "interrupted"
                    codes.append(None)
                    break
                rec = self.run_step(step, acct, run_dir, cleanup_date)
                codes.append(rec["exit_code"])
                res["steps"][step] = rec
                st["steps"][step] = rec
                self.save_state(st)
                if self.command == "clean" and is_error_exit(rec["exit_code"]):
                    res["reason"] = f"{step} failed (exit {rec['exit_code']} {rec.get('error', '')}".strip() \
                        + "); pipeline stopped for this account"
                    break

            exit_codes = {s: r.get("exit_code") for s, r in res["steps"].items()}
            if self.command == "plan":
                new_state = plan_state(codes)
            elif self.command == "clean":
                # --steps re-runs (e.g. only preclose --fix-preclose) keep the other steps' last clean result
                prev = (st["runs"].get("clean") or {}).get("exit_codes") or {}
                merged = {s: (exit_codes[s] if s in exit_codes else prev.get(s))
                          for s in COMMAND_STEPS["clean"] if s in exit_codes or s in prev}
                if None in codes:
                    merged.setdefault("_interrupted", None)
                exit_codes = {k: v for k, v in merged.items() if not k.startswith("_")}
                new_state = clean_state(list(merged.values()))
            else:
                new_state = verify_state(codes[0] if codes else None)
            if not res["reason"]:
                issues = [s for s, r in res["steps"].items() if r.get("exit_code") not in (EXIT_OK,)]
                if issues and new_state not in (PLANNED,):
                    res["reason"] = "see " + ", ".join(f"{s}.log" for s in issues)
            st["runs"][self.command] = {"run_id": self.run_id, "result": new_state, "started_at": started,
                                        "finished_at": now_iso(),
                                        "duration_s": round(time.monotonic() - t0, 1),
                                        "exit_codes": exit_codes,
                                        "cleanup_date": cleanup_date}
            if self.command == "plan" and st.get("state") in POST_CLEAN_STATES:
                self.set_state(st, st["state"], f"plan re-run ({new_state}); state kept")
            else:
                self.set_state(st, new_state, res["reason"])
            return finish(new_state)
        except Exception as e:  # pragma: no cover - defensive; recorded per account
            log(f"[{aid}] unexpected error: {type(e).__name__}: {e}")
            try:
                st = self.load_state(acct)
                fail = {"plan": PLAN_FAILED, "clean": FAILED, "verify": VERIFY_FAILED}.get(self.command)
                if fail:
                    self.set_state(st, fail, f"orchestrator error: {e}")
            except Exception:
                pass
            return finish(FAILED, f"orchestrator error: {type(e).__name__}: {e}")
        finally:
            lock.release()

    def selected_steps(self) -> List[str]:
        steps = COMMAND_STEPS[self.command]
        if not self.args.steps:
            return list(steps)
        wanted = [s.strip() for s in self.args.steps.split(",") if s.strip()]
        return [s for s in steps if s in wanted]

    # ---- run all ----------------------------------------------------------------------------
    def run(self, accounts: List[Dict[str, str]], max_parallel: int) -> List[Dict[str, Any]]:
        results: Dict[str, Dict[str, Any]] = {}
        workers = max(1, min(max_parallel, len(accounts)))
        log(f"{self.command}: {len(accounts)} account(s), cohort {self.cohort}, parallel {workers}, "
            f"regions {','.join(self.regions)}, run {self.run_id}")
        ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acct")
        futs = {ex.submit(self.run_account, a): a for a in accounts}
        try:
            pending = set(futs)
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for f in done:
                    a = futs[f]
                    results[a["account_id"]] = f.result()
        except KeyboardInterrupt:
            log("interrupted: stopping running steps (accounts not started are skipped)...")
            self.abort.set()
            for f in futs:
                f.cancel()
            self.kill_all()
        finally:
            ex.shutdown(wait=True)
        for f, a in futs.items():
            if a["account_id"] in results:
                continue
            if f.cancelled():
                results[a["account_id"]] = {"account_id": a["account_id"], "alias": a["alias"],
                                            "command": self.command, "result": INTERRUPTED, "ok": False,
                                            "steps": {}, "reason": "not started", "duration_s": 0}
            else:
                results[a["account_id"]] = f.result()
        return [results[a["account_id"]] for a in accounts]


# ---------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------
def _exit_cell(rec: Optional[Dict[str, Any]], show_mode: bool = False) -> str:
    if not rec:
        return "-"
    code = rec.get("exit_code")
    if code is None:
        cell = "INT" if rec.get("error") == "interrupted" else "ERR"
    else:
        cell = str(code)
    return cell + ("(dry)" if show_mode and rec.get("mode") == "dry-run" else "")


def _fmt_duration(sec: Any) -> str:
    try:
        s = int(float(sec))
    except (TypeError, ValueError):
        return "-"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def render_table(headers: List[str], rows: List[List[str]]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()  # noqa: E731
    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def run_table(command: str, results: List[Dict[str, Any]], regions: List[str]) -> str:
    if command == "killswitch":
        headers = ["ACCOUNT", "ALIAS", "RESULT"] + [r.upper() for r in regions] + ["DURATION", "REASON"]
        rows = []
        for r in results:
            ks = r.get("killswitch") or {}
            rows.append([r["account_id"], r.get("alias") or "-", r["result"]]
                        + [(ks.get(reg) or {}).get("action", "-") for reg in regions]
                        + [_fmt_duration(r.get("duration_s")), (r.get("reason") or "")[:80]])
        return render_table(headers, rows)
    steps = COMMAND_STEPS[command]
    headers = ["ACCOUNT", "ALIAS", "STATE"] + [STEP_SHORT[s] for s in steps] + ["DURATION", "REASON"]
    rows = []
    for r in results:
        rows.append([r["account_id"], r.get("alias") or "-", r["result"]]
                    + [_exit_cell(r["steps"].get(s)) for s in steps]
                    + [_fmt_duration(r.get("duration_s")), (r.get("reason") or "")[:80]])
    return render_table(headers, rows)


def status_rows(orch: Orchestrator, accounts: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    rows = []
    for a in accounts:
        st = read_json(orch.account_dir(a["account_id"]) / "state.json") or {}
        locked = (orch.account_dir(a["account_id"]) / ".lock").exists()
        state = st.get("state", NEW)
        note = ""
        if state == CLEANING and not locked:
            note = "interrupted? (CLEANING without a lock)"
        last_hist = (st.get("history") or [{}])[-1]
        last_cmd = last_hist.get("command", "")
        last_run = (st.get("runs") or {}).get(last_cmd) or {}
        rows.append({
            "account_id": a["account_id"], "alias": a["alias"], "state": state, "locked": locked,
            "steps": st.get("steps") or {},
            "killswitch": ((st.get("runs") or {}).get("killswitch") or {}).get("result", ""),
            "last_command": last_cmd, "updated_at": st.get("updated_at", ""),
            "duration_s": last_run.get("duration_s"),
            "reason": note or last_hist.get("reason", "") or (st.get("preflight") or {}).get("error", ""),
            "note": a.get("note", ""),
        })
    return rows


def status_table(rows: List[Dict[str, Any]]) -> str:
    headers = ["ACCOUNT", "ALIAS", "STATE"] + [STEP_SHORT[s] for s in STEP_COLUMNS] \
        + ["KILLSWITCH", "LOCK", "UPDATED", "REASON"]
    out = []
    for r in rows:
        out.append([r["account_id"], r["alias"] or "-", r["state"]]
                   + [_exit_cell(r["steps"].get(s), show_mode=True) for s in STEP_COLUMNS]
                   + [r["killswitch"] or "-", "yes" if r["locked"] else "", (r["updated_at"] or "-")[:19],
                      (r["reason"] or "")[:80]])
    return render_table(headers, out)


def write_summary(orch: Orchestrator, accounts: List[Dict[str, str]],
                  results: Optional[List[Dict[str, Any]]], started_at: str) -> Tuple[Path, Path]:
    by_id = {r["account_id"]: r for r in (results or [])}
    rows = status_rows(orch, accounts)
    totals: Dict[str, int] = {}
    for r in rows:
        totals[r["state"]] = totals.get(r["state"], 0) + 1
    payload = {
        "module": "hub", "cohort": orch.cohort, "command": orch.command, "run_id": orch.run_id,
        "generated_at": now_iso(), "started_at": started_at, "regions": orch.regions,
        "hub_account": orch.hub_account, "state_totals": totals,
        "accounts": [],
    }
    for r in rows:
        this = by_id.get(r["account_id"])
        payload["accounts"].append({
            "account_id": r["account_id"], "alias": r["alias"], "state": r["state"],
            "in_this_run": this is not None,
            "result": this["result"] if this else None,
            "ok": this["ok"] if this else None,
            "reason": (this or {}).get("reason") or r["reason"],
            "duration_s": (this or {}).get("duration_s", r["duration_s"]),
            "steps": {s: {k: v.get(k) for k in ("exit_code", "mode", "status", "duration_s", "command",
                                                 "finished_at", "error")}
                      for s, v in r["steps"].items()},
            "killswitch": r["killswitch"],
            "killswitch_regions": (this or {}).get("killswitch")
            or ((read_json(orch.account_dir(r["account_id"]) / "state.json") or {}).get("runs", {})
                .get("killswitch") or {}).get("regions") or {},
            "last_command": r["last_command"], "updated_at": r["updated_at"], "note": r["note"],
        })
    orch.cohort_dir.mkdir(parents=True, exist_ok=True)
    jpath, cpath = orch.cohort_dir / "summary.json", orch.cohort_dir / "summary.csv"
    atomic_write_json(jpath, payload)
    fields = ["account_id", "alias", "state", "in_this_run", "result", "ok"] \
        + [f"{s}_exit" for s in STEP_COLUMNS] + [f"{s}_mode" for s in STEP_COLUMNS] \
        + ["killswitch", "duration_s", "last_command", "updated_at", "reason", "note"]
    tmp = cpath.with_name(f".{cpath.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for a in payload["accounts"]:
            row = {k: a.get(k) for k in ("account_id", "alias", "state", "in_this_run", "result", "ok",
                                         "duration_s", "last_command", "updated_at", "reason", "note")}
            for s in STEP_COLUMNS:
                row[f"{s}_exit"] = (a["steps"].get(s) or {}).get("exit_code", "")
                row[f"{s}_mode"] = (a["steps"].get(s) or {}).get("mode", "")
            regs = a.get("killswitch_regions") or {}
            row["killswitch"] = (a.get("killswitch") or "") + (
                " (" + ";".join(f"{reg}={v.get('action')}" for reg, v in regs.items()) + ")" if regs else "")
            w.writerow(row)
    os.replace(tmp, cpath)
    return jpath, cpath


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="edu-cleanup hub orchestrator",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("--registry", required=True, help="cohort registry JSON (the allowlist)")
    p.add_argument("--accounts", help="comma-separated subset of registry account ids")
    p.add_argument("--max-parallel", type=int, help="accounts in parallel (default: registry max_parallel)")
    p.add_argument("--profile", help="AWS profile of the hub principal (default: env/instance credentials)")
    p.add_argument("--state-dir", default="state", help="state directory (default: %(default)s)")
    p.add_argument("--cleanup-date", help="verify: YYYY-MM-DD the account was cleaned "
                                          "(default: date of the last recorded clean run)")
    p.add_argument("--steps", help="run only these steps of the command, e.g. --steps preclose")
    p.add_argument("--fix-preclose", action="store_true",
                   help="clean: pass --apply to preclose_check.py (default: report-only)")
    p.add_argument("--allow-unplanned", action="store_true",
                   help="clean: allow accounts that have no successful plan recorded")
    p.add_argument("--repo-root", help=f"edu-cleanup repo root (default: ${REPO_ROOT_ENV} or this file's parent)")
    p.add_argument("--step-timeout", type=float, default=DEFAULT_STEP_TIMEOUT_MIN,
                   help="minutes before a step is killed (default: %(default)s)")
    p.add_argument("--lock-ttl-hours", type=float, default=DEFAULT_LOCK_TTL_HOURS,
                   help="a lock older than this is considered stale (default: %(default)s)")
    p.add_argument("--killswitch-template-url",
                   help="killswitch: S3 URL of killswitch.yaml (needed only if the file exceeds 51,200 bytes)")
    p.add_argument("--yes", action="store_true", help="clean/killswitch: do not ask for confirmation on a TTY")
    return p


def _config_error(msg: str) -> int:
    log(f"ERROR: {msg}")
    return EXIT_ERROR


def main(argv: Optional[List[str]] = None, cfn_client_factory: Optional[Callable[..., Any]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        registry = load_registry(args.registry)
        accounts = select_accounts(registry, args.accounts)
    except RegistryError as e:
        return _config_error(str(e))

    # boto3 needs a region for STS when the operator has no default configured; steps inherit it too.
    os.environ.setdefault("AWS_DEFAULT_REGION", registry["regions"][0])
    repo_root = Path(args.repo_root or os.environ.get(REPO_ROOT_ENV) or Path(__file__).resolve().parents[1])
    repo_root = repo_root.resolve()
    max_parallel = args.max_parallel or registry["max_parallel"]
    if max_parallel < 1:
        return _config_error("--max-parallel must be >= 1")
    if args.cleanup_date:
        try:
            dt.date.fromisoformat(args.cleanup_date)
        except ValueError:
            return _config_error(f"--cleanup-date must be YYYY-MM-DD, got {args.cleanup_date!r}")
    if args.command in COMMAND_STEPS and args.steps:
        wanted = [s.strip() for s in args.steps.split(",") if s.strip()]
        bad = [s for s in wanted if s not in COMMAND_STEPS[args.command]]
        if bad or not wanted:
            return _config_error(f"--steps for {args.command} must be among {COMMAND_STEPS[args.command]}")

    orch = Orchestrator(args, registry, repo_root, cfn_client_factory=cfn_client_factory)
    started_at = now_iso()

    if args.command == "status":
        rows = status_rows(orch, accounts)
        print(status_table(rows))
        jpath, cpath = write_summary(orch, accounts, None, started_at)
        log(f"summary: {jpath} / {cpath}")
        bad = [r for r in rows if r["state"] in STATUS_BAD_STATES or r["reason"].startswith("interrupted")]
        return EXIT_ISSUES if bad else EXIT_OK

    # ---- configuration checks before touching any account ----
    if args.command in COMMAND_STEPS:
        missing = [str(repo_root / STEP_DEFS[s]["path"]) for s in orch.selected_steps()
                   if not (repo_root / STEP_DEFS[s]["path"]).is_file()]
        if missing:
            return _config_error("step script(s) not found: " + ", ".join(missing)
                                 + f" (repo root {repo_root}; override with --repo-root)")
    if args.command == "killswitch" and not args.killswitch_template_url:
        tpl = repo_root / "killswitch" / "killswitch.yaml"
        if not tpl.is_file():
            return _config_error(f"{tpl} not found")
        body = tpl.read_text(encoding="utf-8")
        if len(body.encode("utf-8")) > TEMPLATE_BODY_LIMIT:
            return _config_error(f"{tpl} is larger than {TEMPLATE_BODY_LIMIT} bytes; upload it to S3 and pass "
                                 "--killswitch-template-url")
        orch.ks_template_body = body
        names = template_parameter_names(body)
        if names is not None:
            orch.ks_template_params = names
            sent = {p["ParameterKey"] for p in killswitch_parameters(registry.get("killswitch"))}
            dropped = sorted(sent - set(names))
            if dropped:
                log(f"WARNING: {tpl.name} does not declare {dropped}; not sending them")

    try:
        hub_ident = client(get_session(profile=args.profile), "sts", "us-east-1").get_caller_identity()
        orch.hub_account = hub_ident.get("Account")
        log(f"hub principal: {hub_ident.get('Arn')}")
    except (ClientError, BotoCoreError) as e:
        return _config_error(f"cannot read hub credentials ({type(e).__name__}: {e})")

    if args.command in ("clean", "killswitch") and not args.yes and sys.stdin.isatty():
        what = "DELETE resources in" if args.command == "clean" else "deploy the kill switch to"
        print(f"About to {what} {len(accounts)} account(s) of cohort {orch.cohort}: "
              + ", ".join(f"{a['account_id']}({a['alias'] or '-'})" for a in accounts), file=sys.stderr)
        answer = input(f"Type the cohort name '{orch.cohort}' to continue: ").strip()
        if answer != orch.cohort:
            return _config_error("not confirmed")

    prev_term = None
    if threading.current_thread() is threading.main_thread():
        def _on_term(signum, frame):  # treat SIGTERM like Ctrl-C so locks are released
            raise KeyboardInterrupt
        prev_term = signal.signal(signal.SIGTERM, _on_term)
    try:
        results = orch.run(accounts, max_parallel)
    finally:
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)

    print(run_table(args.command, results, orch.regions))
    jpath, cpath = write_summary(orch, accounts, results, started_at)
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    log(f"summary: {jpath} / {cpath}")
    return EXIT_OK if all(r.get("ok") for r in results) else EXIT_ISSUES


if __name__ == "__main__":
    sys.exit(main())
