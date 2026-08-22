from tinyquery.catalog import Catalog
from tinyquery.schema import Column, Schema


def sample_catalog() -> Catalog:
    catalog = Catalog()
    catalog.register(
        "orders",
        Schema(
            (
                Column("id", "orders"),
                Column("region", "orders"),
                Column("year", "orders"),
            )
        ),
        [
            (1, "west", 2024),
            (2, "east", 2024),
            (3, "west", 2023),
            (4, "north", 2024),
        ],
    )
    catalog.register(
        "lineitem",
        Schema(
            (
                Column("order_id", "lineitem"),
                Column("sku", "lineitem"),
                Column("revenue", "lineitem"),
            )
        ),
        [
            (1, "widget", 10),
            (1, "gadget", 5),
            (2, "widget", 20),
            (3, "widget", 100),
            (4, "gadget", 7),
            (4, "widget", 3),
            (None, "orphan", 1),
        ],
    )
    return catalog
