import json
import math

import numpy as np
import pytest

from nodecode_fea import ops
from nodecode_fea.engine import Engine
from nodecode_fea.nodes import CATEGORIES, REGISTRY

MOCK = "mock://cantilever?load=1000&length=100"
P, L, h, b, E = 1000.0, 100.0, 10.0, 10.0, 210000.0
I = b * h**3 / 12


def graph(nodes, edges):
    return {
        "nodes": [{"id": i, "type": t, "params": p} for i, t, p in nodes],
        "edges": [{"source": s, "sourceHandle": sh, "target": t, "targetHandle": th} for s, sh, t, th in edges],
    }


def run(nodes, edges):
    return Engine().run(graph(nodes, edges))


def test_every_node_has_unique_ports_and_valid_defaults():
    for d in REGISTRY.values():
        names = [p.name for p in d.inputs]
        assert len(names) == len(set(names)), d.type
        assert d.category in CATEGORIES
        json.dumps(d.to_json())


def test_max_mises_matches_beam_theory():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("m", "field.invariant", {"invariant": "Mises"}), ("r", "field.reduce", {"op": "Max"})],
        [("odb", "odb", "s", "odb"), ("s", "field", "m", "field"), ("m", "field", "r", "field")],
    )
    assert res["r"]["status"] == "ok", res["r"]
    # Hottest element: first along x, outermost in y (centroid location).
    x, y = L / 40 / 2, h / 2 - h / 4 / 2
    sigma = P * (L - x) * y / I
    tau = P * (h**2 / 4 - y**2) / (2 * I)
    expected = math.sqrt(sigma**2 + 3 * tau**2)
    assert res["r"]["display"]["value"] == pytest.approx(expected, rel=1e-9)
    assert "element BEAM-1." in res["r"]["display"]["where"]


def test_tip_deflection_history_and_xy_curve():
    tip_region = "Node BEAM-1.{}"
    res = run(
        [("odb", "odb.open", {"path": MOCK})], [])
    regions = res["odb"]["display"]  # just make sure the ODB opens
    assert regions["kind"] == "info"

    from nodecode_fea.sources import open_source
    src = open_source(MOCK)
    region = tip_region.format(src.tip_node)
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("u", "odb.history", {"region": region, "output": "U2"}),
         ("rf", "odb.history", {"region": "Node set FIXED", "output": "RF2"}),
         ("pk", "series.reduce", {"op": "Final"}),
         ("xy", "series.xy", {"xscale": -1}),
         ("chart", "view.chart", {})],
        [("odb", "odb", "u", "odb"), ("odb", "odb", "rf", "odb"), ("u", "series", "pk", "series"),
         ("u", "series", "xy", "x"), ("rf", "series", "xy", "y"), ("xy", "series", "chart", "series")],
    )
    assert res["pk"]["display"]["value"] == pytest.approx(-P * L**3 / (3 * E * I))
    curve = res["chart"]["display"]["series"][0]
    stiffness = np.array(curve["y"][1:]) / np.array(curve["x"][1:])
    assert stiffness == pytest.approx(3 * E * I / L**3)


def test_history_output_can_concatenate_all_steps():
    class HistSource:
        def history(self, step, region, output):
            data = {"Preload": ([0.0, 1.0], [0.0, 10.0]), "Load": ([0.0, 2.0], [10.0, 30.0])}[step]
            return {"x": np.asarray(data[0]), "y": np.asarray(data[1])}

    from nodecode_fea.nodes import odb_history
    from nodecode_fea.values import Odb
    info = {"steps": [{"name": "Preload"}, {"name": "Load"}]}
    curve = odb_history({"odb": Odb("two.odb", 0, HistSource(), info)},
                        {"steps": "All steps", "step": "", "region": "Node set FIXED", "output": "RF2"})["series"]
    assert curve.xlabel == "Total time"
    assert curve.x.tolist() == [0.0, 1.0, 1.0, 3.0]
    assert curve.y.tolist() == [0.0, 10.0, 10.0, 30.0]


