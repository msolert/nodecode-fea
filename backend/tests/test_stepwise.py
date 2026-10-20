"""Settings that change by step and Active in: loads, boundary conditions, predefined fields and contact."""

import pytest

from nodecode_fea import stepwise

from .test_cae_export import calls, run_in_fake_cae
from .test_model import evaluate, script

NAMES = ["Initial", "Zenith", "Horizon", "Wind"]


def test_split_and_join_step_values():
    assert stepwise.split("0; Horizon: -9810; Wind: 0") == ("0", (("Horizon", "-9810"), ("Wind", "0")))
    assert stepwise.split("Horizon: 5") == ("", (("Horizon", "5"),))
    assert stepwise.split(-1.0) == ("-1.0", ())
    assert stepwise.split("Free; Load step: 0.5") == ("Free", (("Load step", "0.5"),))
    # an amplitude's own rows keep their ';', and an empty change means Abaqus' default
    assert stepwise.split("0, 0; 1, 1; Wind: 0, 1; 1, 0; Unload:") == (
        "0, 0; 1, 1", (("Wind", "0, 1; 1, 0"), ("Unload", "")))
    assert stepwise.join("0", [("Horizon", "-9810")]) == "0; Horizon: -9810"
    assert stepwise.join("", [("Horizon", "1")]) == "Horizon: 1"


def test_active_in_text():
    assert stepwise.parse_active("Zenith to Horizon, Wind") == (("range", "Zenith", "Horizon"), ("step", "Wind"))
    assert stepwise.parse_active("Horizon to end") == (("range", "Horizon", None),)
    assert stepwise.parse_active("all") == (("range", None, None),)
    assert stepwise.active_mask(stepwise.parse_active("Zenith, Wind"), NAMES, "Zenith") == [False, True, False, True]
    assert stepwise.active_mask((), NAMES, "Horizon") == [False, False, True, True]
    assert stepwise.active_mask(stepwise.parse_active("all"), NAMES, "Zenith") == [False, True, True, True]
    assert stepwise.format_active(["Zenith", "Horizon", "Wind"], NAMES) == "Zenith to end"
    assert stepwise.format_active(["Initial", "Wind"], NAMES) == "Initial, Wind"
    assert stepwise.format_active(["Zenith", "Horizon"], NAMES) == "Zenith to Horizon"


def test_plan_sets_changes_turns_off_and_on_again():
    events = stepwise.plan(NAMES, "Zenith", stepwise.parse_active("Zenith, Wind"), {"z": -1.0},
                           (("z", (("Wind", 2.0),)),))
    assert events == [("Zenith", "create", {"z": -1.0}, 0), ("Horizon", "off", None),
                      ("Wind", "create", {"z": 2.0}, 1)]
    assert stepwise.copy_name("Load", "Wind", 1) == "Load-from-Wind"
    events = stepwise.plan(NAMES, "Zenith", (), {"z": -1.0}, (("z", (("Horizon", 0.0),)),))
    assert events == [("Zenith", "create", {"z": -1.0}, 0), ("Horizon", "change", {"z": 0.0})]
    # a value in a step it is off in (an unticked row) is kept: it holds when it is on again
    events = stepwise.plan(NAMES, "Zenith", stepwise.parse_active("Zenith, Wind"), {"z": -1.0},
                           (("z", (("Horizon", 3.0),)),))
    assert events == [("Zenith", "create", {"z": -1.0}, 0), ("Horizon", "off", None),
                      ("Wind", "create", {"z": 3.0}, 1)]
    assert stepwise.plan(NAMES, "Zenith", stepwise.parse_active("Zenith"), {}, (("z", (("Wind", 1.0),)),)) == [
        ("Zenith", "create", {}, 0), ("Horizon", "off", None)]
    with pytest.raises(ValueError, match="not found"):
        stepwise.plan(NAMES, "Zenith", (), {}, (("z", (("Gust", 1.0),)),))
    with pytest.raises(ValueError, match="Initial"):
        stepwise.plan(NAMES, "Zenith", stepwise.parse_active("Initial to end"), {}, ())


