"""Measure hash join against nested loop, and predicate pushdown against not.

Two questions:

  1. How much does hashing the build side actually buy over rescanning it?
     Nested loop does O(n*m) key comparisons, hash join does O(n+m). The
     ratio should grow roughly linearly with the probe side.

  2. How much does pushing a filter below the join buy? Filtering first
     shrinks the build side, so the hash table is smaller and fewer probes
     find a match.

Both operators return identical rows (tests/test_nested_loop_join.py pins
that), so the only difference measured here is the strategy.

Run:
    python benchmarks/join_benchmark.py
    python benchmarks/join_benchmark.py --sizes 1000 10000 --repeat 5
"""

from __future__ import annotations

import argparse
import random
import time

from tinyquery.engine import collect
from tinyquery.expr import BinaryOp, ColumnRef, Literal
from tinyquery.operators import Filter, HashJoin, NestedLoopJoin, Scan
from tinyquery.schema import Column, Schema

ORDER_COLUMNS = ("id", "region", "year")
LINEITEM_COLUMNS = ("order_id", "sku", "revenue")
REGIONS = ("west", "east", "north", "south")


def make_orders(n: int, rng: random.Random) -> Scan:
    schema = Schema(tuple(Column(c, table="o") for c in ORDER_COLUMNS))
    rows = [
        (i, REGIONS[i % len(REGIONS)], 2023 + (i % 2))
        for i in range(n)
    ]
    rng.shuffle(rows)
    return Scan("o", schema, rows)


def make_lineitems(n_orders: int, per_order: int, rng: random.Random) -> Scan:
    schema = Schema(tuple(Column(c, table="l") for c in LINEITEM_COLUMNS))
    rows = [
        (rng.randrange(n_orders), "sku", rng.randrange(1, 500))
        for _ in range(n_orders * per_order)
    ]
    return Scan("l", schema, rows)


def time_join(op_class, orders: Scan, lineitems: Scan, repeat: int) -> tuple[float, int]:
    """Best-of-`repeat` wall time, plus the row count as a correctness check."""
    best = float("inf")
    count = -1
    for _ in range(repeat):
        join = op_class(
            orders,
            lineitems,
            ColumnRef("id", table="o"),
            ColumnRef("order_id", table="l"),
        )
        start = time.perf_counter()
        rows, _ = collect(join)
        best = min(best, time.perf_counter() - start)
        count = len(rows)
    return best, count


def time_pushdown(orders: Scan, lineitems: Scan, repeat: int) -> tuple[float, float, int]:
    """Filter below the join versus above it, same predicate either way."""
    predicate = BinaryOp("=", ColumnRef("year", table="o"), Literal(2024))
    keys = (ColumnRef("id", table="o"), ColumnRef("order_id", table="l"))

    below = float("inf")
    above = float("inf")
    count = -1
    for _ in range(repeat):
        pushed = HashJoin(Filter(orders, predicate), lineitems, *keys)
        start = time.perf_counter()
        rows, _ = collect(pushed)
        below = min(below, time.perf_counter() - start)
        count = len(rows)

        naive = Filter(HashJoin(orders, lineitems, *keys), predicate)
        start = time.perf_counter()
        collect(naive)
        above = min(above, time.perf_counter() - start)
    return below, above, count


def time_join_order(n_facts: int, n_dim: int, n_tiny: int, repeat: int) -> tuple[float, float, int]:
    """Cost-based join order versus joining in the order written.

    A fact table joined to a wide dimension and a heavily filtered tiny one.
    Written order pays for the wide join across every fact row; the cost model
    joins the filtered table first and shrinks the input to everything after.
    """
    from tinyquery.catalog import Catalog
    from tinyquery.engine import execute
    from tinyquery.schema import Column, Schema

    catalog = Catalog()
    for name, columns, rows in (
        ("f", ["id", "dim_id", "tiny_id", "amount"],
         [(i, i % n_dim, i % n_tiny, i) for i in range(n_facts)]),
        ("d", ["id", "label"], [(i, f"d{i}") for i in range(n_dim)]),
        ("t", ["id", "label"], [(i, f"t{i}") for i in range(n_tiny)]),
    ):
        catalog.register(
            name, Schema(tuple(Column(c, table=name) for c in columns)), rows
        )

    sql = """
        SELECT f.id, d.label, t.label
        FROM f
        JOIN d ON f.dim_id = d.id
        JOIN t ON f.tiny_id = t.id
        WHERE t.id = 1
    """
    best_on = float("inf")
    best_off = float("inf")
    count = -1
    for _ in range(repeat):
        start = time.perf_counter()
        rows, _ = execute(catalog, sql)
        best_on = min(best_on, time.perf_counter() - start)
        count = len(rows)

        start = time.perf_counter()
        rows_off, _ = execute(catalog, sql, reorder=False)
        best_off = min(best_off, time.perf_counter() - start)
        assert len(rows_off) == count, "reordering changed the row count"
    return best_on, best_off, count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes", type=int, nargs="+", default=[1_000, 2_000, 5_000, 20_000, 100_000],
        help="number of orders; lineitems are --per-order times this",
    )
    parser.add_argument("--per-order", type=int, default=4)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--max-nested", type=int, default=2_000,
        help="skip nested loop above this size; it is quadratic and will crawl",
    )
    args = parser.parse_args()
    rng = random.Random(20260831)

    print(f"{'orders':>8} {'lineitems':>10} {'rows out':>10} "
          f"{'hash':>9} {'nested':>11} {'speedup':>9}")
    print("-" * 62)
    for n in args.sizes:
        orders = make_orders(n, rng)
        lineitems = make_lineitems(n, args.per_order, rng)
        hash_s, hash_rows = time_join(HashJoin, orders, lineitems, args.repeat)
        if n <= args.max_nested:
            # One pass only: it is quadratic and deterministic, so repeating
            # it costs minutes and tells us nothing extra.
            nested_s, nested_rows = time_join(NestedLoopJoin, orders, lineitems, 1)
            assert nested_rows == hash_rows, "operators disagree on row count"
            print(f"{n:>8,} {n * args.per_order:>10,} {hash_rows:>10,} "
                  f"{hash_s:>8.3f}s {nested_s:>10.3f}s {nested_s / hash_s:>8.0f}x")
        else:
            print(f"{n:>8,} {n * args.per_order:>10,} {hash_rows:>10,} "
                  f"{hash_s:>8.3f}s {'skipped':>11} {'':>9}")

    print()
    print(f"{'orders':>8} {'rows out':>10} {'pushed down':>13} "
          f"{'above join':>12} {'speedup':>9}")
    print("-" * 58)
    for n in args.sizes:
        orders = make_orders(n, rng)
        lineitems = make_lineitems(n, args.per_order, rng)
        below, above, rows = time_pushdown(orders, lineitems, args.repeat)
        print(f"{n:>8,} {rows:>10,} {below:>12.3f}s {above:>11.3f}s "
              f"{above / below:>8.2f}x")

    print()
    print(f"{'facts':>8} {'rows out':>10} {'cost-based':>12} "
          f"{'as written':>12} {'speedup':>9}")
    print("-" * 56)
    for n in args.sizes:
        on, off, rows = time_join_order(n, n_dim=200, n_tiny=50, repeat=args.repeat)
        print(f"{n:>8,} {rows:>10,} {on:>11.3f}s {off:>11.3f}s {off / on:>8.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
