"""Shared helpers for every edu-cleanup Python script.

Every script imports this module the same way:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.session import add_common_args, get_session, ...

Only boto3/botocore are required (both preinstalled in AWS CloudShell).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from typing import Any, Iterable

import boto3
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session as _botocore_session

# ---- Fixed names shared by all modules (see CONTRACTS.md) -------------------
ROLE_NAME = "EduCleanupRole"
BOOTSTRAP_STACK_PREFIX = "edu-bootstrap"
KILLSWITCH_STACK_PREFIX = "edu-killswitch"
DEFAULT_REGIONS = ["ap-northeast-2", "us-east-1"]

# ---- Exit codes shared by all modules --------------------------------------
EXIT_OK = 0        # nothing left to do / clean
EXIT_ISSUES = 1    # ran fine, but problems remain (residual resources, blockers)
EXIT_ERROR = 2     # could not run (bad args, auth failure, unexpected exception)
EXIT_PENDING = 3   # too early to judge (e.g. cost data not settled yet)

# Explicit timeouts: botocore's default 60 s read timeout x 10 attempts lets one unresponsive
# endpoint stall a step for ~10 minutes.
RETRY_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"},
                      connect_timeout=10, read_timeout=30)


def add_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Flags every script accepts, with identical meaning."""
    g = parser.add_argument_group("common")
    g.add_argument("--profile", help="AWS CLI profile to start from (default: env/instance credentials)")
    g.add_argument("--role-arn", help="Assume this role in the target account (e.g. arn:aws:iam::<ID>:role/EduCleanupRole)")
    g.add_argument("--external-id", help="ExternalId for --role-arn")
    g.add_argument("--session-name", default="edu-cleanup", help="STS session name (default: edu-cleanup)")
    g.add_argument("--regions", default=",".join(DEFAULT_REGIONS),
                   help="Comma-separated regions (default: %(default)s)")
    g.add_argument("--json-out", help="Write the machine-readable result to this file")
    g.add_argument("--apply", action="store_true",
                   help="Actually change things. Without it the script only reports (dry-run).")
    return parser


def parse_regions(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    regions = [r.strip() for r in items if r and r.strip()]
    # keep order, drop duplicates
    seen: set[str] = set()
    return [r for r in regions if not (r in seen or seen.add(r))]


def _assume(base: boto3.Session, role_arn: str, external_id: str | None, session_name: str) -> dict[str, str]:
    sts = base.client("sts", config=RETRY_CONFIG)
    kwargs: dict[str, Any] = {"RoleArn": role_arn, "RoleSessionName": session_name[:64], "DurationSeconds": 3600}
    if external_id:
        kwargs["ExternalId"] = external_id
    creds = sts.assume_role(**kwargs)["Credentials"]
    return {
        "access_key": creds["AccessKeyId"],
        "secret_key": creds["SecretAccessKey"],
        "token": creds["SessionToken"],
        "expiry_time": creds["Expiration"].isoformat(),
    }


def get_session(args: argparse.Namespace | None = None, *, profile: str | None = None,
                role_arn: str | None = None, external_id: str | None = None,
                session_name: str = "edu-cleanup") -> boto3.Session:
    """Return a boto3 Session.

    With a role ARN the session uses auto-refreshing assumed-role credentials,
    so runs longer than the 1-hour role-chaining limit keep working.
    """
    if args is not None:
        profile = getattr(args, "profile", None)
        role_arn = getattr(args, "role_arn", None)
        external_id = getattr(args, "external_id", None)
        session_name = getattr(args, "session_name", session_name) or session_name

    base = boto3.Session(profile_name=profile) if profile else boto3.Session()
    if not role_arn:
        return base

    refreshable = RefreshableCredentials.create_from_metadata(
        metadata=_assume(base, role_arn, external_id, session_name),
        refresh_using=lambda: _assume(base, role_arn, external_id, session_name),
        method="sts-assume-role",
    )
    bc = _botocore_session()
    bc._credentials = refreshable  # documented workaround for refreshable creds in boto3
    return boto3.Session(botocore_session=bc)


def client(session: boto3.Session, service: str, region: str | None = None):
    return session.client(service, region_name=region, config=RETRY_CONFIG)


def account_id(session: boto3.Session) -> str:
    return client(session, "sts", "us-east-1").get_caller_identity()["Account"]


def caller_arn(session: boto3.Session) -> str:
    return client(session, "sts", "us-east-1").get_caller_identity()["Arn"]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: str | None, payload: dict[str, Any]) -> None:
    """Write result JSON (if a path was given). Datetimes become ISO strings."""
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, (_dt.datetime, _dt.date)):
        return o.isoformat()
    return str(o)


def log(msg: str) -> None:
    """Human-readable progress goes to stderr so stdout stays clean."""
    print(msg, file=sys.stderr, flush=True)
