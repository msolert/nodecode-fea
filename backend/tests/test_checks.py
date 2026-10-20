"""Model checks: warnings on the nodes that cause what Abaqus would refuse (checks.lint), and Abaqus' data check
messages mapped onto nodes."""

from nodecode_fea import checks, jobs
from nodecode_fea.engine import Engine

from .test_engine import graph

BOX = [("m", "model.model", {"name": "M"}), ("s", "model.sketch", {}),
       ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})]
BOX_EDGES = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile")]


def lint(nodes, edges):
    g = graph(nodes, edges)
    res, inputs = Engine().evaluate(g)
    bad = {k: v["message"] for k, v in res.items() if v["status"] != "ok"}
    assert not bad, bad
    return checks.lint(g, res, inputs)


def meshed(section=None):
    """Box -> Material & Section -> Mesh -> Instance, with a step."""
    nodes = BOX + [("sec", "model.section", section or {"density": 7.85e-9}), ("mesh", "model.mesh", {}),
                   ("i", "model.instance", {}), ("st", "model.step", {})]
    edges = BOX_EDGES + [("p", "part", "sec", "target"), ("sec", "part", "mesh", "target"),
                         ("mesh", "part", "i", "part"), ("m", "model", "st", "after")]
    return nodes, edges


def fixed(region_source="i", at="0, 25, 5"):
    return ([("at", "input.value", {"type": "Text", "value": at}),
             ("face", "model.region", {"method": "Find at", "kind": "Face"}),
             ("bc", "model.condition", {"name": "Clamp", "kind": "Fixed"})],
            [(region_source, "instance" if region_source == "i" else "part", "face", "source"),
             ("at", "value", "face", "points"), ("st", "step", "bc", "step"), ("face", "region", "bc", "region")])


def test_a_part_without_section_or_mesh_is_flagged_on_its_instance():
    nodes = BOX + [("i", "model.instance", {}), ("st", "model.step", {})]
    edges = BOX_EDGES + [("p", "part", "i", "part"), ("m", "model", "st", "after")]
    warnings = lint(nodes, edges)["i"]
    assert any("no Material & Section" in w for w in warnings)
    assert any("isn't meshed" in w for w in warnings)
    # no step or job: still building, no analysis yet
    assert "i" not in lint(nodes[:-1], edges[:-1])


def test_nothing_holds_an_instance_without_a_boundary_condition():
    nodes, edges = meshed()
    assert any("Nothing holds Part-1-1" in w for w in lint(nodes, edges)["i"])
    n, e = fixed()
    assert "i" not in lint(nodes + n, edges + e)
    # a Region made on the part holds each of its instances
    n, e = fixed("mesh")
    assert "i" not in lint(nodes + n, edges + e)
    # free-free vibration is fine
    nodes[-1] = ("st", "model.step", {"kind": "Frequency"})
    assert not any("Nothing holds" in w for w in lint(nodes, edges).get("i", []))


