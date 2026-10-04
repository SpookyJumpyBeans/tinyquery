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


def test_without_pushdown_the_filter_stays_above_the_join():
    plan = explain(
        sample_catalog(),
        """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024 AND l.sku = 'widget'
        """,
        pushdown=False,
    )
    lines = plan.splitlines()
    filter_idx = next(i for i, line in enumerate(lines) if "Filter" in line)
    join_idx = next(i for i, line in enumerate(lines) if "HashJoin" in line)
    assert filter_idx < join_idx
    assert "Filter o.year = 2024 AND l.sku = 'widget'" in plan
    assert sum("Filter" in line for line in lines) == 1


def test_pushdown_off_returns_the_same_rows():
    sql = """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024 AND l.sku = 'widget'
        GROUP BY o.region
    """
    on, _ = execute(sample_catalog(), sql)
    off, _ = execute(sample_catalog(), sql, pushdown=False)
    assert sorted(on) == sorted(off)


def test_pushdown_off_feeds_the_join_more_rows():
    from tinyquery.engine import explain_analyze

    sql = """
        SELECT o.region, l.revenue
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
    """

    def join_input_rows(pushdown):
        plan = explain_analyze(sample_catalog(), sql, pushdown=pushdown).plan
        join = plan
        while join.operator != "HashJoin":
            join = join.children[0]
        return sum(child.actual_rows for child in join.children)

    assert join_input_rows(pushdown=True) < join_input_rows(pushdown=False)


def test_cli_no_pushdown_flag(capsys):
    from tinyquery.cli import main

    sql = (
        "SELECT o.region, l.revenue FROM orders o "
        "JOIN lineitem l ON o.id = l.order_id WHERE o.year = 2024"
    )
    assert main(["--explain", "--no-pushdown", sql]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].strip().startswith("Filter o.year = 2024")