def _model(*extra_nodes, edges=()):
    nodes = [("m", "model.model", {"name": "Stepwise"}),
             ("zenith", "model.step", {"name": "Zenith"}), ("horizon", "model.step", {"name": "Horizon"}),
             ("wind", "model.step", {"name": "Wind"}),
             ("s", "model.sketch", {}), ("p", "model.part", {}), ("i", "model.instance", {}),
             ("face", "model.region", {"kind": "Face", "value": "0"}),
             ("other", "model.region", {"kind": "Face", "value": "1"}), *extra_nodes]
    return nodes, [("m", "model", "zenith", "after"), ("zenith", "step", "horizon", "after"),
                   ("horizon", "step", "wind", "after"), ("m", "model", "s", "model"),
                   ("s", "sketch", "p", "profile"), ("p", "part", "i", "part"), ("p", "part", "face", "source"),
                   ("p", "part", "other", "source"), *edges]


def test_one_node_per_condition_across_steps_builds_in_cae(tmp_path):
    nodes, edges = _model(
        ("gravity", "model.condition", {"name": "Gravity", "kind": "Gravity", "x": "0; Horizon: -9810",
                                        "z": "-9810; Horizon: 0", "active": "Zenith to Horizon"}),
        ("windload", "model.condition", {"name": "Wind-load", "kind": "Pressure", "magnitude": 0.001,
                                     "active": "Wind", "amplitude": "0, 0; 1, 1"}),
        ("hold", "model.condition", {"name": "Hold", "kind": "Displacement / rotation", "u1": "0",
                                     "u2": "0; Wind: 0.5", "active": "Initial to Zenith, Wind",
                                     "amplitude": "Wind: 0, 0; 1, 1"}),
        ("heat", "model.condition", {"name": "Heat", "kind": "Temperature", "temperature": "20; Horizon: 200",
                                     "active": "Initial to Horizon"}),
        ("rub", "model.interaction", {"name": "Rub", "kind": "Frictional contact", "friction": "0.1; Wind: 0.4",
                                      "active": "Zenith, Wind"}),
        ("everything", "model.interaction", {"name": "All", "kind": "General contact",
                                             "friction": "0.2; Horizon: 0"}),
        edges=[("zenith", "step", "gravity", "step"), ("gravity", "condition", "windload", "step"),
               ("face", "region", "windload", "region"), ("zenith", "step", "hold", "step"),
               ("other", "region", "hold", "region"), ("zenith", "step", "heat", "step"),
               ("face", "region", "heat", "region"), ("zenith", "step", "rub", "step"),
               ("face", "region", "rub", "master"), ("other", "region", "rub", "slave"),
               ("zenith", "step", "everything", "step")])
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    # gravity: created in Zenith, turned in Horizon, off in Wind
    assert "m.Gravity(name='Gravity', createStepName='Zenith', comp1=0.0, comp2=0.0, comp3=-9810.0" in code
    assert "m.loads['Gravity'].setValuesInStep(stepName='Horizon', comp1=-9810.0, comp3=0.0)" in code
    assert "m.loads['Gravity'].deactivate(stepName='Wind')" in code
    # a load chained after the gravity one, but only in Wind (a later step than its Step input)
    assert "m.Pressure(name='Wind-load', createStepName='Wind'" in code and "amplitude='Wind-load-Amp'" in code
    # a BC from Initial, off in Horizon, on again in Wind as a fresh copy with its own amplitude
    assert "m.DisplacementBC(name='Hold', createStepName='Initial', region=target_region, u1=SET, u2=SET" in code
    assert "m.boundaryConditions['Hold'].deactivate(stepName='Horizon')" in code
    assert ("m.DisplacementBC(name='Hold-from-Wind', createStepName='Wind', region=target_region, u1=0.0, u2=0.5"
            in code) and "amplitude='Hold-Amp-Wind'" in code
    # temperature: initial value, then 200 in Horizon, back to the initial one in Wind
    assert "m.Temperature(name='Heat', createStepName='Initial'" in code and "magnitudes=(20.0,)" in code
    assert "m.predefinedFields['Heat'].setValuesInStep(stepName='Horizon', magnitudes=(200.0,))" in code
    assert "m.predefinedFields['Heat'].resetToInitial(stepName='Wind')" in code
    # pair contact: off in Horizon, on again in Wind with another friction (its own property)
    assert "m.SurfaceToSurfaceContactStd('Rub', 'Zenith'" in code and "interactionProperty='Rub-Property'" in code
    assert "m.interactions['Rub'].deactivate(stepName='Horizon')" in code
    assert "m.SurfaceToSurfaceContactStd('Rub-from-Wind', 'Wind'" in code
    assert "interactionProperty='Rub-Property-Wind'" in code and "table=((0.4,),)" in code
    # general contact: frictionless from Horizon
    assert ("m.interactions['All'].contactPropertyAssignments.changeValuesInStep(stepName='Horizon', index=0, "
            "value='All-Property-Horizon')") in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, ".Temperature") and calls(log, ".predefinedFields[Heat].resetToInitial")
    assert calls(log, ".boundaryConditions[Hold].deactivate") and calls(log, ".DisplacementBC")
    assert calls(log, ".interactions[Rub].deactivate")


