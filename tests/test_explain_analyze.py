"""EXPLAIN ANALYZE must report what really happened without changing it."""

import json

from tinyquery.catalog import Catalog
from tinyquery.cli import main
from tinyquery.engine import compile_sql, execute, explain_analyze
from tinyquery.profiler import MISESTIMATE_FACTOR, format_analyze, profile_plan
from tinyquery.schema import Column, Schema

from tests.test_join_order import THREE_TABLE_SQL, star_catalog


def catalog_with(**tables):
    catalog = Catalog()
    for name, (columns, rows) in tables.items():
        catalog.register(name, Schema(tuple(Column(c, table=name) for c in columns)), rows)
    return catalog


def find(plan, prefix):
    """First node, depth first, whose label starts with `prefix`."""
    if plan.label.startswith(prefix):
        return plan
    for child in plan.children:
        hit = find(child, prefix)
        if hit is not None:
            return hit
    return None


def all_nodes(plan):
    yield plan
    for child in plan.children:
        yield from all_nodes(child)


def test_profiling_does_not_change_the_answer():
    catalog = star_catalog()
    assert sorted(explain_analyze(catalog, THREE_TABLE_SQL).rows) == sorted(
        execute(catalog, THREE_TABLE_SQL)[0]
    )


def test_root_actual_rows_match_the_result():
    result = explain_analyze(star_catalog(), THREE_TABLE_SQL)
    assert result.plan.actual_rows == len(result.rows)


def test_scan_and_filter_report_exact_counts():
    catalog = catalog_with(t=(["id", "k"], [(i, i % 4) for i in range(100)]))
    plan = explain_analyze(catalog, "SELECT t.id FROM t WHERE t.k = 1").plan
    assert find(plan, "Scan").actual_rows == 100
    assert find(plan, "Filter").actual_rows == 25


def test_uniform_data_is_estimated_exactly():
    catalog = catalog_with(t=(["id", "k"], [(i, i % 4) for i in range(100)]))
    plan = explain_analyze(catalog, "SELECT t.id FROM t WHERE t.k = 1").plan
    assert find(plan, "Scan").estimated_rows == 100
    assert find(plan, "Filter").estimated_rows == 25  # 100 rows / 4 distinct values


def test_skewed_data_is_flagged_as_misestimated():
    """Equality selectivity assumes uniform values. Break that on purpose.

    97 of 100 rows have k = 0 and the rest are distinct, so the estimator
    sees four distinct values and guesses 25 rows where 97 come back.
    """
    rows = [(i, 0) for i in range(97)] + [(97, 1), (98, 2), (99, 3)]
    catalog = catalog_with(t=(["id", "k"], rows))
    result = explain_analyze(catalog, "SELECT t.id FROM t WHERE t.k = 1")
    flt = find(result.plan, "Filter")
    assert flt.actual_rows == 1
    assert flt.estimated_rows == 25
    assert flt.misestimate >= MISESTIMATE_FACTOR
    assert "estimate off by" in format_analyze(result.plan)


def test_reordering_shrinks_the_largest_intermediate_result():
    """The whole case for the cost model, stated as rows instead of seconds."""
    catalog = star_catalog(n_facts=2000, n_dim=50, n_tiny=10)

    def widest_join(reorder):
        plan = explain_analyze(catalog, THREE_TABLE_SQL, reorder=reorder).plan
        return max(n.actual_rows for n in all_nodes(plan) if n.label.startswith("HashJoin"))

    assert widest_join(reorder=True) < widest_join(reorder=False)


def test_limit_stops_pulling_from_its_input():
    """Streaming means the scan under a LIMIT only produces what is needed."""
    catalog = catalog_with(t=(["id"], [(i,) for i in range(1000)]))
    plan = explain_analyze(catalog, "SELECT t.id FROM t LIMIT 3").plan
    assert plan.actual_rows == 3
    assert find(plan, "Scan").actual_rows <= 4  # not 1000


def test_operators_are_restored_after_profiling():
    """Profiling patches instances temporarily and must leave no trace."""
    root = compile_sql(star_catalog(), THREE_TABLE_SQL)
    profile_plan(root)

    def walk(op):
        yield op
        for child in op.children():
            yield from walk(child)

    for op in walk(root):
        for name in ("open", "next_row", "close"):
            assert name not in op.__dict__, f"{type(op).__name__}.{name} left patched"


def test_operators_are_restored_even_when_the_query_fails():
    catalog = catalog_with(t=(["id"], [(1,), (0,)]))
    root = compile_sql(catalog, "SELECT t.id FROM t")
    root.child.next_row = lambda: 1 / 0  # sabotage one operator mid-run
    try:
        profile_plan(root)
    except ZeroDivisionError:
        pass
    assert "open" not in root.__dict__ and "close" not in root.__dict__


def test_timings_are_consistent():
    plan = explain_analyze(star_catalog(), THREE_TABLE_SQL).plan
    for node in all_nodes(plan):
        assert 0 <= node.self_ms <= node.total_ms + 1e-9
        for child in node.children:
            # A child's time is spent inside its parent's calls.
            assert child.total_ms <= node.total_ms + 1e-6


def test_plan_serialises_to_json():
    plan = explain_analyze(star_catalog(), THREE_TABLE_SQL).plan
    data = json.loads(json.dumps(plan.to_dict()))
    assert data["actual_rows"] == plan.actual_rows
    assert {
        "label", "operator", "estimated_rows", "actual_rows", "total_ms", "self_ms", "children"
    } <= set(data)
    operators = {node["operator"] for node in _json_nodes(data)}
    assert {"Project", "Reorder", "HashJoin", "Filter", "Scan"} <= operators


def _json_nodes(data):
    yield data
    for child in data["children"]:
        yield from _json_nodes(child)


def test_cli_analyze_prints_estimates_and_actuals(capsys):
    assert main(["--analyze", "SELECT region FROM orders WHERE year = 2024"]) == 0
    out = capsys.readouterr().out
    assert "est=" in out and "actual=" in out


def test_cli_analyze_json_is_machine_readable(capsys):
    assert main(["--analyze", "--json", "SELECT region FROM orders WHERE year = 2024"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["columns"] == ["region"]
    assert len(data["rows"]) == data["plan"]["actual_rows"]
