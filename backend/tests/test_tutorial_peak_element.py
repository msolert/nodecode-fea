"""The walkthrough 'find the element with the highest Mises stress and plot it', as a graph."""

from nodecode_fea.cae_export import build_script
from nodecode_fea.engine import Engine

from .test_engine import MOCK, graph

NODES = [
    ("odb", "odb.open", {"path": MOCK}),
    ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),      # Mises at the last frame
    ("max", "math.reduce", {"op": "Max"}),                            # the peak, remembering its element
    ("peak", "view.value", {"label": "Peak Mises", "unit": "MPa"}),
    ("map", "view.contour", {}),                                      # contour with the peak marked
    ("hist", "field.time", {"variable": "S", "quantity": "Mises"}),   # Mises vs time at that element
    ("chart", "view.chart", {"title": "Mises at the peak element", "viewport": "New"}),
    ("only", "view.group", {"name": "Peak element", "show": "Create only"}),
]
EDGES = [
    ("odb", "odb", "s", "odb"), ("s", "field", "max", "field"), ("max", "value", "peak", "value"),
    ("s", "field", "map", "field"), ("max", "value", "map", "markers"),
    ("odb", "odb", "hist", "odb"), ("max", "value", "hist", "set"), ("hist", "series", "chart", "series"),
    ("max", "value", "only", "members"), ("odb", "odb", "only", "odb"),
]


def test_peak_element_walkthrough():
    g = graph(NODES, EDGES)
    res, inputs = Engine().evaluate(g)
    assert all(r["status"] == "ok" for r in res.values()), {k: r["message"] for k, r in res.items() if r["message"]}
    assert "element BEAM-1." in res["max"]["summary"]
    assert len(res["map"]["display"]["markers"]) == 1
    curve = res["chart"]["display"]["series"]
    assert len(curve) == 1 and curve[0]["y"][-1] == res["peak"]["display"]["value"]
    assert "1 elements" in res["only"]["display"]["title"]
    build_script(g, res, inputs)  # and it all goes to CAE
