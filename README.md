# tinyquery

[![tests](https://github.com/SpookyJumpyBeans/tinyquery/actions/workflows/tests.yml/badge.svg)](https://github.com/SpookyJumpyBeans/tinyquery/actions/workflows/tests.yml)

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

## Does any of it actually help?

Two optimisations are worth measuring: hashing the build side instead of
rescanning it, and pushing a filter below the join instead of above it.

```bash
python benchmarks/join_benchmark.py
```

`NestedLoopJoin` exists only as the baseline here. The planner never picks it,
and `tests/test_nested_loop_join.py` fuzzes both operators against each other
so the comparison is between two things that return identical rows.

**Hash join vs nested loop**, joining orders to four lineitems each:

| orders | lineitems | rows out | hash | nested loop | speedup |
| --- | --- | --- | --- | --- | --- |
| 1,000 | 4,000 | 4,000 | 0.011s | 7.197s | **644x** |
| 2,000 | 8,000 | 8,000 | 0.026s | 25.051s | **954x** |
| 5,000 | 20,000 | 20,000 | 0.060s | skipped | |
| 20,000 | 80,000 | 80,000 | 0.267s | skipped | |
| 100,000 | 400,000 | 400,000 | 1.603s | skipped | |

The speedup *grows* with input size, which is the point: nested loop does
O(n\*m) key comparisons and hash join does O(n+m). Nested loop is skipped above
2,000 orders because it would take minutes. Hash join goes from 1k to 100k
orders, a 100x jump, in 146x the time.

**Predicate pushdown**, filtering `o.year = 2024` below the join versus above it:

| orders | rows out | pushed down | above join | speedup |
| --- | --- | --- | --- | --- |
| 1,000 | 1,993 | 0.011s | 0.018s | 1.72x |
| 2,000 | 3,988 | 0.021s | 0.038s | 1.81x |
| 5,000 | 10,003 | 0.058s | 0.100s | 1.73x |
| 20,000 | 39,872 | 0.272s | 0.453s | 1.67x |
| 100,000 | 200,034 | 1.344s | 2.337s | 1.74x |

This one is a flat ~1.7x at every size, not a growing win, and that is what you
should expect. The filter keeps about half the orders, so the build side is
half as large and half as many probes find a match. It changes the constant,
not the complexity.

**Cost-based join ordering**, a fact table joined to a 200 row dimension and a
50 row table filtered down to one:

| facts | rows out | cost-based | as written | speedup |
| --- | --- | --- | --- | --- |
| 1,000 | 20 | 0.004s | 0.006s | 1.30x |
| 5,000 | 100 | 0.018s | 0.024s | 1.35x |
| 20,000 | 400 | 0.073s | 0.108s | 1.48x |
| 100,000 | 2,000 | 0.324s | 0.711s | **2.19x** |

Written order joins the facts to the dimension first, which means carrying
every fact row through a wide join before anything filters it down. The cost
model starts from the filtered table instead:

```
HashJoin f.dim_id = d.id
  HashJoin t.id = f.tiny_id
    Filter t.id = 1
      Scan t
    Scan f
  Scan d
```

That one row leads, the join to `f` collapses the fact table immediately, and
`d` joins against something small. Like hash join and unlike pushdown, the win
grows with size, because the cost of getting the order wrong is proportional to
how big the intermediate result gets.

Measured on CPython 3.13, best of three runs.

## Was the planner right? EXPLAIN ANALYZE

`--analyze` runs the query and reports, for every operator, the row count the
cost model predicted next to the row count that actually came out, plus time
spent:

```
$ python -m tinyquery --analyze "SELECT f.id, d.label, t.label FROM f JOIN d ON f.dim_id = d.id JOIN t ON f.tiny_id = t.id WHERE t.id = 1"

Project f.id, d.label, t.label  (rows est=400 actual=400  time=63.70ms self=3.21ms)
  Reorder to declared column order  (rows est=400 actual=400  time=60.50ms self=1.00ms)
    HashJoin f.dim_id = d.id  (rows est=400 actual=400  time=59.50ms self=2.09ms)
      HashJoin t.id = f.tiny_id  (rows est=400 actual=400  time=57.29ms self=46.26ms)
        Filter t.id = 1  (rows est=1 actual=1  time=0.12ms self=0.09ms)
          Scan t  (rows est=50 actual=50  time=0.03ms self=0.03ms)
        Scan f  (rows est=20,000 actual=20,000  time=10.90ms self=10.90ms)
      Scan d  (rows est=200 actual=200  time=0.12ms self=0.12ms)
```

Add `--no-reorder` and the same query shows why the cost model bothers. The
first join now produces a 20,000 row intermediate result before anything
filters it:

```
  HashJoin f.tiny_id = t.id  (rows est=400 actual=400  time=107.05ms self=42.27ms)
    HashJoin f.dim_id = d.id  (rows est=20,000 actual=20,000  time=64.47ms self=54.00ms)
```

Those estimates are exact only because that data is uniform. Equality
selectivity assumes every value is equally common, so skewed data fools it,
and any node off by 10x or more gets flagged:

```
Filter t.k = 1  (rows est=25 actual=1  time=0.02ms self=0.01ms)   <-- estimate off by 25x
```

Estimates are recomputed bottom-up over the finished plan using the same
statistics and formulas the join orderer uses. Timing is inclusive of
children, and `self` subtracts them. `--analyze --json` emits the plan and rows
as JSON, with each node's operator class alongside its label.

Profiling works from outside the operators: for one run, each instance gets
its `open`, `next_row`, and `close` wrapped with counters, and the originals
are restored afterwards, even if the query fails. Operators never know they
are being watched, and an unprofiled query pays nothing for the feature.

## What it does not do

No nested queries, no outer joins, no disk spill. NULL join keys never match. NULLs in `ORDER BY` sort last.

AND-clauses in `WHERE` that only mention one table are pushed below the join (so `o.year = 2024` filters orders before the hash table is built). Predicates that mention both sides, and `OR`s we cannot split, stay above the join.

Join ordering searches left-deep plans only, using the Selinger dynamic program over subsets, and gives up above 10 tables. Statistics come from a full scan rather than a sample, which is honest only because the tables are already in memory. Estimates use the usual independence assumptions, so correlated predicates will mislead it in exactly the way they mislead every other optimiser.

## Run

```bash
python -m tinyquery "SELECT region FROM orders WHERE year = 2024"
python -m tinyquery --explain "SELECT o.region, SUM(l.revenue) AS total FROM orders o JOIN lineitem l ON o.id = l.order_id WHERE o.year = 2024 GROUP BY o.region"
python -m tinyquery --analyze "SELECT o.region, SUM(l.revenue) AS total FROM orders o JOIN lineitem l ON o.id = l.order_id WHERE o.year = 2024 GROUP BY o.region"
```

`--explain` prints the plan without running it. `--analyze` runs it and reports
estimated vs actual rows per operator. `--no-reorder` turns off the cost-based
join order, and `--no-pushdown` keeps every `WHERE` filter above the joins, for
comparison.

`--data examples` is the default. Each `*.csv` becomes a table named after the file.

```bash
pip install -e ".[dev]"
pytest
```

## In the browser

`site/` is a static page that runs tinyquery and tinydelta in the browser with
[Pyodide](https://pyodide.org). It builds a small shop -- a tinydelta `orders`
table with six versions, plus customers and line items -- and runs every query
under `EXPLAIN ANALYZE`. The plan is drawn as a tree, with estimated and actual
rows as paired bars on one log scale, and any operator off by 10x or more is
flagged, the same as in the text output.

```bash
pip install -e ".[delta]"
python site/build.py
python -m http.server -d _site
```

Pyodide has no hard links, and tinydelta publishes a commit by linking a
finished temp file to its version name. The demo stands in with exclusive
create plus copy, which is only safe because a browser tab runs one Python
thread. Run natively, the real `link()` is used.

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
