"""Properties: read any value's properties (a step's name, an ODB's steps, a set's count…)."""

from .test_engine import MOCK, run


def chosen(result) -> str:
    """The chosen property's value as the node's table shows it (marked with ▶)."""
    assert result["status"] == "ok", result
    return next(v for k, v in result["display"]["items"] if k.startswith("▶ "))


def test_step_properties_chain_and_drive_text():
    res = run(
        [("m", "model.model", {"name": "Bracket"}),
         ("s1", "model.step", {"name": "Preload", "time": 0.5}),
         ("s2", "model.step", {"name": "Load", "nlgeom": "Yes"}),
         ("name", "data.properties", {"property": "Name"}),
         ("time", "data.properties", {"property": "time period"}),  # names match whatever the case
         ("pos", "data.properties", {"property": "Position"}),
         ("prev", "data.properties", {"property": "Previous step"}),
         ("prev_name", "data.properties", {"property": "Name"}),
         ("model", "data.properties", {"property": "Model"}),
         ("bad", "data.properties", {"property": "Colour"}),
         ("fmt", "text.format", {"template": "{a} after {b}"})],
        [("m", "model", "s1", "after"), ("s1", "step", "s2", "after"),
         ("s2", "step", "name", "value"), ("s2", "step", "time", "value"), ("s2", "step", "pos", "value"),
         ("s2", "step", "prev", "value"), ("prev", "value", "prev_name", "value"),
         ("s2", "step", "model", "value"), ("s2", "step", "bad", "value"),
         ("s2", "step", "fmt", "a"), ("s1", "step", "fmt", "b")],
    )
    assert chosen(res["name"]) == "Load"
    assert chosen(res["time"]) == "1"
    assert chosen(res["pos"]) == "2"
    assert chosen(res["prev_name"]) == "Preload"
    assert chosen(res["model"]) == "Bracket"
    assert res["bad"]["status"] == "error" and "Procedure" in res["bad"]["message"]
    assert "Time period" in [o["value"] for o in res["name"]["hints"]["property"]]
    # a step as text is its name
    assert res["fmt"]["display"]["text"] == "Load after Preload"


def test_odb_field_and_set_properties():
    res = run(
        [("odb", "odb.open", {"path": MOCK}), ("u", "odb.field", {"variable": "U"}), ("tip", "odb.set", {"name": "TIP"}),
         ("steps", "data.properties", {"property": "Step count"}),
         ("fstep", "data.properties", {"property": "Step"}),
         ("count", "data.properties", {"property": "Count"}),
         ("odb_of", "data.properties", {"property": "ODB"}), ("path", "data.properties", {"property": "Path"})],
        [("odb", "odb", "u", "odb"), ("odb", "odb", "tip", "odb"), ("odb", "odb", "steps", "value"),
         ("u", "field", "fstep", "value"), ("tip", "set", "count", "value"),
         ("u", "field", "odb_of", "value"), ("odb_of", "value", "path", "value")],
    )
    assert int(chosen(res["steps"])) >= 1
    assert chosen(res["fstep"])
    assert int(chosen(res["count"])) >= 1
    assert chosen(res["path"]) == MOCK


def test_list_of_properties_and_list_of_values_loop():
    res = run(
        [("s1", "model.step", {"name": "A"}), ("s2", "model.step", {"name": "B", "kind": "Frequency"}),
         ("names", "text.value", {"text": "Name, Procedure"}), ("split", "text.split", {"sep": ","}),
         ("both", "data.properties", {})],
        [("s1", "step", "s2", "after"), ("names", "text", "split", "text"),
         ("split", "list", "both", "param:property"), ("s2", "step", "both", "value")],
    )
    assert res["both"]["status"] == "ok", res["both"]
    assert [r[-1] for r in res["both"]["display"]["rows"]] == ["B", "Frequency"]


def test_every_model_value_has_a_name_and_description():
    from nodecode_fea.properties import properties
    from nodecode_fea.values import (AnalysisStep, Condition, DatumPlane, Interaction, Job, Material, Model, Part,
                                      Instance, Region, Section, Sketch)

    model = Model("M")
    sketch = Sketch("S", (("Rectangle", (0, 0), (10, 2)),), model)
    part = Part("Beam", model, "Solid", "Extrude", sketch, depth=5.0)
    inst = Instance("Beam-1", part)
    face = Region(inst, "face", indices=(1, 2))
    step = AnalysisStep("Load", model, "Static")
    material = Material("Steel", model, 210000.0, 0.3)
    values = [model, sketch, part, inst, face, step, material, Section("Sec", material, "Solid"),
              DatumPlane("D", part, "XY", 1.0), Condition("Fix", step, "Encastre", face),
              Interaction("Tie", None, "Tie", face, Region(inst, "face", indices=(3,))), Job("J", model, step)]
    for v in values:
        props = properties(v)
        assert props["Name"].value, v
    assert properties(part)["Depth"].value == 5.0
    assert properties(face)["Count"].value == 2
    assert properties(values[-1])["ODB path"].value.endswith("J.odb")
