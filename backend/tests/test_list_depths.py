"""Every node says how deep a value each input takes; one level deeper runs it once per item, keeping the shape.

Each test feeds a node a flat list (the result it always gave) and a grid (one result per row)."""

from nodecode_fea.engine import Engine, port_depth
from nodecode_fea.nodes import REGISTRY
from nodecode_fea.values import EntitySet, Field, Series, Text

from .test_engine import MOCK, graph
from .test_model import evaluate

GRID, FLAT = [[3, 1, 2], [5, 9, 4]], [3, 1, 2]


def given(value, nid="v"):
    return (nid, "input.value", {"type": "List / matrix", "value": str(value)})


def out(node, params, value, port_in, port_out, more=(), edges=(), **graph):
    """The raw output of ``node`` with ``value`` (typed in an Input) wired into ``port_in``."""
    nodes = [given(value), ("n", node, params), *more]
    _, res, inputs = evaluate(nodes, [("v", "value", "n", port_in), *edges], **graph)
    assert res["n"]["status"] == "ok", res["n"]
    return inputs.outputs["n"][port_out]


def nums(v):
    return [nums(x) for x in v] if isinstance(v, list) else (v.value if hasattr(v, "value") else v)


def test_list_nodes_say_how_deep_each_input_is():
    # Only these keep taking whatever comes, for the reasons given next to their inputs.
    whatever = {("math.formula", k) for k in "abcd"} | {("list.merge", k) for k in "abcd"} | {
        ("view.value", "value"), ("view.group", "members"), ("view.contour", "markers"), ("view.chart", "series"),
        ("model.sketch", "add")}
    for t, d in REGISTRY.items():
        if d.hidden:
            continue
        for p in d.inputs:
            if (t, p.name) not in whatever:
                assert port_depth(d, p) is not None, (t, p.name)


def test_reduce_per_row():
    assert nums(out("math.reduce", {"op": "Max"}, FLAT, "field", "value")) == 3
    assert nums(out("math.reduce", {"op": "Max"}, GRID, "field", "value")) == [3, 9]
    assert nums(out("math.reduce", {"op": "Sum"}, GRID, "field", "running")) == [[3, 4, 6], [5, 14, 18]]
    assert nums(out("math.reduce", {"op": "Count"}, GRID, "field", "value")) == [3, 3]


def test_count_per_row():
    assert nums(out("list.length", {}, FLAT, "list", "value")) == 3
    assert nums(out("list.length", {}, [[1, 2], [3, 4, 5], [6]], "list", "value")) == [2, 3, 1]


def test_pick_per_row():
    assert nums(out("list.pick", {"which": "-1"}, FLAT, "list", "list")) == 2
    assert nums(out("list.pick", {"which": "-1"}, GRID, "list", "list")) == [2, 4]
    assert nums(out("list.pick", {"which": "0:2"}, GRID, "list", "list")) == [[3, 1], [5, 9]]


def test_sort_per_row():
    assert nums(out("list.sort", {"order": "Ascending"}, FLAT, "list", "list")) == [1, 2, 3]
    assert nums(out("list.sort", {"order": "Ascending"}, GRID, "list", "list")) == [[1, 2, 3], [4, 5, 9]]


def test_compare_per_row():
    p = {"op": ">", "value": "2"}
    assert nums(out("logic.test", p, FLAT, "a", "kept")) == [3]
    assert nums(out("logic.test", p, FLAT, "a", "count")) == 1
    assert nums(out("logic.test", p, GRID, "a", "kept")) == [[3], [5, 9, 4]]
    assert nums(out("logic.test", p, GRID, "a", "count")) == [1, 3]
    assert nums(out("logic.test", p, GRID, "a", "result")) == [[True, False, False], [True, True, True]]


def test_if_per_row():
    def kept(value):
        more = [("t", "logic.test", {"op": ">", "value": "2"})]
        return nums(out("logic.if", {}, value, "then", "result", more,
                        [("v", "value", "t", "a"), ("t", "result", "n", "condition")]))

    assert kept(FLAT) == [3]
    assert kept(GRID) == [[3], [5, 9, 4]]


