from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tinyquery.errors import TinyQueryError
from tinyquery.schema import Column, Row, Schema


def _parse_value(raw: str) -> Any:
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


@dataclass
class Table:
    name: str
    schema: Schema
    rows: list[Row]


@dataclass
class Catalog:
    tables: dict[str, Table] = field(default_factory=dict)

    def register(self, name: str, schema: Schema, rows: list[Row]) -> None:
        self.tables[name] = Table(name, schema, rows)

    def load_csv(self, name: str, path: str | Path, alias: str | None = None) -> None:
        csv_path = Path(path)
        with csv_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                raise TinyQueryError(f"csv {csv_path} is empty") from None
            rows = [tuple(_parse_value(cell) for cell in row) for row in reader]

        table = alias or name
        schema = Schema(tuple(Column(col, table=table) for col in header))
        self.register(name, schema, rows)

    def load_dir(self, directory: str | Path) -> None:
        folder = Path(directory)
        for csv_path in sorted(folder.glob("*.csv")):
            self.load_csv(csv_path.stem, csv_path)

    def load_delta(
        self,
        name: str,
        path: str | Path,
        alias: str | None = None,
        version: int | None = None,
    ) -> int:
        """Register a tinydelta table, optionally at an older version.

        The commit log decides which data files are visible, so a scan only ever
        sees rows some commit published -- never a half-written file, and never
        one a later overwrite removed. Passing `version` reads that snapshot
        instead of the latest, which is what makes time travel a plain SELECT.

        Returns the version actually read, so callers can report it.
        """
        try:
            from tinydelta.table import DeltaTable
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise TinyQueryError(
                "reading tinydelta tables needs the delta extra: "
                'pip install "tinyquery[delta]"'
            ) from exc

        from tinydelta.errors import TinyDeltaError

        try:
            table = DeltaTable.open(Path(path))
            snapshot = table.snapshot(version)
            records = table.read(version)
        except TinyDeltaError as exc:
            raise TinyQueryError(f"tinydelta: {exc}") from exc

        # Unlike CSV, the log carries a declared schema, so column order and
        # types come from the table rather than from guessing at the text.
        names = snapshot.schema.names()
        rows = [tuple(record.get(column) for column in names) for record in records]

        label = alias or name
        schema = Schema(tuple(Column(column, table=label) for column in names))
        self.register(name, schema, rows)
        return snapshot.version

    def get(self, name: str) -> Table:
        if name not in self.tables:
            raise TinyQueryError(f"unknown table {name}")
        return self.tables[name]
