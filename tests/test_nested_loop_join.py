"""NestedLoopJoin is the baseline HashJoin gets measured against.

A speed comparison only means something if both operators return the same
answer, so these pin the two together rather than testing nested loop alone.
"""

import random

from tinyquery.engine import collect
from tinyquery.expr import ColumnRef
from tinyquery.operators import HashJoin, NestedLoopJoin, Scan
from tinyquery.schema import Column, Schema


def _table(name, columns, rows):
    schema = Schema(tuple(Column(c, table=name) for c in columns))
    return Scan(name, schema, rows)


def _both(left_rows, right_rows):
    """Run the same join through each operator and return (hash, nested)."""
    results = []
    for op in (HashJoin, NestedLoopJoin):
        left = _table("l", ["id", "region"], list(left_rows))
        right = _table("r", ["fk", "amount"], list(right_rows))
        join = op(
            left,
            right,
            ColumnRef("id", table="l"),
            ColumnRef("fk", table="r"),
        )
        rows, _ = collect(join)
        results.append(sorted(rows))
    return results


def test_agrees_on_a_simple_equijoin():
    hash_rows, nested_rows = _both(
        [(1, "west"), (2, "east"), (3, "north")],
        [(1, 10), (1, 5), (2, 20)],
    )
    assert hash_rows == nested_rows
    assert hash_rows == [
        (1, "west", 1, 5),
        (1, "west", 1, 10),
        (2, "east", 2, 20),
    ]


def test_agrees_when_null_keys_are_present():
    """NULL must not match on either side, in either operator."""
    hash_rows, nested_rows = _both(
        [(1, "west"), (None, "unknown"), (2, "east")],
        [(1, 10), (None, 99), (2, 20)],
    )
    assert hash_rows == nested_rows
    assert all(row[0] is not None for row in hash_rows)
    assert 99 not in [row[3] for row in hash_rows]


def test_agrees_on_a_many_to_many_join():
    hash_rows, nested_rows = _both(
        [(1, "a"), (1, "b"), (2, "c")],
        [(1, 10), (1, 20), (2, 30)],
    )
    assert hash_rows == nested_rows
    assert len(hash_rows) == 5  # 2x2 on key 1, 1x1 on key 2


def test_agrees_when_one_side_is_empty():
    assert _both([], [(1, 10)]) == [[], []]
    assert _both([(1, "west")], []) == [[], []]


def test_agrees_on_random_inputs():
    """Fuzz the shapes rather than hand-picking cases the code already passes."""
    rng = random.Random(1234)
    for _ in range(40):
        left = [
            (rng.choice([None, 1, 2, 3, 4]), f"r{i}")
            for i in range(rng.randint(0, 30))
        ]
        right = [
            (rng.choice([None, 1, 2, 3, 5]), rng.randint(0, 100))
            for _ in range(rng.randint(0, 30))
        ]
        hash_rows, nested_rows = _both(left, right)
        assert hash_rows == nested_rows


def test_explain_names_the_operator():
    left = _table("l", ["id", "region"], [(1, "west")])
    right = _table("r", ["fk", "amount"], [(1, 10)])
    join = NestedLoopJoin(
        left, right, ColumnRef("id", table="l"), ColumnRef("fk", table="r")
    )
    assert join.explain_label().startswith("NestedLoopJoin")
    assert len(join.children()) == 2
