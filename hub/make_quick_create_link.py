#!/usr/bin/env python3
"""Print CloudFormation quick-create links that install EduCleanupRole (stack edu-bootstrap).

Open each link while signed in as ROOT of the student account it is meant for, tick
"I acknowledge that AWS CloudFormation might create IAM resources with custom names",
and click "Create stack". The equivalent `aws cloudformation create-stack` line is
printed too (e.g. for CloudShell opened as root in that account).

Single account:
    python3 hub/make_quick_create_link.py \
        --template-url https://my-bucket.s3.ap-northeast-2.amazonaws.com/edu/bootstrap.yaml \
        --hub-principal-arn arn:aws:iam::999999999999:role/EduHubOrchestrator \
        --external-id "$EXT_ID" --alias edu-2026-10-01

Whole cohort (ExternalId and aliases come from the registry):
    python3 hub/make_quick_create_link.py --registry registry.json \
        --template-url https://... --hub-principal-arn arn:aws:iam::999999999999:role/EduHubOrchestrator

Standard library only (plus the stdlib-only hub_registry helper).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import argparse  # noqa: E402
import csv  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shlex  # noqa: E402
from urllib.parse import quote, urlsplit  # noqa: E402

from common.session import BOOTSTRAP_STACK_PREFIX, EXIT_ERROR, EXIT_OK, log  # noqa: E402
from hub_registry import (  # noqa: E402
    ALIAS_RE, EXTERNAL_ID_MIN, EXTERNAL_ID_RE, REGION_RE, RegistryError, contains_prod, load_registry,
)

STACK_NAME = BOOTSTRAP_STACK_PREFIX  # "edu-bootstrap"
DEFAULT_REGION = "ap-northeast-2"
HUB_PRINCIPAL_RE = re.compile(r"^arn:aws(-cn|-us-gov)?:iam::[0-9]{12}:(role|user)/[\w+=,.@/-]+$")
# Console routes: "quickcreate" (current console) and "create/review" (form shown in the AWS docs).
ROUTES = {"quickcreate": "stacks/quickcreate", "review": "stacks/create/review"}

NOECHO_WARNING = (
    "NOTE: ExternalId is a NoEcho parameter. AWS documents that quick-create links IGNORE NoEcho\n"
    "      parameters, so param_ExternalId may not be pre-filled. If the ExternalId field is empty,\n"
    "      paste the cohort ExternalId into it before clicking Create stack (or use the CLI line)."
)


def _validate_template_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc.endswith(".amazonaws.com") or "s3" not in parts.netloc \
            or not parts.path.strip("/"):
        raise ValueError(f"--template-url must be an https S3 object URL "
                         f"(https://<bucket>.s3.<region>.amazonaws.com/<key>), got {url!r}")


def _validate_inputs(template_url: str, hub_principal_arn: str, external_id: str, alias: str, region: str) -> None:
    _validate_template_url(template_url)
    if not HUB_PRINCIPAL_RE.match(hub_principal_arn or ""):
        raise ValueError(f"--hub-principal-arn must be an IAM role/user ARN, got {hub_principal_arn!r}")
    if not external_id or len(external_id) < EXTERNAL_ID_MIN or not EXTERNAL_ID_RE.match(external_id):
        raise ValueError(f"external id must be >= {EXTERNAL_ID_MIN} chars of letters, digits and +=,.@:/_-")
    if alias:
        if contains_prod(alias):
            raise ValueError(f"alias {alias!r} must not contain 'prod'")
        if not ALIAS_RE.match(alias):
            raise ValueError(f"alias {alias!r} is not a valid IAM account alias")
    if not REGION_RE.match(region or ""):
        raise ValueError(f"invalid region {region!r}")


def build_quick_create_url(template_url: str, hub_principal_arn: str, external_id: str, alias: str = "",
                           region: str = DEFAULT_REGION, route: str = "quickcreate") -> str:
    """Return the console quick-create URL. Every value is fully percent-encoded."""
    _validate_inputs(template_url, hub_principal_arn, external_id, alias, region)
    query = [
        ("templateURL", template_url),
        ("stackName", STACK_NAME),
        ("param_HubPrincipalArn", hub_principal_arn),
        ("param_ExternalId", external_id),
        ("param_AccountAlias", alias or ""),
    ]
    fragment = "&".join(f"{k}={quote(v, safe='')}" for k, v in query)
    region_q = quote(region, safe="")
    return (f"https://{region_q}.console.aws.amazon.com/cloudformation/home?region={region_q}"
            f"#/{ROUTES[route]}?{fragment}")


def build_cli_command(template_url: str, hub_principal_arn: str, external_id: str, alias: str = "",
                      region: str = DEFAULT_REGION) -> str:
    """Return the equivalent `aws cloudformation create-stack` command (shell-quoted)."""
    _validate_inputs(template_url, hub_principal_arn, external_id, alias, region)
    params = [
        {"ParameterKey": "HubPrincipalArn", "ParameterValue": hub_principal_arn},
        {"ParameterKey": "ExternalId", "ParameterValue": external_id},
        {"ParameterKey": "AccountAlias", "ParameterValue": alias or ""},
    ]
    argv = [
        "aws", "cloudformation", "create-stack",
        "--region", region,
        "--stack-name", STACK_NAME,
        "--template-url", template_url,
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--enable-termination-protection",
        "--parameters", json.dumps(params, separators=(",", ":")),
    ]
    return " ".join(shlex.quote(a) for a in argv)


def build_bucket_policy(template_url: str, account_ids: list) -> dict:
    """Bucket policy letting the given student accounts read ONLY the template object."""
    _validate_template_url(template_url)
    parts = urlsplit(template_url)
    host, path = parts.netloc, parts.path.lstrip("/")
    if host.startswith("s3.") or host.startswith("s3-"):      # path-style: s3.<region>.amazonaws.com/<bucket>/<key>
        bucket, _, key = path.partition("/")
    else:                                                       # virtual-hosted: <bucket>.s3.<region>.amazonaws.com/<key>
        bucket, key = host.split(".s3")[0], path
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "EduStudentAccountsReadBootstrapTemplate",
            "Effect": "Allow",
            "Principal": {"AWS": [f"arn:aws:iam::{a}:root" for a in account_ids]},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{bucket}/{key}",
        }],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Print CloudFormation quick-create links for the edu-bootstrap stack.")
    p.add_argument("--template-url", required=True, help="https S3 URL of hub/bootstrap.yaml")
    p.add_argument("--hub-principal-arn", required=True, help="ARN of the hub role/user that runs orchestrate.py")
    p.add_argument("--external-id", help="ExternalId (default with --registry: registry external_id)")
    p.add_argument("--alias", default="", help="Account alias for a single account (e.g. edu-2026-10-01)")
    p.add_argument("--region", default=DEFAULT_REGION, help="Region to create the stack in (default: %(default)s)")
    p.add_argument("--registry", help="registry.json: print one link per account (alias from the registry)")
    p.add_argument("--accounts", help="With --registry: only these comma-separated account ids")
    p.add_argument("--route", choices=sorted(ROUTES), default="quickcreate",
                   help="Console route: quickcreate (default) or review (#/stacks/create/review, as in AWS docs)")
    p.add_argument("--csv", help="With --registry: also write account_id,alias,link,cli to this CSV (Excel-friendly)")
    p.add_argument("--bucket-policy", action="store_true",
                   help="With --registry: also print an S3 bucket policy granting the accounts read access "
                        "to the template object")
    args = p.parse_args(argv)

    try:
        if args.registry:
            reg = load_registry(args.registry)
            external_id = args.external_id or reg["external_id"]
            accounts = reg["accounts"]
            if args.accounts:
                wanted = {a.strip() for a in args.accounts.split(",") if a.strip()}
                unknown = wanted - {a["account_id"] for a in accounts}
                if unknown:
                    raise ValueError(f"--accounts not in registry: {sorted(unknown)}")
                accounts = [a for a in accounts if a["account_id"] in wanted]
            rows = []
            for a in accounts:
                rows.append({
                    "account_id": a["account_id"],
                    "alias": a["alias"],
                    "link": build_quick_create_url(args.template_url, args.hub_principal_arn, external_id,
                                                   a["alias"], args.region, args.route),
                    "cli": build_cli_command(args.template_url, args.hub_principal_arn, external_id,
                                             a["alias"], args.region),
                })
            print(f"# cohort {reg['cohort']}: {len(rows)} account(s). Sign in as ROOT of the listed account, "
                  f"then open its link.")
            for i, r in enumerate(rows, 1):
                print(f"\n# [{i}/{len(rows)}] account {r['account_id']}  alias {r['alias'] or '(none)'}")
                print(r["link"])
                print(f"# CLI (CloudShell as root in {r['account_id']}):")
                print(r["cli"])
            if args.csv:
                with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=["account_id", "alias", "link", "cli"])
                    w.writeheader()
                    w.writerows(rows)
                log(f"wrote {args.csv}")
            if args.bucket_policy:
                print("\n# S3 bucket policy for the template bucket:")
                print(json.dumps(build_bucket_policy(args.template_url, [r["account_id"] for r in rows]),
                                 indent=2))
        else:
            if not args.external_id:
                raise ValueError("--external-id is required without --registry")
            print(build_quick_create_url(args.template_url, args.hub_principal_arn, args.external_id,
                                         args.alias, args.region, args.route))
            print("# CLI equivalent:")
            print(build_cli_command(args.template_url, args.hub_principal_arn, args.external_id,
                                    args.alias, args.region))
    except RegistryError as e:
        log(f"ERROR: {e}")
        return EXIT_ERROR
    except ValueError as e:
        log(f"ERROR: {e}")
        return EXIT_ERROR
    log(NOECHO_WARNING)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
