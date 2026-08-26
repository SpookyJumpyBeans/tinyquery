from tinyquery.engine import execute, explain
from tinyquery.errors import TinyQueryError
from tinyquery.parser import parse

from tests.conftest import sample_catalog

import pytest


def test_order_by_year():
    rows, _ = execute(sample_catalog(), "SELECT id, year FROM orders ORDER BY year, id")
    assert rows == [(3, 2023), (1, 2024), (2, 2024), (4, 2024)]


def test_order_by_desc():
    rows, _ = execute(sample_catalog(), "SELECT id FROM orders ORDER BY year DESC, id DESC")
    assert rows == [(4,), (2,), (1,), (3,)]


def test_limit_without_order_is_scan_order():
    rows, _ = execute(sample_catalog(), "SELECT id FROM orders LIMIT 2")
    assert rows == [(1,), (2,)]


def test_limit_zero():
    rows, _ = execute(sample_catalog(), "SELECT id FROM orders LIMIT 0")
    assert rows == []


def test_order_then_limit():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        GROUP BY o.region
        ORDER BY total DESC
        LIMIT 1
        """,
    )
    assert rows == [("east", 20)]


def test_explain_order_limit_uses_topk():
    plan = explain(sample_catalog(), "SELECT id FROM orders ORDER BY id DESC LIMIT 1")
    assert "TopK 1 by id DESC" in plan
    assert "Sort " not in plan
    assert "Limit 1" not in plan


def test_explain_limit_alone_is_not_topk():
    plan = explain(sample_catalog(), "SELECT id FROM orders LIMIT 1")
    assert "Limit 1" in plan
    assert "TopK" not in plan


def test_explain_order_alone_is_sort():
    plan = explain(sample_catalog(), "SELECT id FROM orders ORDER BY id")
    assert "Sort id ASC" in plan
    assert "TopK" not in plan


def test_parse_order_limit():
    query = parse("SELECT id FROM orders ORDER BY year ASC, id DESC LIMIT 3")
    assert query.limit == 3
    assert len(query.order_by) == 2
    assert not query.order_by[0].descending
    assert query.order_by[1].descending


def test_negative_limit_is_an_error():
    with pytest.raises(TinyQueryError, match="LIMIT"):
        execute(sample_catalog(), "SELECT id FROM orders LIMIT -1")
