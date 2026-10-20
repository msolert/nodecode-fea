"""Loops (lists run nodes once per item), Yes/No values, conditions and filters."""

import numpy as np
import pytest

from nodecode_fea import ops

from .test_engine import MOCK, run


def test_loop_over_frames_matches_field_vs_time():
    """All Frames -> Field Output (Frame #) -> Mises -> Reduce -> Numbers to Curve == Field vs Time."""
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("frames", "odb.frames", {}),
         ("s", "odb.field", {"variable": "S"}), ("m", "field.invariant", {"invariant": "Mises"}),
         ("max", "field.reduce", {"op": "Max"}), ("curve", "list.to_series", {"name": "max Mises"}),
         ("ref", "field.over_frames", {"variable": "S", "quantity": "Mises", "op": "Max"})],
        [("odb", "odb", "frames", "odb"), ("odb", "odb", "s", "odb"), ("frames", "frames", "s", "frame"),
         ("s", "field", "m", "field"), ("m", "field", "max", "field"), ("max", "value", "curve", "y"),
         ("frames", "times", "curve", "x"), ("odb", "odb", "ref", "odb")],
    )
    assert res["s"]["summary"] == "11 fields · S"
    loop = res["curve"]["display"]["series"][0]
    ref = res["ref"]["display"]["series"][0]
    assert loop["x"] == pytest.approx(ref["x"]) and loop["y"] == pytest.approx(ref["y"])


def test_compare_if_and_filters():
    nums = [(f"n{i}", "math.number", {"value": v, "label": f"N{i}"}) for i, v in enumerate([100, 400, 250])]
    res = run(
        nums + [("merge", "list.merge", {}), ("big", "logic.compare", {"op": ">", "value": 200}),
                ("keep", "list.filter", {}), ("byvalue", "list.filter", {"op": "<", "value": 300}),
                ("hi", "math.number", {"value": 1}), ("lo", "math.number", {"value": 0}),
                ("ifl", "logic.if", {}), ("one", "logic.compare", {"op": "==", "value": 100}),
                ("not", "logic.gate", {"op": "Not a"}), ("and", "logic.gate", {"op": "And"}),
                ("if1", "logic.if", {}), ("if_no_else", "logic.if", {})],
        [("n0", "value", "merge", "a"), ("n1", "value", "merge", "b"), ("n2", "value", "merge", "c"),
         ("merge", "list", "big", "a"), ("merge", "list", "keep", "list"), ("big", "result", "keep", "keep"),
         ("merge", "list", "byvalue", "list"),
         ("big", "result", "ifl", "condition"), ("hi", "value", "ifl", "then"), ("lo", "value", "ifl", "else"),
         ("n0", "value", "one", "a"), ("one", "result", "not", "a"), ("one", "result", "and", "a"),
         ("not", "result", "and", "b"),
         ("not", "result", "if1", "condition"), ("hi", "value", "if1", "then"), ("lo", "value", "if1", "else"),
         ("not", "result", "if_no_else", "condition"), ("hi", "value", "if_no_else", "then")],
    )
    assert res["big"]["summary"] == "2 Yes of 3"
    assert [r[2] for r in res["keep"]["display"]["rows"]] == [400, 250]
    assert [r[2] for r in res["byvalue"]["display"]["rows"]] == [100, 250]
    assert [r[2] for r in res["ifl"]["display"]["rows"]] == [0, 1, 1]  # item by item
    assert res["one"]["display"] == {"kind": "text", "text": "Yes", "label": "N0 == 100"}
    assert res["and"]["display"]["text"] == "No"
    assert res["if1"]["display"]["value"] == 0
    assert res["if_no_else"]["status"] == "error" and "Else" in res["if_no_else"]["message"]


