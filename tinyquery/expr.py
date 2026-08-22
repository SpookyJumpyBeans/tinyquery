from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from tinyquery.schema import Schema


class Expr(ABC):
    @abstractmethod
    def eval(self, row: tuple[Any, ...], schema: Schema) -> Any:
        raise NotImplementedError

    def columns_resolvable(self, schema: Schema) -> bool:
        try:
            self.eval(tuple(None for _ in schema.columns), schema)
            return True
        except Exception:
            return False


@dataclass(frozen=True)
class ColumnRef(Expr):
    name: str
    table: str | None = None

    def eval(self, row: tuple[Any, ...], schema: Schema) -> Any:
        return row[schema.index_of(self.name, self.table)]

    def __str__(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Literal(Expr):
    value: Any

    def eval(self, row: tuple[Any, ...], schema: Schema) -> Any:
        return self.value

    def columns_resolvable(self, schema: Schema) -> bool:
        return True

    def __str__(self) -> str:
        if isinstance(self.value, str):
            return "'" + self.value.replace("'", "''") + "'"
        if self.value is None:
            return "NULL"
        return str(self.value)


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str
    left: Expr
    right: Expr

    def eval(self, row: tuple[Any, ...], schema: Schema) -> Any:
        if self.op == "AND":
            return bool(self.left.eval(row, schema)) and bool(self.right.eval(row, schema))
        if self.op == "OR":
            return bool(self.left.eval(row, schema)) or bool(self.right.eval(row, schema))

        left = self.left.eval(row, schema)
        right = self.right.eval(row, schema)
        if left is None or right is None:
            return False
        if self.op == "=":
            return left == right
        if self.op == "!=":
            return left != right
        if self.op == "<":
            return left < right
        if self.op == ">":
            return left > right
        if self.op == "<=":
            return left <= right
        if self.op == ">=":
            return left >= right
        raise ValueError(f"unknown operator {self.op}")

    def __str__(self) -> str:
        return f"{self.left} {self.op} {self.right}"


@dataclass(frozen=True)
class NotOp(Expr):
    inner: Expr

    def eval(self, row: tuple[Any, ...], schema: Schema) -> Any:
        return not bool(self.inner.eval(row, schema))

    def __str__(self) -> str:
        return f"NOT {self.inner}"


@dataclass(frozen=True)
class Aggregate:
    func: str  # COUNT, SUM, MIN, MAX, AVG
    arg: Expr | None  # None means COUNT(*)
    alias: str | None = None

    def output_name(self) -> str:
        if self.alias:
            return self.alias
        if self.arg is None:
            return "COUNT(*)"
        return f"{self.func}({self.arg})"

    def __str__(self) -> str:
        return self.output_name()


@dataclass(frozen=True)
class Star:
    def __str__(self) -> str:
        return "*"


@dataclass(frozen=True)
class SelectItem:
    expr: Expr | Aggregate | Star
    alias: str | None = None

    def output_name(self) -> str:
        if self.alias:
            return self.alias
        return str(self.expr)

    def is_aggregate(self) -> bool:
        return isinstance(self.expr, Aggregate)

    def is_star(self) -> bool:
        return isinstance(self.expr, Star)
