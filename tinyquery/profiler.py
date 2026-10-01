"""EXPLAIN ANALYZE: run a plan and report, per operator, what the planner
expected against what actually happened.

Two numbers per node are the point:

  * estimated rows, from the same statistics and formulas the join orderer
    uses, recomputed bottom-up over the finished plan;
  * actual rows, counted as the operator emits them.

When those disagree by an order of magnitude, the cost model was wrong about
that subtree, and any decision it made there was made on bad information.

Profiling works from outside the operators. For one run, each operator
instance gets its open/next_row/close wrapped with counting and timing, and
the originals are restored afterwards. No operator knows it is being watched,
and a plan that is not being profiled pays nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from tinyquery.cost import (
    DEFAULT_SELECTIVITY,
    TableStats,
    analyze,
    estimate_join_rows,
    estimate_selectivity,
)
from tinyquery.expr import BinaryOp, ColumnRef, Expr, NotOp
from tinyquery.operators import (
    Filter,
    HashAggregate,
    HashJoin,
    Limit,
    NestedLoopJoin,
    Operator,
    Scan,
    TopK,
)
from tinyquery.schema import Row, Schema

# Flag estimates this far off in either direction in the text output.
MISESTIMATE_FACTOR = 10.0


@dataclass
class PlanStats:
    """One operator's numbers, with its children's beneath it."""

    label: str
    estimated_rows: float
    actual_rows: int
    total_ms: float  # this operator plus everything beneath it
    self_ms: float  # this operator alone
    children: list[PlanStats] = field(default_factory=list)

    @property
    def misestimate(self) -> float:
        """How far off the estimate was, as a factor >= 1."""
        est = max(self.estimated_rows, 1.0)
        act = max(float(self.actual_rows), 1.0)
        return max(est / act, act / est)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "estimated_rows": round(self.estimated_rows, 1),
            "actual_rows": self.actual_rows,
            "total_ms": round(self.total_ms, 3),
            "self_ms": round(self.self_ms, 3),
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class AnalyzeResult:
    rows: list[Row]
    schema: Schema
    plan: PlanStats


def profile_plan(root: Operator) -> AnalyzeResult:
    """Execute `root` once, recording rows and time for every operator."""
    counters: dict[int, list] = {}  # id(op) -> [rows, ns]
    patched: list[Operator] = []
    try:
        for op in _walk(root):
            counters[id(op)] = [0, 0]
            _instrument(op, counters[id(op)])
            patched.append(op)

        rows: list[Row] = []
        root.open()
        try:
            while True:
                row = root.next_row()
                if row is None:
                    break
                rows.append(row)
        finally:
            root.close()
    finally:
        for op in patched:
            # Drop the instance attributes so the class methods show through again.
            for name in ("open", "next_row", "close"):
                op.__dict__.pop(name, None)

    estimates = _estimate_tree(root, _stats_by_alias(root))
    return AnalyzeResult(rows, root.schema, _build(root, counters, estimates))


def format_analyze(plan: PlanStats, indent: int = 0) -> str:
    """Indented text tree, flagging badly misestimated nodes."""
    flag = ""
    if plan.misestimate >= MISESTIMATE_FACTOR:
        flag = f"   <-- estimate off by {plan.misestimate:.0f}x"
    line = (
        "  " * indent
        + f"{plan.label}  "
        + f"(rows est={plan.estimated_rows:,.0f} actual={plan.actual_rows:,}  "
        + f"time={plan.total_ms:.2f}ms self={plan.self_ms:.2f}ms)"
        + flag
    )
    return "\n".join(
        [line] + [format_analyze(child, indent + 1) for child in plan.children]
    )


# ------------------------------------------------------------- instrumenting


def _instrument(op: Operator, counter: list) -> None:
    """Shadow the bound methods with timing wrappers on this instance only."""

    def timed(method: Callable[[], Any], counts_rows: bool) -> Callable[[], Any]:
        def wrapper() -> Any:
            start = time.perf_counter_ns()
            try:
                result = method()
            finally:
                counter[1] += time.perf_counter_ns() - start
            if counts_rows and result is not None:
                counter[0] += 1
            return result

        return wrapper

    # Bind before assigning, so each wrapper calls the real class method.
    op.open = timed(op.open, counts_rows=False)  # type: ignore[method-assign]
    op.next_row = timed(op.next_row, counts_rows=True)  # type: ignore[method-assign]
    op.close = timed(op.close, counts_rows=False)  # type: ignore[method-assign]


def _build(
    op: Operator, counters: dict[int, list], estimates: dict[int, float]
) -> PlanStats:
    children = [_build(child, counters, estimates) for child in op.children()]
    rows, ns = counters[id(op)]
    total_ms = ns / 1e6
    # Time is inclusive because a parent's next_row calls its child's. Subtract
    # the children to get what this operator spent on its own work.
    self_ms = max(0.0, total_ms - sum(child.total_ms for child in children))
    return PlanStats(
        label=op.explain_label(),
        estimated_rows=estimates[id(op)],
        actual_rows=rows,
        total_ms=total_ms,
        self_ms=self_ms,
        children=children,
    )


def _walk(op: Operator):
    yield op
    for child in op.children():
        yield from _walk(child)


# ---------------------------------------------------------------- estimating


def _stats_by_alias(root: Operator) -> dict[str, TableStats]:
    return {
        op.table: analyze(op.schema, op.rows)
        for op in _walk(root)
        if isinstance(op, Scan)
    }


def _estimate_tree(op: Operator, stats: dict[str, TableStats]) -> dict[int, float]:
    out: dict[int, float] = {}
    _estimate(op, stats, out)
    return out


def _estimate(op: Operator, stats: dict[str, TableStats], out: dict[int, float]) -> float:
    child_estimates = [_estimate(child, stats, out) for child in op.children()]
    first = child_estimates[0] if child_estimates else 1.0

    if isinstance(op, Scan):
        rows = float(stats[op.table].row_count)
    elif isinstance(op, Filter):
        table = _stats_for(op.predicate, stats)
        selectivity = (
            estimate_selectivity(op.predicate, table)
            if table is not None
            else DEFAULT_SELECTIVITY
        )
        rows = first * selectivity
    elif isinstance(op, (HashJoin, NestedLoopJoin)):
        left, right = child_estimates
        rows = estimate_join_rows(
            left,
            right,
            _ndv(op.left_key, stats, fallback=left),
            _ndv(op.right_key, stats, fallback=right),
        )
    elif isinstance(op, HashAggregate):
        if not op.group_keys:
            rows = 1.0
        else:
            groups = 1.0
            for key in op.group_keys:
                groups *= _ndv(key, stats, fallback=first)
            rows = min(first, groups)
    elif isinstance(op, (Limit, TopK)):
        rows = min(first, float(op.count))
    else:
        # Project, Reorder, Sort, Distinct: no better information than the input.
        rows = first

    if not isinstance(op, Scan):
        # An empty table really is empty; anything derived from it gets a floor.
        rows = max(1.0, rows)
    out[id(op)] = rows
    return rows


def _column_refs(expr: Expr):
    if isinstance(expr, ColumnRef):
        yield expr
    elif isinstance(expr, BinaryOp):
        yield from _column_refs(expr.left)
        yield from _column_refs(expr.right)
    elif isinstance(expr, NotOp):
        yield from _column_refs(expr.inner)


def _stats_for(expr: Expr, stats: dict[str, TableStats]) -> TableStats | None:
    """The statistics for whichever single table a predicate is about."""
    for ref in _column_refs(expr):
        if ref.table is not None and ref.table in stats:
            return stats[ref.table]
        if ref.table is None:
            owners = [s for s in stats.values() if ref.name in s.ndv]
            if len(owners) == 1:
                return owners[0]
    return None


def _ndv(expr: Expr, stats: dict[str, TableStats], fallback: float) -> int:
    if isinstance(expr, ColumnRef):
        table = (
            stats.get(expr.table)
            if expr.table is not None
            else _stats_for(expr, stats)
        )
        if table is not None:
            return table.distinct(expr.name)
    return max(1, int(fallback))