def test_all_steps_history_uses_each_step_total_time():
    class HistSource:
        def history(self, step, region, output):
            # output stops before the end of Preload: its last point is not where Load starts
            data = {"Preload": ([0.0, 0.5], [0.0, 5.0]), "Load": ([0.0, 2.0], [10.0, 30.0])}[step]
            return {"x": np.asarray(data[0]), "y": np.asarray(data[1])}

    from nodecode_fea.nodes import odb_history
    from nodecode_fea.values import Odb
    info = {"steps": [{"name": "Preload", "totalTime": 0.0}, {"name": "Load", "totalTime": 1.0}]}
    curve = odb_history({"odb": Odb("two.odb", 0, HistSource(), info)},
                        {"steps": "All steps", "step": "", "region": "Node set FIXED", "output": "RF2"})["series"]
    assert curve.x.tolist() == [0.0, 0.5, 1.0, 3.0]


def test_hidden_section_point_does_not_filter_automatic_fields():
    res, inputs = Engine().evaluate(graph(
        [("odb", "odb.open", {"path": MOCK}),
         ("a", "odb.field", {"variable": "S"}),
         ("b", "odb.field", {"variable": "S", "position": "Automatic", "section_point": 3})],
        [("odb", "odb", "a", "odb"), ("odb", "odb", "b", "odb")]))
    assert res["b"]["status"] == "ok", res["b"]
    assert np.allclose(inputs.outputs["a"]["field"].values, inputs.outputs["b"]["field"].values)


def test_filter_by_set_and_wrong_set_kind():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("root", "odb.set", {"name": "ROOT_ELEMENTS"}), ("tip", "odb.set", {"name": "TIP"}),
         ("f", "field.filter", {}), ("bad", "field.filter", {})],
        [("odb", "odb", "s", "odb"), ("odb", "odb", "root", "odb"), ("odb", "odb", "tip", "odb"),
         ("s", "field", "f", "field"), ("root", "set", "f", "set"),
         ("s", "field", "bad", "field"), ("tip", "set", "bad", "set")],
    )
    assert res["f"]["status"] == "ok"
    assert res["f"]["display"]["total"] == 5 * 4 * 4
    # A node set on an element field keeps the elements touching those nodes (the 4 x 4 tip elements).
    assert res["bad"]["status"] == "ok"
    assert res["bad"]["display"]["total"] == 16


def test_missing_inputs_errors_and_blocking():
    res = run(
        [("odb", "odb.open", {"path": "mock://nope"}), ("s", "odb.field", {}), ("lonely", "field.invariant", {})],
        [("odb", "odb", "s", "odb")],
    )
    assert res["odb"]["status"] == "error"
    assert res["s"]["status"] == "blocked"
    assert res["lonely"]["status"] == "waiting"


def test_cycle_is_reported():
    res = run(
        [("a", "field.invariant", {}), ("b", "field.component", {})],
        [("a", "field", "b", "field"), ("b", "field", "a", "field")],
    )
    assert res["a"]["status"] == res["b"]["status"] == "error"


