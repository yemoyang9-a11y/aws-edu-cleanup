#!/usr/bin/env python3
"""cfn_predelete - delete every CloudFormation root stack BEFORE aws-nuke runs.

Why: aws-nuke deletes stacks one at a time inside a blocking SDK waiter (up to
60 min per stack, single-threaded queue), fails when a stack's service role is
already gone ("Role ... is invalid or cannot be assumed"), retries DELETE_FAILED
badly and never uses DeletionMode=FORCE_DELETE_STACK or Express mode.

What this script does (dry-run unless --apply):
  * regions run concurrently; inside a region up to --max-parallel stacks delete
    at the same time and are polled (never blocked on) every --poll-interval s
  * root stacks only (nested stacks go with their root); edu-bootstrap* skipped
  * order: importers before the stacks they import from; CDKToolkit last
  * DeleteStack with DeploymentConfig={"Mode": "EXPRESS"} (falls back without it)
  * stored service role gone -> RoleARN = --cfn-role-arn | EduCleanupRole |
    temporary edu-cfn-delete-temp role (created only with --apply, always deleted)
  * DELETE_FAILED -> DeleteStack(DeletionMode="FORCE_DELETE_STACK") once
  * one more listing pass afterwards (orphaned nested stacks, late arrivals)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.session import add_common_args, get_session, parse_regions, client, account_id, write_json, log, EXIT_OK, EXIT_ISSUES, EXIT_ERROR, EXIT_PENDING  # noqa: E402,F401
from common.session import BOOTSTRAP_STACK_PREFIX, ROLE_NAME, now_iso  # noqa: E402

import argparse  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import urllib.parse  # noqa: E402
from collections import Counter  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple  # noqa: E402

import botocore  # noqa: E402
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError  # noqa: E402

MODULE = "cfn_predelete"
TEMP_ROLE_NAME = "edu-cfn-delete-temp"
CDK_TOOLKIT_STACK = "CDKToolkit"
EXPRESS_CONFIG = {"Mode": "EXPRESS"}          # DeleteStack.DeploymentConfig (botocore >= 2026-06)
FORCE_MODE = "FORCE_DELETE_STACK"             # DeleteStack.DeletionMode
CFN_PRINCIPAL = "cloudformation.amazonaws.com"
CONFORMANCE_PACK_DESC = "DO NOT MODIFY THIS STACK! This stack is managed by Config Conformance Packs."

GONE, PLANNED, TIMEOUT, FAILED, SKIPPED = "GONE", "PLANNED", "TIMEOUT", "FAILED", "SKIPPED_PROTECTED"
ROLE_ERROR = "is invalid or cannot be assumed"
BUSY_RE = re.compile(r"_IN_PROGRESS|in progress", re.I)
PERMISSION_RE = re.compile(r"not authorized|AccessDenied|is invalid or cannot be assumed|unable to assume", re.I)
MAX_REISSUE = 4        # DeleteStack re-sent when the stack stays in a non-delete state
MAX_POLL_ERRORS = 5
HEARTBEAT_S = 60.0
NOT_EXPRESS_CODES = {"AccessDenied", "AccessDeniedException", "Throttling", "ThrottlingException",
                     "RequestLimitExceeded", "TokenAlreadyExistsException", "ExpiredToken",
                     "InvalidClientTokenId", "UnrecognizedClientException"}


def _err(e: ClientError) -> Tuple[str, str]:
    err = e.response.get("Error", {}) or {}
    return err.get("Code", "") or "", err.get("Message", "") or str(e)


def _is_gone(e: ClientError) -> bool:
    code, msg = _err(e)
    return code == "ValidationError" and "does not exist" in msg


def _maybe_express_rejection(e: ClientError) -> bool:
    """True when a failed DeleteStack *with* DeploymentConfig may have failed because of it."""
    code, msg = _err(e)
    if _is_gone(e) or ROLE_ERROR in msg or "TerminationProtection" in msg or BUSY_RE.search(msg):
        return False
    return code not in NOT_EXPRESS_CODES


def _model_members(cfn) -> set:
    return set(cfn.meta.service_model.operation_model("DeleteStack").input_shape.members)


def model_supports(cfn, member: str) -> bool:
    """Does the installed botocore know DeleteStack.<member>?

    botocore >= 1.43 (Python >= 3.10) has DeploymentConfig; 1.42.x - the last line for
    Python 3.9, e.g. CloudShell's python3 - has DeletionMode but no DeploymentConfig.
    """
    return member in _model_members(cfn)


def _flatten(prefix: str, value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _flatten(f"{prefix}.{k}", v)
    else:
        yield prefix, value


def delete_stack(cfn, **kw: Any) -> Dict[str, Any]:
    """cfn.delete_stack(), also when the installed botocore predates a parameter.

    CloudFormation speaks the AWS *query* protocol, so a parameter missing from the local
    service model is added to the form body as-is (DeploymentConfig.Mode=EXPRESS). The
    service validates it exactly like a modern SDK request.
    """
    members = _model_members(cfn)
    raw: Dict[str, Any] = {}
    for name in [k for k in kw if k not in members]:
        raw.update(_flatten(name, kw.pop(name)))
    if not raw:
        return cfn.delete_stack(**kw)

    def inject(params: Dict[str, Any], **_: Any) -> None:
        if isinstance(params.get("body"), dict):
            params["body"].update(raw)

    event = "before-call.cloudformation.DeleteStack"
    cfn.meta.events.register_first(event, inject)   # one region = one thread = one client
    try:
        return cfn.delete_stack(**kw)
    finally:
        cfn.meta.events.unregister(event, inject)


class NoRoleError(Exception):
    pass


# ---------------------------------------------------------------- planning ---
@dataclass
class StackTask:
    region: str
    stack_name: str
    stack_id: str
    initial_status: str
    status_reason: str = ""
    stored_role: Optional[str] = None
    termination_protection: Optional[bool] = None
    is_toolkit: bool = False
    protected: bool = False
    skip_reason: str = ""
    orphan_nested: bool = False
    nested_count: int = 0
    depends_on: Dict[str, str] = field(default_factory=dict)   # stack id -> name
    order: int = 1
    # outcome
    action: str = ""
    final_status: Optional[str] = None
    used_express: bool = False
    used_force: bool = False
    role_missing: bool = False
    role_override: Optional[str] = None
    role_source: str = "caller"        # stored | caller (no role: CFN uses our creds) | cli | EduCleanupRole | temp-role
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    # runtime
    last_status: str = ""
    express_rejected: bool = False
    tp_disabled: bool = False
    reissues: int = 0
    poll_errors: int = 0
    last_error: Optional[str] = None
    force_grace: int = 0                 # stale DELETE_FAILED right after the force request
    started: Optional[float] = None
    finished: Optional[float] = None

    def __post_init__(self) -> None:
        self.last_status = self.initial_status
        if self.stored_role:
            self.role_source = "stored"

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    @property
    def role_used(self) -> Optional[str]:
        return self.role_override or self.stored_role

    def to_result(self) -> Dict[str, Any]:
        duration = None
        if self.started is not None and self.finished is not None:
            duration = round(self.finished - self.started, 1)
        return {
            "region": self.region, "stack_name": self.stack_name, "stack_id": self.stack_id,
            "initial_status": self.initial_status, "action": self.action,
            "final_status": self.final_status, "used_express": self.used_express,
            "used_force": self.used_force, "role_used": self.role_used, "error": self.error,
            "express_rejected": self.express_rejected,
            "order": self.order, "role_source": self.role_source, "role_missing": self.role_missing,
            "termination_protection": self.termination_protection, "nested_count": self.nested_count,
            "orphan_nested": self.orphan_nested, "is_cdk_toolkit": self.is_toolkit,
            "depends_on": sorted(self.depends_on.values()), "last_status": self.last_status,
            "duration_s": duration, "notes": self.notes,
        }


def list_stacks(cfn) -> List[Dict[str, Any]]:
    """All stacks of a region except DELETE_COMPLETE (paginated)."""
    out = []
    for page in cfn.get_paginator("describe_stacks").paginate():
        out.extend(s for s in page.get("Stacks", []) if s.get("StackStatus") != "DELETE_COMPLETE")
    return out


def is_cdk_toolkit(stack: Dict[str, Any]) -> bool:
    if stack.get("StackName") == CDK_TOOLKIT_STACK:
        return True
    # custom --toolkit-stack-name: the bootstrap template has a BootstrapVersion *output*
    return any(o.get("OutputKey") == "BootstrapVersion" for o in stack.get("Outputs") or [])


def _imports_of(cfn, export_name: str) -> List[str]:
    names: List[str] = []
    try:
        for page in cfn.get_paginator("list_imports").paginate(ExportName=export_name):
            names.extend(page.get("Imports", []))
    except ClientError as e:
        if "not imported" not in _err(e)[1]:
            raise
    return names


def _owners_of(cfn, region: str, physical_id: str) -> List[Dict[str, Any]]:
    """Stack resources whose physical id is ``physical_id`` (empty when no stack owns it)."""
    try:
        return cfn.describe_stack_resources(PhysicalResourceId=physical_id).get("StackResources", [])
    except ClientError as e:
        code, msg = _err(e)
        if code != "ValidationError":
            log(f"[{region}] WARNING: cannot check which stack owns {physical_id}: {code}: {msg}")
        return []


def plan_region(cfn, region: str, stacks: List[Dict[str, Any]], skip: Optional[set] = None,
                protect_ids: Sequence[str] = ()) -> List[StackTask]:
    """Root stacks (plus orphaned nested ones) of a region with dependency order.

    ``protect_ids`` are physical resource ids (IAM role / user names) that must survive:
    the root stack that created one of them is skipped (SKIPPED_PROTECTED).
    """
    skip = skip or set()
    by_id = {s["StackId"]: s for s in stacks}
    by_name = {s["StackName"]: s for s in stacks}
    tasks: Dict[str, StackTask] = {}
    for sid, s in by_id.items():
        parent = s.get("ParentId")
        if parent and parent in by_id:
            continue                       # nested: deleted together with its root
        reason = ""
        if s["StackName"].startswith(BOOTSTRAP_STACK_PREFIX):
            reason = f"protected ({BOOTSTRAP_STACK_PREFIX}*)"
        elif (s.get("Description") or "").strip() == CONFORMANCE_PACK_DESC:
            reason = "managed by a Config conformance pack (aws-nuke ConfigServiceConformancePack removes it)"
        tasks[sid] = StackTask(
            region=region, stack_name=s["StackName"], stack_id=sid,
            initial_status=s.get("StackStatus", ""), status_reason=s.get("StackStatusReason") or "",
            stored_role=s.get("RoleARN"),
            termination_protection=s.get("EnableTerminationProtection"),
            is_toolkit=is_cdk_toolkit(s), protected=bool(reason), skip_reason=reason,
            orphan_nested=bool(parent))

    def owner(sid: Optional[str]) -> Optional[str]:
        seen = set()
        while sid and sid not in tasks and sid not in seen:
            seen.add(sid)
            sid = (by_id.get(sid) or {}).get("ParentId")
        return sid if sid in tasks else None

    for sid in by_id:
        if sid not in tasks:
            o = owner(sid)
            if o:
                tasks[o].nested_count += 1
    for sid, t in tasks.items():
        if t.orphan_nested:
            t.note("orphaned nested stack (parent already deleted)")
    for pid in protect_ids:
        for res in _owners_of(cfn, region, pid):
            if res.get("ResourceType") not in ("AWS::IAM::Role", "AWS::IAM::User"):
                continue
            o = owner(res.get("StackId"))
            if o and not tasks[o].protected:
                tasks[o].protected = True
                tasks[o].skip_reason = f"created {res.get('ResourceType', 'resource')} {pid} (protected identity)"

    tasks = {sid: t for sid, t in tasks.items() if sid not in skip}
    runnable = {sid: t for sid, t in tasks.items() if not t.protected}
    if len(runnable) > 1:
        try:
            for page in cfn.get_paginator("list_exports").paginate():
                for ex in page.get("Exports", []):
                    exporter = owner(ex.get("ExportingStackId"))
                    if exporter not in runnable:
                        continue
                    for imp_name in _imports_of(cfn, ex["Name"]):
                        imp = owner((by_name.get(imp_name) or {}).get("StackId"))
                        if imp and imp != exporter and imp in runnable:
                            runnable[exporter].depends_on[imp] = runnable[imp].stack_name
        except ClientError as e:
            log(f"[{region}] WARNING: export/import ordering skipped: {_err(e)[1]}")
        others = {sid: t.stack_name for sid, t in runnable.items() if not t.is_toolkit}
        for t in runnable.values():
            if t.is_toolkit:
                t.depends_on.update(others)
    _assign_order(runnable)
    return sorted(tasks.values(), key=lambda t: (t.protected, t.order, t.is_toolkit, t.stack_name))


def _assign_order(tasks: Dict[str, StackTask]) -> None:
    remaining = {sid: set(t.depends_on) & set(tasks) for sid, t in tasks.items()}
    level = 1
    while remaining:
        ready = [sid for sid, deps in remaining.items() if not deps & remaining.keys()]
        if not ready:                         # cycle (should not happen) -> ignore deps
            for sid in remaining:
                tasks[sid].depends_on.clear()
                tasks[sid].order = level
                tasks[sid].note("dependency cycle ignored")
            return
        for sid in ready:
            tasks[sid].order = level
            del remaining[sid]
        level += 1


# ------------------------------------------------------------ role fallback ---
def _trusts_cfn(role: Dict[str, Any]) -> bool:
    doc = role.get("AssumeRolePolicyDocument") or ""
    text = urllib.parse.unquote(doc) if isinstance(doc, str) else json.dumps(doc)
    return CFN_PRINCIPAL in text


class RoleResolver:
    """Checks stored stack roles and provides one working CloudFormation role (shared, thread-safe)."""

    def __init__(self, iam, account: str, partition: str = "aws", *, cli_role_arn: Optional[str] = None,
                 apply: bool = False, propagation_delay: float = 10.0,
                 sleep: Callable[[float], Any] = time.sleep):
        self.iam, self.account, self.partition = iam, account, partition
        self.cli_role_arn, self.apply = cli_role_arn, apply
        self.propagation_delay, self.sleep = propagation_delay, sleep
        self._cache_lock = threading.Lock()
        self._resolve_lock = threading.Lock()
        self._exists: Dict[str, bool] = {}
        self.resolved = False
        self.source: Optional[str] = None
        self.role_arn: Optional[str] = None
        self.temp_created = False
        self.temp_deleted = False
        self.error: Optional[str] = None
        self.cleanup_error: Optional[str] = None

    def role_exists(self, arn: str) -> bool:
        with self._cache_lock:
            if arn not in self._exists:
                self._exists[arn] = self._check(arn)
            return self._exists[arn]

    def _check(self, arn: str) -> bool:
        parts = arn.split(":")
        if len(parts) < 6 or parts[4] != self.account:
            return True                      # other account / odd ARN: cannot check, assume fine
        try:
            role = self.iam.get_role(RoleName=arn.rsplit("/", 1)[-1])["Role"]
            return role.get("Arn", arn) == arn
        except ClientError as e:
            if _err(e)[0] == "NoSuchEntity":
                return False
            log(f"WARNING: cannot check role {arn}: {_err(e)[1]} (assuming it exists)")
            return True

    def fallback(self) -> Tuple[Optional[str], Optional[str]]:
        """(source, role_arn) of the role to pass as RoleARN, resolved once."""
        with self._resolve_lock:
            if not self.resolved:
                self.resolved = True
                try:
                    self._resolve()
                except Exception as e:  # IAM denied, waiter timeout, ...
                    self.error = f"{type(e).__name__}: {e}"
                    self.source = self.role_arn = None
                    log(f"ERROR: no fallback CloudFormation role: {self.error}")
            return self.source, self.role_arn

    def _resolve(self) -> None:
        if self.cli_role_arn:
            self.source, self.role_arn = "cli", self.cli_role_arn
            return
        try:
            edu = self.iam.get_role(RoleName=ROLE_NAME)["Role"]
        except ClientError as e:
            if _err(e)[0] != "NoSuchEntity":
                raise
            edu = None
        if edu and _trusts_cfn(edu):
            self.source, self.role_arn = ROLE_NAME, edu["Arn"]
            return
        if edu:
            log(f"{ROLE_NAME} exists but does not trust {CFN_PRINCIPAL}; using a temporary role")
        if not self.apply:
            self.source = "temp-role-planned"
            self.role_arn = f"arn:{self.partition}:iam::{self.account}:role/{TEMP_ROLE_NAME}"
            return
        self.source, self.role_arn = "temp-role", self._create_temp()

    def _create_temp(self) -> str:
        trust = json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": CFN_PRINCIPAL}, "Action": "sts:AssumeRole"}]})
        try:
            arn = self.iam.create_role(
                RoleName=TEMP_ROLE_NAME, AssumeRolePolicyDocument=trust,
                Description="Temporary CloudFormation role of edu-cleanup cfn_predelete (auto-deleted)",
                Tags=[{"Key": "managed-by", "Value": "edu-cleanup/cfn_predelete"}])["Role"]["Arn"]
        except ClientError as e:
            if _err(e)[0] != "EntityAlreadyExists":
                raise
            arn = self.iam.get_role(RoleName=TEMP_ROLE_NAME)["Role"]["Arn"]
            self.iam.update_assume_role_policy(RoleName=TEMP_ROLE_NAME, PolicyDocument=trust)
        self.temp_created = True             # from here on cleanup() deletes it
        log(f"created temporary role {arn} (AdministratorAccess, trusts {CFN_PRINCIPAL})")
        self.iam.attach_role_policy(RoleName=TEMP_ROLE_NAME,
                                    PolicyArn=f"arn:{self.partition}:iam::aws:policy/AdministratorAccess")
        self.iam.get_waiter("role_exists").wait(RoleName=TEMP_ROLE_NAME,
                                                WaiterConfig={"Delay": 2, "MaxAttempts": 15})
        if self.propagation_delay > 0:
            log(f"waiting {self.propagation_delay:.0f}s for IAM propagation")
            self.sleep(self.propagation_delay)
        return arn

    def cleanup(self) -> None:
        if not self.temp_created or self.temp_deleted:
            return
        try:
            for page in self.iam.get_paginator("list_attached_role_policies").paginate(RoleName=TEMP_ROLE_NAME):
                for p in page.get("AttachedPolicies", []):
                    self.iam.detach_role_policy(RoleName=TEMP_ROLE_NAME, PolicyArn=p["PolicyArn"])
            for page in self.iam.get_paginator("list_role_policies").paginate(RoleName=TEMP_ROLE_NAME):
                for name in page.get("PolicyNames", []):
                    self.iam.delete_role_policy(RoleName=TEMP_ROLE_NAME, PolicyName=name)
            self.iam.delete_role(RoleName=TEMP_ROLE_NAME)
            self.temp_deleted = True
            log(f"deleted temporary role {TEMP_ROLE_NAME}")
        except ClientError as e:
            if _err(e)[0] == "NoSuchEntity":
                self.temp_deleted = True
            else:
                self.cleanup_error = _err(e)[1]
                log(f"ERROR: could not delete temporary role {TEMP_ROLE_NAME}: {self.cleanup_error}")

    def to_json(self) -> Dict[str, Any]:
        return {"needed": self.resolved, "source": self.source, "role_arn": self.role_arn,
                "temp_role_name": TEMP_ROLE_NAME, "temp_role_created": self.temp_created,
                "temp_role_deleted": self.temp_deleted, "error": self.error,
                "cleanup_error": self.cleanup_error}


# ------------------------------------------------------------------ runner ---
class Runner:
    def __init__(self, cfn_clients: Dict[str, Any], resolver: RoleResolver, *, apply: bool,
                 timeout_s: float, max_parallel: int, poll_interval: float, jitter: float = 0.2,
                 protect_ids: Sequence[str] = (),
                 sleep: Optional[Callable[[float], Any]] = None, clock: Callable[[], float] = time.monotonic):
        self.cfn, self.resolver, self.apply = cfn_clients, resolver, apply
        self.protect_ids = list(protect_ids)
        self.timeout_s, self.max_parallel = timeout_s, max(1, max_parallel)
        self.poll_interval, self.jitter = poll_interval, jitter
        self.stop = threading.Event()
        self.sleep = sleep or self.stop.wait
        self.clock = clock
        self.deadline = 0.0

    # -- orchestration
    def run(self) -> Tuple[List[StackTask], List[Dict[str, str]]]:
        self.deadline = self.clock() + self.timeout_s
        results: List[StackTask] = []
        errors: List[Dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.cfn))) as pool:
            futures = [(r, pool.submit(self._region, r)) for r in self.cfn]
            for region, fut in futures:
                while True:                       # workers never raise; Ctrl-C only reaches this thread
                    try:
                        tasks, err = fut.result()
                        break
                    except KeyboardInterrupt:
                        if not self.stop.is_set():
                            log("interrupted - stopping; stacks already deleting keep deleting in AWS")
                        self.stop.set()
                results.extend(tasks)
                if err:
                    errors.append({"region": region, "error": err})
        if self.stop.is_set():
            errors.append({"region": "*", "error": "interrupted by user"})
        return results, errors

    def _expired(self) -> bool:
        return self.stop.is_set() or self.clock() >= self.deadline

    def _region(self, region: str) -> Tuple[List[StackTask], Optional[str]]:
        cfn = self.cfn[region]
        done: List[StackTask] = []
        seen: set = set()
        try:
            for round_no in (1, 2):
                tasks = plan_region(cfn, region, list_stacks(cfn), skip=seen, protect_ids=self.protect_ids)
                if not tasks:
                    break
                seen.update(t.stack_id for t in tasks)
                done.extend(tasks)
                if round_no == 2:
                    log(f"[{region}] second pass: {len(tasks)} more stack(s) found")
                if not self.apply:
                    self._plan_only(tasks)
                    break
                self._execute(cfn, tasks)
                if self._expired():
                    break
            return done, None
        except (Exception, KeyboardInterrupt) as e:
            msg = f"{type(e).__name__}: {e}"
            log(f"[{region}] ERROR: {msg}")
            for t in done:
                if t.final_status is None:
                    t.final_status, t.error = FAILED, t.error or f"internal error: {msg}"
            return done, msg

    def _plan_only(self, tasks: List[StackTask]) -> None:
        for t in tasks:
            if t.protected:
                t.action, t.final_status = "SKIP", SKIPPED
                t.note(t.skip_reason)
                continue
            t.action = {"DELETE_IN_PROGRESS": "POLL", "DELETE_FAILED": "FORCE_DELETE"}.get(t.initial_status, "DELETE")
            try:
                self._ensure_role(t)
            except NoRoleError as e:
                t.note(str(e))
            if t.termination_protection:
                t.note("termination protection will be disabled")
            t.final_status = PLANNED

    def _execute(self, cfn, tasks: List[StackTask]) -> None:
        by_id = {t.stack_id: t for t in tasks}
        for t in tasks:
            if t.protected:
                t.action, t.final_status = "SKIP", SKIPPED
                t.note(t.skip_reason)
                log(f"[{t.region}] {t.stack_name}: skipped - {t.skip_reason}")
        pending = [t for t in tasks if not t.protected]
        active: List[StackTask] = []
        last_beat = self.clock()
        for t in [t for t in pending if t.initial_status == "DELETE_IN_PROGRESS"]:
            pending.remove(t)                 # already deleting: poll now, ignore order and slots
            self._start(cfn, t)
            active.append(t)
        while pending or active:
            for t in active:
                self._poll(cfn, t)
            active = [t for t in active if t.final_status is None]
            if self._expired():               # checked after the poll: last look before giving up
                why = "interrupted" if self.stop.is_set() else f"timeout ({self.timeout_s / 60:g} min)"
                for t in active:
                    self._finish(t, TIMEOUT, f"{why}: still {t.last_status or 'deleting'}")
                for t in pending:
                    t.action = t.action or "NOT_STARTED"
                    waits = ", ".join(sorted(t.depends_on.values())) or "a free slot"
                    self._finish(t, TIMEOUT, f"{why}: not started (waiting for {waits})")
                return
            for t in list(pending):
                if sum(1 for a in active if a.action != "POLL") >= self.max_parallel:
                    break
                deps = [by_id[d] for d in t.depends_on if d in by_id]
                if any(d.final_status is None for d in deps):
                    continue
                pending.remove(t)
                bad = [d for d in deps if d.final_status != GONE]
                if bad:
                    t.action = "BLOCKED"
                    self._finish(t, FAILED, "not started: " + ", ".join(
                        f"{d.stack_name} ended {d.final_status}" for d in bad))
                    continue
                self._start(cfn, t)
                if t.final_status is None:
                    active.append(t)
            if (pending or active) and self.clock() - last_beat >= HEARTBEAT_S:
                last_beat = self.clock()
                left = max(0, int(self.deadline - last_beat))
                log(f"[{tasks[0].region}] {len(active)} deleting, {len(pending)} waiting "
                    f"({', '.join(sorted(t.stack_name for t in active))[:200]}) - {left // 60}m{left % 60:02d}s left")
            if pending or active:
                wait = self.poll_interval * (1 + random.uniform(0, self.jitter))
                self.sleep(max(0.0, min(wait, self.deadline - self.clock())))

    # -- per stack
    def _say(self, t: StackTask, msg: str) -> None:
        log(f"[{t.region}] {t.stack_name}: {msg}")

    def _finish(self, t: StackTask, status: str, error: Optional[str] = None) -> None:
        t.final_status, t.error, t.finished = status, error, self.clock()
        if t.started is None:
            t.started = t.finished
        self._say(t, status + (f" - {error}" if error else ""))

    def _ensure_role(self, t: StackTask) -> None:
        if t.role_override or not t.stored_role or self.resolver.role_exists(t.stored_role):
            return
        t.role_missing = True
        source, arn = self.resolver.fallback()
        if not arn:
            raise NoRoleError(f"stored service role {t.stored_role} is gone and no fallback role is available"
                              + (f" ({self.resolver.error})" if self.resolver.error else ""))
        t.role_override, t.role_source = arn, source
        t.note(f"stored role {t.stored_role} missing -> RoleARN {arn} ({source})")

    def _start(self, cfn, t: StackTask) -> None:
        t.started = self.clock()
        if t.initial_status == "DELETE_IN_PROGRESS":
            t.action = "POLL"
            self._say(t, "already DELETE_IN_PROGRESS - polling only")
            return
        if t.initial_status == "DELETE_FAILED":
            t.action = "FORCE_DELETE"
            if self._switch_role_on_permission_error(t, t.status_reason):
                t.action = "DELETE"           # a working role may delete everything: plain retry first
                self._delete(cfn, t, force=False)
                return
            self._delete(cfn, t, force=True)
            return
        t.action = "DELETE"
        self._delete(cfn, t, force=False)

    def _call_delete(self, cfn, t: StackTask, force: bool) -> None:
        kw: Dict[str, Any] = {"StackName": t.stack_id}
        if t.role_override:
            kw["RoleARN"] = t.role_override
        if force:
            kw["DeletionMode"] = FORCE_MODE
        if not t.express_rejected:
            if not model_supports(cfn, "DeploymentConfig"):
                t.note("installed botocore has no DeploymentConfig - sent as raw query parameter")
            try:
                delete_stack(cfn, DeploymentConfig=dict(EXPRESS_CONFIG), **kw)
                t.used_express = True
                return
            except ParamValidationError as e:
                reason = f"client: {e}"
            except ClientError as e:
                if not _maybe_express_rejection(e):
                    raise
                reason = "%s: %s" % _err(e)
            self._say(t, f"DeleteStack with EXPRESS failed ({reason}) - retrying without DeploymentConfig")
            delete_stack(cfn, **kw)          # if this fails too, EXPRESS was not the cause
            t.express_rejected = True
            t.note(f"EXPRESS rejected ({reason}); deleted without DeploymentConfig")
            return
        delete_stack(cfn, **kw)

    def _delete(self, cfn, t: StackTask, force: bool) -> None:
        for _ in range(3):
            try:
                self._ensure_role(t)
                if t.termination_protection and not t.tp_disabled:
                    self._disable_tp(cfn, t)
                self._call_delete(cfn, t, force)
                if force:
                    t.used_force = True
                    t.force_grace = 1
                    if t.action != "FORCE_DELETE" and not t.action.endswith("+FORCE"):
                        t.action += "+FORCE"
                self._say(t, "DeleteStack sent" + (" (FORCE_DELETE_STACK)" if force else "")
                          + (" [EXPRESS]" if t.used_express and not t.express_rejected else "")
                          + (f" [RoleARN {t.role_override}]" if t.role_override else ""))
                return
            except NoRoleError as e:
                self._finish(t, FAILED, str(e))
                return
            except ParamValidationError as e:
                self._finish(t, FAILED, f"ParamValidationError: {e}")
                return
            except ClientError as e:
                code, msg = _err(e)
                t.last_error = f"{code}: {msg}"
                if _is_gone(e):
                    self._finish(t, GONE)
                    return
                if "TerminationProtection" in msg and not t.tp_disabled:
                    t.termination_protection = True
                    continue
                if ROLE_ERROR in msg:
                    if not t.role_override:
                        source, arn = self.resolver.fallback()
                        if arn:
                            t.role_override, t.role_source = arn, source
                            t.note(f"stored role rejected -> RoleARN {arn} ({source})")
                            continue
                        self._finish(t, FAILED, f"{msg} (no fallback role available)")
                        return
                    t.note(f"fallback role not usable yet: {msg}")
                    self._say(t, "fallback role not usable yet (IAM propagation?) - will retry")
                    return                    # still tracked; poll re-sends DeleteStack
                if BUSY_RE.search(msg):
                    self._say(t, f"busy ({msg}) - will retry when the stack is stable")
                    return
                self._finish(t, FAILED, f"{code}: {msg}")
                return
            except BotoCoreError as e:        # network etc.: keep tracking, poll re-sends
                t.last_error = f"{type(e).__name__}: {e}"
                self._say(t, f"DeleteStack error ({t.last_error}) - will retry")
                return
        self._finish(t, FAILED, "DeleteStack kept failing" + (f" ({t.last_error})" if t.last_error else ""))

    def _switch_role_on_permission_error(self, t: StackTask, reason: str) -> bool:
        """Stored role exists but was stripped of permissions -> use the fallback role (once)."""
        if not (t.stored_role and not t.role_override and PERMISSION_RE.search(reason or "")):
            return False
        source, arn = self.resolver.fallback()
        if not arn:
            return False
        t.role_override, t.role_source = arn, source
        t.note(f"stored role {t.stored_role} lacks permissions -> RoleARN {arn} ({source})")
        return True

    def _disable_tp(self, cfn, t: StackTask) -> None:
        cfn.update_termination_protection(StackName=t.stack_id, EnableTerminationProtection=False)
        t.tp_disabled = True
        t.note("termination protection disabled")
        self._say(t, "termination protection disabled")

    def _poll(self, cfn, t: StackTask) -> None:
        try:
            st = cfn.describe_stacks(StackName=t.stack_id)["Stacks"][0]
        except (ClientError, BotoCoreError) as e:
            if isinstance(e, ClientError) and _is_gone(e):
                self._finish(t, GONE)
                return
            t.poll_errors += 1
            if t.poll_errors >= MAX_POLL_ERRORS:
                why = "%s: %s" % _err(e) if isinstance(e, ClientError) else f"{type(e).__name__}: {e}"
                self._finish(t, FAILED, f"DescribeStacks failed {t.poll_errors}x: {why}")
            return
        t.poll_errors = 0
        status = st.get("StackStatus", "")
        t.last_status = status
        if status == "DELETE_COMPLETE":
            self._finish(t, GONE)
        elif status == "DELETE_IN_PROGRESS":
            t.force_grace = 0
            return
        elif status == "DELETE_FAILED":
            reason = st.get("StackStatusReason") or "DELETE_FAILED"
            if t.used_force:
                if t.force_grace > 0:          # may not have switched to DELETE_IN_PROGRESS yet
                    t.force_grace -= 1
                    return
                self._finish(t, FAILED, f"DELETE_FAILED after FORCE_DELETE_STACK: {reason}")
                return
            t.reissues += 1
            if t.reissues > MAX_REISSUE:
                self._finish(t, FAILED, f"DELETE_FAILED and FORCE_DELETE_STACK was not accepted: {t.last_error or reason}")
                return
            t.note(f"DELETE_FAILED: {reason}")
            if self._switch_role_on_permission_error(t, reason):
                self._say(t, f"DELETE_FAILED ({reason}) - retrying with RoleARN {t.role_override}")
                self._delete(cfn, t, force=False)
                return
            self._say(t, f"DELETE_FAILED ({reason}) - retrying with FORCE_DELETE_STACK")
            self._delete(cfn, t, force=True)
        elif status.endswith("_IN_PROGRESS") and status != "REVIEW_IN_PROGRESS":
            return                            # another operation running; re-send when stable
        else:
            t.reissues += 1
            if t.reissues > MAX_REISSUE:
                self._finish(t, FAILED, f"stack stays in {status}; DeleteStack did not take effect"
                             + (f" (last error {t.last_error})" if t.last_error else ""))
            else:
                self._delete(cfn, t, force=False)


# --------------------------------------------------------------- top level ---
def compute_status(results: List[Dict[str, Any]], errors: List[Any], applied: bool,
                   cleanup_error: Optional[str]) -> Tuple[str, int]:
    if errors:
        return "ERROR", EXIT_ERROR
    want = GONE if applied else PLANNED
    if cleanup_error or any(r["final_status"] not in (want, SKIPPED) for r in results):
        return "ISSUES", EXIT_ISSUES
    return "OK", EXIT_OK


def execute(*, cfn_clients: Dict[str, Any], iam, account: str, partition: str = "aws", apply: bool = False,
            cli_role_arn: Optional[str] = None, timeout_s: float = 1800.0, max_parallel: int = 8,
            poll_interval: float = 15.0, propagation_delay: float = 10.0, jitter: float = 0.2,
            protect_ids: Sequence[str] = (),
            sleep: Optional[Callable[[float], Any]] = None, clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Run the whole module and return the CONTRACTS JSON payload (temp role always cleaned up)."""
    resolver = RoleResolver(iam, account, partition, cli_role_arn=cli_role_arn, apply=apply,
                            propagation_delay=propagation_delay, sleep=sleep or time.sleep)
    runner = Runner(cfn_clients, resolver, apply=apply, timeout_s=timeout_s, max_parallel=max_parallel,
                    poll_interval=poll_interval, jitter=jitter, protect_ids=protect_ids, sleep=sleep, clock=clock)
    resolver.sleep = lambda s: runner.sleep(s)   # interruptible (stop event), same clock as polling
    try:
        tasks, errors = runner.run()
    finally:
        resolver.cleanup()                   # temp role is ALWAYS removed, even on crash/Ctrl-C
    results = [t.to_result() for t in tasks]
    status, code = compute_status(results, errors, apply, resolver.cleanup_error)
    if resolver.temp_created and any(r["final_status"] == TIMEOUT and r["role_source"] == "temp-role" for r in results):
        log("WARNING: temporary role deleted while some stacks using it are still deleting; "
            "re-run cfn_predelete before aws-nuke")
    return {
        "module": MODULE, "account_id": account, "generated_at": now_iso(), "applied": apply,
        "status": status, "exit_code": code, "regions": list(cfn_clients),
        "timeout_min": round(timeout_s / 60, 2), "max_parallel": max_parallel,
        "botocore_version": botocore.__version__,
        "botocore_has_deployment_config": all(model_supports(c, "DeploymentConfig") for c in cfn_clients.values()),
        "botocore_has_deletion_mode": all(model_supports(c, "DeletionMode") for c in cfn_clients.values()),
        "protected_identities": list(protect_ids),
        "role_fallback": resolver.to_json(),
        "summary": dict(Counter(r["final_status"] for r in results)),
        "errors": errors, "results": results,
    }


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "~"