def test_filter_by_value_on_a_field():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("yield", "field.where", {"op": ">", "value": 300}),
         ("n", "list.length", {}), ("c", "view.contour", {}), ("none", "field.where", {"op": ">", "value": 1e9}),
         ("rows", "list.filter", {"op": ">", "value": 300})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "yield", "field"),
         ("yield", "field", "n", "list"), ("yield", "field", "c", "field"), ("m", "field", "none", "field"),
         ("m", "field", "rows", "list")],
    )
    n = res["n"]["display"]["value"]
    assert 0 < n < 640
    assert all(r[2] > 300 for r in res["yield"]["display"]["rows"])
    assert res["c"]["status"] == "ok"
    assert res["none"]["status"] == "error"
    assert res["rows"]["display"]["total"] == n  # Filter List on a field filters its rows the same way


def test_range_and_loop_over_numbers():
    res = run(
        [("r", "list.range", {"start": 0, "stop": 1, "step": 0.25}), ("sq", "math.expression", {"expr": "a ** 2"}),
         ("bad", "list.range", {"start": 0, "stop": 1, "step": -1})],
        [("r", "list", "sq", "a")],
    )
    assert [r[2] for r in res["r"]["display"]["rows"]] == [0, 0.25, 0.5, 0.75, 1]
    assert [r[2] for r in res["sq"]["display"]["rows"]] == pytest.approx([0, 0.0625, 0.25, 0.5625, 1])
    assert res["bad"]["status"] == "error"


def test_conditions_in_formulas():
    assert ops.evaluate("a > 3 and a < 10", {"a": 5.0})
    assert not ops.evaluate("not (a >= 5)", {"a": 5.0})
    assert ops.evaluate("1 < a <= 2", {"a": 2.0})
    x = np.array([-1.0, 2.0, 5.0])
    assert list(ops.evaluate("x if x > 0 else 0", {"x": x})) == [0, 2, 5]
    assert list(ops.evaluate("where(x > 1, 1, 0)", {"x": x})) == [0, 1, 1]
    res = run([("a", "math.number", {"value": 400}), ("e", "math.expression", {"expr": "1 if a > 355 else 0"})],
              [("a", "value", "e", "a")])
    assert res["e"]["display"]["value"] == 1


def test_for_loop_with_accumulate():
    """for i = 1 to 10: total += i**2  ->  385."""
    res = run(
        [("for", "loop.for", {"start": 1, "stop": 10, "step": 1}), ("sq", "math.expression", {"expr": "a ** 2"}),
         ("acc", "loop.accumulate", {"op": "Sum"}), ("max", "loop.accumulate", {"op": "Max"})],
        [("for", "i", "sq", "a"), ("sq", "value", "acc", "values"), ("sq", "value", "max", "values")],
    )
    assert res["for"]["summary"].startswith("10 numbers")
    assert res["acc"]["display"]["rows"][-1][2] == 385
    assert [r[2] for r in res["acc"]["display"]["rows"]][:3] == [1, 5, 14]
    assert res["max"]["status"] == "ok"


def test_for_each_over_a_set_and_a_list():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("tip", "odb.set", {"name": "TIP"}), ("each", "loop.foreach", {}),
         ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": ""}),
         ("peak", "series.reduce", {"op": "Min"}), ("total", "loop.accumulate", {"op": "Min"}),
         ("count", "view.value", {})],
        [("odb", "odb", "tip", "odb"), ("tip", "set", "each", "list"), ("odb", "odb", "p", "odb"),
         ("each", "item", "p", "set"), ("p", "series", "peak", "series"), ("peak", "value", "total", "values"),
         ("each", "count", "count", "value")],
    )
    assert res["count"]["display"]["value"] == 25
    assert res["p"]["summary"].startswith("25 curves")
    assert res["total"]["status"] == "ok"


