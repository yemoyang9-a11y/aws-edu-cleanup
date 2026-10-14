"""Test helpers for cfn_predelete (no tests here).

``FakeCfn`` is a small stateful CloudFormation backend plugged into a *real*
boto3 client through botocore's ``before-call`` event. Request serialisation and
parameter validation against the installed service model still run (so a wrong
``DeploymentConfig`` shape fails exactly like it would against AWS), paginators
work, and errors surface as real ``ClientError``s. moto's CloudFormation backend
is not used because it ignores DeploymentConfig / DeletionMode / RoleARN checks.
"""
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.awsrequest import AWSResponse

MODULE_DIR = Path(__file__).resolve().parents[1]
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import predelete_stacks as ps  # noqa: E402

ACCOUNT = "123456789012"
READ_ONLY_CFN = {"DescribeStacks", "ListExports", "ListImports", "DescribeStackResources"}
CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing", "aws_session_token": "testing"}


def stack_id(region: str, name: str) -> str:
    return f"arn:aws:cloudformation:{region}:{ACCOUNT}:stack/{name}/{abs(hash(name)) % 10**8:08d}"


class FakeClock:
    """Monotonic clock whose sleep() just advances time.

    Time is tracked per thread: region workers sleep in parallel in reality, so one
    region's sleeps must not make time pass for another region.
    """

    def __init__(self) -> None:
        self.base = 1000.0
        self.lock = threading.Lock()
        self.elapsed: Dict[int, float] = {}
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        with self.lock:
            return self.base + self.elapsed.get(threading.get_ident(), 0.0)

    @property
    def now(self) -> float:
        with self.lock:
            return self.base + max(self.elapsed.values(), default=0.0)

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            tid = threading.get_ident()
            self.elapsed[tid] = self.elapsed.get(tid, 0.0) + max(seconds, 0.001)


