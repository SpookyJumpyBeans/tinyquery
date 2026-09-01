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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
