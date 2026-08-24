from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tinyquery.errors import TinyQueryError

Row = tuple[Any, ...]


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Schema:
    columns: tuple[Column, ...]

    def index_of(self, name: str, table: str | None = None) -> int:
        matches = [
            i
            for i, col in enumerate(self.columns)
            if col.name == name and (table is None or col.table == table)
        ]
        # Project names aliases like "o.region"; ORDER BY o.region still has to hit them.
        if not matches and table is not None:
            qualified = f"{table}.{name}"
            matches = [i for i, col in enumerate(self.columns) if col.name == qualified]
        label = f"{table}.{name}" if table else name
        if not matches:
            raise TinyQueryError(f"unknown column {label}")
        if len(matches) > 1:
            raise TinyQueryError(f"ambiguous column {label}")
        return matches[0]

    def has(self, name: str, table: str | None = None) -> bool:
        try:
            self.index_of(name, table)
            return True
        except TinyQueryError:
            return False

    def names(self) -> list[str]:
        return [col.qualified if col.table else col.name for col in self.columns]

    def concat(self, other: Schema) -> Schema:
        return Schema(self.columns + other.columns)
