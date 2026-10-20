"""Settings can be fed by wires (handle 'param:<name>'), including lists, which loop the node."""

from .test_engine import MOCK, graph
from nodecode_fea.engine import Engine


def run(nodes, edges):
    return Engine().run(graph(nodes, edges))


BASE = [("odb", "odb.open", {"path": MOCK}), ("s", "odb.field", {"variable": "S"}),
        ("m", "field.invariant", {"invariant": "Mises"})]
BASE_E = [("odb", "odb", "s", "odb"), ("s", "field", "m", "field")]


def test_filter_by_value_takes_its_value_from_a_wire():
    res = run(
        BASE + [("limit", "math.number", {"value": 300}), ("f", "field.where", {"op": ">", "value": 0}),
                ("n", "list.length", {})],
        BASE_E + [("m", "field", "f", "field"), ("limit", "value", "f", "param:value"), ("f", "field", "n", "list")],
    )
    assert res["f"]["status"] == "ok", res["f"]
    assert all(r[2] > 300 for r in res["f"]["display"]["rows"])


def test_list_into_a_setting_loops_the_node():
    res = run(
        BASE + [("limits", "list.range", {"start": 100, "stop": 300, "step": 100}),
                ("f", "field.where", {"op": ">", "value": 0}), ("n", "list.length", {}),
                ("count", "field.reduce", {"op": "Max"})],
        BASE_E + [("m", "field", "f", "field"), ("limits", "list", "f", "param:value"),
                  ("f", "field", "count", "field")],
    )
    assert res["f"]["summary"] == "3 fields · S Mises > 100"
    assert res["count"]["summary"].startswith("3 numbers")


def test_conversions_and_errors():
    res = run(
        BASE + [("txt", "text.value", {"text": "250"}), ("bad", "text.value", {"text": "abc"}),
                ("i", "input.integer", {"value": 3}), ("yes", "input.bool", {"value": "No"}),
                ("f_text", "field.where", {}), ("f_bad", "field.where", {}),
                ("frame", "odb.field", {"variable": "S"}), ("inv", "field.invariant", {}),
                ("vtext", "text.value", {"text": "Max. Principal"})],
        BASE_E + [("m", "field", "f_text", "field"), ("txt", "text", "f_text", "param:value"),
                  ("m", "field", "f_bad", "field"), ("bad", "text", "f_bad", "param:value"),
                  ("odb", "odb", "frame", "odb"), ("i", "value", "frame", "param:frame"),
                  ("frame", "field", "inv", "field"), ("vtext", "text", "inv", "param:invariant")],
    )
    assert res["f_text"]["status"] == "ok"
    assert res["f_bad"]["status"] == "error" and "needs a number" in res["f_bad"]["message"]
    assert "frame 3" in res["inv"]["display"]["title"]  # Integer -> Frame (a numeric dropdown)
    assert res["inv"]["display"]["title"].startswith("S Max. Principal")  # Text -> Invariant dropdown
    assert res["yes"]["display"]["text"] == "No"


def test_catalog_marks_which_settings_take_wires():
    from nodecode_fea.nodes import REGISTRY
    params = {p["name"]: p["port"] for p in REGISTRY["field.where"].to_json()["params"]}
    assert params == {"op": "text", "value": "number"}
    assert {p["name"]: p["port"] for p in REGISTRY["cae.pick"].to_json()["params"]}["pick"] is None


def test_step_by_number_and_old_frame_wire():
    """Step takes a step number (1 = first, -1 = last) as well as a name; a wire into Field Output's old
    'Frame #' port still drives the Frame setting."""
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("one", "math.number", {"value": 1}),
         ("f", "odb.field", {"variable": "U"}), ("fr", "math.number", {"value": 3}),
         ("bad", "math.number", {"value": 9}), ("g", "odb.field", {"variable": "U"})],
        [("odb", "odb", "f", "odb"), ("one", "value", "f", "param:step"), ("fr", "value", "f", "frame"),
         ("odb", "odb", "g", "odb"), ("bad", "value", "g", "param:step")],
    )
    assert res["f"]["status"] == "ok", res["f"]
    assert "frame 3" in res["f"]["display"]["title"]
    assert res["g"]["status"] == "error" and "a number 1…" in res["g"]["message"]
