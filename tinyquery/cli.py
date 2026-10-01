from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tinyquery.catalog import Catalog
from tinyquery.engine import execute, explain, explain_analyze
from tinyquery.errors import TinyQueryError
from tinyquery.profiler import format_analyze
from tinyquery.schema import Row, Schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tinyquery",
        description="Run a SQL query against CSV files or tinydelta tables.",
    )
    parser.add_argument("sql", help="SQL to run, e.g. \"SELECT region FROM orders\"")
    parser.add_argument(
        "--data",
        default="examples",
        help="directory of CSV files (table name = file stem)",
    )
    parser.add_argument(
        "--table",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="register a tinydelta table; repeatable, shadows a CSV of the same name",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=None,
        help="read --table tables at this version instead of the latest",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="print the physical plan instead of running the query",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="run the query and print each operator's estimated vs actual rows and time",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --analyze, print the plan and rows as JSON",
    )
    parser.add_argument(
        "--no-reorder",
        action="store_true",
        help="join tables in the order written instead of asking the cost model",
    )
    args = parser.parse_args(argv)

    catalog = Catalog()
    data_dir = Path(args.data)
    # The CSV directory is the default, but --table on its own is a complete setup.
    if data_dir.is_dir():
        catalog.load_dir(data_dir)
    elif not args.table:
        print(f"data directory not found: {data_dir}", file=sys.stderr)
        return 2

    for spec in args.table:
        if "=" not in spec:
            print(f"expected --table NAME=PATH, got {spec!r}", file=sys.stderr)
            return 2
        name, _, path = spec.partition("=")
        try:
            version = catalog.load_delta(name.strip(), path.strip(), version=args.version)
        except TinyQueryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.version is not None:
            print(f"-- {name.strip()} at version {version}", file=sys.stderr)

    reorder = not args.no_reorder
    try:
        if args.analyze:
            result = explain_analyze(catalog, args.sql, reorder=reorder)
            if args.json:
                print(json.dumps({
                    "columns": result.schema.names(),
                    "rows": [list(row) for row in result.rows],
                    "plan": result.plan.to_dict(),
                }))
            else:
                print(format_analyze(result.plan))
                n = len(result.rows)
                print(f"({n} row{'s' if n != 1 else ''})")
            return 0
        if args.explain:
            print(explain(catalog, args.sql, reorder=reorder))
            return 0
        rows, schema = execute(catalog, args.sql, reorder=reorder)
        print(_format_table(rows, schema))
        return 0
    except TinyQueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _format_table(rows: list[Row], schema: Schema) -> str:
    headers = schema.names()
    string_rows = [[_cell(value) for value in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in string_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt(headers), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt(row) for row in string_rows)
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(lines)


def _cell(value: object) -> str:
    return "NULL" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
