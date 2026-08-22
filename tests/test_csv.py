from pathlib import Path

from tinyquery.catalog import Catalog
from tinyquery.engine import execute


def test_csv_end_to_end():
    catalog = Catalog()
    examples = Path(__file__).resolve().parents[1] / "examples"
    catalog.load_dir(examples)
    rows, _ = execute(
        catalog,
        """
        SELECT o.region, SUM(l.revenue) AS total
        FROM orders o
        JOIN lineitem l ON o.id = l.order_id
        WHERE o.year = 2024
        GROUP BY o.region
        """,
    )
    by_region = {row[0]: row[1] for row in rows}
    assert by_region == {"west": 15, "east": 20, "north": 10}