def test_hints_follow_upstream_odb():
    res = run([("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {})], [("odb", "odb", "s", "odb")])
    hints = res["s"]["hints"]
    assert [o["value"] for o in hints["variable"]] == ["U", "RF", "S", "E", "PEEQ"]
    assert hints["frame"][0]["value"] == -1 and len(hints["frame"]) == 12


def test_contour_surface_of_box_mesh():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
         ("u", "odb.field", {"variable": "U"}), ("m", "field.invariant", {"invariant": "Mises"}),
         ("c", "view.contour", {"scale": 0})],
        [("odb", "odb", "s", "odb"), ("odb", "odb", "u", "odb"), ("s", "field", "m", "field"),
         ("m", "field", "c", "field"), ("u", "field", "c", "deform")],
    )
    d = res["c"]["display"]
    assert res["c"]["status"] == "ok", res["c"]
    quads = 2 * (40 * 4 + 40 * 4 + 4 * 4)
    assert len(d["triangles"]) == quads * 2 * 3
    surface_nodes = 41 * 5 * 5 - 39 * 3 * 3
    assert len(d["values"]) == surface_nodes
    assert len(d["positions"]) == surface_nodes * 3
    assert d["deformScale"] > 0
    json.dumps(d)


def test_field_vs_time_and_expression():
    res = run(
        [("odb", "odb.open", {"path": MOCK}),
         ("t", "field.over_frames", {"variable": "S", "quantity": "Mises", "op": "Max"}),
         ("pk", "series.reduce", {"op": "Max"}),
         ("y", "math.number", {"value": 355}),
         ("sf", "math.expression", {"expr": "b / a"})],
        [("odb", "odb", "t", "odb"), ("t", "series", "pk", "series"),
         ("pk", "value", "sf", "a"), ("y", "value", "sf", "b")],
    )
    ys = res["t"]["display"]["series"][0]["y"]
    assert ys[0] == pytest.approx(0) and ys == sorted(ys)
    assert res["sf"]["display"]["value"] == pytest.approx(355 / ys[-1])


def test_strain_principal_uses_tensor_shear():
    # Pure shear: engineering gamma12 = 2e-3 -> principal strains are +/- 1e-3.
    vals = np.array([[0, 0, 0, 2e-3, 0, 0]], dtype=float)
    comps = ["E11", "E22", "E33", "E12", "E13", "E23"]
    assert ops.invariant("Max. Principal", comps, vals, "E", "TENSOR")[0] == pytest.approx(1e-3)
    s = ops.invariant("Max. Principal", [c.replace("E", "S") for c in comps], vals, "S", "TENSOR")
    assert s[0] == pytest.approx(2e-3)


def test_quantity_resolves_vector_and_tensor_components_in_local_axes():
    nodes = [("odb", "odb.open", {"path": MOCK}),
             ("u", "odb.field", {"variable": "U"}), ("s", "odb.field", {"variable": "S"}),
             ("axes", "input.value", {"type": "List / matrix", "value": "[[0,1,0],[1,0,0],[0,0,-1]]"}),
             ("lu", "field.quantity", {"quantity": "U1", "coordinates": "Local axes"}),
             ("gu", "field.quantity", {"quantity": "U2"}),
             ("cu", "field.quantity", {"quantity": "U1", "coordinates": "Cylindrical",
                                         "origin": "0, 100, 0", "axis": "0, 0, 1"}),
             ("ls", "field.quantity", {"quantity": "S11", "coordinates": "Local axes"}),
             ("gs", "field.quantity", {"quantity": "S22"})]
    edges = [("odb", "odb", "u", "odb"), ("odb", "odb", "s", "odb"),
             ("u", "field", "lu", "field"), ("u", "field", "gu", "field"),
             ("u", "field", "cu", "field"),
             ("s", "field", "ls", "field"), ("s", "field", "gs", "field"),
             ("axes", "value", "lu", "axes"), ("axes", "value", "ls", "axes")]
    res, inputs = Engine().evaluate(graph(nodes, edges))
    assert res["lu"]["status"] == res["ls"]["status"] == "ok", res
    assert np.allclose(inputs.outputs["lu"]["field"].values, inputs.outputs["gu"]["field"].values)
    assert np.allclose(inputs.outputs["ls"]["field"].values, inputs.outputs["gs"]["field"].values)
    u = inputs.outputs["u"]["field"]
    from nodecode_fea.mesh import surface_for
    surf = surface_for(u.odb)
    idx = surf.lookup("node", u.instances, u.labels)
    radial = surf.coords[idx] - np.array([0.0, 100.0, 0.0])
    radial[:, 2] = 0.0
    radial /= np.linalg.norm(radial, axis=1)[:, None]
    expected = np.sum(radial * u.values, axis=1)
    assert np.allclose(inputs.outputs["cu"]["field"].values[:, 0], expected)


def test_mises_of_uniaxial_and_hydrostatic():
    comps = ["S11", "S22", "S33", "S12", "S13", "S23"]
    vals = np.array([[100, 0, 0, 0, 0, 0], [50, 50, 50, 0, 0, 0]], dtype=float)
    assert ops.invariant("Mises", comps, vals, "S", "TENSOR") == pytest.approx([100, 0])
    assert ops.invariant("Pressure", comps, vals, "S", "TENSOR") == pytest.approx([-100 / 3, -50])


@pytest.mark.parametrize("expr", ["__import__('os')", "a.__class__", "open('x')", "lambda: 1", "np.load('x')"])
def test_expression_rejects_code(expr):
    with pytest.raises(ValueError):
        ops.evaluate(expr, {"a": 1.0})


def test_expression_math():
    assert ops.evaluate("sqrt(a**2 + b**2)", {"a": 3.0, "b": 4.0}) == pytest.approx(5)
    with pytest.raises(ValueError):
        ops.evaluate("10**10**10", {})


def test_results_are_cached():
    eng = Engine()
    g = graph([("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"})],
              [("odb", "odb", "s", "odb")])
    eng.run(g)
    n = len(eng.cache)
    eng.run(g)
    assert len(eng.cache) == n
