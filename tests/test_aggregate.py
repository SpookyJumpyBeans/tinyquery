from tinyquery.engine import execute, explain
from tinyquery.errors import TinyQueryError

from tests.conftest import sample_catalog

import pytest


def test_group_by_sum():
    rows, schema = execute(
        sample_catalog(),
        """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        GROUP BY o.region
        """,
    )
    assert schema.names()[-1] == "total"
    by_region = {row[0]: row[1] for row in rows}
    assert by_region["west"] == 15
    assert by_region["east"] == 20
    assert by_region["north"] == 10
    assert "west" in by_region


def test_count_star_no_group():
    rows, _ = execute(sample_catalog(), "SELECT COUNT(*) FROM orders")
    assert rows == [(4,)]


def test_count_star_empty_table():
    from tinyquery.catalog import Catalog
    from tinyquery.schema import Column, Schema

    catalog = Catalog()
    catalog.register("empty", Schema((Column("id", "empty"),)), [])
    rows, _ = execute(catalog, "SELECT COUNT(*) FROM empty")
    assert rows == [(0,)]


def test_missing_group_by_is_an_error():
    with pytest.raises(TinyQueryError, match="GROUP BY"):
        execute(sample_catalog(), "SELECT region, COUNT(*) FROM orders")


def test_explain_aggregate():
    plan = explain(
        sample_catalog(),
        "SELECT region, COUNT(*) FROM orders GROUP BY region",
    )
    assert "HashAggregate" in plan
