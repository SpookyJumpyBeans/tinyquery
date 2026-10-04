"""The browser demo's Python side, run natively.

site/demo.py has no browser dependencies, so everything the page asks of it
can be checked here: the shop builds, every preset query runs, the bad
estimate the first preset exists to show is really there, and time travel
reads the versions the history claims.
"""

import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("tinydelta")

SITE = Path(__file__).resolve().parent.parent / "site"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"site_{name}", SITE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fresh_demo(tmp_path):
    """Its own shop, for tests that commit new versions."""
    module = _load("demo")
    module.setup(tmp_path / "data")
    return module


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    module = _load("demo")
    module.setup(tmp_path_factory.mktemp("data"))
    return module


def _nodes(plan):
    yield plan
    for child in plan["children"]:
        yield from _nodes(child)


def _first_sql(demo):
    return json.loads(demo.presets())[0]["sql"]


def test_history_has_appends_overwrite_and_restore(demo):
    commits = json.loads(demo.history())
    assert [c["operation"] for c in commits] == [
        "CREATE", "APPEND", "APPEND", "APPEND", "OVERWRITE", "RESTORE",
    ]
    assert [c["rows"] for c in commits] == [0, 600, 1200, 1800, 1200, 1800]


def test_every_preset_runs(demo):
    for preset in json.loads(demo.presets()):
        result = json.loads(demo.run(preset["sql"]))
        assert "error" not in result, (preset["name"], result)
        assert result["rows"], preset["name"]
        assert result["plan"]["actual_rows"] == len(result["rows"])


def test_first_preset_shows_a_flagged_misestimate(demo):
    result = json.loads(demo.run(_first_sql(demo)))
    filter_node = next(n for n in _nodes(result["plan"]) if n["operator"] == "Filter")
    assert filter_node["estimated_rows"] == 50
    assert filter_node["actual_rows"] == 2
    assert "estimate off by 25x" in result["text"]


def test_toggles_change_the_plan_but_not_the_answer(demo):
    sql = _first_sql(demo)
    default = json.loads(demo.run(sql))
    for options in ({"reorder": False}, {"pushdown": False}):
        other = json.loads(demo.run(sql, **options))
        assert other["rows"] == default["rows"], options
        assert other["text"] != default["text"], options


def test_time_travel_reads_each_version(demo):
    sql = "SELECT COUNT(o.id) AS n FROM orders o"
    for commit in json.loads(demo.history()):
        result = json.loads(demo.run(sql, version=commit["version"]))
        assert result["version"] == commit["version"]
        assert result["rows"] == [[commit["rows"]]]


def test_errors_come_back_as_json(demo):
    result = json.loads(demo.run("SELECT nope FROM orders"))
    assert result == {"error": "TinyQueryError: unknown column nope"}
    assert "error" in json.loads(demo.run("SELECT * FROM orders", version=99))


def test_build_bundles_both_packages_and_the_demo(tmp_path):
    bundle = _load("build").build(tmp_path / "site")
    names = set(zipfile.ZipFile(bundle).namelist())
    assert {"demo.py", "tinyquery/engine.py", "tinydelta/log.py"} <= names
    assert not any("__pycache__" in name for name in names)
    for page in ("index.html", "style.css", "app.js", "plan.js", "timeline.js"):
        assert (tmp_path / "site" / page).is_file()


def test_append_orders_commits_a_new_version(fresh_demo):
    before = json.loads(fresh_demo.history())
    after = json.loads(fresh_demo.append_orders(200))
    assert len(after) == len(before) + 1
    assert after[-1]["operation"] == "APPEND"
    assert after[-1]["rows"] == before[-1]["rows"] + 200

    count = "SELECT COUNT(o.id) AS n FROM orders o WHERE o.year = 2026"
    assert json.loads(fresh_demo.run(count))["rows"] == [[200]]
    assert json.loads(fresh_demo.run(count, version=before[-1]["version"]))["rows"] == [[0]]


def test_appended_orders_have_line_items(fresh_demo):
    fresh_demo.append_orders(50)
    sql = (
        "SELECT COUNT(l.order_id) AS n FROM orders o "
        "JOIN lineitem l ON o.id = l.order_id WHERE o.year = 2026"
    )
    assert json.loads(fresh_demo.run(sql))["rows"] == [[50 * 4]]


def test_repeated_appends_never_reuse_an_id(fresh_demo):
    fresh_demo.append_orders(10)
    fresh_demo.append_orders(10)
    sql = "SELECT COUNT(o.id) AS n FROM orders o"
    total = json.loads(fresh_demo.run(sql))["rows"][0][0]
    distinct = json.loads(fresh_demo.run("SELECT DISTINCT o.id FROM orders o"))["rows"]
    assert len(distinct) == total == 1800 + 20