def test_make_curve_per_row():
    one = out("series.make", {}, FLAT, "y", "series")
    assert isinstance(one, Series) and list(one.y) == FLAT
    rows = out("series.make", {}, GRID, "y", "series")
    assert [list(c.y) for c in rows] == GRID


def test_format_text_per_row():
    assert [t.value for t in out("text.format", {"template": "<{a}>"}, FLAT, "a", "text")] == ["<3>", "<1>", "<2>"]
    grid = out("text.format", {"template": "<{a}>"}, [[1, 2], [3]], "a", "text")
    assert [[t.value for t in row] for row in grid] == [["<1>", "<2>"], ["<3>"]]


def test_for_each_per_row():
    assert nums(out("loop.foreach", {}, FLAT, "list", "count")) == 3
    assert nums(out("loop.foreach", {}, [[1, 2], [3]], "list", "count")) == [2, 1]
    assert nums(out("loop.foreach", {}, [[1, 2], [3]], "list", "index")) == [[0, 1], [0]]


# ---- nodes and elements: a list of sets is one group (merged), a grid one group per row
def sets(names):
    """A Set node per name (a list of texts loops Set; a grid of names gives a grid of sets)."""
    texts = [[f'"{n}"' for n in row] for row in names] if isinstance(names[0], list) else [f'"{n}"' for n in names]
    value = str(texts).replace("'", "")
    return [("names", "input.value", {"type": "List / matrix", "value": value}), ("odb", "odb.open", {"path": MOCK}),
            ("v", "odb.set", {})], [("names", "value", "v", "name"), ("odb", "odb", "v", "odb")]


def run_on_sets(node, params, names, port_in, port_out, more=(), edges=()):
    nodes, e = sets(names)
    _, res, inputs = evaluate(nodes + [("n", node, params), *more], e + [("v", "set", "n", port_in), *edges])
    assert res["n"]["status"] == "ok", res["n"]
    return inputs.outputs["n"][port_out]


ODB = [("o2", "odb.open", {"path": MOCK})]


def test_field_output_where_per_row():
    flat = run_on_sets("odb.field", {"variable": "U"}, ["TIP", "FIXED"], "where", "field", ODB,
                       [("o2", "odb", "n", "odb")])
    assert isinstance(flat, Field)
    grid = run_on_sets("odb.field", {"variable": "U"}, [["TIP", "FIXED"], ["TIP"]], "where", "field", ODB,
                       [("o2", "odb", "n", "odb")])
    tip = run_on_sets("odb.field", {"variable": "U"}, ["TIP"], "where", "field", ODB, [("o2", "odb", "n", "odb")])
    assert [len(f.labels) for f in grid] == [len(flat.labels), len(tip.labels)]


def test_over_time_where_per_row():
    p = {"variable": "U", "quantity": "U2", "op": "Min"}
    flat = run_on_sets("field.time", p, ["TIP", "FIXED"], "set", "series", ODB, [("o2", "odb", "n", "odb")])
    assert isinstance(flat, Series)
    grid = run_on_sets("field.time", p, [["TIP", "FIXED"], ["FIXED"]], "set", "series", ODB,
                       [("o2", "odb", "n", "odb")])
    assert len(grid) == 2 and list(grid[0].y) == list(flat.y) and max(abs(grid[1].y)) == 0


def test_set_operation_per_row():
    more = ODB + [("b", "odb.set", {"name": "FIXED"})]
    e = [("o2", "odb", "b", "odb"), ("b", "set", "n", "b")]
    flat = run_on_sets("set.op", {"op": "Union"}, ["TIP", "TOP_SURFACE"], "a", "result", more, e)
    assert isinstance(flat, EntitySet)
    grid = run_on_sets("set.op", {"op": "Union"}, [["TIP", "TOP_SURFACE"], ["TIP"]], "a", "result", more, e)
    tip = run_on_sets("set.op", {"op": "Union"}, ["TIP"], "a", "result", more, e)
    assert [len(s.labels) for s in grid] == [len(flat.labels), len(tip.labels)]


