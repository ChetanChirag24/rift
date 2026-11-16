"""Command-line interface: ``rift BEFORE AFTER --key COLUMN``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, List, Optional, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import __version__
from .engine import DiffResult, RiftError, diff

EXIT_SAME = 0
EXIT_DIFFERENT = 1
EXIT_ERROR = 2


def _split_columns(values: Optional[List[str]]) -> List[str]:
    """Accept both ``-k a -k b`` and ``-k a,b``."""
    out: List[str] = []
    for v in values or []:
        out.extend(part.strip() for part in v.split(",") if part.strip())
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rift",
        description="Local-first data diff, powered by DuckDB. "
        "See exactly what changed before you merge.",
        epilog="Exit status: 0 if the files match, 1 if they differ, 2 on error.",
    )
    parser.add_argument(
        "before",
        help="the original file (CSV, Parquet or JSON), or a git revision like HEAD~1:data/users.csv",
    )
    parser.add_argument("after", help="the new file (or git revision) to compare against it")
    parser.add_argument(
        "-k", "--key", action="append", required=True, metavar="COLUMN",
        help="column(s) that identify a row; repeat or comma-separate for a composite key",
    )
    parser.add_argument(
        "-i", "--ignore", action="append", metavar="COLUMN",
        help="column(s) to leave out of the comparison (e.g. updated_at)",
    )
    parser.add_argument(
        "-n", "--limit", type=int, default=10, metavar="N",
        help="show at most N rows per section (default: 10, 0 for all)",
    )
    parser.add_argument(
        "--summary", action="store_true", help="print only the summary, no row details",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the full diff as JSON (for scripts and CI)",
    )
    parser.add_argument("--version", action="version", version=f"rift {__version__}")
    return parser


def _fmt(value: Any) -> str:
    return "∅" if value is None else str(value)


def _rows_table(title: str, rows: List[dict], total: int, style: str) -> Table:
    columns = list(rows[0].keys())
    table = Table(title=title, title_style=f"bold {style}", title_justify="left")
    for c in columns:
        table.add_column(c, style=style)
    for row in rows:
        table.add_row(*(_fmt(row[c]) for c in columns))
    if total > len(rows):
        table.caption = f"… and {total - len(rows)} more"
    return table


def render(result: DiffResult, console: Console, summary_only: bool = False) -> None:
    console.print(
        Text.assemble(
            ("rift ", "bold magenta"), (result.before_source, "cyan"), " → ",
            (result.after_source, "cyan"), ("  key: ", "dim"), (", ".join(result.key_columns), "bold"),
        )
    )
    console.print()

    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column(justify="left")
    summary.add_column(justify="right")
    summary.add_row("Rows before", f"{result.rows_before:,}")
    summary.add_row("Rows after", f"{result.rows_after:,}")
    summary.add_row(Text("Added", style="green"), Text(f"+{result.rows_added:,}", style="green"))
    summary.add_row(Text("Removed", style="red"), Text(f"-{result.rows_removed:,}", style="red"))
    summary.add_row(Text("Changed", style="yellow"), Text(f"~{result.rows_changed:,}", style="yellow"))
    summary.add_row(Text("Unchanged", style="dim"), Text(f"{result.rows_unchanged:,}", style="dim"))
    console.print(summary)
    console.print()

    if result.has_schema_changes:
        console.print("[bold]Schema changes[/bold]")
        for c in result.columns_added:
            console.print(f"  [green]+ column {c}[/green]")
        for c in result.columns_removed:
            console.print(f"  [red]- column {c}[/red]")
        for c, (old, new) in result.type_changes.items():
            console.print(f"  [yellow]~ column {c}: {old} → {new}[/yellow]")
        console.print()

    if result.column_change_counts:
        cols = Table(title="Changed values by column", title_style="bold", title_justify="left")
        cols.add_column("column")
        cols.add_column("rows changed", justify="right", style="yellow")
        for c, n in sorted(result.column_change_counts.items(), key=lambda kv: -kv[1]):
            cols.add_row(c, f"{n:,}")
        console.print(cols)
        console.print()

    if summary_only:
        return

    if result.added:
        console.print(_rows_table("Added rows", result.added, result.rows_added, "green"))
        console.print()
    if result.removed:
        console.print(_rows_table("Removed rows", result.removed, result.rows_removed, "red"))
        console.print()
    if result.changed:
        table = Table(title="Changed rows", title_style="bold yellow", title_justify="left")
        for k in result.key_columns:
            table.add_column(k, style="bold")
        table.add_column("column")
        table.add_column("before", style="red")
        table.add_column("after", style="green")
        for rc in result.changed:
            first = True
            for c, (old, new) in rc.changes.items():
                key_cells = [_fmt(v) for v in rc.key.values()] if first else [""] * len(rc.key)
                table.add_row(*key_cells, c, _fmt(old), _fmt(new))
                first = False
            table.add_section()
        if result.rows_changed > len(result.changed):
            table.caption = f"… and {result.rows_changed - len(result.changed)} more"
        console.print(table)
        console.print()

    if not result.has_differences:
        console.print("[bold green]✓ No differences.[/bold green]")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    keys = _split_columns(args.key)
    ignore = _split_columns(args.ignore)
    if args.limit < 0:
        print("rift: error: --limit must be 0 or more", file=sys.stderr)
        return EXIT_ERROR
    limit = None if (args.limit == 0 or args.json) else args.limit
    if args.summary and not args.json:
        limit = 0

    try:
        result = diff(args.before, args.after, keys, ignore_columns=ignore, limit=limit)
    except RiftError as exc:
        Console(stderr=True).print(f"[bold red]rift: error:[/bold red] {exc}", highlight=False)
        return EXIT_ERROR

    if args.json:
        payload = result.to_dict()
        if args.summary:
            for section in ("added", "removed", "changed"):
                payload.pop(section)
        print(json.dumps(payload, indent=2, default=str))
    else:
        render(result, Console(), summary_only=args.summary)

    return EXIT_DIFFERENT if result.has_differences else EXIT_SAME


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
