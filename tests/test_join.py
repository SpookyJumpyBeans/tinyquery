from tinyquery.engine import execute, explain

from tests.conftest import sample_catalog


def test_hash_join_one_to_many():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT o.id, l.sku, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.id = 1
        """,
    )
    assert sorted(rows) == [(1, "gadget", 5), (1, "widget", 10)]


def test_join_then_filter_year():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        """,
    )
    got = sorted(rows)
    assert ("west", 10) in got
    assert ("west", 5) in got
    assert ("east", 20) in got
    assert ("west", 100) not in got


def test_null_join_keys_do_not_match():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT l.sku
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE l.sku = 'orphan'
        """,
    )
    assert rows == []


def test_empty_build_side():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT l.sku
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 1999
        """,
    )
    assert rows == []


def test_explain_shows_hash_join():
    plan = explain(
        sample_catalog(),
        """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        """,
    )
    assert "HashJoin" in plan
    assert "Scan o" in plan
    assert "Scan l" in plan
