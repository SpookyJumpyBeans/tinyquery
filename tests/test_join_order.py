"""The optimiser may change the join order but never the answer.

Every test here compares `reorder=True` against `reorder=False` on the same
data, so a reordering bug shows up as a difference rather than as a plausible
looking wrong result.
"""

import random

import pytest

from tinyquery.catalog import Catalog
from tinyquery.cost import analyze, estimate_join_rows, estimate_selectivity
from tinyquery.engine import execute, explain
from tinyquery.expr import BinaryOp, ColumnRef, Literal
from tinyquery.schema import Column, Schema


def catalog_with(**tables):
    """Build a catalog from {name: (columns, rows)}."""
    catalog = Catalog()
    for name, (columns, rows) in tables.items():
        schema = Schema(tuple(Column(c, table=name) for c in columns))
        catalog.register(name, schema, rows)
    return catalog


def star_catalog(n_facts=400, n_dim=40, n_tiny=3):
    """A fact table joined to a big dimension and a very small one.

    Joining the tiny table first shrinks the intermediate result immediately;
    joining it last means carrying every fact row through the wide join. Same
    answer either way, very different amount of work.
    """
    facts = [(i, i % n_dim, i % n_tiny, i) for i in range(n_facts)]
    dims = [(i, f"d{i}") for i in range(n_dim)]
    tinies = [(i, f"t{i}") for i in range(n_tiny)]
    return catalog_with(
        f=(["id", "dim_id", "tiny_id", "amount"], facts),
        d=(["id", "label"], dims),
        t=(["id", "label"], tinies),
    )


THREE_TABLE_SQL = """
    SELECT f.id, d.label, t.label
    FROM f
    JOIN d ON f.dim_id = d.id
    JOIN t ON f.tiny_id = t.id
    WHERE t.id = 1
"""


def test_same_rows_with_and_without_reordering():
    catalog = star_catalog()
    reordered, _ = execute(catalog, THREE_TABLE_SQL)
    written, _ = execute(catalog, THREE_TABLE_SQL, reorder=False)
    assert sorted(reordered) == sorted(written)
    assert reordered  # and it is not trivially empty


def test_reordering_actually_changes_the_plan():
    """If the plan never changes, the rest of these tests prove nothing."""
    catalog = star_catalog()
    assert explain(catalog, THREE_TABLE_SQL) != explain(
        catalog, THREE_TABLE_SQL, reorder=False
    )


def test_select_star_keeps_declared_column_order():
    """Executing joins out of order moves columns; the output must not show it."""
    catalog = star_catalog()
    sql = "SELECT * FROM f JOIN d ON f.dim_id = d.id JOIN t ON f.tiny_id = t.id"
    reordered, schema_a = execute(catalog, sql)
    written, schema_b = execute(catalog, sql, reorder=False)
    assert schema_a.names() == schema_b.names()
    assert schema_a.names()[:4] == ["f.id", "f.dim_id", "f.tiny_id", "f.amount"]
    assert sorted(reordered) == sorted(written)


def test_aggregates_survive_reordering():
    catalog = star_catalog()
    sql = """
        SELECT d.label, SUM(f.amount) AS total
        FROM f
        JOIN d ON f.dim_id = d.id
        JOIN t ON f.tiny_id = t.id
        GROUP BY d.label
    """
    assert sorted(execute(catalog, sql)[0]) == sorted(
        execute(catalog, sql, reorder=False)[0]
    )


def test_two_table_joins_are_left_alone():
    """One join has only one left-deep shape, so there is nothing to choose."""
    catalog = star_catalog()
    sql = "SELECT f.id FROM f JOIN d ON f.dim_id = d.id"
    assert explain(catalog, sql) == explain(catalog, sql, reorder=False)


def test_chain_join_agrees():
    """f to d to t, rather than both joins hanging off f."""
    catalog = star_catalog()
    sql = """
        SELECT f.id
        FROM f
        JOIN d ON f.dim_id = d.id
        JOIN t ON d.id = t.id
    """
    assert sorted(execute(catalog, sql)[0]) == sorted(
        execute(catalog, sql, reorder=False)[0]
    )


