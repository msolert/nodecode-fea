"""Text values: add (join), subtract (remove), format, split, test, and text driving other nodes."""

from .test_engine import MOCK, run


def test_join_remove_format_split():
    res = run(
        [("a", "text.value", {"text": "BEAM"}), ("b", "text.value", {"text": "-1"}), ("n", "math.number", {"value": 311.24}),
         ("join", "text.join", {"sep": ""}), ("joined_n", "text.join", {"sep": " = "}),
         ("rm", "text.replace", {"find": "-1"}), ("rep", "text.replace", {"find": "beam", "replace": "Part",
                                                                        "case": "Ignore case"}),
         ("fmt", "text.format", {"template": "Max {a:.1f} MPa at {b}"}), ("bad", "text.format", {"template": "{x}"}),
         ("csv", "text.value", {"text": "FIXED, TIP ,TOP_SURFACE"}), ("split", "text.split", {"sep": ","}),
         ("num", "text.value", {"text": " 2.5e3 "}), ("tonum", "text.to_number", {}),
         ("has", "text.test", {"op": "contains", "value": "beam"}),
         ("ends", "text.test", {"op": "ends with", "value": "X", "case": "Match case"})],
        [("a", "text", "join", "a"), ("b", "text", "join", "b"), ("a", "text", "joined_n", "a"),
         ("n", "value", "joined_n", "b"), ("join", "text", "rm", "text"), ("join", "text", "rep", "text"),
         ("n", "value", "fmt", "a"), ("join", "text", "fmt", "b"), ("csv", "text", "split", "text"),
         ("num", "text", "tonum", "text"), ("join", "text", "has", "text"), ("join", "text", "ends", "text")],
    )
    assert res["join"]["display"]["text"] == "BEAM-1"
    assert res["joined_n"]["display"]["text"] == "BEAM = 311.2"
    assert res["rm"]["display"]["text"] == "BEAM"
    assert res["rep"]["display"]["text"] == "Part-1"
    assert res["fmt"]["display"]["text"] == "Max 311.2 MPa at BEAM-1"
    assert res["bad"]["status"] == "error" and "{a}" in res["bad"]["message"]
    assert [r[2] for r in res["split"]["display"]["rows"]] == ["FIXED", "TIP", "TOP_SURFACE"]
    assert res["tonum"]["display"]["value"] == 2500
    assert res["has"]["display"]["text"] == "Yes" and res["ends"]["display"]["text"] == "No"


def test_text_drives_odb_and_set_names_in_a_loop():
    """An ODB path built from text; a list of set names drives Set, Filter and Reduce once per name."""
    res = run(
        [("dir", "text.value", {"text": "mock://"}), ("model", "text.value", {"text": "cantilever?load=500"}),
         ("path", "text.join", {"sep": ""}), ("odb", "odb.open", {"path": "ignored-when-connected"}),
         ("names", "text.value", {"text": "FIXED,TIP"}), ("split", "text.split", {}),
         ("set", "odb.set", {}), ("u", "odb.field", {"variable": "U"}), ("u2", "field.component", {"component": "U2"}),
         ("on", "field.filter", {}), ("min", "field.reduce", {"op": "Min"}),
         ("label", "text.format", {"template": "{a}: min U2 = {b:.3f}"})],
        [("dir", "text", "path", "a"), ("model", "text", "path", "b"), ("path", "text", "odb", "path"),
         ("names", "text", "split", "text"), ("odb", "odb", "set", "odb"), ("split", "list", "set", "name"),
         ("odb", "odb", "u", "odb"), ("u", "field", "u2", "field"), ("u2", "field", "on", "field"),
         ("set", "set", "on", "set"), ("on", "field", "min", "field"), ("split", "list", "label", "a"),
         ("min", "value", "label", "b")],
    )
    assert res["odb"]["status"] == "ok", res["odb"]
    assert res["odb"]["display"]["title"] == "mock://cantilever?load=500"
    assert res["set"]["summary"] == "2 items"
    labels = [r[2] for r in res["label"]["display"]["rows"]]
    assert labels[0].startswith("FIXED: min U2 = ") and float(labels[0].split("= ")[1]) == 0
    assert labels[1] == "TIP: min U2 = -0.952"  # 500 N tip load: -P L^3 / 3EI = -0.952 mm


def test_plain_numbers_in_templates_read_nicely():
    res = run([("n", "math.number", {"value": 2}), ("f", "text.format", {"template": "{a} nodes"})],
              [("n", "value", "f", "a")])
    assert res["f"]["display"]["text"] == "2 nodes"
