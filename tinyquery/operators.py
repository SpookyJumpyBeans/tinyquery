from __future__ import annotations

import heapq
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any

from tinyquery.expr import Aggregate, Expr
from tinyquery.schema import Column, Row, Schema


class Operator(ABC):
    schema: Schema

    @abstractmethod
    def open(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def next_row(self) -> Row | None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError

    def explain_label(self) -> str:
        return type(self).__name__

    def children(self) -> list[Operator]:
        return []


class Scan(Operator):
    def __init__(self, table: str, schema: Schema, rows: list[Row]) -> None:
        self.table = table
        self.schema = schema
        self.rows = rows
        self._i = 0

    def open(self) -> None:
        self._i = 0

    def next_row(self) -> Row | None:
        if self._i >= len(self.rows):
            return None
        row = self.rows[self._i]
        self._i += 1
        return row

    def close(self) -> None:
        return

    def explain_label(self) -> str:
        return f"Scan {self.table}"


class Filter(Operator):
    def __init__(self, child: Operator, predicate: Expr) -> None:
        self.child = child
        self.predicate = predicate
        self.schema = child.schema

    def open(self) -> None:
        self.child.open()

    def next_row(self) -> Row | None:
        while True:
            row = self.child.next_row()
            if row is None:
                return None
            if self.predicate.eval(row, self.schema):
                return row

    def close(self) -> None:
        self.child.close()

    def explain_label(self) -> str:
        return f"Filter {self.predicate}"

    def children(self) -> list[Operator]:
        return [self.child]


class Project(Operator):
    def __init__(self, child: Operator, exprs: list[Expr], names: list[str]) -> None:
        self.child = child
        self.exprs = exprs
        self.schema = Schema(tuple(Column(name) for name in names))

    def open(self) -> None:
        self.child.open()

    def next_row(self) -> Row | None:
        row = self.child.next_row()
        if row is None:
            return None
        return tuple(expr.eval(row, self.child.schema) for expr in self.exprs)

    def close(self) -> None:
        self.child.close()

    def explain_label(self) -> str:
        cols = ", ".join(col.name for col in self.schema.columns)
        return f"Project {cols}"

    def children(self) -> list[Operator]:
        return [self.child]


class HashJoin(Operator):
    """Inner equijoin. Build a hash table on the left input, probe with the right.

    NULL join keys never match (same as SQL). The whole left side is stored in
    memory; if it does not fit, a real engine would partition/spill.
    """

    def __init__(
        self,
        left: Operator,
        right: Operator,
        left_key: Expr,
        right_key: Expr,
    ) -> None:
        self.left = left
        self.right = right
        self.left_key = left_key
        self.right_key = right_key
        self.schema = left.schema.concat(right.schema)
        self._table: dict[Any, list[Row]] = {}
        self._probe_row: Row | None = None
        self._matches: list[Row] = []
        self._match_i = 0

    def open(self) -> None:
        self._table = defaultdict(list)
        self.left.open()
        try:
            while True:
                row = self.left.next_row()
                if row is None:
                    break
                key = self.left_key.eval(row, self.left.schema)
                if key is None:
                    continue
                self._table[key].append(row)
        finally:
            self.left.close()

        self.right.open()
        self._probe_row = None
        self._matches = []
        self._match_i = 0

    def next_row(self) -> Row | None:
        while True:
            if self._match_i < len(self._matches):
                left_row = self._matches[self._match_i]
                self._match_i += 1
                assert self._probe_row is not None
                return left_row + self._probe_row

            probe = self.right.next_row()
            if probe is None:
                return None
            key = self.right_key.eval(probe, self.right.schema)
            self._probe_row = probe
            self._matches = self._table.get(key, []) if key is not None else []
            self._match_i = 0

    def close(self) -> None:
        self.right.close()
        self._table = {}

    def explain_label(self) -> str:
        return f"HashJoin {self.left_key} = {self.right_key}"

    def children(self) -> list[Operator]:
        return [self.left, self.right]


class _AggState:
    def __init__(self, spec: Aggregate) -> None:
        self.spec = spec
        self.count = 0
        self.sum: int | float = 0
        self.min: Any = None
        self.max: Any = None

    def add(self, value: Any) -> None:
        if self.spec.func == "COUNT":
            if self.spec.arg is None or value is not None:
                self.count += 1
            return
        if value is None:
            return
        self.count += 1
        if self.spec.func == "SUM" or self.spec.func == "AVG":
            self.sum += value
        if self.min is None or value < self.min:
            self.min = value
        if self.max is None or value > self.max:
            self.max = value

    def finish(self) -> Any:
        func = self.spec.func
        if func == "COUNT":
            return self.count
        if self.count == 0:
            return None
        if func == "SUM":
            return self.sum
        if func == "AVG":
            return self.sum / self.count
        if func == "MIN":
            return self.min
        if func == "MAX":
            return self.max
        raise ValueError(f"unknown aggregate {func}")


class HashAggregate(Operator):
    """Group rows in a hash table, then emit one output row per group."""

    def __init__(
        self,
        child: Operator,
        group_keys: list[Expr],
        group_names: list[str],
        aggregates: list[Aggregate],
    ) -> None:
        self.child = child
        self.group_keys = group_keys
        self.aggregates = aggregates
        self.schema = Schema(
            tuple(Column(name) for name in group_names)
            + tuple(Column(agg.output_name()) for agg in aggregates)
        )
        self._results: list[Row] = []
        self._i = 0

    def open(self) -> None:
        groups: dict[tuple[Any, ...], list[_AggState]] = {}
        self.child.open()
        try:
            while True:
                row = self.child.next_row()
                if row is None:
                    break
                key = tuple(expr.eval(row, self.child.schema) for expr in self.group_keys)
                if key not in groups:
                    groups[key] = [_AggState(spec) for spec in self.aggregates]
                for spec, state in zip(self.aggregates, groups[key], strict=True):
                    value = None if spec.arg is None else spec.arg.eval(row, self.child.schema)
                    state.add(value)
        finally:
            self.child.close()

        self._results = [
            key + tuple(state.finish() for state in states) for key, states in groups.items()
        ]
        # No groups at all: COUNT(*) without GROUP BY should still return one row.
        if not self._results and not self.group_keys:
            empty = [_AggState(spec) for spec in self.aggregates]
            self._results = [tuple(state.finish() for state in empty)]
        self._i = 0

    def next_row(self) -> Row | None:
        if self._i >= len(self._results):
            return None
        row = self._results[self._i]
        self._i += 1
        return row

    def close(self) -> None:
        self._results = []

    def explain_label(self) -> str:
        keys = ", ".join(str(k) for k in self.group_keys) or "(all rows)"
        aggs = ", ".join(str(a) for a in self.aggregates)
        return f"HashAggregate group_by=[{keys}] aggs=[{aggs}]"

    def children(self) -> list[Operator]:
        return [self.child]


class _OrderKey:
    """Compare values so NULLs sort last. One column can be DESC without flipping NULLs."""

    def __init__(self, value: Any, descending: bool) -> None:
        self.value = value
        self.descending = descending

    def __lt__(self, other: _OrderKey) -> bool:
        if self.value is None and other.value is None:
            return False
        if self.value is None:
            return False
        if other.value is None:
            return True
        if self.descending:
            return other.value < self.value
        return self.value < other.value

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _OrderKey):
            return NotImplemented
        return self.value == other.value