def test_what_is_tied_to_a_held_instance_is_held():
    nodes, edges = meshed()
    n, e = fixed()
    nodes += n + [("j", "model.instance", {"name": "Other", "z": 10})]
    edges += e + [("mesh", "part", "j", "part")]
    warnings = lint(nodes, edges)
    assert any("Nothing holds Other" in w for w in warnings["j"]) and "i" not in warnings
    nodes += [("top", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, 1"}),
              ("bot", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, -1"}),
              ("tie", "model.interaction", {"name": "Glue"})]
    edges += [("i", "instance", "top", "source"), ("j", "instance", "bot", "source"),
              ("top", "region", "tie", "master"), ("bot", "region", "tie", "slave")]
    assert "j" not in lint(nodes, edges)


def test_density_and_units():
    nodes, edges = meshed({"density": 0})
    n, e = fixed()
    nodes, edges = nodes + n, edges + e
    nodes[-4] = ("st", "model.step", {"kind": "Dynamic implicit"})
    assert any("has no density" in w for w in lint(nodes, edges)["sec"])
    # E in MPa with density in kg/m³: not one unit system
    nodes[3] = ("sec", "model.section", {"young": 210000, "density": 7850})
    assert any("aren't in one unit system" in w for w in lint(nodes, edges)["sec"])
    nodes[3] = ("sec", "model.section", {"young": 210000, "density": 7.85e-9})
    assert "sec" not in lint(nodes, edges)
    # gravity in m/s² with materials in mm, N, MPa
    g = [("g", "model.condition", {"name": "Weight", "kind": "Gravity", "z": -9.81})]
    assert any("gravity is 9810" in w for w in lint(nodes + g, edges + [("bc", "condition", "g", "step")])["g"])
    g = [("g", "model.condition", {"name": "Weight", "kind": "Gravity", "z": -9810})]
    assert "g" not in lint(nodes + g, edges + [("bc", "condition", "g", "step")])


def test_a_job_without_step_or_instances():
    nodes = [("m", "model.model", {"name": "M"}), ("job", "model.job", {"action": "Write input only"})]
    warnings = lint(nodes, [("m", "model", "job", "analysis")])["job"]
    assert any("no Step" in w for w in warnings) and any("empty assembly" in w for w in warnings)


DAT = """
 ***ERROR: 20 elements have missing property definitions. The elements have
           been identified in element set ErrElemMissingSection.
 ***NOTE: DUE TO AN INPUT ERROR THE ANALYSIS PRE-PROCESSOR HAS BEEN UNABLE TO

 ***ERROR: LOAD TYPE GRAV CANNOT BE APPLIED TO THE WHOLE MODEL. EITHER THE
           MATERIAL DENSITY HAS NOT BEEN SPECIFIED OR THE MODEL DOES NOT
           CONTAIN ANY ELEMENTS THAT SUPPORT THIS LOAD TYPE.

 ***WARNING: IN THIS TYPE OF ANALYSIS ONE WOULD NORMALLY EXPECT THAT A DENSITY
             FOR MATERIAL STEEL WOULD BE DEFINED.

 ***ERROR: THERE ARE NO SOURCES OF INERTIA IN THIS MODEL.
"""


def test_data_check_messages_go_on_the_nodes_they_name(tmp_path):
    """Abaqus' data check of a model (messages as Abaqus 2025 wrote them): the density warning names the material,
    the gravity error the load type, the missing-section error an element set; the rest stays on the Job."""
    nodes = BOX + [("q", "model.part", {"name": "Plain", "body": "Solid", "method": "Extrude", "depth": 5}),
                   ("bare", "model.instance", {"name": "Bare"}),
                   ("sec", "model.section", {"material": "Steel"}), ("mesh", "model.mesh", {}),
                   ("i", "model.instance", {}), ("st", "model.step", {"kind": "Dynamic implicit"}),
                   ("g", "model.condition", {"name": "Weight", "kind": "Gravity", "z": -9810}),
                   ("job", "model.job", {"name": "Bad", "directory": str(tmp_path), "action": "Write input only"})]
    edges = BOX_EDGES + [("s", "sketch", "q", "profile"), ("q", "part", "bare", "part"), ("p", "part", "sec", "target"),
                         ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
                         ("st", "step", "g", "step"), ("g", "condition", "job", "analysis")]
    (tmp_path / "Bad.inp").write_text("*Heading\n")
    (tmp_path / "Bad-Check.dat").write_text(DAT)
    warnings = lint(nodes, edges)
    assert any("Data check warning" in w and "MATERIAL STEEL" in w for w in warnings["sec"])
    assert any("LOAD TYPE GRAV" in w for w in warnings["g"])
    assert any("missing property definitions" in w for w in warnings["bare"])
    assert any("NO SOURCES OF INERTIA" in w for w in warnings["job"])
    assert jobs.runner.check_state(jobs.JobSpec("Bad", str(tmp_path))) == "Checked: 3 errors, 1 warning"
    # the model changed after the check (CAE wrote the input again): the messages say so
    import os
    import time
    later = time.time() + 10
    os.utime(tmp_path / "Bad.inp", (later, later))
    assert any("older model" in w for w in lint(nodes, edges)["g"])


def test_an_imported_mesh_is_meshed_and_an_imported_model_has_its_own_steps(tmp_path):
    (tmp_path / "mesh.inp").write_text("*Heading\n")
    nodes = [("m", "model.model", {"name": "M"}),
             ("p", "model.part", {"method": "Import CAD", "body": "Solid", "file": str(tmp_path / "mesh.inp")}),
             ("sec", "model.section", {}), ("i", "model.instance", {}), ("st", "model.step", {})]
    edges = [("m", "model", "p", "model"), ("p", "part", "sec", "target"), ("sec", "part", "i", "part"),
             ("m", "model", "st", "after")]
    assert not any("isn't meshed" in w for w in lint(nodes, edges).get("i", []))
    # Mesh refuses it: it is a mesh already
    g = graph(nodes + [("mesh", "model.mesh", {})], edges + [("p", "part", "mesh", "target")])
    res, _ = Engine().evaluate(g)
    assert res["mesh"]["status"] == "error" and "already a mesh" in res["mesh"]["message"]
    # a model from an input file: no "no Step" / "empty assembly" for its Job
    nodes = [("m", "model.model", {"name": "W", "file": str(tmp_path / "mesh.inp")}),
             ("job", "model.job", {"action": "Write input only"})]
    assert "job" not in lint(nodes, [("m", "model", "job", "analysis")])


def test_engineering_checks():
    # a 100 x 50 plate 10 thick meshed at 10: one C3D8R through the thickness
    nodes, edges = meshed()
    nodes[2] = ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})
    nodes[4] = ("mesh", "model.mesh", {"size": 10})
    n, e = fixed()
    assert any("across the 10 thickness" in w for w in lint(nodes + n, edges + e)["mesh"])
    nodes[4] = ("mesh", "model.mesh", {"size": 2.5})
    assert "mesh" not in lint(nodes + n, edges + e)
    nodes[4] = ("mesh", "model.mesh", {"size": 2.5, "shape": "Tet", "element": "C3D4"})
    assert any("C3D4" in w for w in lint(nodes + n, edges + e)["mesh"])
    nodes[4] = ("mesh", "model.mesh", {"size": 2.5})
    nodes, edges = nodes + n, edges + e
    # a force on several vertices; a pressure in Pa on a model in MPa; an amplitude shorter than its step
    extra = [("vpts", "input.value", {"type": "Text", "value": "0, 0, 0; 100, 0, 0"}),
             ("verts", "model.region", {"method": "Find at", "kind": "Vertex"}),
             ("cf", "model.condition", {"name": "Pull", "kind": "Concentrated force", "x": 1}),
             ("pr", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 1e6,
                                        "amplitude": "0, 0; 0.5, 1"})]
    wires = [("i", "instance", "verts", "source"), ("vpts", "value", "verts", "points"), ("bc", "condition", "cf", "step"),
             ("verts", "region", "cf", "region"), ("cf", "condition", "pr", "step"), ("face", "region", "pr", "region")]
    warnings = lint(nodes + extra, edges + wires)
    assert any("each of the 2 vertices" in w for w in warnings["cf"])
    assert any("of Young's modulus" in w for w in warnings["pr"]) and any("amplitude ends at time 0.5" in w
                                                                           for w in warnings["pr"])
    # two boundary conditions on the same faces
    both = [("bc2", "model.condition", {"name": "Clamp-2", "kind": "Fixed"})]
    warnings = lint(nodes + both, edges + [("st", "step", "bc2", "step"), ("face", "region", "bc2", "region")])
    assert any("Clamp-2 also holds" in w for w in warnings["bc"]) and any("Clamp also holds" in w
                                                                            for w in warnings["bc2"])