class FakeCfn:
    """In-memory CloudFormation for one region.

    Per-stack knobs (keyword args of ``add``):
      polls_to_delete  number of DescribeStacks polls in DELETE_IN_PROGRESS before it is gone
      fail_delete      a STANDARD delete ends in DELETE_FAILED (FORCE_DELETE_STACK then succeeds)
      never_finishes   stays DELETE_IN_PROGRESS forever
    Global knobs:
      express_supported  False -> DeleteStack with DeploymentConfig -> ValidationError
      existing_roles     role ARNs CloudFormation can assume; a stack whose effective role is
                         not in it is rejected with "Role ... is invalid or cannot be assumed"
    """

    def __init__(self, region: str, *, express_supported: bool = True, page_size: int = 2,
                 existing_roles: Optional[set] = None) -> None:
        self.region = region
        self.express_supported = express_supported
        self.page_size = page_size
        self.existing_roles = existing_roles if existing_roles is not None else set()
        self.role_ok = lambda arn: arn in self.existing_roles   # tests may replace (e.g. ask moto IAM)
        self.stacks: Dict[str, Dict[str, Any]] = {}
        self.knobs: Dict[str, Dict[str, Any]] = {}
        self.exports: List[Dict[str, Any]] = []
        self.imports: Dict[str, List[str]] = {}
        self.owned: Dict[str, str] = {}            # physical id -> stack id
        self.calls: List[tuple] = []
        self.deleted_order: List[str] = []
        self.lock = threading.Lock()

    # ---- setup
    def add(self, name: str, *, status: str = "CREATE_COMPLETE", parent: Optional[str] = None,
            role: Optional[str] = None, tp: bool = False, outputs: Optional[list] = None,
            description: Optional[str] = None, **knobs: Any) -> str:
        sid = stack_id(self.region, name)
        st: Dict[str, Any] = {"StackId": sid, "StackName": name, "StackStatus": status,
                              "CreationTime": "2026-10-01T00:00:00Z", "EnableTerminationProtection": tp}
        if parent:
            st["ParentId"] = stack_id(self.region, parent)
            st["RootId"] = st["ParentId"]
        if role:
            st["RoleARN"] = role
        if outputs:
            st["Outputs"] = outputs
        if description:
            st["Description"] = description
        self.stacks[sid] = st
        self.knobs[sid] = dict(knobs, polls=0)
        return sid

    def export(self, exporter: str, export_name: str, importers: List[str]) -> None:
        self.exports.append({"ExportingStackId": stack_id(self.region, exporter), "Name": export_name,
                             "Value": "v"})
        self.imports[export_name] = list(importers)

    def own(self, physical_id: str, stack_name: str) -> None:
        self.owned[physical_id] = stack_id(self.region, stack_name)

    # ---- plumbing
    def client(self, session: Optional[boto3.Session] = None):
        sess = session or boto3.Session(region_name=self.region, **CREDS)
        c = sess.client("cloudformation", region_name=self.region)
        c.meta.events.register("before-parameter-build.cloudformation", self._record)
        c.meta.events.register("before-call.cloudformation", self._dispatch)
        return c

    def _record(self, params: Dict[str, Any], model, context: Dict[str, Any], **_: Any) -> None:
        rec = dict(params)
        context["fake_params"] = rec               # same context dict is passed to before-call
        with self.lock:
            self.calls.append((model.name, rec))

    def ops(self) -> List[str]:
        return [op for op, _ in self.calls]

    def calls_of(self, op: str) -> List[Dict[str, Any]]:
        return [p for o, p in self.calls if o == op]

    @staticmethod
    def _ok(body: Dict[str, Any]):
        body.setdefault("ResponseMetadata", {"HTTPStatusCode": 200})
        return AWSResponse("https://fake", 200, {}, None), body

    @staticmethod
    def _fail(code: str, message: str):
        return (AWSResponse("https://fake", 400, {}, None),
                {"Error": {"Code": code, "Message": message, "Type": "Sender"},
                 "ResponseMetadata": {"HTTPStatusCode": 400}})

    def _find(self, name_or_id: str) -> Optional[Dict[str, Any]]:
        if name_or_id in self.stacks:
            return self.stacks[name_or_id]
        for st in self.stacks.values():
            if st["StackName"] == name_or_id:
                return st
        return None

    def _dispatch(self, model, params: Dict[str, Any], context: Dict[str, Any], **_: Any):
        with self.lock:
            original = context.get("fake_params", {})   # un-serialized API params
            # parameters injected into the serialized query body (botocore without the model member)
            for key, value in (params.get("body") or {}).items():
                if key in ("Action", "Version"):
                    continue
                head, *rest = key.split(".")
                if head in original and not rest:
                    continue
                if rest:
                    original.setdefault(head, {})[rest[0]] = value
                else:
                    original[head] = value
            handler = getattr(self, "_op_" + model.name, None)
            if handler is None:
                raise AssertionError(f"FakeCfn: unexpected operation {model.name}")
            return handler(original)

    # ---- operations
    def _op_DescribeStacks(self, p: Dict[str, Any]):
        if p.get("StackName"):
            st = self._find(p["StackName"])
            if st is None:
                return self._fail("ValidationError", f"Stack with id {p['StackName']} does not exist")
            k = self.knobs[st["StackId"]]
            if st["StackStatus"] == "DELETE_IN_PROGRESS" and not k.get("never_finishes"):
                k["polls"] += 1
                if k["polls"] >= k.get("polls_to_delete", 1):
                    if k.get("fail_delete") and not k.get("forced"):
                        st["StackStatus"] = "DELETE_FAILED"
                        st["StackStatusReason"] = "The following resource(s) failed to delete: [Bucket]."
                    else:
                        del self.stacks[st["StackId"]]
                        self.deleted_order.append(st["StackName"])
                        if p["StackName"].startswith("arn:"):
                            return self._ok({"Stacks": [dict(st, StackStatus="DELETE_COMPLETE")]})
                        return self._fail("ValidationError", f"Stack with id {p['StackName']} does not exist")
            return self._ok({"Stacks": [dict(st)]})
        items = list(self.stacks.values())
        start = int(p.get("NextToken") or 0)
        page = items[start:start + self.page_size]
        body: Dict[str, Any] = {"Stacks": [dict(s) for s in page]}
        if start + self.page_size < len(items):
            body["NextToken"] = str(start + self.page_size)
        return self._ok(body)

    def _op_ListExports(self, p: Dict[str, Any]):
        live = [e for e in self.exports if e["ExportingStackId"] in self.stacks]
        return self._ok({"Exports": live})

    def _op_ListImports(self, p: Dict[str, Any]):
        names = [n for n in self.imports.get(p["ExportName"], []) if self._find(n)]
        if not names:
            return self._fail("ValidationError", f"Export '{p['ExportName']}' is not imported by any stack.")
        return self._ok({"Imports": names})

    def _op_DescribeStackResources(self, p: Dict[str, Any]):
        sid = self.owned.get(p.get("PhysicalResourceId", ""))
        if not sid or sid not in self.stacks:
            return self._fail("ValidationError", f"Stack for {p.get('PhysicalResourceId')} does not exist")
        return self._ok({"StackResources": [{
            "StackName": self.stacks[sid]["StackName"], "StackId": sid, "LogicalResourceId": "R",
            "PhysicalResourceId": p["PhysicalResourceId"], "ResourceType": "AWS::IAM::Role",
            "Timestamp": "2026-10-01T00:00:00Z", "ResourceStatus": "CREATE_COMPLETE"}]})

    def _op_UpdateTerminationProtection(self, p: Dict[str, Any]):
        st = self._find(p["StackName"])
        if st is None:
            return self._fail("ValidationError", f"Stack [{p['StackName']}] does not exist")
        st["EnableTerminationProtection"] = p["EnableTerminationProtection"]
        return self._ok({"StackId": st["StackId"]})

    def _op_DeleteStack(self, p: Dict[str, Any]):
        if "DeploymentConfig" in p and not self.express_supported:
            return self._fail("ValidationError", "Unrecognized parameter: DeploymentConfig")
        st = self._find(p["StackName"])
        if st is None:
            return self._ok({})                 # AWS: deleting a missing stack is a no-op
        if st["EnableTerminationProtection"]:
            return self._fail("ValidationError",
                              f"Stack [{st['StackName']}] cannot be deleted while TerminationProtection is enabled")
        role = p.get("RoleARN") or st.get("RoleARN")
        if role and not self.role_ok(role):
            return self._fail("ValidationError", f"Role {role} is invalid or cannot be assumed")
        k = self.knobs[st["StackId"]]
        if p.get("DeletionMode") == "FORCE_DELETE_STACK":
            if st["StackStatus"] != "DELETE_FAILED":
                return self._fail("ValidationError", "FORCE_DELETE_STACK requires DELETE_FAILED")
            k["forced"] = True
        if st["StackStatus"] == "DELETE_IN_PROGRESS":
            return self._ok({})
        st["StackStatus"] = "DELETE_IN_PROGRESS"
        k["polls"] = 0
        k.setdefault("delete_calls", []).append(dict(p))
        return self._ok({})


def by_name(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {r["stack_name"]: r for r in payload["results"]}


def run(fakes: Dict[str, FakeCfn], iam, *, apply: bool = True, clock: Optional[FakeClock] = None,
        **kw: Any) -> Dict[str, Any]:
    clock = clock or FakeClock()
    kw.setdefault("timeout_s", 1800.0)
    kw.setdefault("poll_interval", 15.0)
    kw.setdefault("propagation_delay", 0.0)
    return ps.execute(cfn_clients={r: f.client() for r, f in fakes.items()}, iam=iam, account=ACCOUNT,
                      apply=apply, sleep=clock.sleep, clock=clock, **kw)
