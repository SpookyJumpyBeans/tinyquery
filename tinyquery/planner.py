from __future__ import annotations

from tinyquery.catalog import Catalog, Table
from tinyquery.errors import TinyQueryError
from tinyquery.expr import Aggregate, BinaryOp, ColumnRef, Expr
from tinyquery.cost import (
    JoinEdge,
    JoinPlan,
    analyze,
    choose_join_order,
    estimate_filtered_rows,
)
from tinyquery.operators import (
    Distinct,
    Filter,
    HashAggregate,
    HashJoin,
    Limit,
    Operator,
    Project,
    Reorder,
    Scan,
    Sort,
    TopK,
)
from tinyquery.parser import Query, TableRef
from tinyquery.schema import Column, Schema


class Planner:
    def __init__(self, catalog: Catalog, reorder: bool = True) -> None:
        self.catalog = catalog
        # Off means "join in the order written", which is what the planner did
        # before the cost model existed. Tests and the benchmark use it to
        # compare the two plans on identical inputs.
        self.reorder = reorder

    def plan(self, query: Query) -> Operator:
        # Validate WHERE against the full joined schema first so ambiguous
        # columns error the same way they would without pushdown.
        full_schema = self._joined_schema(query)
        conjuncts: list[Expr] = []
        if query.where is not None:
            conjuncts = _flatten_and(query.where)
            for pred in conjuncts:
                _require_resolvable(pred, full_schema)

        node, conjuncts = self._plan_joins(query, conjuncts)

        # Cross-table predicates (and ORs we could not split) stay above the join.
        node = _with_filter(node, conjuncts)

        select = query.select
        if query.distinct and (query.group_by or any(item.is_aggregate() for item in select)):
            raise TinyQueryError("SELECT DISTINCT cannot be mixed with aggregates / GROUP BY")

        if len(select) == 1 and select[0].is_star():
            if query.group_by or any(item.is_aggregate() for item in select):
                raise TinyQueryError("SELECT * cannot be mixed with aggregates / GROUP BY")
            return self._apply_distinct_order_limit(node, query)

        if any(item.is_aggregate() for item in select) or query.group_by:
            node = self._plan_aggregate(node, query)
            return self._apply_order_limit(node, query)

        # Without DISTINCT, sort/topk before Project so ORDER BY can use
        # columns that are not selected. With DISTINCT, project first, then
        # dedupe, then order/limit (SQL evaluation order).
        if not query.distinct:
            if query.order_by and query.limit is not None:
                keys = [term.expr for term in query.order_by]
                descending = [term.descending for term in query.order_by]
                for expr in keys:
                    _require_resolvable(expr, node.schema)
                node = TopK(node, keys, descending, query.limit)
            else:
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
        if query.distinct:
            node = Distinct(node)
            return self._apply_order_limit(node, query)
        if query.limit is not None and not query.order_by:
            node = Limit(node, query.limit)
        return node

    def _plan_joins(
        self, query: Query, conjuncts: list[Expr]
    ) -> tuple[Operator, list[Expr]]:
        """Build the join tree, reordering it when the cost model says to.

        Filters are pushed onto each base table first, because a filter that
        removes 90% of a table changes which join order is cheapest.
        """
        refs = [query.from_table] + [join.table for join in query.joins]
        inputs: dict[str, Operator] = {}
        filters: dict[str, list[Expr]] = {}
        for ref in refs:
            scan = self._scan(ref)
            pushed, conjuncts = _take_resolvable(conjuncts, scan.schema)
            filters[ref.alias] = pushed
            inputs[ref.alias] = _with_filter(scan, pushed)

        plan = self._choose_order(query, refs, filters)
        if plan is None:
            node = self._join_in_written_order(query, inputs)
            return node, conjuncts

        node = self._join_in_plan_order(query, inputs, plan)
        return _restore_declared_order(node, refs, inputs), conjuncts

    def _join_in_written_order(
        self, query: Query, inputs: dict[str, Operator]
    ) -> Operator:
        node = inputs[query.from_table.alias]
        for join in query.joins:
            right = inputs[join.table.alias]
            left_key, right_key = self._bind_join_keys(
                node.schema, right.schema, join.left_key, join.right_key
            )
            node = HashJoin(node, right, left_key, right_key)
        return node

    def _join_in_plan_order(
        self, query: Query, inputs: dict[str, Operator], plan: JoinPlan
    ) -> Operator:
        node = inputs[plan.order[0]]
        for step in plan.steps:
            right = inputs[step.alias]
            left_key, right_key = self._bind_join_keys(
                node.schema, right.schema, step.edge.left_key, step.edge.right_key
            )
            node = HashJoin(node, right, left_key, right_key)
        return node

    def _choose_order(
        self, query: Query, refs: list[TableRef], filters: dict[str, list[Expr]]
    ) -> JoinPlan | None:
        """Ask the cost model for an order, or None to keep the written one."""
        if not self.reorder:
            return None
        edges: list[JoinEdge] = []
        for join in query.joins:
            left = _qualified_ref(join.left_key)
            right = _qualified_ref(join.right_key)
            if left is None or right is None:
                # A key we cannot attribute to one table; do not reorder.
                return None
            edges.append(
                JoinEdge(left[0], join.left_key, right[0], join.right_key)
            )

        base_rows: dict[str, float] = {}
        key_ndv: dict[tuple[str, str], int] = {}
        for ref in refs:
            table = self.catalog.get(ref.name)
            stats = analyze(table.schema, table.rows)
            base_rows[ref.alias] = estimate_filtered_rows(filters[ref.alias], stats)
            for column in table.schema.columns:
                key_ndv[(ref.alias, column.name)] = stats.distinct(column.name)

        return choose_join_order([ref.alias for ref in refs], base_rows, key_ndv, edges)

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
        if query.order_by and query.limit is not None:
            keys = [term.expr for term in query.order_by]
            descending = [term.descending for term in query.order_by]
            for expr in keys:
                _require_resolvable(expr, node.schema)
            return TopK(node, keys, descending, query.limit)
        node = self._apply_sort(node, query)
        if query.limit is not None:
            node = Limit(node, query.limit)
        return node

    def _apply_distinct_order_limit(self, node: Operator, query: Query) -> Operator:
        # Distinct before order/limit so ORDER BY / LIMIT see unique rows.
        if query.distinct:
            node = Distinct(node)
        return self._apply_order_limit(node, query)


def _qualified_ref(expr: Expr) -> tuple[str, str] | None:
    """(table alias, column) for a qualified column reference, else None."""
    if isinstance(expr, ColumnRef) and expr.table is not None:
        return expr.table, expr.name
    return None


def _restore_declared_order(
    node: Operator, refs: list[TableRef], inputs: dict[str, Operator]
) -> Operator:
    """Put columns back where `FROM a JOIN b JOIN c` says they belong.

    Executing joins out of order moves columns around inside the row. Anything
    reading positionally above this point, SELECT * in particular, expects the
    declared order, so undo the permutation before handing the rows up.
    """
    declared: list[tuple[str | None, str]] = []
    for ref in refs:
        for column in inputs[ref.alias].schema.columns:
            declared.append((column.table, column.name))

    executed = [(column.table, column.name) for column in node.schema.columns]
    if executed == declared:
        return node

    positions = {key: i for i, key in enumerate(executed)}
    indices = [positions[key] for key in declared]
    schema = Schema(tuple(Column(name, table=table) for table, name in declared))
    return Reorder(node, indices, schema)


def plan(catalog: Catalog, query: Query, reorder: bool = True) -> Operator:
    return Planner(catalog, reorder=reorder).plan(query)


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