def test_field_from_sets_per_row():
    more = ODB
    e = [("o2", "odb", "n", "odb")]
    flat = run_on_sets("field.from_set", {"expr": "set"}, ["TIP", "FIXED"], "set", "field", more, e)
    assert sorted(set(flat.values[:, 0])) == [1.0, 2.0]
    grid = run_on_sets("field.from_set", {"expr": "set"}, [["TIP", "FIXED"], ["FIXED"]], "set", "field", more, e)
    assert len(grid) == 2 and sorted(set(grid[1].values[:, 0])) == [1.0]
    # a: one number per set, or a grid of them (one field per row)
    nodes, e2 = sets(["TIP", "FIXED"])
    nodes += [given([[10, 20], [30, 40]], "a"), ("n", "field.from_set", {"expr": "a"})] + more
    _, res, inputs = evaluate(nodes, e2 + e + [("v", "set", "n", "set"), ("a", "value", "n", "a")])
    assert res["n"]["status"] == "ok", res["n"]
    assert [sorted(set(f.values[:, 0])) for f in inputs.outputs["n"]["field"]] == [[10, 20], [30, 40]]


def test_sample_field_list_of_point_tables():
    field = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "U", "quantity": "U2"})]
    fe = [("odb", "odb", "s", "odb"), ("s", "field", "n", "field")]
    p = {"mode": "At the points", "method": "Nearest node"}
    one = out("field.path", p, [[0, 5, 0], [0, 5, 100]], "points", "series", field, fe)
    assert len(one) == 2
    many = out("field.path", p, [[[0, 5, 0], [0, 5, 100]], [[0, 5, 100]]], "points", "series", field, fe)
    assert [len(r) for r in many] == [2, 1] and many[1][0].value == one[1].value
    texts = out("field.path", p, '["0, 5, 0", "0, 5, 100"]', "points", "series", field, fe)
    assert [len(r) for r in texts] == [1, 1] and texts[1][0].value == one[1].value


def test_data_panel_opens_nested_results_level_by_level():
    from fastapi.testclient import TestClient

    from nodecode_fea.app import app
    from .test_engine import graph

    g = graph([given([[3, 1, 2], [5, 9, 4], [7, 8]]), ("n", "list.sort", {"order": "Ascending"})],
              [("v", "value", "n", "list")])
    client = TestClient(app)
    res = client.post("/api/run", json=g).json()["results"]["n"]
    assert res["shape"] == [3, 3] and res["summary"] == "3 × 3 items"
    row = client.post("/api/inspect", json={"graph": g, "node": "n", "path": [2]}).json()
    assert row["levels"] == [3, 2] and [r[2] for r in row["display"]["rows"]] == [7, 8]
    item = client.post("/api/inspect", json={"graph": g, "node": "n", "path": [1, 2]}).json()
    assert item["levels"] == [3, 3] and item["display"]["value"] == 9
    assert client.post("/api/inspect", json={"graph": g, "node": "n", "path": [5]}).status_code == 400


def whole_out(node, params, value, port_in, port_out):
    """Like ``out``, with the node set to take its inputs whole (right-click → Lists: take whole)."""
    g = graph([given(value), ("n", node, params)], [("v", "value", "n", port_in)])
    g["nodes"][1]["levels"] = "whole"
    res, inputs = Engine().evaluate(g)
    assert res["n"]["status"] == "ok", res["n"]
    return inputs.outputs["n"][port_out]


def test_take_whole_works_on_the_rows_of_a_grid():
    assert nums(whole_out("list.pick", {"which": "0"}, GRID, "list", "list")) == [3, 1, 2]
    assert nums(whole_out("list.pick", {"which": "-1"}, GRID, "list", "list")) == [5, 9, 4]
    assert nums(whole_out("list.length", {}, [[1, 2], [3, 4, 5], [6]], "list", "value")) == 3
    assert nums(whole_out("list.sort", {"order": "Descending"}, GRID, "list", "list")) == [[5, 9, 4], [3, 1, 2]]
    # A flat list is the same either way.
    assert nums(whole_out("list.pick", {"which": "-1"}, FLAT, "list", "list")) == 2


def test_take_whole_is_part_of_the_cache_key():
    e = Engine()
    g = graph([given(GRID), ("n", "list.length", {})], [("v", "value", "n", "list")])
    _, inputs = e.evaluate(g)
    assert nums(inputs.outputs["n"]["value"]) == [3, 3]
    g["nodes"][1]["levels"] = "whole"
    _, inputs = e.evaluate(g)
    assert nums(inputs.outputs["n"]["value"]) == 2