def render_table(payload: Dict[str, Any]) -> str:
    rows = [["REGION", "#", "STACK", "INITIAL", "ACTION", "RESULT", "EXPRESS", "FORCE", "ROLE", "NOTE"]]
    for r in payload["results"]:
        note = []
        if r["termination_protection"]:
            note.append("TP")
        if r["nested_count"]:
            note.append(f"nested={r['nested_count']}")
        if r["role_missing"]:
            note.append("role missing")
        if r["orphan_nested"]:
            note.append("orphan nested")
        if r["depends_on"]:
            note.append("after: " + _short(",".join(r["depends_on"]), 40))
        if r["error"]:
            note.append(_short(r["error"], 90))
        rows.append([r["region"], str(r["order"]), r["stack_name"], r["initial_status"], r["action"],
                     r["final_status"] or "-", "yes" if r["used_express"] else "-",
                     "yes" if r["used_force"] else "-", r["role_source"], "; ".join(note)])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row[:-1], widths)) + "  " + row[-1] for row in rows]
    if len(rows) == 1:
        lines.append("(no CloudFormation stacks found)")
    mode = "APPLY" if payload["applied"] else "DRY-RUN (nothing changed)"
    summary = " ".join(f"{k}={v}" for k, v in sorted(payload["summary"].items())) or "nothing to do"
    rf = payload["role_fallback"]
    lines += ["",
              f"cfn_predelete | account {payload['account_id']} | {mode} | regions {','.join(payload['regions'])}",
              f"result: {summary} | status {payload['status']} (exit {payload['exit_code']})",
              f"botocore {payload['botocore_version']}: DeploymentConfig "
              f"{'in model' if payload['botocore_has_deployment_config'] else 'not in model (sent raw)'}, DeletionMode "
              f"{'in model' if payload['botocore_has_deletion_mode'] else 'not in model (sent raw)'}"]
    if rf["needed"]:
        lines.append(f"role fallback: {rf['source'] or 'NONE'} {rf['role_arn'] or ''}"
                     + (f" | temp role created={rf['temp_role_created']} deleted={rf['temp_role_deleted']}"
                        if rf["temp_role_created"] else "")
                     + (f" | error: {rf['error']}" if rf["error"] else ""))
    for e in payload["errors"]:
        lines.append(f"ERROR [{e['region']}]: {e['error']}")
    if not payload["applied"]:
        lines.append("next: check the order/roles above, then re-run with --apply")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Delete all CloudFormation root stacks in parallel (non-blocking) "
                                            "before aws-nuke. Dry-run unless --apply.")
    add_common_args(p)
    g = p.add_argument_group("cfn_predelete")
    g.add_argument("--timeout-min", type=float, default=30.0,
                   help="wall-clock limit for the whole run, all regions together (default 30); "
                        "stacks still deleting then are reported TIMEOUT")
    g.add_argument("--max-parallel", type=int, default=8,
                   help="max stacks deleting at the same time per region (default 8)")
    g.add_argument("--cfn-role-arn",
                   help="role CloudFormation assumes for stacks whose own service role is gone "
                        "(must trust cloudformation.amazonaws.com); default: EduCleanupRole, else a temp role")
    g.add_argument("--poll-interval", type=float, default=15.0, help="seconds between status polls (default 15)")
    return p


