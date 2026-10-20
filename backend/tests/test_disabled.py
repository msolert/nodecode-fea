"""Deactivated nodes: pass their input through where types match, drop out of optional inputs, else downstream waits."""

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph


def run(nodes, edges, off):
    g = graph(nodes, edges)
    for n in g["nodes"]:
        if n["id"] in off:
            n["disabled"] = True
    return g, Engine().evaluate(g)


BASE = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
        ("q", "field.quantity", {"quantity": "Mises"}), ("r", "math.reduce", {"op": "Max"})]
E = [("odb", "odb", "s", "odb"), ("s", "field", "q", "field"), ("q", "field", "r", "field")]


def test_bypass_passes_matching_input_through():
    _, (res, inputs) = run(BASE + [("c", "view.contour", {})], E + [("q", "field", "c", "field")], {"q"})
    assert res["q"]["status"] == "disabled" and "passes" in res["q"]["message"]
    # Reduce now gets the whole S field (6 components), so it reports why; the contour too.
    assert res["r"]["status"] == "error" and "scalar" in res["r"]["message"]


def test_optional_input_just_loses_it():
    nodes = BASE + [("c", "view.contour", {})]
    edges = E + [("q", "field", "c", "field"), ("r", "value", "c", "markers")]
    _, (res, inputs) = run(nodes, edges, {"r"})
    assert res["r"]["status"] == "disabled"
    assert res["c"]["status"] == "ok" and not res["c"]["display"]["markers"]


def test_required_input_waits_with_reason():
    nodes = BASE + [("v", "view.value", {})]
    _, (res, _) = run(nodes, E + [("r", "value", "v", "value")], {"r"})
    assert res["v"]["status"] == "blocked" and "Reduce is deactivated" in res["v"]["message"]


def test_wired_setting_falls_back_to_typed_value():
    nodes = BASE + [("lim", "input.value", {"type": "Number", "value": "400"}),
                    ("cmp", "logic.test", {"op": ">", "value": "100"})]
    edges = E + [("q", "field", "cmp", "a"), ("lim", "value", "cmp", "param:value")]
    _, (on, _) = run(nodes, edges, set())
    _, (off, _) = run(nodes, edges, {"lim"})
    assert off["cmp"]["status"] == "ok" and on["cmp"]["summary"] != off["cmp"]["summary"]


def test_deactivated_outputs_are_left_out_of_cae():
    nodes = BASE + [("c", "view.contour", {}), ("v", "view.value", {})]
    g, (res, inputs) = run(nodes, E + [("q", "field", "c", "field"), ("r", "value", "v", "value")], {"c"})
    out = build_script(g, res, inputs)
    assert any("deactivated" in s for s in out["skipped"]) and len(out["contents"]) == 1