def test_step_names_are_checked_against_the_whole_model():
    nodes, edges = _model(
        ("g", "model.condition", {"kind": "Gravity", "z": "-1; Gust: 0"}),
        ("late", "model.condition", {"kind": "Gravity", "active": "Wind"}),
        ("load", "model.condition", {"kind": "Pressure", "active": "Initial to end"}),
        edges=[("zenith", "step", "g", "step"), ("zenith", "step", "late", "step"),
               ("zenith", "step", "load", "step"), ("face", "region", "load", "region")])
    _, res, _ = evaluate(nodes, edges)
    assert res["g"]["status"] == "error" and "Gust" in res["g"]["message"] and "Wind" in res["g"]["message"]
    # Wind is after the Step it is wired to: known once the graph has run, and offered to the Per-step table
    assert res["late"]["status"] == "ok", res["late"]
    assert [o["value"] for o in res["late"]["hints"]["__steps"]] == ["Zenith", "Horizon", "Wind"]
    assert [o["value"] for o in res["late"]["hints"]["__active"]] == ["Wind"]
    assert res["load"]["status"] == "error" and "Initial" in res["load"]["message"]


def test_per_step_settings_validate_their_values():
    nodes, edges = _model(
        ("bad", "model.condition", {"kind": "Pressure", "magnitude": "1; Wind: lots"}),
        ("velocity", "model.condition", {"kind": "Initial velocity", "z": "-1; Wind: 0"}),
        ("tie", "model.interaction", {"kind": "Tie", "active": "Wind"}),
        edges=[("zenith", "step", "bad", "step"), ("face", "region", "bad", "region"),
               ("zenith", "step", "velocity", "step"), ("face", "region", "velocity", "region"),
               ("zenith", "step", "tie", "step"), ("face", "region", "tie", "master"),
               ("other", "region", "tie", "slave")])
    _, res, _ = evaluate(nodes, edges)
    assert res["bad"]["status"] == "error" and "Pressure in Wind must be a number" in res["bad"]["message"]
    assert res["velocity"]["status"] == "error" and "Initial" in res["velocity"]["message"]
    assert res["tie"]["status"] == "error" and "model constraints" in res["tie"]["message"]