def test_a_failed_run_puts_its_errors_on_the_nodes(tmp_path):
    nodes, edges = meshed()
    nodes += [("job", "model.job", {"name": "Run", "directory": str(tmp_path), "action": "Write input only"})]
    edges += [("st", "step", "job", "analysis")]
    (tmp_path / "Run.inp").write_text("*Heading\n")
    (tmp_path / "Run.log").write_text("Abaqus/Analysis exited with errors\n")
    (tmp_path / "Run.msg").write_text(
        " ***WARNING: SOLVER PROBLEM. ZERO PIVOT WHEN PROCESSING ELEMENT 2 INSTANCE PART-1-1\n\n"
        " ***ERROR: TOO MANY ATTEMPTS MADE FOR THIS INCREMENT\n\n")
    warnings = lint(nodes, edges)
    assert any(w.startswith("Run error: TOO MANY ATTEMPTS") for w in warnings["job"])
    assert not any("ZERO PIVOT" in w for ws in warnings.values() for w in ws)  # run warnings are left out


def test_explicit_checks():
    nodes, edges = meshed({"density": 0})
    nodes[-1] = ("st", "model.step", {"kind": "Dynamic explicit", "time": 0.01})
    warnings = lint(nodes, edges)
    # a free body is fine in Explicit (it just flies), but it needs mass
    assert not any("Nothing holds" in w for w in warnings.get("i", []))
    assert any("has no density" in w for w in warnings["sec"])
    nodes[3] = ("sec", "model.section", {"density": 7.85e-9})
    nodes[4] = ("mesh", "model.mesh", {"element": "C3D20R"})
    assert any("C3D20R isn't available in Abaqus/Explicit" in w for w in lint(nodes, edges)["mesh"])
    nodes[4] = ("mesh", "model.mesh", {})
    # a load without amplitude is applied at once in Explicit
    n, e = fixed()
    push = [("pr", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 5})]
    wires = [("bc", "condition", "pr", "step"), ("face", "region", "pr", "region")]
    assert any("applied at once" in w for w in lint(nodes + n + push, edges + e + wires)["pr"])
    push[0][2]["amplitude"] = "0, 0; 0.01, 1"
    assert "pr" not in lint(nodes + n + push, edges + e + wires)


