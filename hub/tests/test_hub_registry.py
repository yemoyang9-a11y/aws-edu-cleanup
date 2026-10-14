import copy
import json

import pytest

import hub_test_support  # noqa: F401  (sets sys.path)
from hub_registry import RegistryError, load_registry, select_accounts, validate_registry

HUB_DIR = hub_test_support.HUB_DIR

BASE = {
    "cohort": "2026-10",
    "external_id": "abcdefgh-ijkl",
    "regions": ["ap-northeast-2", "us-east-1"],
    "max_parallel": 10,
    "killswitch": {"TokenThreshold": 2000000, "PeriodMinutes": 5},
    "accounts": [
        {"account_id": "111111111111", "alias": "edu-2026-10-01", "note": ""},
        {"account_id": "222222222222", "alias": "edu-2026-10-02"},
    ],
}


def reg(**changes):
    d = copy.deepcopy(BASE)
    d.update(changes)
    return d


def problems_of(data):
    with pytest.raises(RegistryError) as ei:
        validate_registry(data)
    return " | ".join(ei.value.problems)


def test_valid_registry_is_normalised():
    r = validate_registry(reg())
    assert r["cohort"] == "2026-10"
    assert [a["account_id"] for a in r["accounts"]] == ["111111111111", "222222222222"]
    assert r["accounts"][1]["note"] == ""
    assert r["regions"] == ["ap-northeast-2", "us-east-1"]


@pytest.mark.parametrize("bad_id", ["11111111111", "1111111111111", "11111111111a", "", None])
def test_account_id_must_be_12_digits(bad_id):
    data = reg(accounts=[{"account_id": bad_id, "alias": "edu-2026-10-01"}])
    assert "12 digits" in problems_of(data)


def test_account_id_as_number_is_rejected_with_hint():
    data = reg(accounts=[{"account_id": 11111111111, "alias": "edu-2026-10-01"}])
    assert "quoted string" in problems_of(data)


def test_duplicate_account_ids_rejected():
    data = reg(accounts=[{"account_id": "111111111111", "alias": "edu-2026-10-01"},
                         {"account_id": "111111111111", "alias": "edu-2026-10-02"}])
    assert "duplicated" in problems_of(data)


@pytest.mark.parametrize("alias", ["edu-prod-01", "PROD-edu", "edu-production", "myprodaccount"])
def test_alias_must_not_contain_prod(alias):
    data = reg(accounts=[{"account_id": "111111111111", "alias": alias}])
    assert "prod" in problems_of(data)


def test_alias_must_be_valid_iam_alias():
    data = reg(accounts=[{"account_id": "111111111111", "alias": "Edu_2026"}])
    assert "not a valid IAM account alias" in problems_of(data)


def test_empty_alias_allowed():
    r = validate_registry(reg(accounts=[{"account_id": "111111111111", "alias": ""}]))
    assert r["accounts"][0]["alias"] == ""


@pytest.mark.parametrize("ext", ["short", "", "1234567"])
def test_external_id_min_length(ext):
    assert "at least 8" in problems_of(reg(external_id=ext))


def test_external_id_placeholder_rejected():
    assert "placeholder" in problems_of(reg(external_id="CHANGE-ME-LONG-RANDOM"))


def test_external_id_charset():
    assert "may only contain" in problems_of(reg(external_id="has spaces in it"))


def test_global_region_and_bad_parallel_rejected():
    p = problems_of(reg(regions=["ap-northeast-2", "global"], max_parallel=0))
    assert "global" in p and "max_parallel" in p


def test_unknown_killswitch_key_rejected():
    assert "unknown keys" in problems_of(reg(killswitch={"Threshold": 1}))


def test_all_problems_reported_together():
    data = reg(external_id="x", accounts=[{"account_id": "1", "alias": "edu-prod"}])
    with pytest.raises(RegistryError) as ei:
        validate_registry(data)
    assert len(ei.value.problems) >= 2


def test_example_registry_shape_except_placeholder():
    data = json.loads((HUB_DIR / "registry.example.json").read_text(encoding="utf-8"))
    # The shipped example must fail ONLY because of the placeholder ExternalId.
    with pytest.raises(RegistryError) as ei:
        validate_registry(data)
    assert len(ei.value.problems) == 1 and "placeholder" in ei.value.problems[0]
    data["external_id"] = "a-real-long-random-value"
    r = validate_registry(data)
    assert set(r["killswitch"]) == {"TokenThreshold", "PeriodMinutes", "TargetMode", "TargetGroupName",
                                    "ExemptUserNames", "NotificationEmail"}


def test_load_registry_errors(tmp_path):
    with pytest.raises(RegistryError, match="not found"):
        load_registry(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(str(bad))


def test_load_registry_accepts_utf8_bom(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(reg()), encoding="utf-8-sig")
    assert load_registry(str(p))["cohort"] == "2026-10"


def test_select_accounts_is_allowlist():
    r = validate_registry(reg())
    assert [a["account_id"] for a in select_accounts(r, None)] == ["111111111111", "222222222222"]
    assert [a["account_id"] for a in select_accounts(r, "222222222222, 111111111111")] == \
        ["222222222222", "111111111111"]
    with pytest.raises(RegistryError, match="not in the registry"):
        select_accounts(r, "999999999999")