def test_bolt_force_by_step_fixes_its_length_after_each_force(tmp_path):
    nodes, edges = _model(
        ("bolt", "model.condition", {"name": "Bolt", "kind": "Bolt load", "force": "1000; Wind: 2000"}),
        edges=[("zenith", "step", "bolt", "step")])
    nodes[6] = ("i", "model.instance", {})
    nodes[7] = ("face", "model.region", {"kind": "Face", "value": "0"})
    edges = [e for e in edges if e != ("p", "part", "face", "source")] + [("i", "instance", "face", "source"),
                                                                          ("face", "region", "bolt", "region")]
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "m.loads['Bolt'].setValuesInStep(stepName='Horizon', boltMethod=FIX_LENGTH)" in code
    assert "m.loads['Bolt'].setValuesInStep(stepName='Wind', magnitude=2000.0, boltMethod=APPLY_FORCE)" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout


def test_a_per_step_table_from_another_node(tmp_path):
    table = ('[["Step", "Active", "Z component", "Amplitude"], ["Zenith", "Yes", -9810, ""], '
             '["Horizon", "No", "", ""], ["Wind", "Yes", -4000, "0, 0; 1, 1"]]')
    nodes, edges = _model(
        ("t", "input.value", {"type": "List / matrix", "value": table}),
        ("g", "model.condition", {"name": "Weight", "kind": "Gravity"}),
        ("f", "input.value", {"type": "Text", "value": "Step, Active, Friction; Zenith, Yes, 0.1; Horizon, No, ; "
                                                         "Wind, Yes, 0.4"}),
        ("rub", "model.interaction", {"name": "Rub", "kind": "Frictional contact"}),
        edges=[("zenith", "step", "g", "step"), ("t", "value", "g", "steps"), ("zenith", "step", "rub", "step"),
               ("face", "region", "rub", "master"), ("other", "region", "rub", "slave"), ("f", "value", "rub", "steps")])
    out, bad = script(nodes, edges)
    assert not bad, bad
    code = out["script"]
    assert "m.Gravity(name='Weight', createStepName='Zenith', comp1=0.0, comp2=0.0, comp3=-9810.0" in code
    assert "m.loads['Weight'].deactivate(stepName='Horizon')" in code
    assert ("m.Gravity(name='Weight-from-Wind', createStepName='Wind', comp1=0.0, comp2=0.0, comp3=-4000.0" in code
            and "amplitude='Weight-Amp-Wind'" in code)
    assert "m.SurfaceToSurfaceContactStd('Rub-from-Wind', 'Wind'" in code and "table=((0.4,),)" in code
    _, res, _ = evaluate(nodes, edges)
    cells = res["g"]["hints"]["__cells"][0]["value"]
    assert cells["z"] == {"Zenith": "-9810", "Wind": "-4000"} and cells["amplitude"]["Wind"] == "0, 0; 1, 1"
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout

    bad_table = '[["Step", "Gust"], ["Zenith", 1]]'
    nodes = [(i, t, dict(p, value=bad_table)) if i == "t" else (i, t, p) for i, t, p in nodes]
    _, res, _ = evaluate(nodes, edges)
    assert res["g"]["status"] == "error" and "no setting 'gust'" in res["g"]["message"]


def test_unticking_a_step_keeps_the_table():
    # gravity with its own values in Horizon, then Horizon unticked: still fine, and still listed for the table
    nodes, edges = _model(
        ("g", "model.condition", {"kind": "Gravity", "x": "0; Horizon: -9810", "z": "-9810; Horizon: 0",
                                  "active": "Zenith, Wind"}),
        ("typo", "model.condition", {"kind": "Gravity", "z": "-1; Gust: 0"}),
        edges=[("zenith", "step", "g", "step"), ("zenith", "step", "typo", "step")])
    _, res, _ = evaluate(nodes, edges)
    assert res["g"]["status"] == "ok", res["g"]
    assert [o["value"] for o in res["g"]["hints"]["__active"]] == ["Zenith", "Wind"]
    # a step name that doesn't exist is an error, but the table keeps its rows to put it right
    assert res["typo"]["status"] == "error" and "Gust" in res["typo"]["message"]
    assert [o["value"] for o in res["typo"]["hints"]["__steps"]] == ["Zenith", "Horizon", "Wind"]
