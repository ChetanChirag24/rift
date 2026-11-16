"""Core diff engine: load two datasets into DuckDB and compare them by key."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import duckdb

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
JSON_SUFFIXES = {".json", ".jsonl", ".ndjson"}
COMPRESSION_SUFFIXES = {".gz", ".zst"}


class RiftError(Exception):
    """Raised for user-facing problems: missing files, bad keys, duplicate keys."""


@dataclass
class RowChange:
    """One row that exists on both sides but whose values differ."""

    key: Dict[str, Any]
    changes: Dict[str, Tuple[Any, Any]]  # column -> (before, after)


@dataclass
class DiffResult:
    before_source: str
    after_source: str
    key_columns: List[str]
    rows_before: int
    rows_after: int
    rows_added: int
    rows_removed: int
    rows_changed: int
    columns_added: List[str] = field(default_factory=list)
    columns_removed: List[str] = field(default_factory=list)
    type_changes: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    compared_columns: List[str] = field(default_factory=list)
    column_change_counts: Dict[str, int] = field(default_factory=dict)
    added: List[Dict[str, Any]] = field(default_factory=list)
    removed: List[Dict[str, Any]] = field(default_factory=list)
    changed: List[RowChange] = field(default_factory=list)

    @property
    def rows_unchanged(self) -> int:
        return self.rows_before - self.rows_removed - self.rows_changed

    @property
    def has_schema_changes(self) -> bool:
        return bool(self.columns_added or self.columns_removed or self.type_changes)

    @property
    def has_differences(self) -> bool:
        return bool(
            self.rows_added or self.rows_removed or self.rows_changed or self.has_schema_changes
        )

    def summary(self) -> Dict[str, int]:
        return {
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_added": self.rows_added,
            "rows_removed": self.rows_removed,
            "rows_changed": self.rows_changed,
            "rows_unchanged": self.rows_unchanged,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "before": self.before_source,
            "after": self.after_source,
            "key": self.key_columns,
            "summary": self.summary(),
            "schema": {
                "columns_added": self.columns_added,
                "columns_removed": self.columns_removed,
                "type_changes": {
                    c: {"before": b, "after": a} for c, (b, a) in self.type_changes.items()
                },
            },
            "column_change_counts": self.column_change_counts,
            "added": self.added,
            "removed": self.removed,
            "changed": [
                {
                    "key": rc.key,
                    "changes": {c: {"before": b, "after": a} for c, (b, a) in rc.changes.items()},
                }
                for rc in self.changed
            ],
        }


def quote(identifier: str) -> str:
    """Quote a SQL identifier so any column name is safe to interpolate."""
    return '"' + identifier.replace('"', '""') + '"'


def _detect_format(path: Path) -> str:
    suffixes = [s.lower() for s in path.suffixes]
    while suffixes and suffixes[-1] in COMPRESSION_SUFFIXES:
        suffixes.pop()
    ext = suffixes[-1] if suffixes else ""
    if ext in CSV_SUFFIXES:
        return "csv"
    if ext in PARQUET_SUFFIXES:
        return "parquet"
    if ext in JSON_SUFFIXES:
        return "json"
    raise RiftError(
        f"Can't tell the file format of '{path}'. "
        "Supported: .csv, .tsv, .txt, .parquet, .json, .jsonl, .ndjson (optionally .gz/.zst)."
    )


def _git_revision_file(source: str, workdir: Path) -> Optional[Path]:
    """Resolve git's ``REV:path`` syntax (e.g. ``HEAD~1:data/users.csv``) to a temp file.

    Returns None if ``source`` doesn't look like a git object reference.
    """
    rev, sep, path = source.partition(":")
    if not sep or not rev or not path or len(rev) == 1:  # len 1 => Windows drive letter
        return None
    try:
        blob = subprocess.run(
            ["git", "show", source], capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RiftError(f"Could not run git to read '{source}': {exc}") from exc
    if blob.returncode != 0:
        message = blob.stderr.decode(errors="replace").strip()
        raise RiftError(f"git could not read '{source}': {message}")
    target = workdir / f"{len(list(workdir.iterdir()))}_{Path(path).name}"
    target.write_bytes(blob.stdout)
    return target


def _load(
    con: duckdb.DuckDBPyConnection, source: Union[str, Path], table: str, workdir: Path
) -> None:
    path = Path(source)
    if not path.exists():
        path = _git_revision_file(str(source), workdir) or path
    if not path.exists():
        raise RiftError(f"File not found: {source}")
    fmt = _detect_format(path)
    try:
        if fmt == "csv":
            rel = con.read_csv(str(path))
        elif fmt == "parquet":
            rel = con.read_parquet(str(path))
        else:
            rel = con.read_json(str(path))
        rel.create(table)
    except duckdb.Error as exc:
        raise RiftError(f"Could not read '{path}': {exc}") from exc


def _schema(con: duckdb.DuckDBPyConnection, table: str) -> Dict[str, str]:
    return {row[0]: row[1] for row in con.execute(f"DESCRIBE {table}").fetchall()}


def _rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> List[Dict[str, Any]]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _check_keys(con: duckdb.DuckDBPyConnection, table: str, label: str, keys: List[str]) -> None:
    key_list = ", ".join(quote(k) for k in keys)

    null_cond = " OR ".join(f"{quote(k)} IS NULL" for k in keys)
    nulls = con.execute(f"SELECT count(*) FROM {table} WHERE {null_cond}").fetchone()[0]
    if nulls:
        raise RiftError(f"{nulls} row(s) in the {label} file have an empty key ({', '.join(keys)}).")

    dup = con.execute(
        f"SELECT {key_list}, count(*) AS n FROM {table} "
        f"GROUP BY ALL HAVING count(*) > 1 ORDER BY n DESC LIMIT 1"
    ).fetchone()
    if dup:
        example = ", ".join(f"{k}={v!r}" for k, v in zip(keys, dup[:-1]))
        raise RiftError(
            f"Key ({', '.join(keys)}) is not unique in the {label} file: "
            f"{example} appears {dup[-1]} times. Pick a key that identifies each row, "
            "or combine columns with a composite key."
        )


def diff(
    before_source: Union[str, Path],
    after_source: Union[str, Path],
    key_column: Union[str, Sequence[str]],
    ignore_columns: Sequence[str] = (),
    limit: Optional[int] = None,
) -> DiffResult:
    """Compare two datasets row by row, matched on ``key_column``.

    Each source is a file path or a git object reference like
    ``HEAD~1:data/users.csv`` (read with ``git show``).

    ``key_column`` may be one column name or a list (composite key).
    ``limit`` caps how many added/removed/changed rows are returned in detail;
    counts are always exact.
    """
    keys = [key_column] if isinstance(key_column, str) else list(key_column)
    if not keys:
        raise RiftError("At least one key column is required.")

    con = duckdb.connect()
    with tempfile.TemporaryDirectory(prefix="rift-") as tmp:
        _load(con, before_source, "before", Path(tmp))
        _load(con, after_source, "after", Path(tmp))

    schema_before = _schema(con, "before")
    schema_after = _schema(con, "after")

    for label, schema in (("before", schema_before), ("after", schema_after)):
        missing = [k for k in keys if k not in schema]
        if missing:
            raise RiftError(
                f"Key column(s) {', '.join(missing)} not found in the {label} file. "
                f"Available columns: {', '.join(schema)}"
            )

    unknown_ignored = [c for c in ignore_columns if c not in schema_before and c not in schema_after]
    if unknown_ignored:
        raise RiftError(f"Ignored column(s) not found in either file: {', '.join(unknown_ignored)}")

    _check_keys(con, "before", "before", keys)
    _check_keys(con, "after", "after", keys)

    ignored = set(ignore_columns) | set(keys)
    columns_removed = [c for c in schema_before if c not in schema_after and c not in ignored]
    columns_added = [c for c in schema_after if c not in schema_before and c not in ignored]
    compared = [c for c in schema_before if c in schema_after and c not in ignored]
    type_changes = {
        c: (schema_before[c], schema_after[c])
        for c in compared
        if schema_before[c] != schema_after[c]
    }

    join_cond = " AND ".join(f"a.{quote(k)} = b.{quote(k)}" for k in keys)
    order_by = ", ".join(quote(k) for k in keys)
    limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""

    rows_before = con.execute("SELECT count(*) FROM before").fetchone()[0]
    rows_after = con.execute("SELECT count(*) FROM after").fetchone()[0]

    added_sql = f"FROM after a WHERE NOT EXISTS (SELECT 1 FROM before b WHERE {join_cond})"
    removed_sql = f"FROM before b WHERE NOT EXISTS (SELECT 1 FROM after a WHERE {join_cond})"
    rows_added = con.execute(f"SELECT count(*) {added_sql}").fetchone()[0]
    rows_removed = con.execute(f"SELECT count(*) {removed_sql}").fetchone()[0]
    added = _rows_as_dicts(con.execute(f"SELECT a.* {added_sql} ORDER BY {order_by}{limit_sql}"))
    removed = _rows_as_dicts(con.execute(f"SELECT b.* {removed_sql} ORDER BY {order_by}{limit_sql}"))

    def differs(c: str) -> str:
        if c in type_changes:
            # Different types: compare their text form so e.g. 1 vs '1' is equal.
            return f"CAST(b.{quote(c)} AS VARCHAR) IS DISTINCT FROM CAST(a.{quote(c)} AS VARCHAR)"
        return f"b.{quote(c)} IS DISTINCT FROM a.{quote(c)}"

    column_change_counts: Dict[str, int] = {}
    changed: List[RowChange] = []
    rows_changed = 0

    if compared:
        any_diff = " OR ".join(f"({differs(c)})" for c in compared)
        joined = f"FROM before b JOIN after a ON {join_cond}"

        counts = con.execute(
            "SELECT count(*) FILTER (WHERE " + any_diff + "), "
            + ", ".join(f"count(*) FILTER (WHERE {differs(c)})" for c in compared)
            + f" {joined}"
        ).fetchone()
        rows_changed = counts[0]
        column_change_counts = {c: n for c, n in zip(compared, counts[1:]) if n}

        select = [f"b.{quote(k)}" for k in keys]
        for c in compared:
            select += [f"b.{quote(c)}", f"a.{quote(c)}", f"({differs(c)})"]
        b_order = ", ".join(f"b.{quote(k)}" for k in keys)
        detail = con.execute(
            f"SELECT {', '.join(select)} {joined} WHERE {any_diff} ORDER BY {b_order}{limit_sql}"
        ).fetchall()

        n = len(keys)
        for row in detail:
            key = dict(zip(keys, row[:n]))
            changes = {}
            for i, c in enumerate(compared):
                old, new, is_diff = row[n + 3 * i : n + 3 * i + 3]
                if is_diff:
                    changes[c] = (old, new)
            changed.append(RowChange(key=key, changes=changes))

    con.close()

    return DiffResult(
        before_source=str(before_source),
        after_source=str(after_source),
        key_columns=keys,
        rows_before=rows_before,
        rows_after=rows_after,
        rows_added=rows_added,
        rows_removed=rows_removed,
        rows_changed=rows_changed,
        columns_added=columns_added,
        columns_removed=columns_removed,
        type_changes=type_changes,
        compared_columns=compared,
        column_change_counts=column_change_counts,
        added=added,
        removed=removed,
        changed=changed,
    )
