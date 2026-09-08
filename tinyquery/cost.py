"""Table statistics and a cost model for choosing a join order.

The planner used to join tables in the order they were written. That is fine
until the order is wrong, at which point an intermediate result blows up and
every later join pays for it. This module decides the order instead.

Three pieces:

  * `analyze` walks a table once and records its row count and how many
    distinct values each column holds. That is what a real engine gets from
    ANALYZE, except a real engine samples and this one can afford to look at
    everything, because the table is already in memory.

  * `estimate_*` turns those counts into a guess at how many rows a filter or
    a join will produce. Textbook formulas, and they are guesses: the point of
    a cost model is to rank plans, not to be right.

  * `choose_join_order` searches left-deep plans with the Selinger dynamic
    program, keeping the cheapest way to produce each subset of tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

from tinyquery.expr import BinaryOp, ColumnRef, Expr, Literal, NotOp
from tinyquery.schema import Row, Schema

# Used when we have no basis for a guess. Postgres uses a similar constant.
DEFAULT_SELECTIVITY = 0.1
# Range comparisons against an unknown distribution.
RANGE_SELECTIVITY = 1.0 / 3.0
# Beyond this many tables the subset DP costs more than the plan it saves.
MAX_TABLES_TO_REORDER = 10


@dataclass(frozen=True)
class TableStats:
    """What ANALYZE would record for one table."""

    row_count: int
    ndv: dict[str, int]  # column name -> number of distinct non-null values

    def distinct(self, column: str) -> int:
        """Distinct values in a column, falling back to "all rows differ"."""
        return max(1, self.ndv.get(column, self.row_count))


def analyze(schema: Schema, rows: Sequence[Row]) -> TableStats:
    """One pass over the table, counting rows and distinct values per column."""
    seen: list[set] = [set() for _ in schema.columns]
    for row in rows:
        for i, value in enumerate(row):
            if value is not None:
                seen[i].add(value)
    return TableStats(
        row_count=len(rows),
        ndv={col.name: len(seen[i]) for i, col in enumerate(schema.columns)},
    )


def estimate_selectivity(predicate: Expr, stats: TableStats) -> float:
    """Fraction of rows a predicate is expected to keep, in [0, 1]."""
    if isinstance(predicate, BinaryOp):
        if predicate.op == "AND":
            return estimate_selectivity(predicate.left, stats) * estimate_selectivity(
                predicate.right, stats
            )
        if predicate.op == "OR":
            left = estimate_selectivity(predicate.left, stats)
            right = estimate_selectivity(predicate.right, stats)
            # Inclusion-exclusion, assuming the two are independent.
            return min(1.0, left + right - left * right)
        if predicate.op in ("=", "!=", "<>"):
            column = _column_of(predicate)
            if column is None:
                return DEFAULT_SELECTIVITY
            equality = 1.0 / stats.distinct(column)
            return equality if predicate.op == "=" else 1.0 - equality
        if predicate.op in ("<", "<=", ">", ">="):
            return RANGE_SELECTIVITY
    if isinstance(predicate, NotOp):
        return 1.0 - estimate_selectivity(predicate.expr, stats)
    return DEFAULT_SELECTIVITY


def estimate_filtered_rows(predicates: Iterable[Expr], stats: TableStats) -> float:
    rows = float(stats.row_count)
    for predicate in predicates:
        rows *= estimate_selectivity(predicate, stats)
    return max(1.0, rows)


def estimate_join_rows(
    left_rows: float, right_rows: float, left_ndv: int, right_ndv: int
) -> float:
    """Textbook equijoin estimate: |A| * |B| / max(distinct keys on either side).

    The intuition is that the side with more distinct keys spreads its rows
    thinner, so it decides how many matches an average row on the other side
    finds. Assumes keys on the smaller side all appear on the larger one, which
    is exactly the assumption that makes estimates drift on real data.
    """
    return max(1.0, left_rows * right_rows / max(1, left_ndv, right_ndv))


@dataclass(frozen=True)
class JoinEdge:
    """An equijoin predicate, as a link between two table aliases."""

    left_alias: str
    left_key: Expr
    right_alias: str
    right_key: Expr

    def other(self, alias: str) -> str:
        return self.right_alias if alias == self.left_alias else self.left_alias

    def touches(self, alias: str) -> bool:
        return alias in (self.left_alias, self.right_alias)

    def key_for(self, alias: str) -> Expr:
        return self.left_key if alias == self.left_alias else self.right_key


@dataclass(frozen=True)
class JoinStep:
    """One join in the chosen order: add `alias` to what we already have."""

    alias: str
    edge: JoinEdge


@dataclass(frozen=True)
class JoinPlan:
    order: tuple[str, ...]
    steps: tuple[JoinStep, ...]
    estimated_rows: float
    cost: float


def choose_join_order(
    aliases: Sequence[str],
    base_rows: dict[str, float],
    key_ndv: dict[tuple[str, str], int],
    edges: Sequence[JoinEdge],
) -> JoinPlan | None:
    """Cheapest left-deep join order, or None if we should not reorder.

    `key_ndv` maps (alias, column) to distinct values in that base column, used
    to estimate how many rows a join produces.

    Cost is the number of rows a hash join has to touch, which is the build
    side plus the probe side. Summing that over the whole plan rewards orders
    that keep intermediate results small, which is the entire point.

    Returns None when there is nothing to decide (fewer than two joins) or when
    the search would be too large to be worth it.
    """
    n = len(aliases)
    if n < 3 or n > MAX_TABLES_TO_REORDER:
        # With two tables there is only one left-deep shape, so nothing to pick.
        return None
    if not _is_connected(aliases, edges):
        # A cross product somewhere; leave the written order alone.
        return None

    # best[subset] = (cost so far, estimated rows, order, steps)
    best: dict[frozenset[str], tuple[float, float, tuple[str, ...], tuple[JoinStep, ...]]] = {}
    for alias in aliases:
        best[frozenset([alias])] = (0.0, base_rows[alias], (alias,), ())

    for size in range(2, n + 1):
        for subset in combinations(aliases, size):
            key = frozenset(subset)
            for alias in subset:
                prefix = key - {alias}
                if prefix not in best:
                    continue
                edge = _connecting_edge(prefix, alias, edges)
                if edge is None:
                    continue  # would be a cross product at this step
                prefix_cost, prefix_rows, order, steps = best[prefix]
                left_alias = edge.other(alias)
                rows = estimate_join_rows(
                    prefix_rows,
                    base_rows[alias],
                    key_ndv.get((left_alias, _column_name(edge.key_for(left_alias))), 1),
                    key_ndv.get((alias, _column_name(edge.key_for(alias))), 1),
                )
                cost = prefix_cost + prefix_rows + base_rows[alias]
                current = best.get(key)
                if current is None or cost < current[0]:
                    best[key] = (
                        cost,
                        rows,
                        order + (alias,),
                        steps + (JoinStep(alias, edge),),
                    )

    final = best.get(frozenset(aliases))
    if final is None:
        return None
    cost, rows, order, steps = final
    return JoinPlan(order=order, steps=steps, estimated_rows=rows, cost=cost)


def _connecting_edge(
    have: frozenset[str], alias: str, edges: Sequence[JoinEdge]
) -> JoinEdge | None:
    for edge in edges:
        if edge.touches(alias) and edge.other(alias) in have:
            return edge
    return None


def _is_connected(aliases: Sequence[str], edges: Sequence[JoinEdge]) -> bool:
    if not aliases:
        return False
    reached = {aliases[0]}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            for a, b in (
                (edge.left_alias, edge.right_alias),
                (edge.right_alias, edge.left_alias),
            ):
                if a in reached and b not in reached:
                    reached.add(b)
                    changed = True
    return reached.issuperset(aliases)


def _column_of(predicate: BinaryOp) -> str | None:
    """Column name in a `column <op> literal` comparison, if it is one."""
    if isinstance(predicate.left, ColumnRef) and isinstance(predicate.right, Literal):
        return predicate.left.name
    if isinstance(predicate.right, ColumnRef) and isinstance(predicate.left, Literal):
        return predicate.right.name
    return None


def _column_name(expr: Expr) -> str:
    return expr.name if isinstance(expr, ColumnRef) else str(expr)