def test_numpy_in_formulas():
    x = np.array([1.0, 4.0, 9.0, 16.0])
    assert list(ops.evaluate("np.sqrt(x)", {"x": x})) == [1, 2, 3, 4]
    assert ops.evaluate("np.percentile(x, 50)", {"x": x}) == pytest.approx(6.5)
    assert list(ops.evaluate("np.clip(x, 2, 10)", {"x": x})) == [2, 4, 9, 10]
    assert list(ops.evaluate("x - np.mean(x)", {"x": x})) == pytest.approx([-6.5, -3.5, 1.5, 8.5])
    assert list(ops.evaluate("x[x > 5]", {"x": x})) == [9, 16]
    assert ops.evaluate("np.round(np.pi, decimals=2)", {}) == pytest.approx(3.14)
    assert ops.evaluate("norm([3, 4])", {}) == 5
    for bad in ["np.load('x')", "np.save('x', 1)", "np.lib", "numpy.fromfile('x')", "x.__class__", "np.__dict__"]:
        with pytest.raises(ValueError):
            ops.evaluate(bad, {"x": x})


def test_field_math_with_numpy():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}),
         ("norm", "field.expression", {"expr": "x / np.max(x)"}), ("mx", "field.reduce", {"op": "Max"}),
         ("agg", "field.expression", {"expr": "np.percentile(x, 95)"}),
         ("bad", "field.expression", {"expr": "x[x > 100]"})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "norm", "field"),
         ("norm", "field", "mx", "field"), ("m", "field", "agg", "field"), ("m", "field", "bad", "field")],
    )
    assert res["mx"]["display"]["value"] == pytest.approx(1.0)
    assert res["agg"]["status"] == "ok"  # one number: the same for every element
    assert res["bad"]["status"] == "error" and "one value per element" in res["bad"]["message"]


def test_loop_over_steps_and_odbs(tmp_path):
    # Steps: All Steps -> Field Output's Step setting (defaults stay: last frame)
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("steps", "odb.steps", {}),
         ("s", "odb.field", {"variable": "S", "step": "nope"}), ("m", "field.invariant", {}),
         ("max", "field.reduce", {"op": "Max"})],
        [("odb", "odb", "steps", "odb"), ("odb", "odb", "s", "odb"), ("steps", "names", "s", "param:step"),
         ("s", "field", "m", "field"), ("m", "field", "max", "field")],
    )
    assert [r[2] for r in res["steps"]["display"]["rows"]] == ["Load"]
    assert res["max"]["status"] == "ok", res["max"]

    # Several ODBs: a list of paths into Open ODB runs everything after it once per ODB
    res = run(
        [("paths", "text.value", {"text": "mock://cantilever?load=100, mock://cantilever?load=300"}),
         ("split", "text.split", {"sep": ","}), ("odb", "odb.open", {}),
         ("s", "odb.field", {"variable": "S"}), ("m", "field.invariant", {}), ("max", "field.reduce", {"op": "Max"}),
         ("label", "text.format", {"template": "{a}: {b:.1f}"})],
        [("paths", "text", "split", "text"), ("split", "list", "odb", "path"), ("odb", "odb", "s", "odb"),
         ("s", "field", "m", "field"), ("m", "field", "max", "field"), ("split", "list", "label", "a"),
         ("max", "value", "label", "b")],
    )
    values = [r[2] for r in res["max"]["display"]["rows"]]
    assert len(values) == 2 and values[1] == pytest.approx(3 * values[0])
    assert res["label"]["display"]["rows"][0][2].startswith("mock://cantilever?load=100: ")

    # Files in Folder lists the ODBs
    for name in ("Job-2.odb", "Job-1.odb", "notes.txt"):
        (tmp_path / name).write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Job-3.odb").write_text("x")
    res = run([("f", "source.files", {"folder": str(tmp_path)}),
               ("all", "source.files", {"folder": str(tmp_path), "subfolders": "Yes"}),
               ("none", "source.files", {"folder": str(tmp_path / "missing")})], [])
    assert [r[1] for r in res["f"]["display"]["rows"]] == ["Job-1.odb", "Job-2.odb"]
    assert res["all"]["summary"] == "3 texts"
    assert res["none"]["status"] == "error"
