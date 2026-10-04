"""The browser demo's Python side: build a small shop, then run queries on it.

app.js unpacks this file into Pyodide next to the tinyquery and tinydelta
sources, calls `setup()` once, and then `run()` for every query. Nothing here
knows about the browser, so tests/test_site_demo.py runs it natively as well.

The shop has three tables:

  * `orders` is a tinydelta table with a history worth travelling through:
    three yearly appends, an overwrite that drops 2023, and a restore that
    brings it back.
  * `customers` has a skewed `tier` column. Two customers in two hundred are
    platinum, but equality selectivity assumes all four tiers are equally
    common, so `WHERE c.tier = 'platinum'` is estimated 25x too high.
  * `lineitem` has four items per order, for every order any version holds.

`append_orders()` commits another version from the page, so visitors can
extend the history and watch the plan follow it.

`customers` and `lineitem` never change, so they are registered in memory.
"""

from __future__ import annotations

import json
import os
import random
import shutil
from pathlib import Path

from tinydelta import DeltaTable, Schema
from tinyquery.catalog import Catalog
from tinyquery.engine import explain_analyze
from tinyquery.profiler import MISESTIMATE_FACTOR, format_analyze
from tinyquery.schema import Column, Row
from tinyquery.schema import Schema as RowSchema

if not hasattr(os, "link"):
    # Pyodide's filesystem has no hard links, and tinydelta publishes a commit
    # by linking a finished temp file to its version name. Stand in with an
    # exclusive create plus a copy. That gives up link()'s atomicity, which is
    # only acceptable because a browser tab runs a single Python thread: there
    # is no second writer to race and no reader to catch a half-copied file.
    # Native runs, including the tests, keep the real link().
    def _link(src, dst, *args, **kwargs):
        with open(src, "rb") as source, open(dst, "xb") as target:
            shutil.copyfileobj(source, target)

    os.link = _link

REGIONS = ("west", "east", "north", "south")
SKUS = ("widget", "gadget", "gizmo", "doohickey")
# (tier, how many customers have it). Skewed on purpose; see the module docstring.
TIERS = (("platinum", 2), ("gold", 18), ("silver", 60), ("bronze", 120))
ORDERS_PER_YEAR = 600
ITEMS_PER_ORDER = 4
YEARS = (2023, 2024, 2025)

PRESETS = [
    {
        "name": "Join order and a bad estimate",
        "sql": (
            "SELECT c.tier, COUNT(o.id) AS orders, SUM(l.revenue) AS revenue\n"
            "FROM lineitem l\n"
            "JOIN orders o ON l.order_id = o.id\n"
            "JOIN customers c ON o.customer_id = c.id\n"
            "WHERE c.tier = 'platinum'\n"
            "GROUP BY c.tier"
        ),
    },
    {
        "name": "Orders per year",
        "sql": (
            "SELECT o.year, COUNT(o.id) AS orders\n"
            "FROM orders o\n"
            "GROUP BY o.year\n"
            "ORDER BY o.year"
        ),
    },
    {
        "name": "Top orders by revenue",
        "sql": (
            "SELECT o.id, o.region, SUM(l.revenue) AS total\n"
            "FROM orders o\n"
            "JOIN lineitem l ON o.id = l.order_id\n"
            "WHERE o.year = 2024\n"
            "GROUP BY o.id, o.region\n"
            "ORDER BY total DESC\n"
            "LIMIT 5"
        ),
    },
]

_state: dict[str, object] = {}