class Sort(Operator):
    """Blocking sort: read the child fully, then emit in order.

    LIMIT after this still pays for the full sort. A real engine would keep a heap
    of size LIMIT; we do not.
    """

    def __init__(self, child: Operator, keys: list[Expr], descending: list[bool]) -> None:
        self.child = child
        self.keys = keys
        self.descending = descending
        self.schema = child.schema
        self._rows: list[Row] = []
        self._i = 0

    def open(self) -> None:
        self.child.open()
        rows: list[Row] = []
        try:
            while True:
                row = self.child.next_row()
                if row is None:
                    break
                rows.append(row)
        finally:
            self.child.close()
        rows.sort(key=self._key)
        self._rows = rows
        self._i = 0

    def _key(self, row: Row) -> tuple[_OrderKey, ...]:
        return tuple(
            _OrderKey(expr.eval(row, self.schema), desc)
            for expr, desc in zip(self.keys, self.descending, strict=True)
        )

    def next_row(self) -> Row | None:
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def close(self) -> None:
        self._rows = []

    def explain_label(self) -> str:
        parts = []
        for expr, desc in zip(self.keys, self.descending, strict=True):
            parts.append(f"{expr} {'DESC' if desc else 'ASC'}")
        return f"Sort {', '.join(parts)}"

    def children(self) -> list[Operator]:
        return [self.child]


