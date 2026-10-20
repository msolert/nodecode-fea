"""Amplitude: every Abaqus amplitude type a load or boundary condition can follow, built in (fake) CAE, plotted,
checked and read back from a CAE model."""

import math

import numpy as np
import pytest

from nodecode_fea.cae_import import graph_from_dump, payload
from nodecode_fea.engine import Engine
from nodecode_fea.values import Amplitude

from .test_cae_import import by_name, dump, wired  # noqa: F401 (dump: a fixture)
from .test_checks import lint
from .test_model import calls, evaluate, run_in_fake_cae, script

BASE = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
        ("face", "model.region", {"kind": "Face", "value": "0"}), ("step", "model.step", {"name": "Loading"}),
        ("i", "model.instance", {})]
WIRES = [("s", "sketch", "p", "profile"), ("p", "part", "face", "source"), ("step", "step", "push", "step"),
         ("face", "region", "push", "region"), ("p", "part", "i", "part"), ("amp", "amplitude", "push", "amplitude")]
PUSH = ("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 2})

TYPES = {
    "Tabular": ({"points": "0, 0; 0.5, 1; 1, 0.2", "smoothing": "0.1"},
                "m.TabularAmplitude(name='Push-Amp', timeSpan=STEP, smooth=0.1, data=((0.0, 0.0), (0.5, 1.0), "
                "(1.0, 0.2),))"),
    "Smooth step": ({"points": "0, 0; 1, 1", "time_span": "Total time"},
                    "m.SmoothStepAmplitude(name='Push-Amp', timeSpan=TOTAL, data=((0.0, 0.0), (1.0, 1.0),))"),
    "Equally spaced": ({"values": "0, 1, 0.5", "interval": 0.5, "begin": 0.1},
                       "m.EquallySpacedAmplitude(name='Push-Amp', timeSpan=STEP, smooth=SOLVER_DEFAULT, "
                       "fixedInterval=0.5, begin=0.1, data=(0.0, 1.0, 0.5,))"),
    "Periodic": ({"frequency": 2.0, "terms": "0, 1; 0.5, 0", "initial": 1, "start": 0.2},
                 "m.PeriodicAmplitude(name='Push-Amp', timeSpan=STEP, frequency=2.0, start=0.2, a_0=1.0, "
                 "data=((0.0, 1.0), (0.5, 0.0),))"),
    "Modulated": ({"initial": 1, "magnitude": 2, "start": 0, "frequency1": 3, "frequency2": 30},
                  "m.ModulatedAmplitude(name='Push-Amp', timeSpan=STEP, initial=1.0, magnitude=2.0, start=0.0, "
                  "frequency1=3.0, frequency2=30.0)"),
    "Exponential decay": ({"initial": 0, "magnitude": 1, "start": 0.1, "decay": 0.2},
                          "m.DecayAmplitude(name='Push-Amp', timeSpan=STEP, initial=0.0, maximum=1.0, start=0.1, "
                          "decayTime=0.2)"),
    "Solution dependent": ({"factor": 1, "minimum": 0.5, "maximum": 10},
                           "m.SolutionDependentAmplitude(name='Push-Amp', timeSpan=STEP, initial=1.0, minimum=0.5, "
                           "maximum=10.0)"),
    "User": ({"variables": 2}, "m.UserAmplitude(name='Push-Amp', timeSpan=STEP, numVariables=2)"),
}


@pytest.mark.parametrize("kind", list(TYPES))
def test_every_amplitude_type_builds_in_cae(kind, tmp_path):
    settings, made = TYPES[kind]
    out, bad = script(BASE + [("amp", "model.amplitude", {"kind": kind, **settings}), PUSH], WIRES)
    assert not bad, bad
    code = out["script"]
    assert made in code, code
    assert "magnitude=2.0, amplitude='Push-Amp'" in code
    log, stdout = run_in_fake_cae(code, tmp_path)
    assert "failed" not in stdout, stdout
    assert calls(log, "." + made.split("(")[0][2:])


def test_the_curve_follows_abaqus_formulas():
    def curve(kind, **settings):
        _, res, inputs = evaluate([("amp", "model.amplitude", {"kind": kind, "until": 1.0, **settings})], [])
        assert res["amp"]["status"] == "ok", res["amp"]
        c = inputs.outputs["amp"]["curve"]
        return lambda t: float(np.interp(t, c.x, c.y))

    a = curve("Periodic", frequency=2 * math.pi, terms="0, 1", initial=0.5, start=0.25)
    assert a(0.1) == pytest.approx(0.5) and a(0.5) == pytest.approx(0.5 + math.sin(2 * math.pi * 0.25), abs=1e-3)
    a = curve("Exponential decay", initial=0, magnitude=1, start=0, decay=0.5)
    assert a(0.5) == pytest.approx(math.exp(-1), abs=1e-3)
    a = curve("Modulated", initial=0, magnitude=1, start=0, frequency1=1, frequency2=2)
    assert a(0.75) == pytest.approx(math.sin(0.75) * math.sin(1.5), abs=1e-3)
    a = curve("Smooth step", points="0, 0; 1, 1")
    assert a(0.5) == pytest.approx(0.5) and a(0.1) == pytest.approx(0.1 ** 3 * (10 - 1.5 + 0.06), abs=1e-3)
    a = curve("Equally spaced", values="0, 1, 0", interval=0.5, begin=0)
    assert a(0.25) == pytest.approx(0.5) and a(0.5) == pytest.approx(1)
    _, res, inputs = evaluate([("amp", "model.amplitude", {"kind": "User"})], [])
    assert res["amp"]["status"] == "ok" and "curve" not in inputs.outputs["amp"]


def test_a_curve_or_matrix_gives_the_points_and_mistakes_are_named():
    nodes = BASE + [("pts", "input.value", {"type": "List / matrix", "value": "[[0, 0], [2, 1]]"}),
                    ("amp", "model.amplitude", {"kind": "Smooth step"}), PUSH]
    _, res, inputs = evaluate(nodes, WIRES + [("pts", "value", "amp", "points")])
    assert res["push"]["status"] == "ok", res["push"]
    cond = inputs.outputs["push"]["condition"]
    assert cond.amplitude == Amplitude("Smooth step", ((0.0, 0.0), (2.0, 1.0)))
    assert "over time" in cond.summary()
    for settings, message in (({"kind": "Tabular", "points": "1, 0; 0, 1"}, "never go back"),
                              ({"kind": "Periodic", "terms": "1, 2, 3"}, "2 numbers per row"),
                              ({"kind": "Periodic", "frequency": 0}, "greater than 0"),
                              ({"kind": "Equally spaced", "values": "1"}, "at least two"),
                              ({"kind": "Tabular", "smoothing": "0.9"}, "between 0 and 0.5"),
                              ({"kind": "Solution dependent", "factor": 0.01}, "Minimum ≤ Initial factor")):
        _, res, _ = evaluate([("amp", "model.amplitude", settings)], [])
        assert res["amp"]["status"] == "error" and message in res["amp"]["message"], (settings, res["amp"])


def test_one_amplitude_drives_several_conditions_each_with_its_own_copy():
    nodes = BASE + [("amp", "model.amplitude", {"kind": "Exponential decay"}), PUSH,
                    ("pull", "model.condition", {"name": "Pull", "kind": "Pressure", "magnitude": 1})]
    out, bad = script(nodes, WIRES + [("push", "condition", "pull", "step"), ("face", "region", "pull", "region"),
                                      ("amp", "amplitude", "pull", "amplitude")])
    assert not bad, bad
    assert "m.DecayAmplitude(name='Push-Amp'" in out["script"] and "m.DecayAmplitude(name='Pull-Amp'" in out["script"]


def test_user_amplitude_asks_for_the_jobs_subroutine(tmp_path):
    nodes = BASE + [("amp", "model.amplitude", {"kind": "User"}), PUSH,
                    ("j", "model.job", {"directory": str(tmp_path), "action": "Write input only"})]
    edges = WIRES + [("push", "condition", "j", "analysis")]
    assert any("UAMP" in w for w in lint(nodes, edges)["push"])
    sub = tmp_path / "uamp.f"
    sub.write_text("      SUBROUTINE UAMP()\n      END\n")
    nodes[-1] = ("j", "model.job", {"directory": str(tmp_path), "action": "Write input only", "user": str(sub)})
    out, bad = script(nodes, edges)
    assert not bad, bad
    assert f"userSubroutine={str(sub)!r}" in out["script"]
    assert not any("UAMP" in w for w in lint(nodes, edges).get("push", []))
    nodes[-1] = ("j", "model.job", {"directory": str(tmp_path), "user": str(tmp_path / "missing.f")})
    _, res, _ = evaluate(nodes, edges)
    assert res["j"]["status"] == "error" and "no file" in res["j"]["message"]


def test_a_cae_models_amplitudes_come_back_as_amplitude_nodes(dump):
    m = dump["model"]
    ramp = next(a for a in m["amplitudes"] if a["name"] == "Ramp2")
    ramp.clear()
    ramp.update(name="Ramp2", type="PeriodicAmplitude", timeSpan="TOTAL", data=[[0.0, 1.0]], frequency=3.0,
                start=0.0, a_0=0.5)
    g = graph_from_dump(dump)["graph"]
    amp = by_name(g, "model.amplitude")["Ramp2"]
    assert amp["data"]["params"] == {"kind": "Periodic", "time_span": "Total time", "terms": "0, 1",
                                     "frequency": 3.0, "start": 0.0, "initial": 0.5}
    push = by_name(g, "model.condition")["Push"]
    assert wired(g, push["id"], "amplitude") == [amp["id"]] and "amplitude" not in push["data"]["params"]
    res, _ = Engine().evaluate(payload(g))
    assert res[push["id"]]["status"] == "ok", res[push["id"]]
