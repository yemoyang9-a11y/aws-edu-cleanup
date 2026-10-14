"""Test helpers for killswitch tests (imported by conftest.py and the test files).

moto does not implement IAM service-specific credentials (Bedrock API keys), so
FakeServiceSpecificCredentials answers ListServiceSpecificCredentials /
UpdateServiceSpecificCredential through a botocore ``before-call`` hook.
"""
import importlib.util
import json
from pathlib import Path

KS = Path(__file__).resolve().parents[1]
ROOT = KS.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Http:
    def __init__(self, status):
        self.status_code = status
        self.headers = {}
        self.content = b""
        self.raw = None


class FakeServiceSpecificCredentials:
    """In-memory Bedrock API keys, paged 2 per response to exercise Marker handling."""

    PAGE = 2

    def __init__(self):
        self.creds = {}  # user -> list of dicts
        self.calls = []

    def add(self, user, cred_id, service="bedrock.amazonaws.com", status="Active"):
        self.creds.setdefault(user, []).append({
            "UserName": user, "ServiceName": service, "ServiceSpecificCredentialId": cred_id,
            "Status": status, "ServiceUserName": "%s-at-123" % user,
        })

    def status(self, user, cred_id):
        return next(c["Status"] for c in self.creds[user] if c["ServiceSpecificCredentialId"] == cred_id)

    def attach(self, iam_client):
        iam_client.meta.events.register("before-call.iam.ListServiceSpecificCredentials", self._list)
        iam_client.meta.events.register("before-call.iam.UpdateServiceSpecificCredential", self._update)
        return iam_client

    def _list(self, params, **_):
        body = params["body"]
        self.calls.append(("list", dict(body)))
        items = self.creds.get(body.get("UserName"), [])
        start = int(body.get("Marker") or 0)
        page = items[start:start + self.PAGE]
        more = start + self.PAGE < len(items)
        out = {"ServiceSpecificCredentials": [dict(c) for c in page], "IsTruncated": more}
        if more:
            out["Marker"] = str(start + self.PAGE)
        return _Http(200), out

    def _update(self, params, **_):
        body = params["body"]
        self.calls.append(("update", dict(body)))
        for c in self.creds.get(body.get("UserName"), []):
            if c["ServiceSpecificCredentialId"] == body["ServiceSpecificCredentialId"]:
                c["Status"] = body["Status"]
                return _Http(200), {}
        return _Http(404), {"Error": {"Code": "NoSuchEntity", "Message": "no such credential"}}


def alarm_event(state="ALARM", name="edu-killswitch-us-east-1-bedrock-tokens", **extra):
    ev = {"Records": [{"EventSource": "aws:sns", "Sns": {
        "Message": json.dumps({"AlarmName": name, "NewStateValue": state,
                               "NewStateReason": "Threshold Crossed: 1 datapoint [912345.0] >= 500000.0"}),
        "MessageAttributes": {}}}]}
    ev.update(extra)
    return ev


def api_event(event_name, user_identity, request=None, source="iam.amazonaws.com", error=None, **extra):
    detail = {"eventSource": source, "eventName": event_name, "userIdentity": user_identity,
              "requestParameters": request, "awsRegion": "us-east-1"}
    if error:
        detail["errorCode"] = error
    ev = {"detail-type": "AWS API Call via CloudTrail", "source": "aws." + source.split(".")[0],
          "detail": detail}
    ev.update(extra)
    return ev


def iam_user(name):
    return {"type": "IAMUser", "userName": name, "arn": "arn:aws:iam::123456789012:user/" + name}
