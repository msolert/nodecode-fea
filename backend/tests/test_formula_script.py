"""Formula scripts: several lines, variables, if/for/def, and named outputs; still without exec/eval."""

import numpy as np
import pytest

from nodecode_fea import ops
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph
from .test_formula import ODB, ODB_E


def outputs(nodes, edges, node, ports=("value",)):
    """What the Formula gives on each port, as the next node (List Length, which takes anything) receives it."""
    shows = [(f"show{i}", "list.length", {}) for i in range(len(ports))]
    res, inputs = Engine().evaluate(graph(nodes + shows, edges + [(node, p, f"show{i}", "list")
                                                                    for i, p in enumerate(ports)]))
    assert res[node]["status"] == "ok", res[node]
    return res, [inputs[f"show{i}"]["list"] for i in range(len(ports))]


SCRIPT = """
peak = np.max(a)
if peak > 1e9:
    note = "very high"
elif peak > 0:
    note = f"peak {peak / 1e6:.1f} MPa"
else:
    note = "none"
ratio = a / peak
ratio
"""


def test_script_gives_a_field_and_named_outputs():
    _, (f, peak, note) = outputs(
        ODB + [("fx", "math.formula", {"expr": SCRIPT, "outputs": "peak, note"})],
        ODB_E + [("m", "field", "fx", "a")], "fx", ("value", "out1", "out2"))
    assert type(f).__name__ == "Field" and np.nanmax(f.values) == pytest.approx(1.0)
    assert f.components[0] != SCRIPT  # a script is described by its name, not its whole text
    assert type(peak).__name__ == "Number" and peak.label == "peak" and peak.value > 0
    assert type(note).__name__ == "Text" and note.value.startswith("peak ") and note.value.endswith(" MPa")


def test_named_output_that_is_per_row_is_a_field():
    _, (f, hot) = outputs(
        ODB + [("fx", "math.formula", {"expr": "hot = where(a > np.mean(a), a, 0)\nresult = np.sum(hot > 0)",
                                       "outputs": "hot"})],
        ODB_E + [("m", "field", "fx", "a")], "fx", ("value", "out1"))
    assert type(f).__name__ == "Number" and f.value > 0
    assert type(hot).__name__ == "Field" and hot.name == "hot"


def test_a_list_of_fields_gives_one_result_and_one_named_output_each():
    nodes = [("odb", "odb.open", {"path": MOCK}), ("fr", "odb.frames", {}), ("u", "odb.field", {"variable": "U"}),
             ("u2", "field.component", {"component": "U2"}),
             ("fx", "math.formula", {"expr": "m = np.max(np.abs(a))\na * 2", "outputs": "m"})]
    edges = [("odb", "odb", "fr", "odb"), ("odb", "odb", "u", "odb"), ("fr", "frames", "u", "param:frame"),
             ("u", "field", "u2", "field"), ("u2", "field", "fx", "a")]
    _, (fields, peaks) = outputs(nodes, edges, "fx", ("value", "out1"))
    assert len(fields) == len(peaks) > 1
    assert all(type(f).__name__ == "Field" for f in fields) and all(type(m).__name__ == "Number" for m in peaks)


def test_script_language():
    run = ops.evaluate
    assert run("total = 0\nfor i in range(5):\n    total += i\ntotal", {}) == 10
    assert run("def sq(x, k=2):\n    return x ** k\nsq(3) + sq(2, k=3)", {}) == 17
    assert run("f = lambda x: x + 1\n[f(v) for v in [1, 2, 3] if v > 1]", {}) == [3, 4]
    assert run("d = {'L': 1, 'R': 2}\nd['C'] = 3\nsorted(d.keys())", {}) == ["C", "L", "R"]
    assert run("x = [1, 2]\nx.append(3)\nlen(x)", {}) == 3
    assert run("n = 0\nwhile n < 10:\n    n += 3\n    if n == 6:\n        break\nn", {}) == 6
    assert run("p, q = 1, 2\nresult = q - p", {}) == 1
    assert run("max([3, 7, 1])", {}) == 7 and list(run("max(a, 2)", {"a": np.array([1, 3])})) == [2, 3]
    assert run("x = None\n'none' if x is None else 'set'", {}) == "none"
    assert run("a = 0\na > 0 and 1 / a > 1", {}) == False  # noqa: E712 (one value: the right side isn't run)
    assert run("s = 0\nfor i, v in enumerate([5, 6]):\n    s += i * v\ns", {}) == 6


def test_script_does_not_change_its_inputs():
    a = np.array([1.0, 2.0, 3.0])
    assert list(ops.evaluate("a[0] = 100\na += 1\na", {"a": a})) == [101, 3, 4]
    assert list(a) == [1, 2, 3]


@pytest.mark.parametrize("code, words", [
    ("import os\n1", "Imports aren't available"),
    ("x = 1\nx.__class__", "isn't available"),
    ("x = [1]\nx.__len__()", "has no '.__len__()'"),
    ("open('x')\n1", "Unknown function"),
    ("y = np.zeros(3)\nnp.clip(y, 0, 1, out=y)\ny", "out= isn't available"),
    ("while True:\n    pass\n1", "loop rounds"),
    ("def f(n):\n    return f(n + 1)\nf(0)", "too deeply"),
    ("x = 1\ny = x +\n2", "line 2"),
    ("x = 1\ny = q * 2\ny", "Line 2: Unknown name 'q'"),
    ("x = 1", "set 'result"),
    ("x = [0] * 1000000000\nx", "too big"),
    ("lambda: 1", "gives a function"),
])
def test_script_errors_are_clear_and_nothing_unsafe_runs(code, words):
    with pytest.raises(ValueError) as exc:
        ops.evaluate(code, {"a": 1.0})
    assert words in str(exc.value)


def test_a_named_output_the_script_does_not_set_is_an_error():
    res, _ = Engine().evaluate(graph([("n", "math.number", {"value": 2}),
                                      ("fx", "math.formula", {"expr": "a * 2", "outputs": "peak"})],
                                     [("n", "value", "fx", "a")]))
    assert res["fx"]["status"] == "error" and "'peak' isn't set" in res["fx"]["message"]
    res, _ = Engine().evaluate(graph([("fx", "math.formula", {"expr": "1", "outputs": "a b, 3x"})], []))
    assert "'3x' isn't one" in res["fx"]["message"]


def test_texts_and_mixed_lists_leave_as_values():
    _, (v, names) = outputs([("fx", "math.formula", {"expr": "names = ['L', 'R']\n[1, 'two', [3]]",
                                                      "outputs": "names"})], [], "fx", ("value", "out1"))
    assert [type(x).__name__ for x in v] == ["Number", "Text", "list"]
    assert [n.value for n in names] == ["L", "R"]


def test_script_over_tables_with_a_named_output():
    _, (col, n) = outputs(
        ODB + [("fx", "math.formula", {"expr": "col = a[:, 2]\nn = len(col)\ncol * 2", "outputs": "n"})],
        ODB_E + [("m", "field", "fx", "a")], "fx", ("value", "out1"))
    assert isinstance(col, list) and n.value == len(col)


def test_one_line_formulas_are_unchanged():
    assert ops.evaluate("x if x > 0 else 0", {"x": np.array([-1.0, 2.0])}).tolist() == [0, 2]
    assert not ops.is_script("np.max(a)") and ops.is_script("x = 1\nx")
