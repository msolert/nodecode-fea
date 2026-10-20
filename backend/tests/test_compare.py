"""Compare: one test node for numbers, lists, fields, curves and texts."""

import pytest

from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph


def run(nodes, edges):
    sinks = [(f"n_{h}", "list.length", {}) for h in ("kept", "result", "count")]
    res, inputs = Engine().evaluate(graph(nodes + sinks, edges + [("cmp", h, f"n_{h}", "list") for h in
                                                                   ("kept", "result", "count")]))
    assert res["cmp"]["status"] == "ok", res["cmp"]
    return res, {h: inputs[f"n_{h}"]["list"] for h in ("kept", "result", "count")}


FIELD = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"})]
FIELD_E = [("odb", "odb", "s", "odb"), ("s", "field", "m", "field")]


def test_field_keeps_passing_rows_like_filter_by_value():
    res, out = run(FIELD + [("cmp", "logic.test", {"op": ">", "value": "300"}), ("old", "field.where",
                                                                                 {"op": ">", "value": 300})],
                   FIELD_E + [("m", "field", "cmp", "a"), ("m", "field", "old", "field")])
    assert (out["kept"].values[:, 0] > 300).all()
    assert out["count"].value == len(out["kept"].labels) == res["old"]["display"]["total"]
    assert out["result"].value is True


def test_number_with_b_wired():
    _, out = run([("a", "math.number", {"value": 400}), ("b", "math.number", {"value": 355.25}),
                  ("cmp", "logic.test", {"op": ">="})], [("a", "value", "cmp", "a"), ("b", "value", "cmp", "b")])
    assert out["result"].value is True and out["count"].value == 1 and out["kept"][0].value == 400


def test_list_gives_one_answer_per_item():
    _, out = run([("r", "list.range", {"start": 1, "stop": 5, "step": 1}),
                  ("cmp", "logic.test", {"op": "<", "value": "3"})], [("r", "list", "cmp", "a")])
    assert [b.value for b in out["result"]] == [True, True, False, False, False]
    assert [k.value for k in out["kept"]] == [1, 2]


def test_texts():
    _, out = run([("t", "text.value", {"text": "Job-Load-2.odb"}),
                  ("cmp", "logic.test", {"op": "contains", "value": "load"})], [("t", "text", "cmp", "a")])
    assert out["result"].value is True
    _, out = run([("t", "text.value", {"text": "Job-Load-2.odb"}),
                  ("cmp", "logic.test", {"op": "contains", "value": "load", "case": "Match case"})],
                 [("t", "text", "cmp", "a")])
    assert out["result"].value is False and out["kept"] == []


def test_curves_by_peak():
    _, out = run([("odb", "odb.open", {"path": MOCK}),
                  ("p", "field.probe", {"variable": "U", "quantity": "U2", "labels": "1, 2, 3, 4"}),
                  ("cmp", "logic.test", {"op": ">", "value": "0"})],
                 [("odb", "odb", "p", "odb"), ("p", "series", "cmp", "a")])
    assert len(out["result"]) == 4


def test_bad_combinations_say_why():
    res, _ = Engine().evaluate(graph(
        [("t", "text.value", {"text": "abc"}), ("cmp", "logic.test", {"op": ">", "value": "x"})],
        [("t", "text", "cmp", "a")]))
    assert res["cmp"]["status"] == "error" and "compares numbers" in res["cmp"]["message"]
