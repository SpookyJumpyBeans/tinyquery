from __future__ import annotations

from tinyquery.catalog import Catalog, Table
from tinyquery.errors import TinyQueryError
from tinyquery.expr import Aggregate, ColumnRef, Expr
from tinyquery.operators import Filter, HashAggregate, HashJoin, Operator, Project, Scan
from tinyquery.parser import Query, TableRef
from tinyquery.schema import Column, Schema


class Planner:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def plan(self, query: Query) -> Operator:
        node = self._scan(query.from_table)
        for join in query.joins:
            right = self._scan(join.table)
            left_key, right_key = self._bind_join_keys(
                node.schema, right.schema, join.left_key, join.right_key
            )
            node = HashJoin(node, right, left_key, right_key)
        if query.where is not None:
            _require_resolvable(query.where, node.schema)
            node = Filter(node, query.where)

        select = query.select
        if len(select) == 1 and select[0].is_star():
            if query.group_by or any(item.is_aggregate() for item in select):
                raise TinyQueryError("SELECT * cannot be mixed with aggregates / GROUP BY")
            return node

        has_agg = any(item.is_aggregate() for item in select)
        if has_agg or query.group_by:
            return self._plan_aggregate(node, query)

        exprs: list[Expr] = []
        names: list[str] = []
        for item in select:
            if not isinstance(item.expr, Expr):
                raise TinyQueryError("invalid SELECT item")
            _require_resolvable(item.expr, node.schema)
            exprs.append(item.expr)
            names.append(item.output_name())
        return Project(node, exprs, names)

    def _scan(self, ref: TableRef) -> Scan:
        table = self.catalog.get(ref.name)
        schema = _alias_schema(table, ref.alias)
        return Scan(ref.alias, schema, table.rows)

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
