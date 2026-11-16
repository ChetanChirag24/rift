import json
from pathlib import Path

import duckdb
import pytest

from riftdiff import RiftError, diff

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BEFORE = EXAMPLES / "customers_before.csv"
AFTER = EXAMPLES / "customers_after.csv"


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text.strip() + "\n")
    return path


def test_example_counts():
    r = diff(BEFORE, AFTER, "customer_id")
    assert r.summary() == {
        "rows_before": 6,
        "rows_after": 7,
        "rows_added": 2,
        "rows_removed": 1,
        "rows_changed": 2,
        "rows_unchanged": 3,
    }
    assert r.has_differences
    assert not r.has_schema_changes


def test_example_details():
    r = diff(BEFORE, AFTER, "customer_id")
    assert [row["customer_id"] for row in r.added] == [7, 8]
    assert [row["name"] for row in r.removed] == ["Initech"]

    changes = {rc.key["customer_id"]: rc.changes for rc in r.changed}
    assert changes == {
        1: {"revenue": (12000, 13500)},
        2: {"status": ("active", "churned"), "revenue": (8500, 0)},
    }
    assert r.column_change_counts == {"status": 1, "revenue": 2}


def test_identical_files(tmp_path):
    r = diff(BEFORE, BEFORE, "customer_id")
    assert not r.has_differences
    assert r.rows_unchanged == 6
    assert r.added == r.removed == r.changed == []


def test_limit_caps_details_not_counts():
    r = diff(BEFORE, AFTER, "customer_id", limit=1)
    assert r.rows_added == 2 and len(r.added) == 1
    assert r.rows_changed == 2 and len(r.changed) == 1


def test_ignore_columns():
    r = diff(BEFORE, AFTER, "customer_id", ignore_columns=["revenue"])
    assert r.rows_changed == 1
    assert r.changed[0].changes == {"status": ("active", "churned")}


def test_composite_key(tmp_path):
    b = write(tmp_path, "b.csv", "region,id,v\nUS,1,a\nEU,1,b")
    a = write(tmp_path, "a.csv", "region,id,v\nUS,1,a\nEU,1,c\nEU,2,d")
    r = diff(b, a, ["region", "id"])
    assert r.rows_added == 1 and r.added[0]["id"] == 2
    assert r.rows_changed == 1
    assert r.changed[0].key == {"region": "EU", "id": 1}
    assert r.changed[0].changes == {"v": ("b", "c")}


def test_nulls_compare_correctly(tmp_path):
    b = write(tmp_path, "b.csv", "id,v\n1,\n2,x\n3,")
    a = write(tmp_path, "a.csv", "id,v\n1,\n2,\n3,y")
    r = diff(b, a, "id")
    changes = {rc.key["id"]: rc.changes["v"] for rc in r.changed}
    assert changes == {2: ("x", None), 3: (None, "y")}


def test_schema_changes(tmp_path):
    b = write(tmp_path, "b.csv", "id,old_col,v\n1,x,1")
    a = write(tmp_path, "a.csv", "id,v,new_col\n1,1,y")
    r = diff(b, a, "id")
    assert r.columns_added == ["new_col"]
    assert r.columns_removed == ["old_col"]
    assert r.rows_changed == 0
    assert r.has_differences


def test_type_change_compares_text_form(tmp_path):
    b = write(tmp_path, "b.csv", "id,v\n1,10\n2,20")
    a = write(tmp_path, "a.csv", "id,v\n1,10\n2,abc")
    r = diff(b, a, "id")
    assert r.type_changes == {"v": ("BIGINT", "VARCHAR")}
    assert r.rows_changed == 1
    assert r.changed[0].changes == {"v": (20, "abc")}


def test_awkward_column_names(tmp_path):
    b = write(tmp_path, "b.csv", '"my id","select","we""ird"\n1,a,x')
    a = write(tmp_path, "a.csv", '"my id","select","we""ird"\n1,b,x')
    r = diff(b, a, "my id")
    assert r.changed[0].changes == {"select": ("a", "b")}


def test_parquet_and_json(tmp_path):
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM read_csv('{BEFORE}')) TO '{tmp_path / 'b.parquet'}'")
    rows = [
        {"customer_id": 1, "name": "Acme Corp", "status": "active", "revenue": 13500, "region": "West"},
    ]
    (tmp_path / "a.json").write_text(json.dumps(rows))
    r = diff(tmp_path / "b.parquet", tmp_path / "a.json", "customer_id")
    assert r.rows_removed == 5
    assert r.changed[0].changes == {"revenue": (12000, 13500)}


def test_missing_file():
    with pytest.raises(RiftError, match="File not found"):
        diff("nope.csv", AFTER, "customer_id")


def test_unknown_format(tmp_path):
    p = write(tmp_path, "data.xlsx", "x")
    with pytest.raises(RiftError, match="file format"):
        diff(p, AFTER, "customer_id")


def test_missing_key_column():
    with pytest.raises(RiftError, match="not found in the before file"):
        diff(BEFORE, AFTER, "nope")


def test_duplicate_key(tmp_path):
    b = write(tmp_path, "b.csv", "id,v\n1,a\n1,b")
    with pytest.raises(RiftError, match="not unique in the before file"):
        diff(b, b, "id")


def test_null_key(tmp_path):
    a = write(tmp_path, "a.csv", "id,v\n1,a\n,b")
    with pytest.raises(RiftError, match="empty key"):
        diff(a, a, "id")


def test_unknown_ignore_column():
    with pytest.raises(RiftError, match="Ignored column"):
        diff(BEFORE, AFTER, "customer_id", ignore_columns=["nope"])


def test_to_dict_is_json_serialisable():
    payload = diff(BEFORE, AFTER, "customer_id").to_dict()
    text = json.dumps(payload, default=str)
    assert '"rows_changed": 2' in text
    assert payload["changed"][0]["changes"]["revenue"] == {"before": 12000, "after": 13500}


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def test_git_revision_sources(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    data = tmp_path / "customers.csv"
    data.write_text(BEFORE.read_text())
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "v1")
    data.write_text(AFTER.read_text())
    monkeypatch.chdir(tmp_path)

    r = diff("HEAD:customers.csv", "customers.csv", "customer_id")
    assert r.before_source == "HEAD:customers.csv"
    assert (r.rows_added, r.rows_removed, r.rows_changed) == (2, 1, 2)


def test_git_bad_revision(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RiftError, match="git could not read"):
        diff("HEAD~5:nope.csv", "nope.csv", "id")
