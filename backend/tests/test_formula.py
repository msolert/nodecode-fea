"""Formula: one node for numbers, fields, curves, lists and yes/no values; the output follows the inputs."""

import numpy as np
import pytest

from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph


def run(nodes, edges):
    res, inputs = Engine().evaluate(graph(nodes, edges))
    return res, inputs


def out(nodes, edges, node):
    """The Formula's result value, as the next node (List Length, which takes anything) receives it."""
    res, inputs = run(nodes + [("show", "list.length", {})], edges + [(node, "value", "show", "list")])
    assert res[node]["status"] == "ok", res[node]
    return res, inputs["show"]["list"]


ODB = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
       ("m", "field.invariant", {"invariant": "Mises"}), ("u", "odb.field", {"variable": "U"}),
       ("u2", "field.component", {"component": "U2"})]
ODB_E = [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("odb", "odb", "u", "odb"),
         ("u", "field", "u2", "field")]


def test_numbers():
    _, v = out([("a", "math.number", {"value": 200}), ("b", "math.number", {"value": 355}),
                ("fx", "math.formula", {"expr": "b / a", "label": "SF"})],
               [("a", "value", "fx", "a"), ("b", "value", "fx", "b")], "fx")
    assert v.value == pytest.approx(1.775) and v.label == "SF"


def test_field_gives_a_field_and_a_reduction_gives_a_number():
    res, f = out(ODB + [("fx", "math.formula", {"expr": "a / 1e6"})], ODB_E + [("m", "field", "fx", "a")], "fx")
    assert type(f).__name__ == "Field" and f.values.shape[1] == 1
    res, n = out(ODB + [("fx", "math.formula", {"expr": "np.percentile(a, 95)"})],
                 ODB_E + [("m", "field", "fx", "a")], "fx")
    assert type(n).__name__ == "Number" and n.value > 0


def test_two_fields_are_lined_up_by_label():
    # The filtered field has fewer rows: rows it lacks become NaN, the rest subtract to zero.
    nodes = ODB + [("hot", "field.where", {"op": ">", "value": 300}),
                   ("fx", "math.formula", {"expr": "a - b"})]
    edges = ODB_E + [("m", "field", "hot", "field"), ("m", "field", "fx", "a"), ("hot", "field", "fx", "b")]
    _, f = out(nodes, edges, "fx")
    d = f.values[:, 0]
    assert np.isnan(d).any() and np.nanmax(np.abs(d)) == 0


def test_fields_at_nodes_and_elements_dont_mix():
    res, _ = run(ODB + [("fx", "math.formula", {"expr": "a + b"})],
                 ODB_E + [("m", "field", "fx", "a"), ("u2", "field", "fx", "b")])
    assert res["fx"]["status"] == "error" and "lined up" in res["fx"]["message"]


def test_curves_and_list_of_curves():
    tip = [("odb", "odb.open", {"path": MOCK}),
           ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1, 2"})]
    _, s = out(tip + [("one", "list.item", {"index": 1}), ("fx", "math.formula", {"expr": "-a * 2 + t"})],
               [("odb", "odb", "p", "odb"), ("p", "series", "one", "list"), ("one", "item", "fx", "a")], "fx")
    assert type(s).__name__ == "Series" and len(s.y) == len(s.x) and s.y[0] == pytest.approx(0)
    res, items = run(tip + [("fx", "math.formula", {"expr": "-a"}), ("n", "list.length", {})],
                     [("odb", "odb", "p", "odb"), ("p", "series", "fx", "a"), ("fx", "value", "n", "list")])
    assert res["fx"]["status"] == "ok", res["fx"]
    assert len(items["n"]["list"]) == 2 and all(type(i).__name__ == "Series" for i in items["n"]["list"])


def test_list_of_numbers_is_a_vector():
    nodes = [("r", "list.range", {"start": 1, "stop": 4, "step": 1}), ("fx", "math.formula", {"expr": "a ** 2"})]
    res, v = out(nodes, [("r", "list", "fx", "a")], "fx")
    assert [i.value for i in v] == [x ** 2 for x in range(1, 1 + len(v))] and len(v) >= 3
    _, total = out([("r", "list.range", {"start": 1, "stop": 4, "step": 1}),
                    ("fx", "math.formula", {"expr": "np.sum(a)"})], [("r", "list", "fx", "a")], "fx")
    assert type(total).__name__ == "Number"


def test_comparison_gives_yes_no():
    _, v = out([("a", "math.number", {"value": 400}), ("fx", "math.formula", {"expr": "a > 355"})],
               [("a", "value", "fx", "a")], "fx")
    assert type(v).__name__ == "Bool" and v.value is True


def test_old_math_nodes_still_run_but_are_hidden():
    from nodecode_fea.nodes import REGISTRY
    for t in ("field.expression", "series.expression", "math.expression"):
        assert REGISTRY[t].hidden
    assert not REGISTRY["math.formula"].hidden


def test_autocomplete_lists_exactly_what_formulas_accept():
    from nodecode_fea import ops

    names = {n["name"]: n for n in ops.formula_names()}
    for n in ("a", "t", "sqrt", "np.max", "np.percentile", "np.linalg.inv", "np.pi"):
        assert n in names and names[n]["doc"], n
    assert names["np.max"]["doc"] != names["max"]["doc"]
    for n, d in names.items():  # every suggested function really runs in a formula
        if d["kind"] == "function" and n not in ("len",):
            msg = _try(f"{n}(1)")  # may fail on its arguments, but must be a known function
            assert "isn't available" not in msg and "Unknown function" not in msg, (n, msg)


def _try(expr: str) -> str:
    from nodecode_fea import ops
    try:
        ops.evaluate(expr, {})
        return ""
    except ValueError as exc:
        return str(exc)


def test_int_and_float_work_like_python_and_element_by_element():
    import numpy as np

    from nodecode_fea import ops
    assert ops.evaluate("int(2.7)", {}) == 2 and ops.evaluate("int(-2.7)", {}) == -2
    assert ops.evaluate("float(a)", {"a": np.array(["1.5", "2e3"], dtype=object)}).tolist() == [1.5, 2000.0]
    assert ops.evaluate("int(a / 100)", {"a": np.array([155.0, 299.0])}).tolist() == [1.0, 2.0]
    names = {n["name"] for n in ops.formula_names()}
    assert {"int", "float"} <= names


def test_int_on_a_field_column_in_a_formula_node():
    from .test_curves_apart import MISES, MISES_E, formula
    from .test_general_nodes import out
    mises = out(MISES, MISES_E, "q", "field")
    got = formula("int(a[:, 2])", MISES, MISES_E, "q", "field")
    assert [n.value for n in got] == [float(int(v)) for v in mises.values[:, 0]]
