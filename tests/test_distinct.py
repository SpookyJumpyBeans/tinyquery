from tinyquery.engine import execute, explain
from tinyquery.errors import TinyQueryError
from tinyquery.parser import parse

from tests.conftest import sample_catalog

import pytest


def test_distinct_year():
    rows, _ = execute(sample_catalog(), "SELECT DISTINCT year FROM orders ORDER BY year")
    assert rows == [(2023,), (2024,)]


def test_distinct_preserves_all_when_unique():
    rows, _ = execute(sample_catalog(), "SELECT DISTINCT id FROM orders ORDER BY id")
    assert rows == [(1,), (2,), (3,), (4,)]


def test_explain_distinct():
    plan = explain(sample_catalog(), "SELECT DISTINCT year FROM orders")
    assert "Distinct" in plan


def test_parse_distinct():
    query = parse("SELECT DISTINCT year FROM orders")
    assert query.distinct is True


def test_distinct_with_group_by_is_error():
    with pytest.raises(TinyQueryError, match="DISTINCT"):
        execute(sample_catalog(), "SELECT DISTINCT year FROM orders GROUP BY year")
