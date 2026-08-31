"""Querying tinydelta tables.

The point of these is the seam between the two projects: the commit log decides
which files a scan sees, so SQL run at an older version has to return that
version's rows and nothing else.
"""

from __future__ import annotations

import pytest

from tinyquery.catalog import Catalog
from tinyquery.engine import execute
from tinyquery.errors import TinyQueryError

tinydelta = pytest.importorskip("tinydelta")

from tinydelta.schema import Schema as DeltaSchema  # noqa: E402
from tinydelta.table import DeltaTable  # noqa: E402


@pytest.fixture
def orders(tmp_path):
    """A table with two commits, so there is a past to travel to."""
    table = DeltaTable.create(
        tmp_path / "orders",
        DeltaSchema.parse(["id:int", "region:str", "year:int"]),
    )
    table.append(
        [
            {"id": 1, "region": "west", "year": 2024},
            {"id": 2, "region": "east", "year": 2024},
        ]
    )
    table.append([{"id": 3, "region": "west", "year": 2023}])
    return table


def test_query_reads_the_latest_snapshot(orders):
    catalog = Catalog()
    version = catalog.load_delta("orders", orders.path)

    assert version == 2
    rows, _ = execute(catalog, "SELECT id FROM orders")
    assert sorted(row[0] for row in rows) == [1, 2, 3]


def test_select_can_time_travel(orders):
    """Version 1 predates the third row, so the same SQL must not see it."""
    catalog = Catalog()
    version = catalog.load_delta("orders", orders.path, version=1)

    assert version == 1
    rows, _ = execute(catalog, "SELECT id FROM orders")
    assert sorted(row[0] for row in rows) == [1, 2]


def test_overwrite_hides_the_replaced_files(orders):
    """A scan must see only what the newest commit published, not everything on disk."""
    orders.overwrite([{"id": 9, "region": "south", "year": 2025}])

    catalog = Catalog()
    catalog.load_delta("orders", orders.path)
    rows, _ = execute(catalog, "SELECT id FROM orders")
    assert [row[0] for row in rows] == [9]

    # The older version stays queryable even though those files were removed.
    older = Catalog()
    older.load_delta("orders", orders.path, version=2)
    rows, _ = execute(older, "SELECT id FROM orders")
    assert sorted(row[0] for row in rows) == [1, 2, 3]


def test_schema_comes_from_the_log_not_from_guessing(orders):
    """CSV infers types from text; the log declares them, so 'year' stays an int."""
    catalog = Catalog()
    catalog.load_delta("orders", orders.path)

    assert catalog.get("orders").schema.names() == [
        "orders.id",
        "orders.region",
        "orders.year",
    ]
    rows, _ = execute(catalog, "SELECT id FROM orders WHERE year = 2024")
    assert sorted(row[0] for row in rows) == [1, 2]


def test_delta_tables_join_against_csv_tables(orders, tmp_path):
    """Mixed sources should plan and join like any other pair of tables."""
    catalog = Catalog()
    catalog.load_delta("orders", orders.path)

    lineitem = tmp_path / "lineitem.csv"
    lineitem.write_text(
        "order_id,revenue\n1,10\n1,5\n2,20\n3,100\n", encoding="utf-8"
    )
    catalog.load_csv("lineitem", lineitem)

    rows, _ = execute(
        catalog,
        """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        GROUP BY o.region
        """,
    )
    assert {row[0]: row[1] for row in rows} == {"west": 15, "east": 20}


def test_aliases_qualify_delta_columns(orders):
    catalog = Catalog()
    catalog.load_delta("orders", orders.path, alias="o")
    rows, _ = execute(catalog, "SELECT o.region FROM orders o WHERE o.year = 2023")
    assert [row[0] for row in rows] == ["west"]


def test_missing_table_is_a_tinyquery_error(tmp_path):
    catalog = Catalog()
    with pytest.raises(TinyQueryError, match="tinydelta"):
        catalog.load_delta("nope", tmp_path / "not-a-table")


def test_unknown_version_is_a_tinyquery_error(orders):
    catalog = Catalog()
    with pytest.raises(TinyQueryError, match="tinydelta"):
        catalog.load_delta("orders", orders.path, version=99)
