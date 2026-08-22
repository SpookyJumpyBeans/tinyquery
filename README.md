# tinyquery

Single-node SQL engine. You write a small subset of SQL; it parses it, builds an iterator plan, and runs it.

This is the same shape as Spark/Postgres (parse → plan → operators), on one machine, in memory.

## What works

- `SELECT` columns or `*`
- `FROM` + `JOIN ... ON a = b` (inner equijoin)
- `WHERE` with `= != < > <= >= AND OR NOT`
- `GROUP BY` with `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`
- `EXPLAIN` via `--explain` (prints the physical plan)

Tables are CSV files. One file per table, header row required.

## What it does not do

No nested queries, no `ORDER BY`/`LIMIT`, no outer joins, no disk spill, no optimizer. `WHERE` runs after joins, so it is correct but not always cheap. NULL join keys never match.

## Run

```bash
python -m tinyquery "SELECT region FROM orders WHERE year = 2024"
python -m tinyquery --explain "SELECT o.region, SUM(l.revenue) AS total FROM orders o JOIN lineitem l ON o.id = l.order_id WHERE o.year = 2024 GROUP BY o.region"
```

`--data examples` is the default. Each `*.csv` becomes a table named after the file.

```bash
pip install -e ".[dev]"
pytest
```

## How a query runs

```text
SQL
  → parser (recursive descent)
  → planner (Scan / Filter / HashJoin / HashAggregate / Project)
  → Volcano iterators: open() / next_row() / close()
```

`HashJoin` builds a hash table on the left input, then probes with the right. `HashAggregate` does the same thing with group keys. Both need memory proportional to the build/group side.

## Example

`examples/orders.csv` and `examples/lineitem.csv`:

```text
o.region | total
---------+------
west     | 15
east     | 20
north    | 10
```
