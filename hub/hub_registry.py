"""Cohort registry (= allowlist) loading and validation for the hub module.

The registry is the ONLY list of accounts the hub will ever touch. It is a
plain JSON file (no PyYAML in CloudShell):

    {
      "cohort": "2026-10",
      "external_id": "<long random string, same value as in every bootstrap stack>",
      "regions": ["ap-northeast-2", "us-east-1"],
      "max_parallel": 10,
      "killswitch": {"TokenThreshold": 2000000, ...},
      "accounts": [{"account_id": "111111111111", "alias": "edu-2026-10-01", "note": ""}]
    }

Only the standard library is used here so make_quick_create_link.py stays light.
"""
from __future__ import annotations

import json
import re
from typing import Any

ACCOUNT_ID_RE = re.compile(r"^[0-9]{12}$")
# IAM account alias rules: 3-63 chars, lowercase letters, digits, single hyphens.
ALIAS_RE = re.compile(r"^[a-z0-9]([a-z0-9]|-(?!-)){1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z]{2}(-gov|-iso[a-z]*)?-[a-z]+-[0-9]$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9+=,.@:/_-]+$")
COHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLACEHOLDER_PREFIX = "CHANGE-ME"

EXTERNAL_ID_MIN = 8
EXTERNAL_ID_MAX = 1224
MAX_PARALLEL_LIMIT = 64
DEFAULT_MAX_PARALLEL = 10

# Parameter names of killswitch/killswitch.yaml (see CONTRACTS / hub README).
KILLSWITCH_KEYS = (
    "TokenThreshold",
    "PeriodMinutes",
    "TargetMode",
    "TargetGroupName",
    "ExemptUserNames",
    "NotificationEmail",
    "EnableApiTriggers",
)


class RegistryError(ValueError):
    """Raised when registry.json is unusable. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid registry:\n  - " + "\n  - ".join(problems))


def contains_prod(value: str) -> bool:
    return "prod" in (value or "").lower()


def validate_registry(data: Any) -> dict[str, Any]:
    """Validate and normalise a registry dict. Raises RegistryError listing all problems."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise RegistryError(["registry must be a JSON object"])

    cohort = data.get("cohort")
    if not isinstance(cohort, str) or not COHORT_RE.match(cohort):
        problems.append("cohort must be a short string of letters/digits/._- (e.g. '2026-10'); "
                        f"got {cohort!r}")

    ext = data.get("external_id")
    if not isinstance(ext, str):
        problems.append("external_id must be a string")
    else:
        if len(ext) < EXTERNAL_ID_MIN:
            problems.append(f"external_id must be at least {EXTERNAL_ID_MIN} characters (got {len(ext)})")
        if len(ext) > EXTERNAL_ID_MAX:
            problems.append(f"external_id must be at most {EXTERNAL_ID_MAX} characters")
        if ext and not EXTERNAL_ID_RE.match(ext):
            problems.append("external_id may only contain letters, digits and +=,.@:/_-")
        if ext.upper().startswith(PLACEHOLDER_PREFIX):
            problems.append("external_id is still the placeholder 'CHANGE-ME...'; "
                            "generate one, e.g. python3 -c 'import secrets;print(secrets.token_urlsafe(32))'")

    regions = data.get("regions")
    norm_regions: list[str] = []
    if not isinstance(regions, list) or not regions:
        problems.append("regions must be a non-empty list, e.g. [\"ap-northeast-2\", \"us-east-1\"]")
    else:
        for r in regions:
            if r == "global":
                problems.append("regions must not contain 'global' (the nuke module adds it by itself)")
            elif not isinstance(r, str) or not REGION_RE.match(r):
                problems.append(f"invalid region {r!r}")
            elif r not in norm_regions:
                norm_regions.append(r)

    max_parallel = data.get("max_parallel", DEFAULT_MAX_PARALLEL)
    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) \
            or not 1 <= max_parallel <= MAX_PARALLEL_LIMIT:
        problems.append(f"max_parallel must be an integer 1..{MAX_PARALLEL_LIMIT} (got {max_parallel!r})")

    killswitch = data.get("killswitch")
    if killswitch is not None:
        if not isinstance(killswitch, dict):
            problems.append("killswitch must be an object")
        else:
            unknown = sorted(set(killswitch) - set(KILLSWITCH_KEYS))
            if unknown:
                problems.append(f"killswitch has unknown keys {unknown}; allowed: {list(KILLSWITCH_KEYS)}")

    accounts = data.get("accounts")
    norm_accounts: list[dict[str, str]] = []
    if not isinstance(accounts, list) or not accounts:
        problems.append("accounts must be a non-empty list")
        accounts = []
    seen_ids: set[str] = set()
    seen_aliases: set[str] = set()
    for i, acct in enumerate(accounts):
        where = f"accounts[{i}]"
        if not isinstance(acct, dict):
            problems.append(f"{where} must be an object")
            continue
        aid = acct.get("account_id")
        if isinstance(aid, int) and not isinstance(aid, bool):
            problems.append(f"{where}.account_id must be a quoted string (\"{aid:012d}\"), not a number "
                            "(numbers lose leading zeros)")
            continue
        if not isinstance(aid, str) or not ACCOUNT_ID_RE.match(aid):
            problems.append(f"{where}.account_id must be exactly 12 digits (got {aid!r})")
            continue
        if aid in seen_ids:
            problems.append(f"{where}.account_id {aid} is duplicated")
            continue
        seen_ids.add(aid)

        alias = acct.get("alias", "") or ""
        if not isinstance(alias, str):
            problems.append(f"{where}.alias must be a string")
            alias = ""
        elif alias:
            if contains_prod(alias):
                problems.append(f"{where}.alias {alias!r} contains 'prod' (forbidden: aws-nuke refuses it "
                                "and it signals a non-training account)")
            elif not ALIAS_RE.match(alias):
                problems.append(f"{where}.alias {alias!r} is not a valid IAM account alias "
                                "(3-63 chars, lowercase letters, digits, single hyphens)")
            elif alias in seen_aliases:
                problems.append(f"{where}.alias {alias!r} is duplicated")
            seen_aliases.add(alias)
        note = acct.get("note", "") or ""
        norm_accounts.append({"account_id": aid, "alias": alias, "note": str(note)})

    if problems:
        raise RegistryError(problems)

    return {
        "cohort": cohort,
        "external_id": ext,
        "regions": norm_regions,
        "max_parallel": max_parallel,
        "killswitch": dict(killswitch) if isinstance(killswitch, dict) else None,
        "accounts": norm_accounts,
    }


def load_registry(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise RegistryError([f"registry file not found: {path}"]) from None
    except json.JSONDecodeError as e:
        raise RegistryError([f"registry is not valid JSON: {e}"]) from None
    return validate_registry(data)


def select_accounts(registry: dict[str, Any], account_ids: str | None) -> list[dict[str, str]]:
    """Return registry accounts, optionally narrowed by a comma-separated id list.

    Ids that are not in the registry are an error: the registry is the allowlist.
    """
    accounts = registry["accounts"]
    if not account_ids:
        return list(accounts)
    wanted = [a.strip() for a in account_ids.split(",") if a.strip()]
    by_id = {a["account_id"]: a for a in accounts}
    missing = [a for a in wanted if a not in by_id]
    if missing:
        raise RegistryError([f"--accounts contains ids that are not in the registry: {missing}"])
    seen: set[str] = set()
    return [by_id[a] for a in wanted if not (a in seen or seen.add(a))]
