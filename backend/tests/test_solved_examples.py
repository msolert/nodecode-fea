"""The editor's "Solved in Abaqus" examples are the models test_real_abaqus.py solves and checks against theory.

They are built by the same builders, written to frontend/src/solvedExamples.json (laid out, the Job run on
request in its default folder, each answer node named with the value it should give), so an example can't drift
from what was verified. Regenerate after changing a builder:

    cd backend && python -m tests.test_solved_examples
"""
import json
from pathlib import Path

from nodecode_fea import checks
from nodecode_fea.nodes import REGISTRY

from .test_model import evaluate
from .test_real_abaqus import (buckled_column, flat_bar, heated_bar, rigid_press, riks_bar, sprung_mass, tied_bars,
                               turned_pull)

OUT = Path(__file__).resolve().parents[2] / "frontend" / "src" / "solvedExamples.json"
HERE = Path("")

# title, builder, {answer node: its name in the editor (what it should read)}
SOLVED = [
    ("Plane stress bar pulled by 10 MPa: the end moves σL/E, the reactions balance the pull",
     lambda: flat_bar(HERE, "2D planar"),
     {"r": "End U1 (σL/E = 0.005)", "total": "Total RF1 (10 × 20 × 2 = -400)"}),
    ("Axisymmetric bar pulled by 10 MPa: the same stretch, the reaction over the whole round",
     lambda: flat_bar(HERE, "Axisymmetric"),
     {"r": "End U2 (σL/E = 0.005)", "total": "Total RF2 (10 × π 10² = -3141.6)"}),
    ("Analytical rigid press: a rigid line squeezes a 2D block through frictionless contact",
     lambda: rigid_press(HERE),
     {"r": "Tool reaction RF2 (E ε A = -4000)", "floor_rf": "Floor reaction per node (400)"}),
    ("Buckling: a clamped column's first critical load against Euler's",
     lambda: buckled_column(HERE),
     {"r": "First buckling load (Euler π²EI/(2L)² = 3875.8)"}),
    ("Static Riks: the bar followed by arc length up to load factor 2",
     lambda: riks_bar(HERE),
     {"r": "End U1 at load factor 2 (about 2 × -100/210000 = -9.5e-4)"}),
    ("Heat transfer: a heat flux into a bar's end, held at 0° at the other",
     lambda: heated_bar(HERE, "flux"),
     {"r": "Hottest temperature (qL/k = 1000)"}),
    ("Heat transfer: convection from a bar's end, held at 100° at the other",
     lambda: heated_bar(HERE, "film"),
     {"r": "Coolest temperature (h = k/L: 50)"}),
    ("Thermal expansion: a coupled temperature-displacement step, held by X, Y and Z symmetry",
     lambda: heated_bar(HERE, "coupled"),
     {"r": "Growth U1 (αΔTL = 0.1)"}),
    ("Local coordinate system: a force along a csys turned 90° about z pulls along global y",
     lambda: turned_pull(HERE),
     {"r": "Total RF2 (-1000)"}),
    ("Rigid body with a point mass on a bushing: six natural frequencies at √(k/m)/2π",
     lambda: sprung_mass(HERE),
     {"lo": "Lowest frequency (√1000/2π = 5.033 Hz)", "hi": "Highest frequency (also 5.033 Hz)"}),
    ("Equation: one bar's end corner made to move opposite to another's",
     lambda: tied_bars(HERE),
     {"mn": "Bar corner U1 (minus the twin's)", "mx": "Twin corner U1 (minus the bar's)"}),
]


def _example(build, answers):
    nodes, edges = build()
    params = {}
    for i, t, p in nodes:
        if t == "model.job":
            p = {k: v for k, v in p.items() if k not in ("directory", "action")}
        params[i] = p
    # columns by the longest wire path from a source, stacked in the builder's order with room for each node's
    # settings (a Section is tall)
    depth = {i: 0 for i, _, _ in nodes}
    for _ in nodes:
        for s, _, t, _ in edges:
            depth[t] = max(depth[t], depth[s] + 1)
    tops: dict[int, int] = {}
    out = []
    for i, t, _ in nodes:
        col = depth[i]
        d = REGISTRY[t]
        y = tops.get(col, 0)
        tops[col] = y + 160 + 17 * (len(d.inputs) + len(d.params) + len(d.outputs))
        data = {"type": t, "params": params[i]}
        if i in answers:
            data["name"] = answers[i]
        elif t == "input.value":
            data["name"] = "Point (x, y, z)"
        out.append({"id": i, "type": "op", "position": {"x": col * 320, "y": y}, "data": data})
    return {"nodes": out,
            "edges": [{"id": f"{s}.{sh}-{t}.{th}", "source": s, "sourceHandle": sh, "target": t, "targetHandle": th}
                      for s, sh, t, th in edges]}


def examples():
    return [{"title": title, "graph": _example(build, answers)} for title, build, answers in SOLVED]


def test_the_editors_solved_examples_are_the_solved_models():
    assert json.loads(OUT.read_text(encoding="utf-8")) == examples(), \
        "frontend/src/solvedExamples.json is stale: run `python -m tests.test_solved_examples` in backend/"


def test_every_solved_example_builds_its_model_cleanly():
    for title, build, answers in SOLVED:
        nodes, edges = build()
        g, res, inputs = evaluate(nodes, edges)
        bad = {k: v["message"] for k, v in res.items()
               if v["status"] != "ok" and next(t for i, t, _ in nodes if i == k).startswith(("model.", "input."))}
        assert not bad, (title, bad)
        assert not checks.lint(g, res, inputs), (title, checks.lint(g, res, inputs))
        assert set(answers) <= {i for i, _, _ in nodes}, title


if __name__ == "__main__":
    OUT.write_text(json.dumps(examples(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(SOLVED)} examples to {OUT}")