def _partition(session, region: str) -> str:
    try:
        return session.get_partition_for_region(region)
    except Exception:
        return "aws"


def protected_identities(caller: str) -> List[str]:
    """EduCleanupRole + the IAM user / role the script itself runs as (CONTRACTS)."""
    ids = [ROLE_NAME]
    resource = caller.split(":", 5)[-1] if caller.count(":") >= 5 else ""
    if resource.startswith("user/"):
        ids.append(resource.rsplit("/", 1)[-1])
    elif resource.startswith("assumed-role/"):
        ids.append(resource.split("/")[1])
    elif resource.startswith("role/"):
        ids.append(resource.rsplit("/", 1)[-1])
    return list(dict.fromkeys(ids))


def _sigterm_as_interrupt() -> None:
    """hub/orchestrate.py stops a step with SIGTERM: treat it like Ctrl-C so finally-cleanup runs."""
    def handler(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError, AttributeError):  # not the main thread / platform without SIGTERM
        pass


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    regions = [r for r in parse_regions(args.regions) if r != "global"]
    if not regions or args.timeout_min <= 0 or args.max_parallel < 1 or args.poll_interval <= 0:
        log("ERROR: need at least one real region, --timeout-min > 0, --max-parallel >= 1, --poll-interval > 0")
        return EXIT_ERROR
    acct = None
    try:
        session = get_session(args)
        ident = client(session, "sts", "us-east-1").get_caller_identity()
        acct = ident["Account"]
        protect = protected_identities(ident["Arn"])
        log(f"cfn_predelete: account {acct}, regions {','.join(regions)}, "
            f"{'APPLY' if args.apply else 'dry-run'}, timeout {args.timeout_min:g} min")
        payload = execute(
            cfn_clients={r: client(session, "cloudformation", r) for r in regions},
            iam=client(session, "iam", regions[0]), account=acct, partition=_partition(session, regions[0]),
            apply=args.apply, cli_role_arn=args.cfn_role_arn, timeout_s=args.timeout_min * 60,
            max_parallel=args.max_parallel, poll_interval=args.poll_interval, protect_ids=protect)
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        write_json(args.json_out, {"module": MODULE, "account_id": acct, "generated_at": now_iso(),
                                   "applied": bool(args.apply), "status": "ERROR", "exit_code": EXIT_ERROR,
                                   "error": f"{type(e).__name__}: {e}", "errors": [], "results": []})
        return EXIT_ERROR
    print(render_table(payload))
    write_json(args.json_out, payload)
    return payload["exit_code"]


if __name__ == "__main__":
    _sigterm_as_interrupt()
    sys.exit(main())
