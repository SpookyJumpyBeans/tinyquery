from tinyquery.engine import execute, explain
from tinyquery.errors import TinyQueryError
from tinyquery.parser import parse

from tests.conftest import sample_catalog

import pytest


def test_parse_select_from_where():
    query = parse("SELECT region FROM orders WHERE year = 2024")
    assert query.from_table.name == "orders"
    assert query.where is not None


def test_scan_and_filter():
    rows, schema = execute(sample_catalog(), "SELECT region FROM orders WHERE year = 2024")
    assert schema.names() == ["region"]
    assert sorted(rows) == [("east",), ("north",), ("west",)]


def test_select_star():
    rows, _ = execute(sample_catalog(), "SELECT * FROM orders WHERE id = 1")
    assert rows == [(1, "west", 2024)]


def test_unknown_column():
    with pytest.raises(TinyQueryError, match="unknown column"):
        execute(sample_catalog(), "SELECT missing FROM orders")


def test_explain_is_a_tree():
    plan = explain(sample_catalog(), "SELECT region FROM orders WHERE year = 2024")
    assert "Project region" in plan
    assert "Filter year = 2024" in plan
    assert "Scan orders" in plan
