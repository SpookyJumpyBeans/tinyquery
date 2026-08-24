from __future__ import annotations

from tinyquery.catalog import Catalog, Table
from tinyquery.errors import TinyQueryError
from tinyquery.expr import Aggregate, BinaryOp, ColumnRef, Expr
from tinyquery.operators import Filter, HashAggregate, HashJoin, Limit, Operator, Project, Scan, Sort
from tinyquery.parser import Query, TableRef
from tinyquery.schema import Column, Schema


class Planner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def plan(self, query: Query) -> Operator:
        # Validate WHERE against the full joined schema first so ambiguous
        # columns error the same way they would without pushdown.
        full_schema = self._joined_schema(query)
        conjuncts: list[Expr] = []
        if query.where is not None:
            conjuncts = _flatten_and(query.where)
            for pred in conjuncts:
                _require_resolvable(pred, full_schema)

        node = self._scan(query.from_table)
        pushed, conjuncts = _take_resolvable(conjuncts, node.schema)
        node = _with_filter(node, pushed)

        for join in query.joins:
            right = self._scan(join.table)
            pushed, conjuncts = _take_resolvable(conjuncts, right.schema)
            right = _with_filter(right, pushed)
            left_key, right_key = self._bind_join_keys(
                node.schema, right.schema, join.left_key, join.right_key
            )
            node = HashJoin(node, right, left_key, right_key)

        # Cross-table predicates (and ORs we could not split) stay above the join.
        node = _with_filter(node, conjuncts)

        select = query.select
        if len(select) == 1 and select[0].is_star():
            if query.group_by or any(item.is_aggregate() for item in select):
                raise TinyQueryError("SELECT * cannot be mixed with aggregates / GROUP BY")
            return self._apply_order_limit(node, query)

        if any(item.is_aggregate() for item in select) or query.group_by:
            node = self._plan_aggregate(node, query)
            return self._apply_order_limit(node, query)

        # Sort on the input so ORDER BY year works even if year is not selected.
        node = self._apply_sort(node, query)
        exprs: list[Expr] = []
        names: list[str] = []
        for item in select:
            if not isinstance(item.expr, Expr):
                raise TinyQueryError("invalid SELECT item")
            _require_resolvable(item.expr, node.schema)
            exprs.append(item.expr)
            names.append(item.output_name())
        node = Project(node, exprs, names)
        if query.limit is not None:
            node = Limit(node, query.limit)
        return node

    def _scan(self, ref: TableRef) -> Scan:
        table = self.catalog.get(ref.name)
        schema = _alias_schema(table, ref.alias)
        return Scan(ref.alias, schema, table.rows)

    def _table_schema(self, ref: TableRef) -> Schema:
        return _alias_schema(self.catalog.get(ref.name), ref.alias)

    def _joined_schema(self, query: Query) -> Schema:
        schema = self._table_schema(query.from_table)
        for join in query.joins:
            schema = schema.concat(self._table_schema(join.table))
        return schema

    def _bind_join_keys(
        self,
        left_schema: Schema,
        right_schema: Schema,
        a: Expr,
        b: Expr,
    ) -> tuple[Expr, Expr]:
        a_left = _resolvable(a, left_schema)
        b_right = _resolvable(b, right_schema)
        if a_left and b_right:
            return a, b
        a_right = _resolvable(a, right_schema)
        b_left = _resolvable(b, left_schema)
        if b_left and a_right:
            return b, a
        raise TinyQueryError(
            f"JOIN keys must each belong to one side: {a} = {b}"
        )

    def _plan_aggregate(self, source: Operator, query: Query) -> Operator:
        agg_specs: list[Aggregate] = []
        for item in query.select:
            if item.is_star():
                raise TinyQueryError("SELECT * cannot be mixed with aggregates / GROUP BY")
            if isinstance(item.expr, Aggregate):
                if item.expr.arg is not None:
                    _require_resolvable(item.expr.arg, source.schema)
                agg_specs.append(
                    Aggregate(item.expr.func, item.expr.arg, item.alias or item.expr.alias)
                )
            else:
                if not query.group_by:
                    raise TinyQueryError(
                        f"column {item.expr} must appear in GROUP BY or an aggregate"
                    )
                if not _expr_in(item.expr, query.group_by):
                    raise TinyQueryError(
                        f"column {item.expr} must appear in GROUP BY or an aggregate"
                    )
                _require_resolvable(item.expr, source.schema)

        for key in query.group_by:
            _require_resolvable(key, source.schema)

        group_names = [str(key) for key in query.group_by]
        node: Operator = HashAggregate(source, query.group_by, group_names, agg_specs)

        out_exprs: list[Expr] = []
        out_names: list[str] = []
        for item in query.select:
            out_names.append(item.output_name())
            if isinstance(item.expr, Aggregate):
                spec = Aggregate(item.expr.func, item.expr.arg, item.alias or item.expr.alias)
                out_exprs.append(ColumnRef(spec.output_name()))
            else:
                out_exprs.append(ColumnRef(str(item.expr)))
        return Project(node, out_exprs, out_names)

    def _apply_sort(self, node: Operator, query: Query) -> Operator:
        if not query.order_by:
            return node
        keys = [term.expr for term in query.order_by]
        descending = [term.descending for term in query.order_by]
        for expr in keys:
            _require_resolvable(expr, node.schema)
        return Sort(node, keys, descending)

    def _apply_order_limit(self, node: Operator, query: Query) -> Operator:
        node = self._apply_sort(node, query)
        if query.limit is not None:
            node = Limit(node, query.limit)
        return node


def plan(catalog: Catalog, query: Query) -> Operator:
    return Planner(catalog).plan(query)


def _alias_schema(table: Table, alias: str) -> Schema:
    return Schema(tuple(Column(col.name, table=alias) for col in table.schema.columns))


def _resolvable(expr: Expr, schema: Schema) -> bool:
    dummy = tuple(None for _ in schema.columns)
    try:
        expr.eval(dummy, schema)
        return True
    except TinyQueryError:
        return False


def _require_resolvable(expr: Expr, schema: Schema) -> None:
    dummy = tuple(None for _ in schema.columns)
    expr.eval(dummy, schema)


def _expr_in(expr: Expr, group_by: list[Expr]) -> bool:
    return any(str(expr) == str(other) for other in group_by)


def _flatten_and(expr: Expr) -> list[Expr]:
    if isinstance(expr, BinaryOp) and expr.op == "AND":
        return _flatten_and(expr.left) + _flatten_and(expr.right)
    return [expr]


def _combine_and(predicates: list[Expr]) -> Expr | None:
    if not predicates:
        return None
    node = predicates[0]
    for pred in predicates[1:]:
        node = BinaryOp("AND", node, pred)
    return node


def _take_resolvable(predicates: list[Expr], schema: Schema) -> tuple[list[Expr], list[Expr]]:
    pushed: list[Expr] = []
    leftover: list[Expr] = []
    for pred in predicates:
        if _resolvable(pred, schema):
            pushed.append(pred)
        else:
            leftover.append(pred)
    return pushed, leftover


def _with_filter(node: Operator, predicates: list[Expr]) -> Operator:
    combined = _combine_and(predicates)
    if combined is None:
        return node
    return Filter(node, combined)
