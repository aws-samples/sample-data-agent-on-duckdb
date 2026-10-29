"""scripts/setup_lakeformation.py — the planning half, no AWS: the shipped
declarative file validates, converging only ever ADDS unless --prune, and the
LF_GOVERNANCE it prints is what the runtime's loader accepts."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("setup_lf", ROOT / "scripts/setup_lakeformation.py")
lfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lfs)

ACCT = "111122223333"


def _cfg():
    return lfs.load_config(lfs.CONFIG, ACCT)


def test_the_shipped_file_loads_and_substitutes_the_account():
    cfg = _cfg()
    assert cfg["session_tag"] == "data-agent"
    assert cfg["tables"]["eth_traces"]["catalog_id"] == f"{ACCT}:s3tablescatalog/data-agent-demo"
    junior = cfg["personas"]["exchange-a:junior"]["filters"]["eth_traces"]
    assert junior["exclude_columns"] == ["block_timestamp"]
    assert junior["row_filter"].startswith('"date"')  # PartiQL: reserved name quoted


def test_the_printed_env_is_what_the_runtime_loader_accepts(monkeypatch):
    env = lfs.lf_governance(_cfg(), ACCT)
    monkeypatch.setenv("LF_GOVERNANCE", json.dumps(env))
    from data_agent.governance import lakeformation

    cfg = lakeformation._load_config()  # the runtime's own validation, fail-stop on a bad value
    assert set(cfg["roles"]) == {"exchange-a:investigator", "exchange-a:junior"}
    assert cfg["roles"]["exchange-a:junior"] == f"arn:aws:iam::{ACCT}:role/lf-persona-junior"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda c: c.update(session_tag="has space"), "session_tag"),
        (lambda c: c["tables"]["eth_traces"].pop("table_bucket"), "missing"),
        (lambda c: c["personas"].update({"noseparator": {"role_name": "r"}}), "tenant:role"),
        (
            lambda c: c["personas"]["exchange-a:junior"]["filters"].update(nope={"name": "x"}),
            "unknown",
        ),
        (
            lambda c: c["personas"]["exchange-a:junior"]["filters"]["eth_traces"].update(
                row_filter=""
            ),
            "row_filter",
        ),
    ],
    ids=["bad-tag", "no-bucket", "bad-persona", "unknown-table", "empty-filter"],
)
def test_a_malformed_file_refuses(tmp_path, mutate, match):
    import yaml

    raw = yaml.safe_load(lfs.CONFIG.read_text())
    mutate(raw)
    p = tmp_path / "lf.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=match):
        lfs.load_config(p, ACCT)


def test_settings_merge_only_adds():
    cur = {
        "DataLakeAdmins": [{"DataLakePrincipalIdentifier": "arn:aws:iam::1:role/Admin"}],
        "AuthorizedSessionTagValueList": ["other"],
    }
    new, changes = lfs.merge_settings(cur, ACCT, "data-agent")
    assert new["DataLakeAdmins"] == cur["DataLakeAdmins"]  # never touched
    assert new["AuthorizedSessionTagValueList"] == ["other", "data-agent"]
    assert new["AllowExternalDataFiltering"] is True and len(changes) == 3
    again, none = lfs.merge_settings(new, ACCT, "data-agent")
    assert none == [] and again == new


def test_converging_never_silently_removes_a_permission():
    """Measured 2026-10-29: a run without the Athena flag stripped the
    idempotence check's grants. Additive by default; --prune to remove."""
    want = lfs.read_policy("us-west-2", ACCT, ["b"], None)
    have = {"Statement": [*want["Statement"], {"Sid": "AthenaResults", "Effect": "Allow"}]}
    kept = lfs.merge_statements(have, want, prune=False)
    assert any(st.get("Sid") == "AthenaResults" for st in kept["Statement"])
    assert not any(
        st.get("Sid") == "AthenaResults"
        for st in lfs.merge_statements(have, want, prune=True)["Statement"]
    )
    trust = {"Statement": [{"Principal": {"AWS": ["arn:aws:iam::1:role/worker"]}}]}
    assert lfs.existing_principals(trust) == ["arn:aws:iam::1:role/worker"]
    fetch_want = {"Statement": [{"Resource": ["arn:r/a"]}]}
    fetch_have = {"Statement": [{"Resource": "arn:r/b"}]}
    merged = lfs.merge_resources(fetch_have, fetch_want, prune=False)["Statement"][0]
    assert merged["Resource"] == ["arn:r/a", "arn:r/b"]
    assert lfs.merge_resources(fetch_have, fetch_want, prune=True) == fetch_want


def test_trust_requires_the_session_tag():
    doc = lfs.trust_policy(["arn:b", "arn:a"], "data-agent")
    tag_stmt = doc["Statement"][1]
    assert tag_stmt["Action"] == "sts:TagSession"
    cond = tag_stmt["Condition"]["StringEquals"]
    assert cond == {"aws:RequestTag/LakeFormationAuthorizedCaller": "data-agent"}
    assert doc["Statement"][0]["Principal"]["AWS"] == ["arn:a", "arn:b"]


def test_filter_diff_and_iam_doc_normalization():
    t = _cfg()["tables"]["eth_traces"]
    f = _cfg()["personas"]["exchange-a:junior"]["filters"]["eth_traces"]
    body = lfs.filter_payload(t, f)
    same = {
        "RowFilter": {"FilterExpression": f["row_filter"]},
        "ColumnWildcard": {"ExcludedColumnNames": ["block_timestamp"]},
    }
    assert not lfs.filter_differs(same, body)
    assert lfs.filter_differs({**same, "RowFilter": {"FilterExpression": "true"}}, body)
    # IAM collapses one-element lists and reorders: still the same document
    assert lfs._same_doc({"A": ["x"], "B": ["2", "1"]}, {"A": "x", "B": ["1", "2"]})
    assert not lfs._same_doc(None, {"A": 1})