def test_incompressible_rubber_needs_hybrid_elements():
    nodes, edges = meshed({"behavior": "Neo-Hooke", "c10": 0.5})
    assert any("hybrid elements" in w for w in lint(nodes, edges).get("mesh", []))
    nodes[-3] = ("mesh", "model.mesh", {"element": "C3D8H"})
    assert not lint(nodes, edges).get("mesh")


def test_two_whole_part_meshes_on_one_part_are_flagged():
    # side by side on the part: one CAE part, so the later seed replaces the earlier
    nodes = BOX + [("a", "model.mesh", {"size": 5}), ("b", "model.mesh", {"size": 2})]
    edges = BOX_EDGES + [("p", "part", "a", "target"), ("p", "part", "b", "target")]
    warnings = lint(nodes, edges)
    assert all(any("seed the whole of Part-1" in w for w in warnings.get(n, [])) for n in ("a", "b")), warnings
    # a whole-part seed then a local one (an Edge Region, or a Face Region's own edges) is how to refine
    for region, mesh in (({"kind": "Edge", "value": "0"}, {"size": 1}),
                         ({"kind": "Face", "value": "0"}, {"size": 1, "seed_on": "Its edges"})):
        nodes = BOX + [("a", "model.mesh", {"size": 5}), ("r", "model.region", region), ("b", "model.mesh", mesh)]
        edges = BOX_EDGES + [("p", "part", "a", "target"), ("a", "part", "r", "source"), ("r", "region", "b", "target")]
        assert not any("seed the whole" in w for ws in lint(nodes, edges).values() for w in ws)
    # a list of sizes makes separate parts on purpose: no warning
    nodes = BOX + [("sizes", "input.value", {"type": "List / matrix", "value": "10, 5"}), ("a", "model.mesh", {})]
    edges = BOX_EDGES + [("p", "part", "a", "target"), ("sizes", "value", "a", "param:size")]
    assert not any("seed the whole" in w for ws in lint(nodes, edges).values() for w in ws)


def test_a_sketch_drawing_the_same_item_twice_is_flagged():
    """A ring of holes at 0..360° inclusive draws the hole at 0° again at 360° (CAE: 'Shell extrude feature failed')."""
    nodes = [("a", "model.sketch", {"name": "A", "shape": "Circle", "x": 50, "radius": 10}),
             ("b", "model.sketch", {"name": "B", "shape": "Circle", "x": 50, "y": -1.2e-14, "radius": 10}),
             ("c", "model.sketch", {"name": "C", "shape": "Circle", "x": -50, "radius": 10}),
             ("o", "model.sketch", {"name": "Plate", "shape": "Circle", "radius": 100})]
    edges = [("a", "sketch", "o", "add"), ("b", "sketch", "o", "add")]
    warnings = lint(nodes, edges)
    assert any("Plate draws the same circle (centre 50, 0, radius 10) twice (items 1 and 2)" in w
               for w in warnings["o"])
    assert "o" not in lint(nodes, [("a", "sketch", "o", "add"), ("c", "sketch", "o", "add")])
