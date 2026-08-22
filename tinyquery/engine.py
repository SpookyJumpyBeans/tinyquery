from __future__ import annotations

from tinyquery.catalog import Catalog
from tinyquery.operators import Operator
from tinyquery.parser import parse
from tinyquery.planner import plan
from tinyquery.schema import Row, Schema


def compile_sql(catalog: Catalog, sql: str) -> Operator:
    return plan(catalog, parse(sql))


def collect(operator: Operator) -> tuple[list[Row], Schema]:
    operator.open()
    rows: list[Row] = []
    try:
        while True:
            row = operator.next_row()
            if row is None:
                break
            rows.append(row)
    finally:
        operator.close()
    return rows, operator.schema


def execute(catalog: Catalog, sql: str) -> tuple[list[Row], Schema]:
    return collect(compile_sql(catalog, sql))


def explain(catalog: Catalog, sql: str) -> str:
    return format_explain(compile_sql(catalog, sql))


def format_explain(operator: Operator, indent: int = 0) -> str:
    lines = ["  " * indent + operator.explain_label()]
    for child in operator.children():
        lines.append(format_explain(child, indent + 1))
    return "\n".join(lines)