class Limit(Operator):
    """Stop after n rows. Cheap if the child is already streaming; not a top-k."""

    def __init__(self, child: Operator, count: int) -> None:
        self.child = child
        self.count = count
        self.schema = child.schema
        self._emitted = 0

    def open(self) -> None:
        self._emitted = 0
        self.child.open()

    def next_row(self) -> Row | None:
        if self._emitted >= self.count:
            return None
        row = self.child.next_row()
        if row is None:
            return None
        self._emitted += 1
        return row

    def close(self) -> None:
        self.child.close()

    def explain_label(self) -> str:
        return f"Limit {self.count}"

    def children(self) -> list[Operator]:
        return [self.child]


class Distinct(Operator):
    """Drop duplicate output rows. Memory grows with the number of unique rows."""

    def __init__(self, child: Operator) -> None:
        self.child = child
        self.schema = child.schema
        self._seen: set[Row] = set()

    def open(self) -> None:
        self._seen = set()
        self.child.open()

    def next_row(self) -> Row | None:
        while True:
            row = self.child.next_row()
            if row is None:
                return None
            if row in self._seen:
                continue
            self._seen.add(row)
            return row

    def close(self) -> None:
        self._seen = set()
        self.child.close()

    def explain_label(self) -> str:
        return "Distinct"

    def children(self) -> list[Operator]:
        return [self.child]


class _WorseKey:
    """Heap ordering: smaller means worse (should be evicted from the top-k first)."""

    def __init__(self, keys: tuple[_OrderKey, ...]) -> None:
        self.keys = keys

    def __lt__(self, other: _WorseKey) -> bool:
        return other.keys < self.keys


class TopK(Operator):
    """ORDER BY + LIMIT without a full sort: keep a heap of the best k rows.

    Still reads the whole child once, but memory is O(k) instead of O(n).
    """

    def __init__(
        self,
        child: Operator,
        keys: list[Expr],
        descending: list[bool],
        count: int,
    ) -> None:
        self.child = child
        self.keys = keys
        self.descending = descending
        self.count = count
        self.schema = child.schema
        self._rows: list[Row] = []
        self._i = 0

    def open(self) -> None:
        self._rows = []
        self._i = 0
        if self.count <= 0:
            self.child.open()
            self.child.close()
            return

        heap: list[tuple[_WorseKey, int, Row]] = []
        seq = 0
        self.child.open()
        try:
            while True:
                row = self.child.next_row()
                if row is None:
                    break
                order = self._order_keys(row)
                item = (_WorseKey(order), seq, row)
                seq += 1
                if len(heap) < self.count:
                    heapq.heappush(heap, item)
                elif order < heap[0][0].keys:
                    heapq.heapreplace(heap, item)
        finally:
            self.child.close()

        rows = [entry[2] for entry in heap]
        rows.sort(key=self._order_keys)
        self._rows = rows

    def _order_keys(self, row: Row) -> tuple[_OrderKey, ...]:
        return tuple(
            _OrderKey(expr.eval(row, self.schema), desc)
            for expr, desc in zip(self.keys, self.descending, strict=True)
        )

    def next_row(self) -> Row | None:
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def close(self) -> None:
        self._rows = []

    def explain_label(self) -> str:
        parts = []
        for expr, desc in zip(self.keys, self.descending, strict=True):
            parts.append(f"{expr} {'DESC' if desc else 'ASC'}")
        return f"TopK {self.count} by {', '.join(parts)}"

    def children(self) -> list[Operator]:
        return [self.child]