def setup(base: str | Path = "/data") -> str:
    """Build the shop under `base`. Returns the history, as `history()` does."""
    rng = random.Random(20261003)
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    table_path = base / "orders"
    if table_path.exists():
        shutil.rmtree(table_path)

    customers: list[Row] = []
    for tier, count in TIERS:
        for _ in range(count):
            customers.append((len(customers), f"customer {len(customers)}", tier))
    rng.shuffle(customers)

    by_year: dict[int, list[dict[str, object]]] = {}
    lineitem: list[Row] = []
    for y, year in enumerate(YEARS):
        rows = []
        for i in range(ORDERS_PER_YEAR):
            order_id = y * ORDERS_PER_YEAR + i
            rows.append(
                {
                    "id": order_id,
                    "customer_id": rng.randrange(len(customers)),
                    "region": rng.choice(REGIONS),
                    "year": year,
                }
            )
            for _ in range(ITEMS_PER_ORDER):
                lineitem.append((order_id, rng.choice(SKUS), rng.randrange(1, 200)))
        by_year[year] = rows

    table = DeltaTable.create(
        table_path,
        Schema.parse(["id:int", "customer_id:int", "region:str", "year:int"]),
    )
    for year in YEARS:
        table.append(by_year[year])
    table.overwrite([row for year in YEARS[1:] for row in by_year[year]])
    table.restore(len(YEARS))  # the version before the overwrite

    _state.update(
        table_path=table_path,
        customers=customers,
        lineitem=lineitem,
        rng=rng,
        next_id=len(YEARS) * ORDERS_PER_YEAR,
    )
    return history()


def append_orders(count: int = 200, year: int = YEARS[-1] + 1) -> str:
    """Commit `count` new orders as a new version. Returns the new history.

    Their line items join the in-memory `lineitem`, which every version shares;
    older versions simply have no orders for them to match.
    """
    rng: random.Random = _state["rng"]
    first = _state["next_id"]
    rows = []
    for order_id in range(first, first + count):
        rows.append(
            {
                "id": order_id,
                "customer_id": rng.randrange(len(_state["customers"])),
                "region": rng.choice(REGIONS),
                "year": year,
            }
        )
        for _ in range(ITEMS_PER_ORDER):
            _state["lineitem"].append((order_id, rng.choice(SKUS), rng.randrange(1, 200)))
    DeltaTable.open(_state["table_path"]).append(rows)
    _state["next_id"] = first + count
    return history()


def history() -> str:
    """Every commit in `orders`, with the row count of the snapshot it made."""
    table = DeltaTable.open(_state["table_path"])
    return json.dumps(
        [
            {
                "version": commit.version,
                "operation": commit.operation,
                "timestamp": commit.timestamp,
                "rows": table.describe(commit.version)["num_records"],
            }
            for commit in table.history()
        ]
    )


def presets() -> str:
    return json.dumps(PRESETS)


def run(
    sql: str,
    version: int | None = None,
    reorder: bool = True,
    pushdown: bool = True,
) -> str:
    """Run `sql` under EXPLAIN ANALYZE against `orders` at `version`.

    Returns JSON: columns, rows, the plan tree, the text rendering of it, the
    version actually read, and the factor at which a misestimate is flagged. Any failure comes back as {"error": ...} rather
    than an exception, so the page has one shape to handle.
    """
    try:
        catalog = Catalog()
        read = catalog.load_delta("orders", _state["table_path"], version=version)
        _register(catalog, "customers", ["id", "name", "tier"], _state["customers"])
        _register(catalog, "lineitem", ["order_id", "sku", "revenue"], _state["lineitem"])
        result = explain_analyze(catalog, sql, reorder=reorder, pushdown=pushdown)
    except Exception as exc:  # the page shows the message; it must not crash
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
    return json.dumps(
        {
            "version": read,
            "columns": result.schema.names(),
            "rows": [list(row) for row in result.rows],
            "plan": result.plan.to_dict(),
            "text": format_analyze(result.plan),
            # So the page flags exactly the nodes the text output flags.
            "misestimate_factor": MISESTIMATE_FACTOR,
        }
    )


def _register(catalog: Catalog, name: str, columns: list[str], rows: list[Row]) -> None:
    catalog.register(name, RowSchema(tuple(Column(c, table=name) for c in columns)), rows)
