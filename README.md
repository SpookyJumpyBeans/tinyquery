# tinyquery

Single node SQL engine. You write a small subset of SQL, it parses it, builds an iterator plan, and runs it.

This is the same shape as Spark/Postgres (parse → plan → operators), on one machine, in memory.

## What works

- `SELECT` columns or `*`
- `SELECT DISTINCT`
- `FROM` + `JOIN ... ON a = b` (inner equijoin)
- `WHERE` with `= != < > <= >= AND OR NOT`
- `GROUP BY` with `COUNT`, `SUM`, `MIN`, `MAX`, `AVG`
- `ORDER BY` (`ASC` / `DESC`) and `LIMIT`
- `EXPLAIN` via `--explain` (prints the physical plan)
- Reading [tinydelta](https://github.com/SpookyJumpyBeans/tinydelta) tables, at
  the latest version or any older one

Tables are CSV files -- one per table, header row required -- or tinydelta
tables, where a commit log decides which data files a scan can see.

## Querying a tinydelta table

`tinydelta` is the storage layer: a directory of data files plus an atomic JSON
commit log. `tinyquery` is the engine. Together they are the two halves of a
very small lakehouse -- storage decides *what is visible*, the engine decides
*how to compute it*.

```bash
pip install -e ".[delta]"

python -m tinyquery --table orders=./orders \
  "SELECT region, COUNT(id) AS n FROM orders GROUP BY region ORDER BY n DESC"
```

Because the log is the source of truth, a scan only ever sees rows some commit
published -- never a half-written file, and never one a later `overwrite`
removed. Pointing at an older version makes time travel a plain `SELECT`:

```bash
python -m tinyquery --table orders=./orders --version 1 \
  "SELECT region, COUNT(id) AS n FROM orders GROUP BY region ORDER BY n DESC"
```

```text
latest (v2)              --version 1

region | n               region | n
-------+--               -------+--
west   | 2               west   | 1
east   | 1               east   | 1
north  | 1               (2 rows)
(3 rows)
```

`--table` is repeatable and mixes with `--data`, so a tinydelta table joins
against a CSV like any other pair of tables. One difference from CSV: the log
carries a declared schema, so column types come from the table rather than from
guessing at the text.

## What it does not do

No nested queries, no outer joins, no disk spill. NULL join keys never match. NULLs in `ORDER BY` sort last.

AND-clauses in `WHERE` that only mention one table are pushed below the join (so `o.year = 2024` filters orders before the hash table is built). Predicates that mention both sides, and `OR`s we cannot split, stay above the join.

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
  → planner (Scan / Filter / HashJoin / HashAggregate / Project / Distinct / Sort / TopK / Limit)
  → Volcano iterators: open() / next_row() / close()
```

`HashJoin` builds a hash table on the left input, then probes with the right. `HashAggregate` does the same thing with group keys. Both need memory proportional to the build/group side.

`ORDER BY` alone uses a blocking `Sort` (memory O(n)). `ORDER BY` + `LIMIT` becomes `TopK`: still one pass over the child, but the heap only keeps k rows. `LIMIT` alone just stops after n rows.

The planner splits `WHERE` on `AND` and attaches each piece to the lowest input whose columns can resolve it. That is the same idea as predicate pushdown in Spark; there is no cost-based optimizer.

## Example

`examples/orders.csv` and `examples/lineitem.csv`:

```text
o.region | total
---------+------
west     | 15
east     | 20
north    | 10
```
