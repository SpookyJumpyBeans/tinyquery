from tinyquery.engine import execute, explain

from tests.conftest import sample_catalog


def test_single_table_predicate_moves_under_join():
    plan = explain(
        sample_catalog(),
        """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        """,
    )
    lines = plan.splitlines()
    filter_idx = next(i for i, line in enumerate(lines) if "Filter o.year = 2024" in line)
    join_idx = next(i for i, line in enumerate(lines) if "HashJoin" in line)
    assert filter_idx > join_idx
    assert "Scan o" in lines[filter_idx + 1]


def test_each_side_gets_its_own_filter():
    plan = explain(
        sample_catalog(),
        """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024 AND l.sku = 'widget'
        """,
    )
    assert "Filter o.year = 2024" in plan
    assert "Filter l.sku = 'widget'" in plan
    assert "Filter o.year = 2024 AND l.sku = 'widget'" not in plan


def test_or_across_tables_stays_above_join():
    plan = explain(
        sample_catalog(),
        """
        SELECT o.region, l.sku
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024 OR l.sku = 'widget'
        """,
    )
    lines = plan.splitlines()
    filter_idx = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_idx = next(i for i, line in enumerate(lines) if "HashJoin" in line)
    assert filter_idx < join_idx


def test_pushdown_does_not_change_results():
    rows, _ = execute(
        sample_catalog(),
        """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024 AND l.sku = 'widget'
        GROUP BY o.region
        """,
    )
    by_region = {row[0]: row[1] for row in rows}
    assert by_region == {"west": 10, "east": 20, "north": 3}