def test_the_filtered_table_is_joined_first():
    """The whole point: a filter that leaves one row should lead the plan."""
    catalog = star_catalog()
    plan = explain(catalog, THREE_TABLE_SQL)
    # The filtered scan of t sits at the bottom, inside the innermost join.
    assert plan.index("Filter t.id = 1") > plan.index("HashJoin")
    assert plan.rindex("Scan d") > plan.index("Filter t.id = 1")


@pytest.mark.parametrize("seed", range(15))
def test_random_three_table_joins_agree(seed):
    rng = random.Random(seed)
    n_dim = rng.randint(2, 12)
    n_tiny = rng.randint(1, 5)
    n_facts = rng.randint(0, 120)
    facts = [
        (i, rng.randrange(n_dim + 2), rng.randrange(n_tiny + 2), rng.randrange(50))
        for i in range(n_facts)
    ]
    catalog = catalog_with(
        f=(["id", "dim_id", "tiny_id", "amount"], facts),
        d=(["id", "label"], [(i, f"d{i}") for i in range(n_dim)]),
        t=(["id", "label"], [(i, f"t{i}") for i in range(n_tiny)]),
    )
    sql = """
        SELECT f.id, d.label, t.label
        FROM f
        JOIN d ON f.dim_id = d.id
        JOIN t ON f.tiny_id = t.id
    """
    assert sorted(execute(catalog, sql)[0]) == sorted(
        execute(catalog, sql, reorder=False)[0]
    )


# ----------------------------------------------------------- cost model units


def test_analyze_counts_rows_and_distinct_values():
    schema = Schema((Column("a"), Column("b")))
    stats = analyze(schema, [(1, "x"), (1, "y"), (2, "x"), (None, "x")])
    assert stats.row_count == 4
    assert stats.distinct("a") == 2  # NULL is not a distinct value
    assert stats.distinct("b") == 2


def test_equality_selectivity_follows_distinct_count():
    schema = Schema((Column("a"),))
    stats = analyze(schema, [(i % 10,) for i in range(100)])
    predicate = BinaryOp("=", ColumnRef("a"), Literal(3))
    assert estimate_selectivity(predicate, stats) == pytest.approx(0.1)


def test_and_multiplies_selectivity():
    schema = Schema((Column("a"), Column("b")))
    stats = analyze(schema, [(i % 10, i % 4) for i in range(100)])
    both = BinaryOp(
        "AND",
        BinaryOp("=", ColumnRef("a"), Literal(1)),
        BinaryOp("=", ColumnRef("b"), Literal(1)),
    )
    assert estimate_selectivity(both, stats) == pytest.approx(0.1 * 0.25)


def test_join_estimate_divides_by_the_wider_key():
    # 100 rows joined to 10 rows on a key with 10 distinct values -> 100.
    assert estimate_join_rows(100, 10, 10, 10) == pytest.approx(100)
    # A more selective key on either side means fewer matches.
    assert estimate_join_rows(100, 100, 100, 10) == pytest.approx(100)


def test_join_estimate_never_returns_zero():
    assert estimate_join_rows(0, 0, 1, 1) >= 1.0


def test_not_predicate_does_not_crash_the_cost_model():
    """Regression: the estimator read NotOp.expr, but the field is NotOp.inner,
    so any NOT filter in a 3+ table join raised AttributeError while planning."""
    catalog = star_catalog()
    sql = """
        SELECT f.id
        FROM f
        JOIN d ON f.dim_id = d.id
        JOIN t ON f.tiny_id = t.id
        WHERE NOT t.id = 1
    """
    assert sorted(execute(catalog, sql)[0]) == sorted(
        execute(catalog, sql, reorder=False)[0]
    )


def test_not_inverts_selectivity():
    schema = Schema((Column("a"),))
    stats = analyze(schema, [(i % 4,) for i in range(100)])
    from tinyquery.expr import NotOp
    predicate = NotOp(BinaryOp("=", ColumnRef("a"), Literal(1)))
    assert estimate_selectivity(predicate, stats) == pytest.approx(0.75)
