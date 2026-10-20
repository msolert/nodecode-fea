"""Generated CAE code run in real Abaqus (opt-in: the fakes only record calls and can't tell what CAE refuses).

Set NOCODE_REAL_ABAQUS to the Abaqus launcher (e.g. C:/SIMULIA/Commands/abaqus.bat) to run these; they are skipped
otherwise. Every modelling case runs in one `abaqus cae noGUI` session (one licence checkout): a driver starts each
case on a fresh Mdb and ledger, runs the script the editor would send, then evaluates the case's checks inside CAE.
The pipeline test then solves a model built from a graph and reads its ODB back through the real ODB reader.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from nodecode_fea import cae_mesh, cae_queries, jobs
from nodecode_fea.cae_export import build_script
from nodecode_fea.cae_import import graph_from_dump, payload
from nodecode_fea.cae_reader import read_cae
from nodecode_fea.engine import Engine
from nodecode_fea.sources.abaqus import Worker

from .test_engine import graph
from .test_inp_import import DATA, recorded_dump

LAUNCHER = os.environ.get("NOCODE_REAL_ABAQUS", "")
pytestmark = pytest.mark.skipif(not LAUNCHER, reason="set NOCODE_REAL_ABAQUS to the Abaqus launcher to run")

DRIVER = r'''
import json, sys, io, traceback
import visualization, part  # what a live CAE session has loaded; generated code imports the rest itself
from abaqus import *
from abaqusConstants import *

# CAD files for the import cases, written by CAE itself
_m = mdb.Model(name='CadSource')
_s = _m.ConstrainedSketch(name='s', sheetSize=200)
_s.rectangle(point1=(0, 0), point2=(10, 10))
_p = _m.Part(name='Box', dimensionality=THREE_D, type=DEFORMABLE_BODY)
_p.BaseSolidExtrude(sketch=_s, depth=5)
_p.writeStepFile(fileName='box.stp')
_p.writeAcisFile(fileName='box.sat')

cases = json.load(open('cases.json'))
report = {}
for case in cases:
    Mdb()
    sys.modules.pop('nocode_ledger', None)
    out, old = io.StringIO(), sys.stdout
    sys.stdout = out
    error, checks, scope = None, [], {'__name__': '__main__'}
    try:
        if case.get('setup'):  # what the user already has in CAE (e.g. a part that is only a mesh)
            exec(compile(case['setup'], case['name'] + '_setup.py', 'exec'), {'__name__': '__main__'})
        exec(compile(case['script'], case['name'] + '.py', 'exec'), scope)
    except Exception:
        error = traceback.format_exc()
    finally:
        sys.stdout = old
    for expr in case['checks']:
        try:
            checks.append([expr, repr(eval(expr, scope))])
        except Exception as exc:
            checks.append([expr, 'error: %s' % exc])
    report[case['name']] = {'output': out.getvalue(), 'error': error, 'checks': checks}
json.dump(report, open('report.json', 'w'), indent=1)
'''

MARKER = cae_queries.MARKER
M = "mdb.models['NoCode-Model']"
PART = M + ".parts['Part-1']"
BOX = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude"})]  # 100 x 50 x 50
BOX_EDGES = [("s", "sketch", "p", "profile")]


def find(kind, points, tolerance=0.0, rid="r", source="p"):
    return ([(rid + "pts", "input.value", {"type": "Text", "value": points}),
             (rid, "model.region", {"method": "Find at", "kind": kind, "tolerance": tolerance})],
            [(source, "part" if source == "p" else "instance", rid, "source"), (rid + "pts", "value", rid, "points")])


def feature(operation, region_kind, points, tolerance=0.0, **params):
    n, e = find(region_kind, points, tolerance)
    nodes = BOX + n + [("f", "model.feature", {"operation": operation, **params})]
    return nodes, BOX_EDGES + e + [("p", "part", "f", "part"), ("r", "region", "f", "region")]


def with_sketch(case, sketch):
    nodes, edges = case
    return nodes + [("c", "model.sketch", sketch)], edges + [("c", "sketch", "f", "sketch")]


def datum(kind, points):
    return (BOX + [("pts", "input.value", {"type": "Text", "value": points}), ("d", "model.datum", {"kind": kind})],
            BOX_EDGES + [("p", "part", "d", "part"), ("pts", "value", "d", "points")])


CUBE_INP = """*Heading
a 2 x 2 x 2 cube of 10 mm hex elements, no parts (a flat input file)
*Node
""" + "".join(f"{1 + i + 3 * j + 9 * k}, {10. * i}, {10. * j}, {10. * k}\n"
              for k in range(3) for j in range(3) for i in range(3)) + """*Element, type=C3D8R
""" + "".join(f"{1 + i + 2 * j + 4 * k}, " + ", ".join(str(n) for n in (
    1 + i + 3 * j + 9 * k, 2 + i + 3 * j + 9 * k, 5 + i + 3 * j + 9 * k, 4 + i + 3 * j + 9 * k,
    10 + i + 3 * j + 9 * k, 11 + i + 3 * j + 9 * k, 14 + i + 3 * j + 9 * k, 13 + i + 3 * j + 9 * k)) + "\n"
    for k in range(2) for j in range(2) for i in range(2)) + """*Elset, elset=ALL, generate
1, 8, 1
*Material, name=Steel
*Elastic
210000., 0.3
*Solid Section, elset=ALL, material=Steel
,
*Nset, nset=BOTTOM
1, 2, 3, 4, 5, 6, 7, 8, 9
*Step
*Static
*Boundary
BOTTOM, 1, 3
*Cload
27, 3, -1.
*End Step
"""
TWO_PARTS_INP = """*Heading
*Part, name=A
*Node
1, 0., 0., 0.
2, 1., 0., 0.
3, 1., 1., 0.
4, 0., 1., 0.
*Element, type=S4R
1, 1, 2, 3, 4
*End Part
*Part, name=B
*Node
1, 0., 0., 0.
2, 2., 0., 0.
3, 2., 2., 0.
4, 0., 2., 0.
5, 4., 0., 0.
6, 4., 2., 0.
*Element, type=S4R
1, 1, 2, 3, 4
2, 2, 5, 6, 3
*End Part
*Assembly, name=Assembly
*Instance, name=A-1, part=A
*End Instance
*End Assembly
"""


def cases(tmp: Path) -> dict:
    """name: (nodes, edges, [checks evaluated in CAE after the script], [texts the output must contain])."""
    vol = PART + ".getVolume()"
    c = {}
    (tmp / "cube_mesh.inp").write_text(CUBE_INP)
    (tmp / "two.inp").write_text(TWO_PARTS_INP)
    # an input file's mesh as a part: sectioned, placed, held on its bottom and pressed on its top (By shape, found
    # in the mesh), and a job written
    im = "mdb.models['Imported']"
    c["inp_part"] = (
        [("m", "model.model", {"name": "Imported"}),
         ("p", "model.part", {"name": "Cube", "body": "Solid", "method": "Import CAD", "file": str(tmp / "cube_mesh.inp")}),
         ("sec", "model.section", {"density": 7.85e-9}), ("i", "model.instance", {}),
         ("top", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, 1"}),
         ("bottom", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, -1"}),
         ("st", "model.step", {"name": "Press"}), ("fix", "model.condition", {"name": "Hold", "kind": "Fixed"}),
         ("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 1}),
         ("j", "model.job", {"name": "Cube", "directory": str(tmp), "action": "Write input only"}),
         ("q", "model.part", {"name": "Wide", "body": "Shell", "method": "Import CAD", "file": str(tmp / "two.inp"),
                              "entry": "b"})],
        [("m", "model", "p", "model"), ("p", "part", "sec", "target"), ("sec", "part", "i", "part"),
         ("i", "instance", "top", "source"), ("i", "instance", "bottom", "source"), ("m", "model", "st", "after"),
         ("st", "step", "fix", "step"), ("bottom", "region", "fix", "region"), ("fix", "condition", "push", "step"),
         ("top", "region", "push", "region"), ("push", "condition", "j", "analysis"), ("m", "model", "q", "model")],
        [f"sorted({im}.parts.keys())", f"len({im}.parts['Cube'].elements)", f"len({im}.parts['Cube'].sectionAssignments)",
         f"len({im}.parts['Wide'].elements)", "'__nocode_import__' not in mdb.models.keys()",
         f"__import__('os').path.exists({str(tmp / 'Cube.inp')!r})"], ["two.inp has parts A, B: Wide is B"])
    # a whole model from an input file, with a step added to it and its job written
    # instances aligned onto another: B's bottom face onto A's top (found on the part), C's corner onto A's far one
    mate = "mdb.models['Mate'].rootAssembly.instances"
    c["align"] = (
        [("m", "model.model", {"name": "Mate"}), ("s", "model.sketch", {}),
         ("p", "model.part", {"body": "Solid", "method": "Extrude"}),
         ("a", "model.instance", {"name": "A"}),
         ("b", "model.instance", {"name": "B", "x": 300}), ("c", "model.instance", {"name": "C", "x": -300,
                                                                                       "how": "Point to point"}),
         ("bp", "input.value", {"type": "Text", "value": "50, 25, 0"}),
         ("tp", "input.value", {"type": "Text", "value": "50, 25, 50"}),
         ("cp", "input.value", {"type": "Text", "value": "0, 0, 0"}),
         ("fp", "input.value", {"type": "Text", "value": "100, 50, 50"}),
         ("bf", "model.region", {"method": "Find at", "kind": "Face"}),
         ("tf", "model.region", {"method": "Find at", "kind": "Face"}),
         ("cv", "model.region", {"method": "Find at", "kind": "Vertex"}),
         ("fv", "model.region", {"method": "Find at", "kind": "Vertex"})],
        [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "a", "part"),
         ("p", "part", "b", "part"), ("p", "part", "c", "part"),
         ("bp", "value", "bf", "points"), ("p", "part", "bf", "source"), ("tp", "value", "tf", "points"),
         ("a", "instance", "tf", "source"), ("bf", "region", "b", "align"), ("tf", "region", "b", "to"),
         ("cp", "value", "cv", "points"), ("p", "part", "cv", "source"), ("fp", "value", "fv", "points"),
         ("a", "instance", "fv", "source"), ("cv", "region", "c", "align"), ("fv", "region", "c", "to")],
        [f"{mate}['B'].getTranslation()", f"{mate}['C'].getTranslation()",
         "sorted(mdb.models['Mate'].rootAssembly.features.keys())"], [])
    c["inp_model"] = (
        [("m", "model.model", {"name": "Whole", "file": str(tmp / "cube_mesh.inp")}),
         ("st", "model.step", {"name": "More"}),
         ("j", "model.job", {"name": "Whole", "directory": str(tmp), "action": "Write input only"})],
        [("m", "model", "st", "after"), ("st", "step", "j", "analysis")],
        ["sorted(mdb.models['Whole'].steps.keys())", "mdb.models['Whole'].boundaryConditions.keys()",
         "mdb.models['Whole'].loads.keys()", f"__import__('os').path.exists({str(tmp / 'Whole.inp')!r})"], [])
    c["fillet_find_at"] = (*feature("Fillet", "Edge", "50, 0, 0; 50, 0, 50", radius=2),
                           [f"{vol} < 250000"], ["matched 2 unique geometries"])
    c["chamfer_tolerance"] = (*feature("Chamfer", "Edge", "50, 0.4, 0.3", 1.0, radius=2), [f"{vol} < 250000"], [])
    c["find_at_miss_is_reported"] = (*feature("Fillet", "Edge", "50, 20, 20", radius=2), [f"{vol} == 250000"],
                                     ["found no geometry at (50.0, 20.0, 20.0)"])
    c["extrude_cut_on_face"] = (*with_sketch(feature("Extrude cut", "Face", "50, 25, 50", depth=10),
                                             {"shape": "Circle", "radius": 5, "x": 0, "y": 0}),
                                [f"abs({vol} - (250000 - 3.14159265 * 25 * 10)) < 1"], [])
    c["extrude_add_on_face"] = (*with_sketch(feature("Extrude add", "Face", "50, 25, 50", depth=10),
                                             {"shape": "Circle", "radius": 5, "x": 0, "y": 0}),
                                [f"abs({vol} - (250000 + 3.14159265 * 25 * 10)) < 1"], [])
    # Defeaturing: a 10 deep, r 5 hole cut into the top face, then found By shape (radius up to 6) and removed
    cut_nodes, cut_edges = with_sketch(feature("Extrude cut", "Face", "50, 25, 50", depth=10),
                                       {"shape": "Circle", "radius": 5, "x": 0, "y": 0})
    healed = cut_nodes + [("h", "model.region", {"method": "By shape", "shape": "Cylindrical faces", "radius": 6,
                                                 "match": "Up to"}),
                          ("g", "model.feature", {"operation": "Remove faces", "name": "No holes"})]
    healed_edges = cut_edges + [("f", "part", "h", "source"), ("f", "part", "g", "part"), ("h", "region", "g", "region")]
    c["defeature_remove_hole"] = (healed, healed_edges, [f"abs({vol} - 250000) < 1",
                                                         f"'No-holes' in {PART}.features.keys()"],
                                  ["note: Remove faces"])
    c["defeature_repairs"] = (
        BOX + [("rr", "model.feature", {"operation": "Remove redundant entities"})],
        BOX_EDGES + [("p", "part", "rr", "part")],
        [f"abs({vol} - 250000) < 1"], [])
    # Remove wire: one side of a planar wire rectangle (100 x 50) deleted
    n, e = find("Edge", "50, 0, 0")
    c["remove_wire"] = ([("s", "model.sketch", {}), ("p", "model.part", {"body": "Wire", "method": "Planar"})] + n
                        + [("f", "model.feature", {"operation": "Remove wire", "name": "Open"})],
                        BOX_EDGES + e + [("p", "part", "f", "part"), ("r", "region", "f", "region")],
                        [f"len({PART}.edges) == 3", f"'Open' in {PART}.features.keys()"], [])
    # Partitions: along the extension of an L-shape's inner face (two cells), a circle drawn on the top face
    # (one more face) and an edge split at a quarter (one more edge)
    ell = [("s", "model.sketch", {"shape": "Profile", "points": "0,0; 100,0; 100,20; 40,20; 40,50; 0,50",
                                  "closed": "Closed"}),
           ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})]
    n, e = find("Face", "40, 35, 5", rid="t")
    c["partition_extend_face"] = (ell + n + [("f", "model.feature", {"operation": "Partition by extending a face"})],
                                  BOX_EDGES + e + [("p", "part", "f", "part"), ("t", "region", "f", "tool")],
                                  [f"len({PART}.cells) == 2"], [])
    c["partition_sketch_on_face"] = (*with_sketch(feature("Partition by a sketch on a face", "Face", "50, 25, 50"),
                                                  {"shape": "Circle", "radius": 5, "x": 0, "y": 0}),
                                     [f"len({PART}.faces) == 7", f"abs({vol} - 250000) < 1"], [])
    c["split_edges"] = (*feature("Split edges", "Edge", "50, 0, 0", fraction=0.25), [f"len({PART}.edges) == 13"], [])
    # Thin solids as shells: a 100 x 50 x 2 plate's skin, and its midsurface, sectioned and meshed as a shell
    plate = [("s", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 2})]
    c["solid_to_shell"] = (plate + [("f", "model.feature", {"operation": "Solid to shell"})],
                           BOX_EDGES + [("p", "part", "f", "part")],
                           [f"len({PART}.cells) == 0", f"len({PART}.faces) == 6"], [])
    top, top_e = find("Face", "50, 25, 2", rid="top")
    bottom, bottom_e = find("Face", "50, 25, 0", rid="bot")
    c["midsurface_meshed_as_shell"] = (
        plate + top + bottom + [("f", "model.feature", {"operation": "Midsurface", "name": "Mid"}),
                                ("sec", "model.section", {"thickness": 2}), ("mesh", "model.mesh", {"size": 10})],
        BOX_EDGES + top_e + bottom_e + [("p", "part", "f", "part"), ("top", "region", "f", "region"),
                                        ("bot", "region", "f", "opposite"), ("f", "part", "sec", "target"),
                                        ("sec", "part", "mesh", "target")],
        [f"len({PART}.elements) > 0", f"str({PART}.elements[0].type)", f"'Mid' in {PART}.features.keys()"], [])
    # Sketch walls: a 100 x 50 rectangular tube 3 thick (inside), and a C-channel 4 thick from its centreline
    c["sketch_wall_tube"] = ([("s", "model.sketch", {"width": 100, "height": 50, "wall": 3, "wall_side": "Inside"}),
                              ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})], BOX_EDGES,
                             [f"abs({vol} - (100 * 50 - 94 * 44) * 10) < 1e-3"], [])
    c["sketch_wall_channel"] = ([("s", "model.sketch", {"shape": "Profile", "points": "50,0; 0,0; 0,100; 50,100",
                                                        "wall": 4, "wall_side": "Centred"}),
                                 ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10})], BOX_EDGES,
                                [f"abs({vol} - 4 * 200 * 10) < 1e-3"], [])
    # A quasi-isotropic carbon laminate (Lamina, symmetric 8 plies typed as 4) on a flat shell, fibres along x+y
    c["composite_shell_oriented"] = (
        [("s", "model.sketch", {}), ("p", "model.part", {}),
         ("sec", "model.section", {"behavior": "Lamina", "kind": "Composite shell", "symmetric": "Yes",
                                   "plies": "0, 0.125; 45, 0.125; -45, 0.125; 90, 0.125", "orientation": "1, 1, 0"}),
         ("mesh", "model.mesh", {"size": 10})],
        BOX_EDGES + [("p", "part", "sec", "target"), ("sec", "part", "mesh", "target")],
        [f"len({M}.sections['Section-1'].layup)", f"str({M}.materials['Material-1'].elastic.type)",
         f"len({PART}.materialOrientations)", f"len({PART}.elements) > 0"], [])
    n, e = find("Face", "50, 25, 50")
    c["datum_offset_partition"] = (
        BOX + n + [("d", "model.datum", {"kind": "Offset from face", "offset": 5, "flip": "Side 2"}),
                   ("f", "model.feature", {"operation": "Partition"})],
        BOX_EDGES + e + [("p", "part", "d", "part"), ("r", "region", "d", "region"), ("p", "part", "f", "part"),
                         ("d", "datum", "f", "datum")],
        [f"len({PART}.cells)"], [])
    c["datum_point"] = (*datum("Point", "1, 2, 3"), [f"len({PART}.datums)"], [])
    c["datum_axis"] = (*datum("Axis through 2 points", "0,0,0; 0,0,1"), [f"len({PART}.datums)"], [])
    c["datum_plane"] = (*datum("Plane through 3 points", "0,0,25; 1,0,25; 0,1,25"), [f"len({PART}.datums)"], [])
    for fmt in ("stp", "sat"):
        c[f"import_{fmt}"] = ([("p", "model.part", {"method": "Import CAD", "file": str(tmp / f"box.{fmt}"),
                                                     "body": "Solid", "name": "Imp"})], [],
                              [M + ".parts['Imp'].getVolume()"], [])
    c["arc_and_closed_spline"] = (
        [("a", "model.sketch", {"shape": "Arc", "radius": 4, "start": 0, "end": 90, "name": "Arc"}),
         ("sp", "model.sketch", {"shape": "Spline", "points": "0,0; 5,8; 10,0", "closed": "Closed"}),
         ("p", "model.part", {"body": "Shell", "method": "Planar"})],
        [("sp", "sketch", "p", "profile")], [f"len({PART}.faces)", M + ".sketches.keys()"], [])
    for op, expected in (("Cut", 62500), ("Merge", 312500)):
        nodes = [("s", "model.sketch", {}),
                 ("ap", "model.part", {"name": "A", "body": "Solid", "method": "Extrude"}), ("a", "model.instance", {}),
                 ("bp", "model.part", {"name": "B", "body": "Solid", "method": "Extrude"}),
                 ("b", "model.instance", {"x": 25}),
                 ("op", "model.merge_instances", {"operation": op, "name": "Result"})]
        edges = [("s", "sketch", "ap", "profile"), ("s", "sketch", "bp", "profile"), ("ap", "part", "a", "part"),
                 ("bp", "part", "b", "part")]
        edges += ([("a", "instance", "op", "target"), ("b", "instance", "op", "tools")] if op == "Cut" else
                  [("a", "instance", "op", "instances"), ("b", "instance", "op", "instances")])
        c[f"boolean_{op.lower()}"] = (nodes, edges, [f"round({M}.parts['Result'].getVolume())"], [])
        c[f"boolean_{op.lower()}"][2].append(f"round({M}.parts['Result'].getVolume()) == {expected}")
    c["job_writes_input"] = ([("m", "model.model", {"name": "Analysis"}), ("st", "model.step", {"name": "Load"}),
                              ("j", "model.job", {"name": "Probe", "directory": str(tmp),
                                                  "action": "Write input only"})],
                             [("m", "model", "st", "after"), ("st", "step", "j", "analysis")],
                             [f"__import__('os').path.exists({str(tmp / 'Probe.inp')!r})"], [])
    # Regions made on the part (an end face found at a point, all cylindrical faces) used by a BC, a pressure and
    # a tie: they go on every instance of the part (two here), as CAE shows a part's sets on its instances
    on_part = BOX + [("sec", "model.section", {}), ("mesh", "model.mesh", {"size": 25}),
                     ("i1", "model.instance", {"name": "Left"}), ("i2", "model.instance", {"name": "Right", "x": 200}),
                     ("endpts", "input.value", {"type": "Text", "value": "0, 25, 25"}),
                     ("end", "model.region", {"method": "Find at", "kind": "Face"}),
                     ("toppts", "input.value", {"type": "Text", "value": "50, 25, 50"}),
                     ("top", "model.region", {"method": "Find at", "kind": "Face"}),
                     ("botpts", "input.value", {"type": "Text", "value": "50, 25, 0"}),
                     ("bottom", "model.region", {"method": "Find at", "kind": "Face"}),
                     ("st", "model.step", {"name": "Load"}),
                     ("fix", "model.condition", {"name": "Clamp", "kind": "Fixed"}),
                     ("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 3}),
                     ("tie", "model.interaction", {"name": "Glue"}),
                     ("j", "model.job", {"name": "OnPart", "directory": str(tmp), "action": "Write input only"})]
    on_part_edges = BOX_EDGES + [("p", "part", "sec", "target"), ("sec", "part", "mesh", "target"),
                                 ("mesh", "part", "i1", "part"), ("mesh", "part", "i2", "part")] + [
        ("mesh", "part", r, "source") for r in ("end", "top", "bottom")] + [
        ("endpts", "value", "end", "points"), ("toppts", "value", "top", "points"),
        ("botpts", "value", "bottom", "points"), ("st", "step", "fix", "step"), ("end", "region", "fix", "region"),
        ("fix", "condition", "push", "step"), ("top", "region", "push", "region"), ("st", "step", "tie", "step"),
        ("top", "region", "tie", "master"), ("bottom", "region", "tie", "slave"), ("push", "condition", "j", "analysis")]
    c["regions_on_part"] = (on_part, on_part_edges,
                            [f"__import__('os').path.exists({str(tmp / 'OnPart.inp')!r})",
                             f"open({str(tmp / 'OnPart.inp')!r}).read()"], [])
    nodes, edges = flawed(tmp)
    c["flawed_model"] = (nodes, edges, [f"__import__('os').path.exists({str(tmp / 'Flawed.inp')!r})"], [])
    nodes, edges = pipeline(tmp)
    c["pipeline_model"] = (nodes, edges, [f"__import__('os').path.exists({str(tmp / 'Beam.inp')!r})"], [])
    nodes, edges = pipeline(tmp, "Yield", PLASTIC, 300.0, peeq=True)
    c["plastic_model"] = (nodes, edges, ["mdb.models['Yield'].materials['Material-1'].plastic.table"], [])
    for space, model in (("2D planar", "Plane"), ("Axisymmetric", "Axi")):
        nodes, edges = flat_bar(tmp, space)
        bar = f"mdb.models[{model!r}].parts['Bar']"
        c[f"{model.lower()}_model"] = (nodes, edges, [
            f"str({bar}.space) == {('TWO_D_PLANAR' if model == 'Plane' else 'AXISYMMETRIC')!r}",
            f"len({bar}.elements)", f"str({bar}.elements[0].type)"], [])
    nodes, edges = rigid_press(tmp)
    c["press_model"] = (nodes, edges, ["str(mdb.models['Press'].parts['Tool'].type) == 'ANALYTIC_RIGID_SURFACE'",
                                       "len(mdb.models['Press'].parts['Tool'].referencePoints) == 1"], [])
    for name, builder in (("column_model", buckled_column), ("arc_model", riks_bar),
                          ("turned_model", turned_pull), ("sprung_model", sprung_mass), ("tied_model", tied_bars)):
        nodes, edges = builder(tmp)
        c[name] = (nodes, edges, [], [])
    for how in ("flux", "film", "coupled"):
        nodes, edges = heated_bar(tmp, how)
        c[f"{how}_model"] = (nodes, edges, [], [])
    # hardening and damage: kinematic, combined from a half cycle, Johnson-Cook with ductile damage
    hard = "mdb.models['Hard'].materials"
    c["hardening"] = (
        [("m", "model.model", {"name": "Hard"}), ("s", "model.sketch", {})] + [
            (f"p{k}", "model.part", {"name": f"P{k}", "body": "Solid", "method": "Extrude", "depth": 10})
            for k in range(3)] + [
            ("s0", "model.section", {"material": "Kin", "section": "S0", "plastic": "0, 250; 0.1, 350",
                                     "hardening": "Kinematic"}),
            ("s1", "model.section", {"material": "Comb", "section": "S1", "plastic": "0, 250; 0.05, 300; 0.1, 320",
                                     "hardening": "Combined"}),
            ("s2", "model.section", {"material": "JC", "section": "S2", "hardening": "Johnson-Cook", "density": 7.8e-9,
                                     "fracture_strain": 0.3, "failure_displacement": 0.05, "conductivity": 0.05,
                                     "specific_heat": 4.6e8, "expansion": 1.2e-5})],
        [("m", "model", "s", "model")] + [e for k in range(3) for e in (
            ("s", "sketch", f"p{k}", "profile"), (f"p{k}", "part", f"s{k}", "target"))],
        [f"str({hard}['Kin'].plastic.hardening)", f"str({hard}['Comb'].plastic.hardening)",
         f"str({hard}['JC'].plastic.hardening)", f"{hard}['JC'].plastic.table",
         f"{hard}['JC'].ductileDamageInitiation.table",
         f"{hard}['JC'].ductileDamageInitiation.damageEvolution.table",
         f"{hard}['JC'].conductivity.table", f"{hard}['JC'].specificHeat.table", f"{hard}['JC'].expansion.table"], [])
    # a 3D discrete rigid plate (a die), meshed with rigid elements, with its reference point
    c["discrete_rigid"] = (
        [("m", "model.model", {"name": "Die"}), ("s", "model.sketch", {"shape": "Profile", "points": "0, 0; 50, 0"}),
         ("p", "model.part", {"name": "Die", "type": "Discrete rigid", "body": "Shell", "method": "Extrude",
                              "depth": 40}),
         ("rp", "model.feature", {"operation": "Reference point", "name": "Hold", "point": "25, 0, 20"}),
         ("mesh", "model.mesh", {"size": 10}), ("i", "model.instance", {})],
        [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "rp", "part"),
         ("rp", "part", "mesh", "target"), ("mesh", "part", "i", "part")],
        ["str(mdb.models['Die'].parts['Die'].type) == 'DISCRETE_RIGID_SURFACE'",
         "len(mdb.models['Die'].parts['Die'].elements) == 20",
         "str(mdb.models['Die'].parts['Die'].elements[0].type) == 'R3D4'"], [])
    # a rounded L profile (one inside corner) and a rounded hexagon, extruded 10
    c["rounded"] = (
        [("m", "model.model", {"name": "Round"}),
         ("s", "model.sketch", {"shape": "Profile", "points": "0, 0; 100, 0; 100, 20; 20, 20; 20, 80; 0, 80",
                                "closed": "Closed", "corner": 5}),
         ("p", "model.part", {"name": "L", "body": "Solid", "method": "Extrude", "depth": 10}),
         ("h", "model.sketch", {"name": "Hex", "shape": "Polygon", "sides": 6, "radius": 20, "corner": 4}),
         ("q", "model.part", {"name": "Hex", "body": "Solid", "method": "Extrude", "depth": 10})],
        [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("m", "model", "h", "model"),
         ("h", "sketch", "q", "profile")],
        ["mdb.models['Round'].parts['L'].getVolume()", "mdb.models['Round'].parts['Hex'].getVolume()"], [])
    # a pin dropped into a plate's hole: coaxial (cylindrical faces By shape), then flush with the plate's bottom
    c["pin_in_hole"] = (
        [("m", "model.model", {"name": "PinPlate"}),
         ("hole", "model.sketch", {"name": "Hole", "shape": "Circle", "radius": 5}),
         ("ps", "model.sketch", {"name": "Plate", "x": -50, "y": -30, "width": 100, "height": 60, "corner": 8}),
         ("plate", "model.part", {"name": "Plate", "body": "Solid", "method": "Extrude", "depth": 10}),
         ("pin_s", "model.sketch", {"name": "Pin", "shape": "Circle", "radius": 5}),
         ("pin", "model.part", {"name": "Pin", "body": "Solid", "method": "Extrude", "depth": 40}),
         ("pi", "model.instance", {"name": "Plate-1"}),
         ("bore", "model.region", {"method": "By shape", "shape": "Cylindrical faces", "radius": 5, "tolerance": 0.01}),
         ("under", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, -1"}),
         ("shank", "model.region", {"method": "By shape", "shape": "Cylindrical faces"}),
         ("foot", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, -1"}),
         ("in", "model.instance", {"name": "Pin-1", "x": 200, "y": 30, "z": 25, "how": "Coaxial"}),
         ("flush", "model.instance", {"how": "Face to face", "flip": "Yes"})],
        [("m", "model", "hole", "model"), ("m", "model", "ps", "model"), ("hole", "sketch", "ps", "add"),
         ("ps", "sketch", "plate", "profile"), ("m", "model", "pin_s", "model"), ("pin_s", "sketch", "pin", "profile"),
         ("plate", "part", "pi", "part"), ("pi", "instance", "bore", "source"), ("pi", "instance", "under", "source"),
         ("pin", "part", "shank", "source"), ("pin", "part", "foot", "source"), ("pin", "part", "in", "part"),
         ("shank", "region", "in", "align"), ("bore", "region", "in", "to"), ("in", "instance", "flush", "part"),
         ("foot", "region", "flush", "align"), ("under", "region", "flush", "to")],
        ["mdb.models['PinPlate'].rootAssembly.instances['Pin-1'].getTranslation()",
         "sorted(mdb.models['PinPlate'].rootAssembly.features.keys())"], [])
    # the quadratic / incompatible-mode elements and dominated shapes, as CAE meshes them
    els = "mdb.models['Elems'].parts"
    c["elements"] = (
        [("m", "model.model", {"name": "Elems"}), ("s", "model.sketch", {"width": 40, "height": 20}),
         ("q", "model.part", {"name": "Q", "body": "Solid", "method": "Extrude", "depth": 10}),
         ("qm", "model.mesh", {"size": 10, "element": "C3D20R"}),
         ("i", "model.part", {"name": "I", "body": "Solid", "method": "Extrude", "depth": 10}),
         ("im", "model.mesh", {"size": 10, "element": "C3D8I", "shape": "Hex-dominated"}),
         ("sh", "model.part", {"name": "Sh", "body": "Shell", "method": "Planar"}),
         ("shm", "model.mesh", {"size": 10, "element": "S8R", "shape": "Quad-dominated"}),
         ("w", "model.part", {"name": "W", "body": "Wire", "method": "Planar"}),
         ("wm", "model.mesh", {"size": 10, "element": "B32"})],
        [("m", "model", "s", "model"), ("s", "sketch", "q", "profile"), ("q", "part", "qm", "target"),
         ("s", "sketch", "i", "profile"), ("i", "part", "im", "target"), ("s", "sketch", "sh", "profile"),
         ("sh", "part", "shm", "target"), ("s", "sketch", "w", "profile"), ("w", "part", "wm", "target")],
        [f"str({els}['Q'].elements[0].type)", f"str({els}['I'].elements[0].type)",
         f"str({els}['Sh'].elements[0].type)", f"str({els}['W'].elements[0].type)"], [])
    from .test_model import bolted, bolted_joint
    nodes, edges = bolted_joint(str(tmp))
    jm = "mdb.models['BoltedJoint']"
    c["bolted_joint"] = (nodes, edges, [
        f"{jm}.rootAssembly.instances['Bolt-1'].getTranslation()",
        f"sorted({jm}.constraints.keys())", f"{jm}.steps['Pull'].loadStates['Pretension'].boltMethod",
        f"open({str(tmp / 'BoltedJoint.inp')!r}).read().count('*Pre-tension Section')"], [])
    nodes, edges = bolted()
    nodes.append(("j", "model.job", {"name": "Bolted", "directory": str(tmp), "action": "Write input only"}))
    edges.append(("bolt", "condition", "j", "analysis"))
    c["bolt_load"] = (nodes, edges, [
        "mdb.models['Bolt'].steps['Preload'].loadStates['Pretension'].magnitude",
        "str(mdb.models['Bolt'].steps['Work'].loadStates['Pretension'].boltMethod)",
        "'Pretension-Axis' in mdb.models['Bolt'].rootAssembly.features.keys()",
        "'Pretension-Force' in mdb.models['Bolt'].historyOutputRequests.keys()",
        f"'*Pre-tension Section' in open({str(tmp / 'Bolted.inp')!r}).read()"], [])
    nodes, edges = stepwise_bar(tmp)
    sm = "mdb.models['Steps']"
    c["stepwise"] = (nodes, edges, [
        f"{sm}.steps['Load'].loadStates['Push'].magnitude",
        f"{sm}.steps['Release'].loadStates['Push-from-Release'].magnitude",
        f"[str(s.status) for n, s in {sm}.steps['Hold'].loadStates.items() if n in ('Push', 'Weight')] or 'none'",
        f"'Push-from-Release' in {sm}.loads.keys()",
        f"str({sm}.steps['Release'].loadStates['Push-from-Release'].amplitude)",
        f"str({sm}.steps['Release'].boundaryConditionStates['Guide'].u3State)",
        f"sorted({sm}.interactions.keys())",
        f"open({str(tmp / 'Steps.inp')!r}).read().count('*Change Friction')",
        f"open({str(tmp / 'Steps.inp')!r}).read().count('*Temperature, op=NEW')",
        f"open({str(tmp / 'Steps.inp')!r}).read().count('*Model Change')"], [])
    nodes, edges = explicit_bar(tmp)
    dm = "mdb.models['Drop']"
    c["explicit_model"] = (nodes, edges, [
        f"type({dm}.steps['Hit']).__name__", f"{dm}.steps['Hit'].massScaling[0].dt",
        f"type({dm}.interactions['All']).__name__", f"type({dm}.interactions['Rub']).__name__",
        f"open({str(tmp / 'Drop.inp')!r}).read().count('*Dynamic, Explicit')"], [])
    nodes, edges = pipeline(tmp, "Rubber")
    nodes = [("sec", "model.section", {"behavior": "Mooney-Rivlin", "c10": 0.3, "c01": 0.05, "d1": 0, "alpha": 1,
                                       "beta": 1e-4}) if n[0] == "sec" else
             ("mesh", "model.mesh", {"size": 5, "element": "C3D8H"}) if n[0] == "mesh" else n for n in nodes]
    rubber = "mdb.models['Rubber'].materials['Material-1']"
    c["rubber_model"] = (nodes, edges, [f"{rubber}.hyperelastic.type", f"{rubber}.hyperelastic.table",
                                        f"{rubber}.damping.alpha", f"{rubber}.damping.beta",
                                        f"'C3D8H' in open({str(tmp / 'Rubber.inp')!r}).read()"], [])
    nodes, edges = pipeline(tmp, "Vary", push=VARYING)
    c["varying_model"] = (nodes, edges, ["mdb.models['Vary'].amplitudes['Push-Amp'].data",
                                         "mdb.models['Vary'].analyticalFields['Push-Field'].expression"], [])
    nodes, edges = pipeline(tmp, "Mapped", push={"distribution": "0, 0, 0, 1; 100, 0, 0, 1; 100, 10, 10, 2"})
    c["mapped_model"] = (nodes, edges, ["len(mdb.models['Mapped'].analyticalFields['Push-Field'].xyzPointData)",
                                        f"__import__('os').path.exists({str(tmp / 'Mapped.inp')!r})"], [])
    nodes, edges = coupled(tmp)
    c["coupling_model"] = (nodes, edges, ["mdb.models['Grip'].constraints['Grip'].couplingType",
                                          "len(mdb.models['Grip'].rootAssembly.sets['Grip-RP'].referencePoints)"], [])
    nodes, edges = coupled(tmp, "GripNear", influence=20)
    c["coupling_radius_model"] = (nodes, edges, ["mdb.models['GripNear'].constraints['Grip'].influenceRadius == 20"],
                                  [])
    nodes = BOX + [("coarse", "model.mesh", {"size": 25, "generate": "No"}),
                   ("epts", "input.value", {"type": "Text", "value": "50, 0, 0"}),
                   ("edge", "model.region", {"method": "Find at", "kind": "Edge"}),
                   ("graded", "model.mesh", {"seed": "Biased", "number": 10, "ratio": 4, "bias": "Fine at start"}),
                   ("i", "model.instance", {})] + [
        (q, "model.measure", {"quantity": q}) for q in ("Nodes", "Elements", "Worst aspect ratio",
                                                         "Mean aspect ratio", "Failed elements")]
    edges = BOX_EDGES + [("p", "part", "coarse", "target"), ("coarse", "part", "edge", "source"),
                         ("epts", "value", "edge", "points"), ("edge", "region", "graded", "target"),
                         ("graded", "part", "i", "part")] + [
        ("graded", "part", q, "first") for q in ("Nodes", "Elements", "Worst aspect ratio", "Mean aspect ratio")] + [
        ("i", "instance", "Failed elements", "first")]
    c["mesh_controls"] = (nodes, edges, [f"len({PART}.elements)", f"len({PART}.nodes)"], [MARKER])
    # a reference point made on the part: every instance carries it; loads, a BC and history output on it
    rp = [("rp", "model.feature", {"operation": "Reference point", "name": "Top", "point": "50, 25, 80"})]
    nodes = BOX + rp + [("i", "model.instance", {}), ("j", "model.instance", {"name": "B", "x": 300}),
                        ("step", "model.step", {"name": "Pull", "history": "RF1, U1"}),
                        ("hold", "model.condition", {"name": "Hold", "kind": "Fixed"}),
                        ("pull", "model.condition", {"name": "Pull", "kind": "Concentrated force", "x": 100})]
    edges = BOX_EDGES + [("p", "part", "rp", "part"), ("rp", "part", "i", "part"), ("rp", "part", "j", "part"),
                         ("step", "step", "hold", "step"), ("rp", "point", "hold", "region"),
                         ("hold", "condition", "pull", "step"), ("rp", "point", "pull", "region"),
                         ("rp", "point", "step", "history_region")]
    c["part_reference_point"] = (nodes, edges, [
        f"len({PART}.referencePoints)", f"'Top' in {PART}.features.keys()",
        f"len({M}.rootAssembly.instances['B'].referencePoints)", f"'Pull' in {M}.loads.keys()",
        f"'Hold' in {M}.boundaryConditions.keys()", f"'Pull-History' in {M}.historyOutputRequests.keys()"], [])
    # ... and as a coupling's control point and a connector's end
    nodes = BOX + rp + [("i", "model.instance", {}), ("tpts", "input.value", {"type": "Text", "value": "50, 25, 50"}),
                        ("top", "model.region", {"method": "Find at", "kind": "Face"}), ("step", "model.step", {}),
                        ("rbe", "model.interaction", {"name": "Grip", "kind": "Kinematic coupling"}),
                        ("wire", "model.interaction", {"name": "Link", "kind": "Connector", "point2": "50, 25, 300"})]
    edges = BOX_EDGES + [("p", "part", "rp", "part"), ("rp", "part", "i", "part"), ("i", "instance", "top", "source"),
                         ("tpts", "value", "top", "points"), ("top", "region", "rbe", "slave"),
                         ("rp", "point", "rbe", "master"), ("step", "step", "rbe", "step"),
                         ("rp", "point", "wire", "master"), ("step", "step", "wire", "step")]
    c["part_reference_point_coupling"] = (nodes, edges, [
        f"len({M}.rootAssembly.sets['Grip-RP'].referencePoints)", f"'Grip' in {M}.constraints.keys()",
        f"'Link-Wire' in {M}.rootAssembly.sets.keys()"], [])
    nodes, edges = connected(tmp)
    c["connector_model"] = (nodes, edges, ["mdb.models['Joint'].sections['Joint-Section'].assembledType",
                                           "sorted(type(b).__name__ for b in mdb.models['Joint'].sections['Joint-Section'].behaviorOptions)",
                                           "len(mdb.models['Joint'].rootAssembly.sectionAssignments)",
                                           "type(mdb.models['Joint'].interactions['Touch']).__name__",
                                           f"__import__('os').path.exists({str(tmp / 'Joint.inp')!r})"], [])
    for name, sketch in SHAPES.items():  # extruded 1 deep: the volume is the area
        c[f"shape_{name}"] = ([("s", "model.sketch", sketch),
                               ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 1})],
                              [("s", "sketch", "p", "profile")], [f"round({PART}.getVolume(), 3)"], [])
    c["shape_holes"] = ([("hex", "model.sketch", {"shape": "Polygon", "sides": 6, "radius": 10, "x": 30, "y": 25}),
                         ("slot", "model.sketch", {"shape": "Slot", "width": 30, "height": 10, "x": 70, "y": 25,
                                                   "rotation": 90}),
                         ("plate", "model.sketch", {"corner": 5}),
                         ("p", "model.part", {"body": "Shell", "method": "Planar"})],
                        [("hex", "sketch", "plate", "add"), ("slot", "sketch", "plate", "add"),
                         ("plate", "sketch", "p", "profile")], [f"len({PART}.edges)"], [])
    holes = [("hole", "model.sketch", {"shape": "Circle", "x": 30, "y": 25, "radius": 10}),
             ("pin", "model.sketch", {"shape": "Circle", "x": 70, "y": 25, "radius": 5}),
             ("plate", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 20})]
    holes_edges = [("hole", "sketch", "plate", "add"), ("pin", "sketch", "plate", "add"),
                   ("plate", "sketch", "p", "profile")]
    for name, settings in BY_SHAPE_CASES.items():
        c[f"by_shape_{name}"] = (holes + [("r", "model.region", dict({"method": "By shape", "create": "Set",
                                                                        "name": "Found"}, **settings))],
                                 holes_edges + [("p", "part", "r", "source")],
                                 [f"len({PART}.sets['Found'].faces) + len({PART}.sets['Found'].edges)"], [])
    # the holes found By shape, listed: how many, each radius and center (faces: on the axis; edges: the circle's)
    c["by_shape_measures"] = (
        holes + [("r", "model.region", {"method": "By shape"}),
                 ("rim", "model.region", {"method": "By shape", "shape": "Circular edges", "radius": 5})]
        + [(q, "model.measure", {"quantity": q}) for q in ("Count", "Radius", "Center")]
        + [("rims", "model.measure", {"quantity": "Center"})],
        holes_edges + [("p", "part", "r", "source"), ("p", "part", "rim", "source"), ("rim", "region", "rims", "first")]
        + [("r", "region", q, "first") for q in ("Count", "Radius", "Center")], [], [MARKER])
    # a diameter typed as Radius: nothing matches, and CAE says which radii there are
    c["by_shape_wrong_radius"] = (
        holes + [("r", "model.region", {"method": "By shape", "shape": "Circular faces", "radius": 20,
                                        "members": "One per geometry"}),
                 ("rim", "model.region", {"method": "By shape", "shape": "Circular edges", "radius": 20}),
                 ("c", "model.measure", {"quantity": "Count"})],
        holes_edges + [("p", "part", "r", "source"), ("p", "part", "rim", "source"), ("rim", "region", "c", "first")],
        [], [])
    # the holes Measured on the part before a Remove faces deletes them (then meshed: the mesh must survive)
    c["measure_before_remove_faces"] = (
        holes + [("r", "model.region", {"method": "By shape"}),
                 ("count", "model.measure", {"quantity": "Count"}), ("area", "model.measure", {"quantity": "Area"}),
                 ("g", "model.feature", {"operation": "Remove faces", "name": "No holes"}),
                 ("mesh", "model.mesh", {"size": 10})],
        holes_edges + [("p", "part", "r", "source"), ("r", "region", "count", "first"),
                       ("r", "region", "area", "first"), ("p", "part", "g", "part"), ("r", "region", "g", "region"),
                       ("g", "part", "mesh", "target")],
        [f"len({PART}.elements) > 0", f"'__nocode_stage' not in {M}.parts.keys()"], [MARKER])
    # Material & Section, Mesh and a Part Feature side by side on the part, the Instance from the bare Part: one part
    c["side_by_side_edits"] = (
        holes + [("sec", "model.section", {}), ("mesh", "model.mesh", {"size": 5}),
                 ("small", "model.region", {"method": "By shape", "radius": 5, "match": "Up to"}),
                 ("f", "model.feature", {"operation": "Remove faces", "name": "Tidy"}),
                 ("i", "model.instance", {})],
        holes_edges + [("p", "part", "sec", "target"), ("p", "part", "mesh", "target"), ("p", "part", "f", "part"),
                       ("p", "part", "small", "source"), ("small", "region", "f", "region"),
                       ("p", "part", "i", "part")],
        [f"{M}.parts.keys() == ['Part-1']", f"len({PART}.sectionAssignments) == 1", f"len({PART}.elements) > 0",
         f"'Tidy' in {PART}.features.keys()", f"{M}.rootAssembly.instances['Part-1-1'].partName == 'Part-1'"], [])
    # Mesh -> Instance while a second Mesh seeds the holes' rims finer: one part, instanced, meshed with both
    c["instance_of_a_refined_mesh"] = (
        holes + [("mesh", "model.mesh", {"size": 10}),
                 ("rims", "model.region", {"method": "By shape", "shape": "Circular edges"}),
                 ("fine", "model.mesh", {"size": 1}), ("i", "model.instance", {})],
        holes_edges + [("p", "part", "mesh", "target"), ("mesh", "part", "rims", "source"),
                       ("rims", "region", "fine", "target"), ("mesh", "part", "i", "part")],
        [f"{M}.parts.keys() == ['Part-1']", f"len({PART}.elements) > 0",
         f"{M}.rootAssembly.instances['Part-1-1'].partName == 'Part-1'"], [])
    # a material and a section the user keeps in a library model (SETUP), copied in by Material & Section
    lib = [("m", "model.model", {"name": "Work"}), ("s", "model.sketch", {}),
           ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
           ("sec", "model.section", {"section": "Own"}), ("mesh", "model.mesh", {"size": 25})]
    lib_edges = [("m", "model", "p", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
                 ("sec", "part", "mesh", "target"), ("pick", "value", "sec", "existing")]
    lib_part = "mdb.models['Work'].parts['Part-1']"
    c["library_material"] = (lib + [("pick", "input.value", {"type": "Material in CAE", "value": "Lib|Steel"})],
                             lib_edges, [f"{lib_part}.sectionAssignments[0].sectionName == 'Own'",
                                         "mdb.models['Work'].sections['Own'].material == 'Steel'",
                                         "mdb.models['Work'].materials['Steel'].elastic.table[0][0] == 123.0",
                                         "mdb.models['Lib'].materials['Steel'].elastic.table[0][0] == 123.0"], [])
    c["library_section"] = (lib + [("pick", "input.value", {"type": "Section in CAE", "value": "Lib|Plate"})],
                            lib_edges, [f"{lib_part}.sectionAssignments[0].sectionName == 'Plate'",
                                        "'Own' not in mdb.models['Work'].sections.keys()",
                                        "mdb.models['Work'].sections['Plate'].material == 'Steel'",
                                        "mdb.models['Work'].materials['Steel'].elastic.table[0][0] == 123.0",
                                        "'Plate' in mdb.models['Lib'].sections.keys()"], [])
    # a Region on a moved instance of a meshed part: the preview's mesh and highlight are where the instance is
    n, e = find("Face", "500, 25, 25", rid="end", source="i")
    c["instance_region_mesh"] = (BOX + [("mesh", "model.mesh", {"size": 10}), ("i", "model.instance", {"x": 500})] + n,
                                 BOX_EDGES + [("p", "part", "mesh", "target"), ("mesh", "part", "i", "part")] + e,
                                 [f"len({PART}.elements)"], [])
    # the holes' faces seed their own edges finer (Its edges) or, by default, the whole part as before
    for name, seed_on in (("face_edges_refined", "Its edges"), ("face_whole_part", "Whole part")):
        c[name] = (holes + [("mesh", "model.mesh", {"size": 10}),
                            ("bores", "model.region", {"method": "By shape"}),
                            ("fine", "model.mesh", {"size": 1, "seed_on": seed_on})],
                   holes_edges + [("p", "part", "mesh", "target"), ("mesh", "part", "bores", "source"),
                                  ("bores", "region", "fine", "target")],
                   [f"len({PART}.elements)"], [])
    # a pin: a cylinder of radius 5 with a conical tip; the cone (getRadius answers its mid radius) isn't a cylinder
    c["by_shape_not_cones"] = (
        [("s", "model.sketch", {"shape": "Profile", "closed": "Closed", "points": "0, 0; 5, 0; 5, 20; 0, 30"}),
         ("p", "model.part", {"body": "Solid", "method": "Revolve"}),
         ("r", "model.region", {"method": "By shape", "create": "Set", "name": "Found"})],
        [("s", "sketch", "p", "profile"), ("p", "part", "r", "source")],
        [f"len({PART}.sets['Found'].faces)", f"round({PART}.sets['Found'].faces[0].getRadius(), 6)"], [])
    # holes in a part that is only a mesh (as imported from an input file): found from the element faces, listed,
    # loaded (a surface of element faces) and fixed (the nodes of the flat end)
    mesh_part = [("mp", "input.value", {"type": "Part in CAE", "value": "Hand Model|Mesh Plate"}),
                 ("mi", "model.instance", {}),
                 ("holes", "model.region", {"method": "By shape", "radius": 5, "tolerance": 0.01, "create": "Surface",
                                            "name": "Holes"}),
                 ("end", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "-1, 0, 0"}),
                 ("anyhole", "model.region", {"method": "By shape"}),
                 ("mstep", "model.step", {"name": "Load"}),
                 ("press", "model.condition", {"name": "Bolts", "kind": "Pressure", "magnitude": 10}),
                 ("fixend", "model.condition", {"name": "End", "kind": "Fixed"})] + [
        (q, "model.measure", {"quantity": q}) for q in ("Count", "Radius", "Center")]
    mesh_edges = [("mp", "value", "mi", "part"), ("mi", "instance", "holes", "source"),
                  ("mi", "instance", "end", "source"), ("mi", "instance", "anyhole", "source"),
                  ("mstep", "step", "press", "step"), ("mstep", "step", "fixend", "step"),
                  ("holes", "region", "press", "region"), ("end", "region", "fixend", "region")] + [
        ("anyhole", "region", q, "first") for q in ("Count", "Radius", "Center")]
    hm = "mdb.models['Hand Model']"
    c["mesh_holes"] = (mesh_part, mesh_edges,
                       [f"len({hm}.rootAssembly.surfaces['Holes'].nodes)", f"{hm}.loads.keys()",
                        f"'End' in {hm}.boundaryConditions.keys()"], [MARKER])
    n, e = find("Face", "50, 25, 50; 0, 25, 25")
    n[-1] = ("r", "model.region", {"method": "Find at", "kind": "Face", "create": "Set", "name": "Found"})
    c["find_at_set"] = (BOX + n, BOX_EDGES + e, [f"len({PART}.sets['Found'].faces)"], [])
    n, e = find("Face", "50, 25, 50")
    n[-1] = ("r", "model.region", {"method": "Find at", "kind": "Face", "create": "Surface", "name": "Found"})
    c["find_at_surface"] = (BOX + n, BOX_EDGES + e, [f"len({PART}.surfaces['Found'].faces)"], [])
    c["partition_then_mesh"] = (
        BOX + [("d", "model.datum", {"kind": "Principal plane", "plane": "YZ", "offset": 50}),
               ("f", "model.feature", {"operation": "Partition"}), ("sec", "model.section", {}),
               ("mesh", "model.mesh", {"size": 10}), ("i", "model.instance", {})],
        BOX_EDGES + [("p", "part", "d", "part"), ("p", "part", "f", "part"), ("d", "datum", "f", "datum"),
                     ("f", "part", "sec", "target"), ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part")],
        [f"len({M}.parts.keys())", f"len({PART}.cells)", f"len({PART}.elements)",
         f"len({PART}.sectionAssignments)", f"{PART}.getMeshStats(regions={PART}.cells).numHexElems"], [])
    c["by_shape_fillet"] = (holes + [("r", "model.region", {"method": "By shape", "shape": "Circular edges",
                                                              "radius": 5}),
                                     ("f", "model.feature", {"operation": "Fillet", "radius": 1})],
                            holes_edges + [("p", "part", "r", "source"), ("p", "part", "f", "part"),
                                           ("r", "region", "f", "region")],
                            [f"round({PART}.getVolume(), 1)"], [])
    # Set Operation of Regions on the box: every face (Small faces) with the top (By shape) and the +x face (Find at)
    every = ("all", "model.region", {"method": "By shape", "shape": "Small faces", "area": 1e6})
    top = ("top", "model.region", {"method": "By shape", "shape": "Flat faces", "facing": "0, 0, 1"})
    side_n, side_e = find("Face", "100, 25, 25", rid="side")
    for op, a, b in REGION_BOOLEANS:
        c[f"region_{op.lower()}"] = (
            BOX + [every, top] + side_n + [("op", "set.op", {"op": op}),
                                           ("r", "model.region", {"create": "Set", "name": "Found"})],
            BOX_EDGES + side_e + [("p", "part", "all", "source"), ("p", "part", "top", "source"),
                                  (a, "region", "op", "a"), (b, "region", "op", "b"), ("op", "result", "r", "source")],
            [f"len({PART}.sets['Found'].faces)"], [])
    # loaded through an instance: pressure on every face but the top
    c["region_boolean_load"] = (
        BOX + [every, top, ("i", "model.instance", {}), ("op", "set.op", {"op": "Difference"}),
               ("st", "model.step", {"name": "Press"}),
               ("push", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 1})],
        BOX_EDGES + [("p", "part", "i", "part"), ("i", "instance", "all", "source"), ("i", "instance", "top", "source"),
                     ("all", "region", "op", "a"), ("top", "region", "op", "b"), ("st", "step", "push", "step"),
                     ("op", "result", "push", "region")],
        [f"'Push' in {M}.loads.keys()"], [])
    nodes, edges = study(tmp)
    c["study_models"] = (nodes, edges, [f"sorted(k for k in mdb.models.keys() if k.startswith('Bar-'))",
                                        "mdb.models['Bar-100'].parts.keys()", "mdb.models['Bar-100'].steps.keys()"]
                         + [f"__import__('os').path.exists({str(tmp / f'Bar-{i}.inp')!r})" for i in (1, 2, 3)], [])
    nodes, edges = amplitudes_bar(tmp)
    am = "mdb.models['Amps']"
    c["every_amplitude"] = (nodes, edges, [f"type({am}.amplitudes['Push{k}-Amp']).__name__ == {cls!r}"
                                           for k, cls in enumerate(AMPLITUDE_CLASSES)] + [
        f"str({am}.amplitudes['Push1-Amp'].timeSpan) == 'TOTAL'", f"{am}.amplitudes['Push3-Amp'].a_0 == 0.5",
        f"open({str(tmp / 'Amps.inp')!r}).read().count('*Amplitude') == {len(AMPLITUDES)}"], [])
    # an input file opened as a graph (Open CAE model; the dump CAE gave reading it): its sets and surfaces by mesh
    # label, sections on element sets, loads on node sets and an element surface
    opened = payload(graph_from_dump(recorded_dump())["graph"])
    for n in opened["nodes"]:
        if n["type"] == "model.job":
            n["params"]["directory"] = str(tmp)
    pa = "mdb.models['plate_assembly']"
    c["inp_rebuilt"] = (
        [(n["id"], n["type"], n["params"]) for n in opened["nodes"]],
        [(e["source"], e["sourceHandle"], e["target"], e["targetHandle"]) for e in opened["edges"]],
        [f"{pa}.parts.keys()", f"sorted((k, len(s.nodes), len(s.elements)) for k, s in {pa}.parts['PLATE'].sets.items())",
         f"[(k, len(s.elements), sorted(set(str(x) for x in s.sides))) for k, s in {pa}.parts['PLATE'].surfaces.items()]",
         f"sorted((k, len(s.nodes)) for k, s in {pa}.rootAssembly.sets.items())",
         f"[(a.sectionName, sorted(e.label for e in a.getSet().elements)) for a in {pa}.parts['PLATE'].sectionAssignments]",
         f"sorted({pa}.loads.keys()) + sorted({pa}.boundaryConditions.keys())",
         f"__import__('os').path.exists({str(tmp / 'plate_assembly.inp')!r})"], [])
    return c


SHAPES = {
    "polygon": {"shape": "Polygon", "sides": 6, "radius": 10, "rotation": 15},
    "ellipse": {"shape": "Ellipse", "width": 40, "height": 20, "rotation": 30},
    "slot": {"shape": "Slot", "width": 20, "height": 60, "rotation": 10},
    "rounded": {"shape": "Rectangle", "corner": 10},
}
SHAPE_AREAS = {"polygon": 3 * 3 ** 0.5 / 2 * 100, "ellipse": 3.14159265 * 200, "slot": 800 + 3.14159265 * 100,
               "rounded": 5000 - (4 - 3.14159265) * 100}
# a 100 x 50 x 20 plate with holes of radius 10 and 5: what each By shape Region finds
BY_SHAPE_CASES = {"cylinder_10": {"shape": "Cylindrical faces", "radius": 10, "tolerance": 0.01},
                  "cylinders": {"shape": "Cylindrical faces"},
                  "circles_5": {"shape": "Circular edges", "radius": 5},
                  "top": {"shape": "Flat faces", "facing": "0, 0, 1"}}
REGION_BOOLEANS = (("Union", "top", "side"), ("Intersection", "all", "top"), ("Difference", "all", "top"))
REGION_BOOLEANS_FOUND = {"union": 2, "intersection": 1, "difference": 5}
BY_SHAPE_FOUND = {"cylinder_10": 1, "cylinders": 2, "circles_5": 2, "top": 1}
# what the user has in CAE before a case runs
LIBRARY = """
from abaqus import *
from abaqusConstants import *
import material, section
m = mdb.Model(name='Lib')
m.Material(name='Steel').Elastic(table=((123.0, 0.3),))
m.HomogeneousSolidSection(name='Plate', material='Steel', thickness=None)
"""
SETUP = {"library_material": LIBRARY, "library_section": LIBRARY, "mesh_holes": """
from abaqus import *
from abaqusConstants import *
import part, mesh
m = mdb.Model(name='Hand Model')
s = m.ConstrainedSketch(name='plate', sheetSize=300)
s.rectangle(point1=(0, 0), point2=(100, 50))
for x, r in ((25, 5), (50, 8), (75, 5)):
    s.CircleByCenterPerimeter(center=(x, 25), point1=(x + r, 25))
p = m.Part(name='Plate', dimensionality=THREE_D, type=DEFORMABLE_BODY)
p.BaseSolidExtrude(sketch=s, depth=10)
p.seedPart(size=2.0)
p.generateMesh()
p.PartFromMesh(name='Mesh Plate')
del m.parts['Plate']
"""}
LENGTHS = (50, 100, 150)
PLASTIC = "0, 250; 0.1, 350"
# Up to 1, down to half by the end; 1 + Y / 10 across the end face averages 1.5: total 0.5 * 1.5 * 100 mm2.
VARYING = {"amplitude": "0, 0; 0.5, 1; 1, 0.5", "distribution": "1 + Y / 10"}  # plastic strain, yield stress: 1000 MPa of linear hardening


AMPLITUDES = [{"kind": "Tabular", "points": "0, 0; 1, 1", "smoothing": "0.1"},
              {"kind": "Smooth step", "points": "0, 0; 1, 1", "time_span": "Total time"},
              {"kind": "Equally spaced", "values": "0, 1, 0.5", "interval": 0.5},
              {"kind": "Periodic", "frequency": 6.28, "terms": "0, 1; 0.2, 0", "initial": 0.5},
              {"kind": "Modulated", "initial": 1, "magnitude": 0.5, "frequency1": 3, "frequency2": 30},
              {"kind": "Exponential decay", "initial": 0, "magnitude": 1, "decay": 0.2},
              {"kind": "Solution dependent", "factor": 1, "minimum": 0.5, "maximum": 10},
              {"kind": "User", "variables": 1}]
AMPLITUDE_CLASSES = ["TabularAmplitude", "SmoothStepAmplitude", "EquallySpacedAmplitude", "PeriodicAmplitude",
                     "ModulatedAmplitude", "DecayAmplitude", "SolutionDependentAmplitude", "UserAmplitude"]


def amplitudes_bar(tmp: Path, kinds=None):
    """The bar with one pressure on its end per Amplitude type (Push0, Push1…), each following its own amplitude."""
    nodes, edges = pipeline(tmp, "Amps", push={})  # the reaction RF1 is read back
    for k, settings in enumerate(AMPLITUDES):
        if kinds is not None and settings["kind"] not in kinds:
            continue
        nodes += [(f"amp{k}", "model.amplitude", settings),
                  (f"pr{k}", "model.condition", {"name": f"Push{k}", "kind": "Pressure", "magnitude": 0.1})]
        edges += [("st", "step", f"pr{k}", "step"), ("end", "region", f"pr{k}", "region"),
                  (f"amp{k}", "amplitude", f"pr{k}", "amplitude")]
    return nodes, edges


def flawed(tmp: Path):
    """A model with what Abaqus' data check refuses: a part placed without a section, no density in a dynamic step
    with gravity."""
    nodes = [("m", "model.model", {"name": "Flawed"}), ("s", "model.sketch", {}),
             ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
             ("q", "model.part", {"name": "Plain", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {"material": "Steel"}), ("mesh", "model.mesh", {"size": 25}),
             ("qmesh", "model.mesh", {"size": 25}), ("i", "model.instance", {}),
             ("bare", "model.instance", {"name": "Bare", "z": 100}),
             ("st", "model.step", {"name": "Shake", "kind": "Dynamic implicit"}),
             ("g", "model.condition", {"name": "Weight", "kind": "Gravity", "z": -9810}),
             ("j", "model.job", {"name": "Flawed", "directory": str(tmp), "action": "Write input only"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("s", "sketch", "q", "profile"),
             ("p", "part", "sec", "target"), ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"),
             ("q", "part", "qmesh", "target"), ("qmesh", "part", "bare", "part"), ("m", "model", "st", "after"),
             ("st", "step", "g", "step"), ("g", "condition", "j", "analysis")]
    return nodes, edges


def study(tmp: Path):
    """A parameter study from plain list mapping: one model per bar length (names from Format Text), each with
    its own fixed end and 1 MPa end pressure, one job each; then each ODB's min U1 against the lengths."""
    nodes = [("L", "input.value", {"type": "List / matrix", "value": str(list(LENGTHS))}),
             ("names", "text.format", {"template": "Bar-{a:g}"}),
             ("m", "model.model", {}),
             ("s", "model.sketch", {"height": 10}),
             ("p", "model.part", {"name": "Bar", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {}),
             ("mesh", "model.mesh", {"size": 5, "element": "C3D8R"}),
             ("i", "model.instance", {}),
             ("st", "model.step", {"name": "Load"}),
             ("bc", "model.condition", {"name": "Fix", "kind": "Fixed"}),
             ("pr", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 1.0}),
             ("j", "model.job", {"name": "Bar", "directory": str(tmp), "action": "Write input only"}),
             ("fixpts", "input.value", {"type": "Text", "value": "0, 5, 5"}),
             ("fix", "model.region", {"method": "Find at", "kind": "Face"}),
             ("endx", "text.format", {"template": "{a:g}, 5, 5"}),
             ("end", "model.region", {"method": "Find at", "kind": "Face"}),
             ("odb", "odb.open", {}),
             ("u", "odb.field", {"variable": "U", "quantity": "U1"}),
             ("r", "math.reduce", {"op": "Min"}),
             ("curve", "series.make", {"name": "End displacement"})]
    edges = [("L", "value", "names", "a"), ("names", "text", "m", "param:name"),
             ("m", "model", "s", "model"), ("L", "value", "s", "param:width"),
             ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("st", "step", "bc", "step"), ("bc", "condition", "pr", "step"), ("pr", "condition", "j", "analysis"),
             ("i", "instance", "fix", "source"), ("fixpts", "value", "fix", "points"), ("fix", "region", "bc", "region"),
             ("L", "value", "endx", "a"), ("i", "instance", "end", "source"), ("endx", "text", "end", "points"),
             ("end", "region", "pr", "region"),
             ("j", "path", "odb", "path"), ("odb", "odb", "u", "odb"), ("u", "field", "r", "field"),
             ("r", "value", "curve", "y"), ("L", "value", "curve", "x")]
    return nodes, edges


def pipeline(tmp: Path, job: str = "Beam", plastic: str = "", pressure: float = 1.0, peeq: bool = False,
             push: dict | None = None):
    """A 100 x 10 x 10 bar, fixed at x = 0, pressure on the x = 100 end; solved, then U1 (or PEEQ) read back.
    push: more Pressure settings (amplitude, distribution); the total reaction RF1 is read back instead."""
    nodes = [("m", "model.model", {"name": job}),
             ("s", "model.sketch", {"width": 100, "height": 10}),
             ("p", "model.part", {"name": "Bar", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {"plastic": plastic}),
             ("mesh", "model.mesh", {"size": 5, "element": "C3D8R"}),
             ("i", "model.instance", {}),
             ("st", "model.step", {"name": "Load", "field": "U, RF, S, PEEQ", "intervals": 4, "history": "RF1"}),
             ("bc", "model.condition", {"name": "Fix", "kind": "Fixed"}),
             ("pr", "model.condition", dict({"name": "Push", "kind": "Pressure", "magnitude": pressure}, **(push or {}))),
             ("j", "model.job", {"name": job, "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}),
             ("u", "odb.field", {"variable": "PEEQ"} if peeq else {"variable": "RF", "quantity": "RF1"} if push is not None else
              {"variable": "U", "quantity": "U1"}),
             ("r", "math.reduce", {"op": "Max" if peeq else "Sum" if push is not None else "Min"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("st", "step", "bc", "step"), ("bc", "condition", "pr", "step"), ("pr", "condition", "j", "analysis"),
             ("j", "path", "odb", "path"), ("odb", "odb", "u", "odb"), ("u", "field", "r", "field")]
    for rid, pts, cond in (("fix", "0, 5, 5", "bc"), ("end", "100, 5, 5", "pr")):
        n, e = find("Face", pts, rid=rid, source="i")
        nodes += n
        edges += e + [(rid, "region", cond, "region")]
    edges.append(("fix", "region", "st", "history_region"))  # reaction forces where it's held
    return nodes, edges


def flat_bar(tmp: Path, space: str):
    """A bar pulled by 10 MPa in 2D: plane stress (100 x 20, 2 thick, CPS4R) held at x = 0 (U1) and its corner (U2),
    or axisymmetric (radius 10, 100 long along y, CAX4R) held at y = 0 (U2). Its end moves 10 * 100 / E = 0.005;
    the reactions total 10 * 20 * 2 = 400 (plane stress) or 10 * π 10² = 3141.6 (axisymmetric, the whole round)."""
    plane = space == "2D planar"
    job = "Plane" if plane else "Axi"
    nodes = [("m", "model.model", {"name": job}),
             ("s", "model.sketch", {"width": 100 if plane else 10, "height": 20 if plane else 100}),
             ("p", "model.part", {"name": "Bar", "space": space, "body": "Shell", "method": "Planar"}),
             ("sec", "model.section", {"young": 200000, "plane_thickness": 2}),
             ("mesh", "model.mesh", {"size": 5, "element": "CPS4R" if plane else "CAX4R"}),
             ("i", "model.instance", {}),
             ("st", "model.step", {"name": "Pull", "field": "U, RF, S"}),
             ("bc", "model.condition", {"name": "Hold", "kind": "Displacement / rotation",
                                        "u1" if plane else "u2": "0"}),
             ("pr", "model.condition", {"name": "Pull", "kind": "Pressure", "magnitude": -10}),
             ("j", "model.job", {"name": job, "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}),
             ("u", "odb.field", {"variable": "U", "quantity": "U1" if plane else "U2"}),
             ("r", "math.reduce", {"op": "Max"}),
             ("rf", "odb.field", {"variable": "RF", "quantity": "RF1" if plane else "RF2"}),
             ("total", "math.reduce", {"op": "Sum"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("st", "step", "bc", "step"), ("bc", "condition", "pr", "step"), ("pr", "condition", "j", "analysis"),
             ("j", "path", "odb", "path"), ("odb", "odb", "u", "odb"), ("u", "field", "r", "field"),
             ("odb", "odb", "rf", "odb"), ("rf", "field", "total", "field")]
    held, pulled = ("0, 10, 0", "100, 10, 0") if plane else ("5, 0, 0", "5, 100, 0")
    for rid, pts, cond in (("fix", held, "bc"), ("end", pulled, "pr")):
        n, e = find("Edge", pts, rid=rid, source="i")
        nodes += n
        edges += e + [(rid, "region", cond, "region")]
    if plane:  # the corner held in y too, so it can't slide sideways
        n, e = find("Vertex", "0, 0, 0", rid="corner", source="i")
        nodes += n + [("pin", "model.condition", {"name": "Pin", "kind": "Displacement / rotation", "u2": "0"})]
        edges += e + [("corner", "region", "pin", "region"), ("bc", "condition", "pin", "step"),
                      ("pin", "condition", "pr", "step")]
        edges.remove(("bc", "condition", "pr", "step"))
    return nodes, edges


def rigid_press(tmp: Path):
    """A 20 x 10 plane-stress block (1 thick) held at its bottom (U2) and corner (U1), squeezed 0.01 by an
    analytical rigid line pushed down through its reference point, in frictionless contact with its top: the push
    is E ε A = 200000 * 0.001 * 20 = 4000: the reference point's RF2 is -4000, the floor's 400 at each inner node."""
    nodes = [("m", "model.model", {"name": "Press"}),
             ("s", "model.sketch", {"name": "Block", "width": 20, "height": 10}),
             ("p", "model.part", {"name": "Block", "space": "2D planar", "body": "Shell", "method": "Planar"}),
             ("sec", "model.section", {"young": 200000, "poisson": 0.0}),
             ("mesh", "model.mesh", {"size": 2, "element": "CPS4R"}),
             ("i", "model.instance", {}),
             ("ts", "model.sketch", {"name": "Line", "shape": "Profile", "points": "-5, 10; 25, 10"}),
             ("tp", "model.part", {"name": "Tool", "space": "2D planar", "type": "Analytical rigid", "body": "Wire",
                                   "method": "Planar"}),
             ("rp", "model.feature", {"operation": "Reference point", "name": "Grip", "point": "10, 15, 0"}),
             ("ti", "model.instance", {}),
             ("st", "model.step", {"name": "Squeeze", "field": "U, RF, S"}),
             ("bc", "model.condition", {"name": "Floor", "kind": "Displacement / rotation", "u2": "0"}),
             ("pin", "model.condition", {"name": "Pin", "kind": "Displacement / rotation", "u1": "0"}),
             ("push", "model.condition", {"name": "Push", "kind": "Displacement / rotation", "u1": "0",
                                          "u2": "-0.01", "ur3": "0"}),
             ("touch", "model.interaction", {"name": "Touch", "kind": "Frictionless contact", "sliding": "Small"}),
             ("j", "model.job", {"name": "Press", "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}), ("rf", "odb.field", {"variable": "RF", "quantity": "RF2"}),
             ("r", "math.reduce", {"op": "Min"}), ("floor_rf", "math.reduce", {"op": "Max"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "ts", "model"),
             ("ts", "sketch", "tp", "profile"), ("tp", "part", "rp", "part"), ("rp", "part", "ti", "part"),
             ("m", "model", "st", "after"), ("st", "step", "bc", "step"), ("bc", "condition", "pin", "step"),
             ("pin", "condition", "push", "step"), ("rp", "point", "push", "region"), ("st", "step", "touch", "step"),
             ("push", "condition", "j", "analysis"),
             ("j", "path", "odb", "path"), ("odb", "odb", "rf", "odb"), ("rf", "field", "r", "field"),
             ("rf", "field", "floor_rf", "field")]
    for rid, kind, pts, source, target in (("floor", "Edge", "10, 0, 0", "i", ("bc", "region")),
                                           ("corner", "Vertex", "0, 0, 0", "i", ("pin", "region")),
                                           ("top", "Edge", "10, 10, 0", "i", ("touch", "slave")),
                                           ("line", "Edge", "10, 10, 0", "ti", ("touch", "master"))):
        n, e = find(kind, pts, rid=rid, source=source)
        nodes += n
        edges += [(a, b if a != source else "instance", c, d) for a, b, c, d in e] + [(rid, "region", *target)]
    # the line is drawn left to right: its normal points up, away from the block; the surface's other side faces it
    nodes = [(i, t, dict(p, create="Surface", name="ToolFace", side="Side 2")) if i == "line" else (i, t, p)
             for i, t, p in nodes]
    return nodes, edges


def buckled_column(tmp: Path):
    """A 1000 long steel column (radius 10 circle, B31), clamped at its foot and pushed down by 1 at its head:
    the first buckling load factor is Euler's π² E I / (2 L)² = 3875.8."""
    nodes = [("m", "model.model", {"name": "Column"}),
             ("s", "model.sketch", {"shape": "Profile", "points": "0, 0; 0, 1000"}),
             ("p", "model.part", {"name": "Column", "body": "Wire"}),
             ("sec", "model.section", {"young": 200000, "kind": "Beam", "profile": "Circle", "radius": 10}),
             ("mesh", "model.mesh", {"size": 50, "element": "B31"}), ("i", "model.instance", {}),
             ("st", "model.step", {"name": "Buckle", "kind": "Buckling", "modes": 2}),
             ("bc", "model.condition", {"name": "Foot", "kind": "Fixed"}),
             ("push", "model.condition", {"name": "Push", "kind": "Concentrated force", "x": 0, "y": -1, "z": 0}),
             ("j", "model.job", {"name": "Column", "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}), ("tl", "odb.timeline", {"first": 1}), ("r", "math.reduce", {"op": "Min"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("st", "step", "bc", "step"), ("bc", "condition", "push", "step"), ("push", "condition", "j", "analysis"),
             ("j", "path", "odb", "path"), ("odb", "odb", "tl", "odb"), ("tl", "times", "r", "field")]
    for rid, pts, cond in (("foot", "0, 0, 0", "bc"), ("head", "0, 1000, 0", "push")):
        n, e = find("Vertex", pts, rid=rid, source="i")
        nodes += n
        edges += e + [(rid, "region", cond, "region")]
    return nodes, edges


def riks_bar(tmp: Path):
    """The pipeline bar loaded by Static Riks up to load factor 2: its end moves about twice the linear -1/2100."""
    nodes, edges = pipeline(tmp, "Arc")
    nodes = [(i, t, {"name": "Load", "kind": "Static Riks", "max_load_factor": 2, "initial": 0.25, "maximum": 0.5,
                     "increments": 50, "time": 10, "field": "U, RF"}) if i == "st" else (i, t, p)
             for i, t, p in nodes]
    edges = [e for e in edges if e[3] != "history_region"]
    return nodes, edges


def heated_bar(tmp: Path, how: str):
    """A 100 x 10 x 10 bar (conductivity 0.05) held at 0° at x = 0. how = "flux": 0.5 flows in at x = 100, which
    reaches q L / k = 1000°; "film": 100° at x = 0 and convection to 0° (h = 5e-4 = k / L) at x = 100, which sits
    at 50°. "coupled": heated to 100° throughout from 0°, held only by X, Y and Z symmetry on its three faces at
    the origin; it grows α ΔT L = 1e-5 * 100 * 100 = 0.1 along x (coupled temperature-displacement, C3D8T)."""
    job = {"flux": "Heat", "film": "Film", "coupled": "Expand"}[how]
    coupled = how == "coupled"
    nodes = [("m", "model.model", {"name": job}),
             ("s", "model.sketch", {"width": 100, "height": 10}),
             ("p", "model.part", {"name": "Bar", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {"young": 200000, "conductivity": 0.05, "expansion": 1e-5}),
             ("mesh", "model.mesh", {"size": 5, "element": "C3D8T" if coupled else "DC3D8"}),
             ("i", "model.instance", {}),
             ("st", "model.step", {"name": "Heat", "kind": "Coupled temperature-displacement" if coupled else
                                   "Heat transfer", "field": "NT, U" if coupled else "NT, HFL"}),
             ("j", "model.job", {"name": job, "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}),
             ("t", "odb.field", {"variable": "U", "quantity": "U1"} if coupled else {"variable": "NT11"}),
             ("r", "math.reduce", {"op": "Min" if how == "film" else "Max"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("j", "path", "odb", "path"), ("odb", "odb", "t", "odb"), ("t", "field", "r", "field")]
    if coupled:
        conditions = [("initial", "model.condition", {"name": "Cold", "kind": "Temperature", "temperature": 0},
                       "Cell", "50, 5, 5"),
                      ("hot", "model.condition", {"name": "Hot", "kind": "Fixed temperature", "temperature": 100},
                       "Cell", "50, 5, 5"),
                      ("sx", "model.condition", {"name": "SymX", "kind": "Fixed", "fix": "X symmetry"}, "Face",
                       "0, 5, 5"),
                      ("sy", "model.condition", {"name": "SymY", "kind": "Fixed", "fix": "Y symmetry"}, "Face",
                       "50, 0, 5"),
                      ("sz", "model.condition", {"name": "SymZ", "kind": "Fixed", "fix": "Z symmetry"}, "Face",
                       "50, 5, 0")]
    else:
        conditions = [("cold", "model.condition", {"name": "Held", "kind": "Fixed temperature",
                                                   "temperature": 0 if how == "flux" else 100}, "Face", "0, 5, 5"),
                      ("end", "model.condition", {"name": "In", "kind": "Heat flux", "flux": 0.5} if how == "flux" else
                       {"name": "Air", "kind": "Convection", "film": 5e-4, "sink": 0}, "Face", "100, 5, 5")]
    previous = ("st", "step")
    for cid, kind, params, region_kind, pts in conditions:
        n, e = find(region_kind, pts, rid=cid + "_at", source="i")
        nodes += n + [(cid, kind, params)]
        edges += e + [(cid + "_at", "region", cid, "region"), (previous[0], previous[1], cid, "step")]
        previous = (cid, "condition")
    edges.append((previous[0], previous[1], "j", "analysis"))
    return nodes, edges


def turned_pull(tmp: Path):
    """The coupled pull, in a coordinate system turned 90° about z (its 1-axis along global y): 1000 along its
    1-axis pulls along y, so the reactions total -1000 in y."""
    nodes, edges = coupled(tmp, "Turned")
    nodes += [("cpts", "input.value", {"type": "Text", "value": "0, 0, 0; 0, 1, 0; -1, 0, 0"}),
              ("cs", "model.datum", {"name": "Turned", "kind": "Coordinate system"})]
    edges += [("m", "model", "cs", "part"), ("cpts", "value", "cs", "points"), ("cs", "datum", "pull", "csys")]
    nodes = [(i, t, {"variable": "RF", "quantity": "RF2"}) if i == "u" else (i, t, p) for i, t, p in nodes]
    return nodes, edges


def sprung_mass(tmp: Path):
    """A small box made a rigid body about a reference point at the origin, carrying a point mass of 1 with rotary
    inertia 1, on a bushing to ground of stiffness 1000 in all six directions: all six natural frequencies are
    √(k / m) / 2π = 5.033 (the steel box itself, 7.85e-9, is all but massless)."""
    nodes = [("m", "model.model", {"name": "Sprung"}), ("s", "model.sketch", {"width": 1, "height": 1}),
             ("p", "model.part", {"name": "Box", "body": "Solid", "method": "Extrude", "depth": 1}),
             ("sec", "model.section", {"density": 7.85e-9}), ("mesh", "model.mesh", {"size": 1, "element": "C3D8I"}),
             ("i", "model.instance", {"x": -0.5, "y": -0.5, "z": -0.5}),
             ("st", "model.step", {"name": "Modes", "kind": "Frequency", "modes": 6}),
             ("rb", "model.interaction", {"name": "Lump", "kind": "Rigid body", "point": "0, 0, 0"}),
             ("pm", "model.interaction", {"name": "Payload", "kind": "Point mass", "mass": 1, "inertia": "1, 1, 1"}),
             ("sp", "model.interaction", {"name": "Mount", "kind": "Connector", "connector": "Bushing",
                                          "second": "Ground", "stiffness": "1000"}),
             ("j", "model.job", {"name": "Sprung", "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}), ("tl", "odb.timeline", {"first": 1}),
             ("lo", "math.reduce", {"op": "Min"}), ("hi", "math.reduce", {"op": "Max"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("m", "model", "st", "after"),
             ("st", "step", "rb", "step"), ("rb", "interaction", "pm", "master"), ("st", "step", "pm", "step"),
             ("rb", "interaction", "sp", "master"), ("st", "step", "sp", "step"),
             ("sp", "interaction", "j", "analysis"), ("j", "path", "odb", "path"), ("odb", "odb", "tl", "odb"),
             ("tl", "times", "lo", "field"), ("tl", "times", "hi", "field")]
    n, e = find("Cell", "0, 0, 0", rid="body", source="i")
    return nodes + n, edges + e + [("body", "region", "rb", "slave")]


def tied_bars(tmp: Path):
    """The pipeline bar and a twin 50 above it, held at x = 0: an equation makes the twin's end corner move in x
    opposite to the bar's (ratio -1): sampled at the two corners, the larger U1 is minus the smaller."""
    nodes, edges = pipeline(tmp, "Tied")
    nodes += [("i2", "model.instance", {"name": "Twin", "y": 50}),
              ("hold2", "model.condition", {"name": "Hold2", "kind": "Fixed"}),
              ("eq", "model.interaction", {"name": "Mirror", "kind": "Equation", "dof": 1, "ratio": -1}),
              ("corners", "field.path", {"mode": "At the points", "points": "100, 0, 0; 100, 50, 0",
                                         "method": "Nearest node"}),
              ("mn", "math.reduce", {"op": "Min"}), ("mx", "math.reduce", {"op": "Max"})]
    edges += [("mesh", "part", "i2", "part"), ("st", "step", "hold2", "step"), ("st", "step", "eq", "step"),
              ("u", "field", "corners", "field"), ("corners", "series", "mn", "field"),
              ("corners", "series", "mx", "field")]
    for rid, kind, pts, source, target in (("fix2", "Face", "0, 55, 5", "i2", ("hold2", "region")),
                                           ("a", "Vertex", "100, 0, 0", "i", ("eq", "master")),
                                           ("b", "Vertex", "100, 50, 0", "i2", ("eq", "slave"))):
        n, e = find(kind, pts, rid=rid, source=source)
        nodes += n
        edges += [(x, y if x != source else "instance", z, w) for x, y, z, w in e] + [(rid, "region", *target)]
    edges = [e for e in edges if e[3] != "history_region"]
    nodes = [(i, t, dict(p, history="")) if i == "st" else (i, t, p) for i, t, p in nodes]
    return nodes, edges


def explicit_bar(tmp: Path):
    """The bar in Abaqus/Explicit: 0.2 ms with mass scaling to 1e-6 s, the pressure ramped by a smooth step, general
    contact over the model and surface contact of its fixed end with a second, free bar 1 away (Explicit needs no
    support for it); U1 at the end is read back."""
    nodes, edges = pipeline(tmp, "Drop")
    swap = {"sec": ("sec", "model.section", {"density": 7.85e-9}),
            "st": ("st", "model.step", {"name": "Hit", "kind": "Dynamic explicit", "time": 2e-4, "nlgeom": "Yes",
                                        "mass_dt": 1e-6, "field": "U, RF, S", "intervals": 4, "history": "RF1"}),
            "pr": ("pr", "model.condition", {"name": "Push", "kind": "Pressure", "magnitude": 1,
                                             "amplitude": "0, 0; 0.0002, 1", "amplitude_shape": "Smooth step"})}
    nodes = [swap.get(n[0], n) for n in nodes] + [
        ("gc", "model.interaction", {"name": "All", "kind": "General contact", "friction": 0.1}),
        ("rub", "model.interaction", {"name": "Rub", "kind": "Frictional contact", "friction": 0.2}),
        ("i2", "model.instance", {"name": "Free", "x": -101})]
    n, e = find("Face", "-1, 5, 5", rid="near", source="i2")
    nodes += n
    edges += e + [("mesh", "part", "i2", "part"), ("st", "step", "gc", "step"), ("st", "step", "rub", "step"),
                  ("fix", "region", "rub", "master"), ("near", "region", "rub", "slave")]
    return nodes, edges


def stepwise_bar(tmp: Path):
    """The bar over three steps (Load, Hold, Release): its pressure doubled in Hold, off there and on again in
    Release with its own amplitude; gravity turned off in Hold; a temperature from Initial, raised in Hold and reset
    in Release; a displacement freed in Release; pair contact between its ends in Load and Release (a copy) with
    more friction there, and general contact frictionless from Release. Only the input file is written."""
    nodes, edges = pipeline(tmp, "Steps", push={"magnitude": "1; Release: 2", "active": "Load, Release",
                                                "amplitude": "0, 0; 1, 1; Release: 0, 1; 1, 0"})
    nodes = [(i, t, dict(p, density=7.85e-9)) if i == "sec" else (i, t, p) for i, t, p in nodes
             if i not in ("odb", "u", "r")] + [
        ("hold", "model.step", {"name": "Hold"}), ("release", "model.step", {"name": "Release"}),
        ("g", "model.condition", {"name": "Weight", "kind": "Gravity", "z": "-9810; Hold: 0"}),
        ("t", "model.condition", {"name": "Heat", "kind": "Temperature", "temperature": "20; Hold: 100",
                                  "active": "Initial to Hold"}),
        ("d", "model.condition", {"name": "Guide", "kind": "Displacement / rotation", "u3": "0; Release: Free",
                                  "active": "Initial to end"}),
        ("rub", "model.interaction", {"name": "Rub", "kind": "Frictional contact", "friction": "0.1; Release: 0.3",
                                      "active": "Load, Release", "adjust": "No"}),
        ("gc", "model.interaction", {"name": "All", "kind": "General contact", "friction": "0.2; Release: 0"})]
    edges = [e for e in edges if e[2] not in ("odb", "u", "r")] + [
        ("st", "step", "hold", "after"), ("hold", "step", "release", "after"), ("st", "step", "g", "step"),
        ("st", "step", "t", "step"), ("end", "region", "t", "region"), ("st", "step", "d", "step"),
        ("end", "region", "d", "region"), ("st", "step", "rub", "step"), ("fix", "region", "rub", "master"),
        ("end", "region", "rub", "slave"), ("st", "step", "gc", "step")]
    return nodes, edges


def coupled(tmp: Path, model: str = "Grip", **coupling):
    """The bar again, pulled by 1000 N through a kinematic coupling of its x = 100 end to a reference point at
    x = 110, with U1 history at that point; the total reaction RF1 is read back."""
    nodes, edges = pipeline(tmp, model, push={})
    nodes = [n for n in nodes if n[0] != "pr"] + [
        ("rbe", "model.interaction", dict({"name": "Grip", "kind": "Kinematic coupling", "point": "110, 5, 5"},
                                          **coupling)),
        ("pull", "model.condition", {"name": "Pull", "kind": "Concentrated force", "x": 1000, "y": 0, "z": 0})]
    nodes = [(i, t, dict(p, history="U1")) if i == "st" else (i, t, p) for i, t, p in nodes]
    edges = [e for e in edges if "pr" not in (e[0], e[2]) and e[3] != "history_region"]
    edges += [("end", "region", "rbe", "slave"), ("bc", "condition", "pull", "step"),
              ("rbe", "interaction", "pull", "region"), ("pull", "condition", "j", "analysis"),
              ("rbe", "interaction", "st", "history_region")]
    return nodes, edges


def connected(tmp: Path):
    """Two 100 x 10 x 10 bars, 20 apart along x, joined by a bushing (1000 per unit, all six) between couplings
    on their facing ends; the first fixed at x = 0, the second pulled by 1000 N through a coupling at its far end,
    general contact over the whole model. Reactions are read back."""
    nodes = [("m", "model.model", {"name": "Joint"}), ("s", "model.sketch", {"width": 100, "height": 10}),
             ("p", "model.part", {"name": "Bar", "body": "Solid", "method": "Extrude", "depth": 10}),
             ("sec", "model.section", {}), ("mesh", "model.mesh", {"size": 5, "element": "C3D8R"}),
             ("i", "model.instance", {}), ("i2", "model.instance", {"name": "Bar-2", "x": 120}),
             ("st", "model.step", {"name": "Load", "field": "U, RF", "history": "CTF1, CU1"}),
             ("bc", "model.condition", {"name": "Fix", "kind": "Fixed"}),
             ("near", "model.interaction", {"name": "Near", "kind": "Kinematic coupling", "point": "105, 5, 5"}),
             ("far", "model.interaction", {"name": "Far", "kind": "Kinematic coupling", "point": "115, 5, 5"}),
             ("pull_at", "model.interaction", {"name": "PullAt", "kind": "Kinematic coupling", "point": "230, 5, 5"}),
             ("joint", "model.interaction", {"name": "Joint", "kind": "Connector", "connector": "Bushing",
                                             "stiffness": "1000", "damping": "5"}),
             ("touch", "model.interaction", {"name": "Touch", "kind": "General contact", "friction": 0}),
             ("pull", "model.condition", {"name": "Pull", "kind": "Concentrated force", "x": 1000, "y": 0, "z": 0}),
             ("j", "model.job", {"name": "Joint", "directory": str(tmp), "action": "Write input only"}),
             ("odb", "odb.open", {}), ("u", "odb.field", {"variable": "RF", "quantity": "RF1"}),
             ("r", "math.reduce", {"op": "Sum"})]
    edges = [("m", "model", "s", "model"), ("s", "sketch", "p", "profile"), ("p", "part", "sec", "target"),
             ("sec", "part", "mesh", "target"), ("mesh", "part", "i", "part"), ("mesh", "part", "i2", "part"),
             ("m", "model", "st", "after"), ("st", "step", "bc", "step"), ("bc", "condition", "pull", "step"),
             ("near", "interaction", "joint", "master"), ("far", "interaction", "joint", "slave"),
             ("pull_at", "interaction", "pull", "region"), ("joint", "interaction", "st", "history_region"),
             ("st", "step", "touch", "step"), ("touch", "interaction", "j", "analysis"),
             ("pull", "condition", "j", "analysis"),
             ("j", "path", "odb", "path"), ("odb", "odb", "u", "odb"), ("u", "field", "r", "field")]
    for rid, pts, source, target in (("fix", "0, 5, 5", "i", ("bc", "region")),
                                     ("e1", "100, 5, 5", "i", ("near", "slave")),
                                     ("e2", "120, 5, 5", "i2", ("far", "slave")),
                                     ("e3", "220, 5, 5", "i2", ("pull_at", "slave"))):
        n, e = find("Face", pts, rid=rid, source=source)
        nodes += n
        edges += [(a, b if a != source else "instance", c, d) for a, b, c, d in e] + [(rid, "region", *target)]
    return nodes, edges


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("real_abaqus")
    for fmt in ("stp", "sat"):  # Part checks the file exists; the CAE session writes the real geometry first
        (tmp / f"box.{fmt}").write_text("")
    payload = []
    for name, (nodes, edges, checks, _texts) in cases(tmp).items():
        g = graph(nodes, edges)
        res, inputs = Engine().evaluate(g)
        model_nodes = [n["id"] for n in g["nodes"] if n["type"].startswith(("model.", "input."))]
        bad = {k: res[k]["message"] for k in model_nodes if res[k]["status"] != "ok"}
        assert not bad, (name, bad)
        payload.append({"name": name, "script": build_script(g, res, inputs)["script"], "checks": checks,
                        "setup": SETUP.get(name, "")})
    (tmp / "cases.json").write_text(json.dumps(payload))
    (tmp / "driver.py").write_text(DRIVER)
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=driver.py"], cwd=tmp, capture_output=True, text=True,
                          errors="replace", timeout=900)
    report = tmp / "report.json"
    assert report.exists(), proc.stdout[-3000:] + proc.stderr[-3000:]
    return tmp, json.loads(report.read_text())


with tempfile.TemporaryDirectory() as _d:  # cases() writes its input files: not into the current folder
    CASE_NAMES = list(cases(Path(_d)))


@pytest.mark.parametrize("name", CASE_NAMES)
def test_case_builds_in_real_cae(session, name):
    tmp, report = session
    r = report[name]
    _, _, _, texts = cases(tmp)[name]
    assert r["error"] is None, r["error"]
    failed = [ln for ln in r["output"].splitlines() if " failed: " in ln]  # the editor turns these into errors
    if name == "by_shape_wrong_radius":  # the miss is reported (what it says: the test below)
        assert failed, r["output"]
    elif name != "find_at_miss_is_reported":
        assert not failed, r["output"]
    else:  # the feature fails; showing its Region afterwards is not another error
        assert len(failed) == 1 and "Fillet-1 failed" in failed[0], r["output"]
    for text in texts:
        assert text in r["output"], r["output"]
    for expr, value in r["checks"]:
        assert not value.startswith("error") and value not in ("False", "0"), (expr, value, r["output"])


def test_specific_results(session):
    _, report = session
    value = {name: dict(r["checks"]) for name, r in report.items()}
    assert value["datum_offset_partition"][f"len({PART}.cells)"] == "2"
    laminate = value["composite_shell_oriented"]
    assert laminate[f"len({M}.sections['Section-1'].layup)"] == "4"  # symmetric: typed once
    assert laminate[f"str({M}.materials['Material-1'].elastic.type)"] == "'LAMINA'"
    assert laminate[f"len({PART}.materialOrientations)"] == "1"
    assert value["arc_and_closed_spline"][f"len({PART}.faces)"] == "1"
    assert float(value["import_stp"][M + ".parts['Imp'].getVolume()"]) == pytest.approx(500)
    study = value["study_models"]
    assert study["sorted(k for k in mdb.models.keys() if k.startswith('Bar-'))"] == "['Bar-100', 'Bar-150', 'Bar-50']"
    assert study["mdb.models['Bar-100'].parts.keys()"] == "['Bar']"  # plain names inside each design's model
    assert study["mdb.models['Bar-100'].steps.keys()"] == "['Initial', 'Load']"


def test_mesh_controls_and_measures(session):
    """A 100 x 50 x 50 box seeded at 25, one 100-long edge graded into 10 elements: Measure reads CAE's counts."""
    _, report = session
    r = report["mesh_controls"]
    line = next(ln for ln in r["output"].splitlines() if ln.startswith(MARKER))
    answers = sorted(json.loads(line[len(MARKER):]).values())
    checks = dict(r["checks"])
    elements, nodes = int(checks[f"len({PART}.elements)"]), int(checks[f"len({PART}.nodes)"])
    assert elements > 4 * 2 * 2  # finer than the 25 seed alone: the graded edge forces 10 along it
    assert float(elements) in answers and float(nodes) in answers
    failed = min(answers)
    assert failed == 0
    assert any(1 < a < 50 for a in answers)  # the aspect ratios


def test_sketch_shapes_have_their_exact_areas(session):
    _, report = session
    for name, area in SHAPE_AREAS.items():
        assert float(dict(report[f"shape_{name}"]["checks"])[f"round({PART}.getVolume(), 3)"]) == pytest.approx(
            area, rel=1e-4), name
    # rounded plate (4 lines + 4 arcs), hexagon hole (6), slot hole (2 lines + 2 arcs)
    assert dict(report["shape_holes"]["checks"])[f"len({PART}.edges)"] == "18"


def test_region_by_shape_finds_what_it_should(session):
    _, report = session
    for name, count in BY_SHAPE_FOUND.items():
        assert dict(report[f"by_shape_{name}"]["checks"])[
            f"len({PART}.sets['Found'].faces) + len({PART}.sets['Found'].edges)"] == str(count), name
    # both edges of the r = 5 hole rounded by 1: the plate loses a little volume
    plate = 100 * 50 * 20 - 3.14159265 * (100 + 25) * 20
    volume = float(dict(report["by_shape_fillet"]["checks"])[f"round({PART}.getVolume(), 1)"])
    assert plate - 30 < volume < plate


def test_set_operation_of_regions_keeps_the_right_faces(session):
    _, report = session
    for name, count in REGION_BOOLEANS_FOUND.items():
        assert dict(report[f"region_{name}"]["checks"])[f"len({PART}.sets['Found'].faces)"] == str(count), name


def test_measure_lists_the_holes_found_by_shape(session):
    _, report = session
    r = report["by_shape_measures"]
    assert not r["error"], r["error"]
    line = next(ln for ln in r["output"].splitlines() if ln.startswith(MARKER))
    answers = sorted(json.loads(line[len(MARKER):]).values(), key=repr)
    assert 2.0 in answers  # Count
    assert sorted(next(a for a in answers if isinstance(a, list) and not isinstance(a[0], list))) == [5.0, 10.0]
    centers = sorted(sorted(tuple(round(c, 6) for c in pt) for pt in a)
                     for a in answers if isinstance(a, list) and isinstance(a[0], list))
    # the r = 5 hole's two circular edges (bottom and top), and both holes' faces (centroid: mid-depth)
    assert centers == [[(30.0, 25.0, 10.0), (70.0, 25.0, 10.0)], [(70.0, 25.0, 0.0), (70.0, 25.0, 20.0)]]


def test_by_shape_with_no_match_says_which_radii_there_are(session):
    _, report = session
    out = report["by_shape_wrong_radius"]["output"]
    assert "found no circular edges of radius equal to 20 (tolerance 0); the radii here are 5, 10" in out, out
    # the plate's two faces are flat but not discs (each has holes): no circular face at all
    assert "found no circular faces at all" in out, out


def test_measure_sees_the_part_as_it_was_before_remove_faces(session):
    _, report = session
    r = report["measure_before_remove_faces"]
    line = next(ln for ln in r["output"].splitlines() if ln.startswith(MARKER))
    answers = sorted(json.loads(line[len(MARKER):]).values())
    # both holes (r = 10 and 5, 20 deep): counted and their wall area, though Remove faces filled them
    assert answers[0] == 2.0
    assert answers[1] == pytest.approx(2 * 3.14159265 * (10 + 5) * 20, rel=1e-4)


def test_a_face_regions_edges_seeded_finer_refine_only_there(session):
    _, report = session
    fine = int(dict(report["face_edges_refined"]["checks"])[f"len({PART}.elements)"])
    whole = int(dict(report["face_whole_part"]["checks"])[f"len({PART}.elements)"])
    coarse = 100 * 50 * 20 / 10 ** 3  # the 10 seed alone: ~100 elements
    assert coarse < fine < whole, (fine, whole)  # finer around the holes, far fewer than seeding everything at 1


def _meshes(r):
    line = next((ln for ln in r["output"].splitlines() if ln.startswith(cae_mesh.MARKER)), None)
    return json.loads(line[len(cae_mesh.MARKER):]) if line else {}


def test_the_editor_gets_the_mesh_cae_generated(session):
    """The preview's mesh is the part's: as many elements, every face on known nodes, and the graded edge's 10
    element edges highlighted."""
    _, report = session
    r = report["mesh_controls"]
    meshes = _meshes(r)
    elements = int(dict(r["checks"])[f"len({PART}.elements)"])
    whole = [m for m in meshes.values() if "faces" in m and len(m["faces"]) > 20]
    assert whole and whole[0]["elements"] == elements, meshes.keys()
    assert all(str(n) in whole[0]["nodes"] for f in whole[0]["faces"] for n in f)
    assert {len(f) for f in whole[0]["faces"]} <= {3, 4}
    edge = next(m for m in meshes.values() if m.get("lines"))
    assert len(edge["lines"]) == 10 and len(edge["points"]) == 11
    # the holes' faces of the refined part: highlighted element faces, fewer than the whole surface
    faces = sorted((m for m in _meshes(report["face_edges_refined"]).values()), key=lambda m: len(m["faces"]))
    assert len(faces) == 2 and 0 < len(faces[0]["faces"]) < len(faces[1]["faces"])
    for name, r in report.items():  # no case's preview query fails
        # (but a Region of holes that Remove faces fills isn't on the meshed part: a note in the preview)
        assert not [m for m in _meshes(r).values() if "error" in m and "found no" not in m["error"]], name
        assert "Mesh preview failed" not in r["output"], name


def test_a_region_on_an_instance_is_highlighted_where_the_instance_is(session):
    _, report = session
    r = report["instance_region_mesh"]
    elements = int(dict(r["checks"])[f"len({PART}.elements)"])
    meshes = sorted(_meshes(r).values(), key=lambda m: len(m["faces"]))
    end = meshes[0]
    placed = {min(c[0] for c in m["nodes"].values()) for m in meshes[1:] if m["elements"] == elements}
    assert placed == {0, 500}  # the part's mesh, and the instance's where it was moved
    assert len(end["faces"]) == 5 * 5 and all(c[0] == 500 for c in end["nodes"].values())


def test_by_shape_skips_cones(session):
    _, report = session
    checks = dict(report["by_shape_not_cones"]["checks"])
    assert checks[f"len({PART}.sets['Found'].faces)"] == "1"
    assert checks[f"round({PART}.sets['Found'].faces[0].getRadius(), 6)"] == "5.0"


def test_holes_found_in_a_mesh_only_part(session):
    """A plate meshed then turned into a mesh-only part (no geometry), holes r = 5 at x = 25, 75 and r = 8 at 50."""
    _, report = session
    r = report["mesh_holes"]
    assert not r["error"], r["error"]
    assert "matched 2 cylinders" in r["output"], r["output"]  # the r = 5 holes, not the r = 8 one
    line = next(ln for ln in r["output"].splitlines() if ln.startswith(MARKER))
    answers = list(json.loads(line[len(MARKER):]).values())
    assert 3.0 in answers  # Count: every hole (radius 0: any)
    radii = next(a for a in answers if isinstance(a, list) and not isinstance(a[0], list))
    assert sorted(round(x, 3) for x in radii) == [5.0, 5.0, 8.0]
    centers = next(a for a in answers if isinstance(a, list) and isinstance(a[0], list))
    assert sorted(tuple(round(c, 3) for c in pt) for pt in centers) == [(25.0, 25.0, 5.0), (50.0, 25.0, 5.0),
                                                                         (75.0, 25.0, 5.0)]
    checks = dict(r["checks"])
    assert int(checks["len(mdb.models['Hand Model'].rootAssembly.surfaces['Holes'].nodes)"]) > 0
    assert "Bolts" in checks["mdb.models['Hand Model'].loads.keys()"]


def test_regions_on_a_part_go_on_each_of_its_instances(session):
    tmp, report = session
    r = report["regions_on_part"]
    assert not r["error"], r["error"]
    assert "applied on its 2 instances" in r["output"], r["output"]
    inp = eval(dict(r["checks"])[f"open({str(tmp / 'OnPart.inp')!r}).read()"])
    # the BC's nodes and the pressure's faces are on both instances
    for instance in ("Left", "Right"):
        assert f"instance={instance}" in inp, inp[:3000]
    assert "*Boundary" in inp and "*Dsload" in inp and "*Tie" in inp


def test_quadratic_and_incompatible_mode_elements(session):
    _, report = session
    checks = dict(report["elements"]["checks"])
    els = "mdb.models['Elems'].parts"
    assert [checks[f"str({els}['{p}'].elements[0].type)"].strip("'") for p in ("Q", "I", "Sh", "W")] == [
        "C3D20R", "C3D8I", "S8R", "B32"]


def test_the_bolted_joint_example_in_cae(session):
    tmp, report = session
    checks = dict(report["bolted_joint"]["checks"])
    jm = "mdb.models['BoltedJoint']"
    assert checks[f"sorted({jm}.constraints.keys())"] == "['Head', 'Nut']"
    assert "FIX_LENGTH" in checks[f"{jm}.steps['Pull'].loadStates['Pretension'].boltMethod"]
    assert checks[f"open({str(tmp / 'BoltedJoint.inp')!r}).read().count('*Pre-tension Section')"] == "1"


def test_bolt_load_pretension_then_fixed_length(session):
    tmp, report = session
    checks = dict(report["bolt_load"]["checks"])
    assert float(checks["mdb.models['Bolt'].steps['Preload'].loadStates['Pretension'].magnitude"]) == 5000.0
    assert "FIX_LENGTH" in checks["str(mdb.models['Bolt'].steps['Work'].loadStates['Pretension'].boltMethod)"]
    assert checks["'Pretension-Axis' in mdb.models['Bolt'].rootAssembly.features.keys()"] == "True"
    assert checks["'Pretension-Force' in mdb.models['Bolt'].historyOutputRequests.keys()"] == "True"
    assert checks[f"'*Pre-tension Section' in open({str(tmp / 'Bolted.inp')!r}).read()"] == "True"


def test_values_and_activity_change_by_step(session):
    tmp, report = session
    checks = dict(report["stepwise"]["checks"])
    sm = "mdb.models['Steps']"
    assert float(checks[f"{sm}.steps['Load'].loadStates['Push'].magnitude"]) == 1.0
    assert float(checks[f"{sm}.steps['Release'].loadStates['Push-from-Release'].magnitude"]) == 2.0
    # off in Hold: Push by Active in, Weight because it is 0 there
    hold = checks[f"[str(s.status) for n, s in {sm}.steps['Hold'].loadStates.items() if n in ('Push', 'Weight')] or 'none'"]
    assert hold == "'none'" or "PROPAGATED" not in hold and "CREATED" not in hold and "MODIFIED" not in hold, hold
    assert "Push-Amp-Release" in checks[f"str({sm}.steps['Release'].loadStates['Push-from-Release'].amplitude)"]
    assert "FREED" in checks[f"str({sm}.steps['Release'].boundaryConditionStates['Guide'].u3State)"]
    assert checks[f"sorted({sm}.interactions.keys())"] == "['All', 'Rub', 'Rub-from-Release']"
    inp = str(tmp / "Steps.inp")
    assert checks[f"open({inp!r}).read().count('*Change Friction')"] == "1"
    assert checks[f"open({inp!r}).read().count('*Temperature, op=NEW')"] == "1"
    assert int(checks[f"open({inp!r}).read().count('*Model Change')"]) >= 1


def test_a_pin_aligned_into_a_hole_by_shape(session):
    _, report = session
    checks = dict(report["pin_in_hole"]["checks"])
    moved = [float(x) for x in checks["mdb.models['PinPlate'].rootAssembly.instances['Pin-1'].getTranslation()"]
             .strip("()").split(",")]
    assert [round(x, 6) + 0.0 for x in moved] == [0.0, 0.0, 0.0], moved  # back on the hole's axis, flush below
    features = checks["sorted(mdb.models['PinPlate'].rootAssembly.features.keys())"]
    assert "'Pin-1-Align-1'" in features and "'Pin-1-Align-2'" in features


def test_rounded_profiles_have_the_filleted_area(session):
    import math
    _, report = session
    checks = dict(report["rounded"]["checks"])
    corner = (1 - math.pi / 4) * 25  # a 90° corner rounded with r = 5 loses (or, inside, gains) this area
    assert math.isclose(float(checks["mdb.models['Round'].parts['L'].getVolume()"]), (3200 - 4 * corner) * 10,
                        rel_tol=1e-6)
    # a hexagon of radius 20 (area 3√3/2 R²); rounding a corner of interior angle 2θ with r loses r²/tan θ − r²(π − 2θ)/2
    r, half = 4.0, math.radians(60)
    loss = r * r / math.tan(half) - r * r * (math.pi - 2 * half) / 2
    assert math.isclose(float(checks["mdb.models['Round'].parts['Hex'].getVolume()"]),
                        (3 * math.sqrt(3) / 2 * 400 - 6 * loss) * 10, rel_tol=1e-6)


def test_rubber_material_with_damping_and_hybrid_elements(session):
    tmp, report = session
    checks = dict(report["rubber_model"]["checks"])
    rubber = "mdb.models['Rubber'].materials['Material-1']"
    assert checks[f"{rubber}.hyperelastic.type"] == "MOONEY_RIVLIN"
    assert checks[f"{rubber}.hyperelastic.table"] == "((0.3, 0.05, 0.0),)"
    assert float(checks[f"{rubber}.damping.alpha"]) == 1.0 and float(checks[f"{rubber}.damping.beta"]) == 1e-4
    assert checks[f"'C3D8H' in open({str(tmp / 'Rubber.inp')!r}).read()"] == "True"


def test_instances_align_onto_other_instances(session):
    _, report = session
    checks = dict(report["align"]["checks"])
    mate = "mdb.models['Mate'].rootAssembly.instances"
    # B keeps its x move and sits on A (box 50 high); C's origin corner is on A's far top corner
    assert [float(x) for x in checks[f"{mate}['B'].getTranslation()"].strip("()").split(",")] == [300.0, 0.0, 50.0]
    assert [float(x) for x in checks[f"{mate}['C'].getTranslation()"].strip("()").split(",")] == [100.0, 50.0, 50.0]
    assert "'B-Align-1'" in checks["sorted(mdb.models['Mate'].rootAssembly.features.keys())"]


def test_input_files_as_parts_and_models(session):
    _, report = session
    checks = dict(report["inp_part"]["checks"])
    im = "mdb.models['Imported']"
    assert checks[f"sorted({im}.parts.keys())"] == "['Cube', 'Wide']"  # renamed, nothing else left behind
    assert checks[f"len({im}.parts['Cube'].elements)"] == "8"
    assert checks[f"len({im}.parts['Cube'].sectionAssignments)"] == "1"
    assert checks[f"len({im}.parts['Wide'].elements)"] == "2"  # part B of the two
    assert checks["'__nocode_import__' not in mdb.models.keys()"] == "True"
    whole = dict(report["inp_model"]["checks"])
    assert whole["sorted(mdb.models['Whole'].steps.keys())"] == "['Initial', 'More', 'Step-1']"
    assert whole["mdb.models['Whole'].loads.keys()"] != "[]"


def test_partition_before_section_and_mesh_is_one_meshed_part(session):
    """Part -> Datum -> Partition -> Material & Section -> Mesh -> Instance: one part, two cells, meshed after the
    partition (meshing first and partitioning after would delete the mesh)."""
    _, report = session
    checks = dict(report["partition_then_mesh"]["checks"])
    assert checks[f"len({M}.parts.keys())"] == "1"
    assert checks[f"len({PART}.cells)"] == "2"
    assert int(checks[f"len({PART}.elements)"]) == 10 * 5 * 5
    assert checks[f"len({PART}.sectionAssignments)"] == "1"


def test_study_runs_every_design_and_collects_a_curve(session, monkeypatch):
    """Lists of designs -> one job each (run in turn) -> each ODB's min U1 -> a curve against the lengths."""
    tmp, _ = session
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    runner = jobs.JobRunner()
    specs = [jobs.JobSpec(f"Bar-{i}", str(tmp)) for i in (1, 2, 3)]
    assert runner.run(specs) == [f"Bar-{i}: queued" for i in (1, 2, 3)]
    end = time.time() + 900
    while time.time() < end and any(runner.status(s)["state"] not in ("Completed", "Failed") for s in specs):
        time.sleep(1)
    assert [runner.status(s)["state"] for s in specs] == ["Completed"] * 3
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*study(tmp)))
        assert res["curve"]["status"] == "ok", res["curve"]
        curve = outputs.outputs["curve"]["series"]
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert list(curve.x) == list(LENGTHS)
    assert list(curve.y) == pytest.approx([-L / 210000 for L in LENGTHS], rel=0.05)


def test_plastic_bar_yields_by_its_hardening(session, monkeypatch):
    """300 MPa on a bar yielding at 250 with 1000 MPa hardening: plastic strain (300 - 250) / 1000 = 0.05."""
    tmp, report = session
    assert dict(report["plastic_model"]["checks"])["mdb.models['Yield'].materials['Material-1'].plastic.table"] ==         "((250.0, 0.0), (350.0, 0.1))"
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    runner = jobs.JobRunner()
    spec = jobs.JobSpec("Yield", str(tmp))
    runner.run([spec])
    end = time.time() + 600
    while time.time() < end and runner.status(spec)["state"] not in ("Completed", "Failed"):
        time.sleep(1)
    assert runner.status(spec)["state"] == "Completed", runner.status(spec)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*pipeline(tmp, "Yield", PLASTIC, 300.0, peeq=True)))
        assert res["r"]["status"] == "ok", res["r"]
        peeq = outputs.outputs["r"]["value"].value
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert peeq == pytest.approx(0.05, rel=0.1)


def solve(tmp, job, monkeypatch):
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    runner = jobs.JobRunner()
    spec = jobs.JobSpec(job, str(tmp))
    runner.run([spec])
    end = time.time() + 600
    while time.time() < end and runner.status(spec)["state"] not in ("Completed", "Failed"):
        time.sleep(1)
    assert runner.status(spec)["state"] == "Completed", (runner.status(spec),
                                                          Path(spec.file(".log")).read_text(errors="replace")[-2000:])


def _solved(tmp, job, builder, monkeypatch, *outputs):
    """Solve job, then read these output nodes of the graph back from its ODB."""
    solve(tmp, job, monkeypatch)
    Worker._instance = None
    try:
        res, out = Engine().evaluate(graph(*builder()))
        for k in outputs:
            assert res[k]["status"] == "ok", res[k]
        return [out.outputs[k]["value"].value for k in outputs]
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None


@pytest.mark.parametrize("space, job, total", [("2D planar", "Plane", -400.0),
                                               ("Axisymmetric", "Axi", -10 * 3.141592653589793 * 100)])
def test_flat_bars_stretch_as_hookes_law_says(session, monkeypatch, space, job, total):
    """σ L / E = 10 * 100 / 200000 at the end; the reactions balance the pull over the 20 x 2 section (plane
    stress, out-of-plane thickness 2) or the radius-10 disc (axisymmetric: over the whole round)."""
    tmp, report = session
    checks = dict(report[f"{job.lower()}_model"]["checks"])
    assert checks[f"str(mdb.models[{job!r}].parts['Bar'].elements[0].type)"] == ("'CPS4R'" if job == "Plane" else
                                                                                  "'CAX4R'")
    end, reaction = _solved(tmp, job, lambda: flat_bar(tmp, space), monkeypatch, "r", "total")
    assert end == pytest.approx(0.005, rel=1e-3)
    assert reaction == pytest.approx(total, rel=1e-3)


def test_an_analytical_rigid_tool_presses_a_2d_block(session, monkeypatch):
    """Contact between a 2D block's top edge and a rigid line pushed 0.01 down: E ε A = 4000 at the tool's
    reference point, 400 at each inner node of the floor."""
    tmp, _ = session
    tool, floor = _solved(tmp, "Press", lambda: rigid_press(tmp), monkeypatch, "r", "floor_rf")
    assert tool == pytest.approx(-4000, rel=1e-3)
    assert floor == pytest.approx(400, rel=1e-3)


def test_a_column_buckles_at_eulers_load(session, monkeypatch):
    """Clamped-free: π² E I / (2 L)² = 3875.8 for 1 at its head (the first eigenvalue, read from the frames)."""
    tmp, _ = session
    factor, = _solved(tmp, "Column", lambda: buckled_column(tmp), monkeypatch, "r")
    assert factor == pytest.approx(3.14159265 ** 2 * 200000 * (3.14159265 * 10 ** 4 / 4) / 2000 ** 2, rel=2e-3)


def test_riks_follows_the_load_to_its_maximum_factor(session, monkeypatch):
    """Linear bar: the end at load factor 2 (Riks stops just past it) is about twice -100 / 210000."""
    tmp, _ = session
    end, = _solved(tmp, "Arc", lambda: riks_bar(tmp), monkeypatch, "r")
    assert 1.95 <= end / (-100 / 210000) <= 2.6, end


@pytest.mark.parametrize("how, job, expected", [("flux", "Heat", 1000.0), ("film", "Film", 50.0),
                                                ("coupled", "Expand", 0.1)])
def test_heat_flows_and_expands_the_bar(session, monkeypatch, how, job, expected):
    """1D conduction: T = q L / k with a heat flux, k / L = h halfway with convection; a coupled step heated by
    100° grows α ΔT L, held only by symmetry conditions."""
    tmp, _ = session
    value, = _solved(tmp, job, lambda: heated_bar(tmp, how), monkeypatch, "r")
    assert value == pytest.approx(expected, rel=1e-4)


def test_a_force_in_a_turned_coordinate_system(session, monkeypatch):
    """1000 along the 1-axis of a csys turned 90° about z: the reactions take -1000 in global y."""
    tmp, _ = session
    rf2, = _solved(tmp, "Turned", lambda: turned_pull(tmp), monkeypatch, "r")
    assert rf2 == pytest.approx(-1000, rel=1e-4)


def test_a_rigid_body_with_a_point_mass_on_a_spring_vibrates_at_root_k_over_m(session, monkeypatch):
    """Point mass 1 (rotary inertia 1) on a rigid body, bushing 1000 to ground: all six modes at √1000 / 2π."""
    tmp, _ = session
    low, high = _solved(tmp, "Sprung", lambda: sprung_mass(tmp), monkeypatch, "lo", "hi")
    assert low == pytest.approx(1000 ** 0.5 / (2 * 3.141592653589793), rel=1e-3)
    assert high == pytest.approx(low, rel=1e-3)


def test_an_equation_mirrors_one_corner_onto_another(session, monkeypatch):
    tmp, _ = session
    low, high = _solved(tmp, "Tied", lambda: tied_bars(tmp), monkeypatch, "mn", "mx")
    assert low < 0 and high == pytest.approx(-low, rel=1e-6)


def test_hardening_damage_and_thermal_properties_in_cae(session):
    checks = dict(session[1]["hardening"]["checks"])
    hard = "mdb.models['Hard'].materials"
    assert checks[f"str({hard}['Kin'].plastic.hardening)"] == "'KINEMATIC'"
    assert checks[f"str({hard}['Comb'].plastic.hardening)"] == "'COMBINED'"
    assert checks[f"str({hard}['JC'].plastic.hardening)"] == "'JOHNSON_COOK'"
    assert checks[f"{hard}['JC'].plastic.table"] == "((350.0, 275.0, 0.36, 1.0, 1800.0, 20.0),)"
    assert checks[f"{hard}['JC'].ductileDamageInitiation.table"] == "((0.3, 0.0, 0.0),)"
    assert checks[f"{hard}['JC'].ductileDamageInitiation.damageEvolution.table"] == "((0.05,),)"
    assert checks[f"{hard}['JC'].expansion.table"] == "((1.2e-05,),)"


def test_explicit_bar_solves_and_reads_back(session, monkeypatch):
    """Abaqus/Explicit with its own contact types and mass scaling: it solves and its ODB reads back (the end moves
    against the pressure)."""
    tmp, report = session
    checks = dict(report["explicit_model"]["checks"])
    dm = "mdb.models['Drop']"
    assert checks[f"type({dm}.steps['Hit']).__name__"] == "'ExplicitDynamicsStep'"
    assert float(checks[f"{dm}.steps['Hit'].massScaling[0].dt"]) == pytest.approx(1e-6)
    assert checks[f"type({dm}.interactions['All']).__name__"] == "'ContactExp'"
    assert checks[f"type({dm}.interactions['Rub']).__name__"] == "'SurfaceToSurfaceExp'"
    solve(tmp, "Drop", monkeypatch)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*explicit_bar(tmp)))
        assert res["r"]["status"] == "ok", res["r"]
        u1 = outputs.outputs["r"]["value"].value
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert u1 < 0, u1


def test_load_varying_in_time_and_space(session, monkeypatch):
    """The reaction balances the pressure field times the amplitude's last value: 0.5 * 1.5 * 100 = 75."""
    tmp, report = session
    checks = dict(report["varying_model"]["checks"])
    assert checks["mdb.models['Vary'].amplitudes['Push-Amp'].data"] == "((0.0, 0.0), (0.5, 1.0), (1.0, 0.5))"
    assert dict(report["mapped_model"]["checks"])["len(mdb.models['Mapped'].analyticalFields['Push-Field'].xyzPointData)"] == "3"
    solve(tmp, "Vary", monkeypatch)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*pipeline(tmp, "Vary", push=VARYING)))
        assert res["r"]["status"] == "ok", res["r"]
        rf1 = outputs.outputs["r"]["value"].value
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert rf1 == pytest.approx(75, rel=0.01)


def test_loads_follow_every_amplitude_type(session, monkeypatch, tmp_path):
    """Solved: the end's reaction at the step's end is the ramped 1 MPa plus 0.1 MPa times each amplitude's value
    there (100 mm2), as the Amplitude node's curve gives it (a User amplitude needs a subroutine: left out)."""
    tmp, _ = session
    solvable = [a["kind"] for a in AMPLITUDES if a["kind"] != "User"]
    nodes, edges = amplitudes_bar(tmp_path, solvable)
    g = graph(nodes, edges)
    res, inputs = Engine().evaluate(g)
    assert all(res[n["id"]]["status"] == "ok" for n in g["nodes"] if n["type"].startswith("model.")), res
    ends = [float(inputs.outputs[f"amp{k}"]["curve"].y[-1]) for k, a in enumerate(AMPLITUDES) if a["kind"] != "User"]
    script = build_script(g, res, inputs)["script"]
    (tmp_path / "driver.py").write_text("import visualization, part\nexec(open('build.py').read())\n")
    (tmp_path / "build.py").write_text(script)
    subprocess.run([LAUNCHER, "cae", "noGUI=driver.py"], cwd=tmp_path, capture_output=True, timeout=900)
    assert (tmp_path / "Amps.inp").exists()
    solve(tmp_path, "Amps", monkeypatch)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(nodes, edges))
        assert res["r"]["status"] == "ok", res["r"]
        rf1 = outputs.outputs["r"]["value"].value
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert rf1 == pytest.approx(100 + 10 * sum(ends), rel=0.01), (rf1, ends)


def test_coupling_carries_a_point_load_to_the_face(session, monkeypatch):
    """1000 N on the reference point reaches the bar through the coupling: the reactions sum to -1000, and the
    point's U1 history is recorded."""
    tmp, report = session
    assert dict(report["coupling_model"]["checks"])["mdb.models['Grip'].constraints['Grip'].couplingType"] == "KINEMATIC"
    solve(tmp, "Grip", monkeypatch)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*coupled(tmp)))
        assert res["r"]["status"] == "ok", res["r"]
        rf1 = outputs.outputs["r"]["value"].value
        info = outputs.outputs["odb"]["odb"].info["steps"][0]
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert rf1 == pytest.approx(-1000, rel=0.01)
    u1 = [r for r in info["historyRegions"] if any(o["name"] == "U1" for o in r["outputs"])]
    assert len(u1) == 1, info["historyRegions"]


def test_connector_carries_the_load_between_bars(session, monkeypatch):
    """1000 N pulls the second bar; the bushing passes it to the first: reactions sum to -1000, and the connector's
    CTF1/CU1 history is recorded (1000 / 1000 = 1 of stretch)."""
    tmp, report = session
    checks = dict(report["connector_model"]["checks"])
    assert checks["mdb.models['Joint'].sections['Joint-Section'].assembledType"] == "BUSHING"
    behaviours = checks["sorted(type(b).__name__ for b in mdb.models['Joint'].sections['Joint-Section'].behaviorOptions)"]
    assert "Damping" in behaviours and "Elasticity" in behaviours, behaviours
    assert checks["type(mdb.models['Joint'].interactions['Touch']).__name__"] == "'ContactStd'"
    solve(tmp, "Joint", monkeypatch)
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*connected(tmp)))
        assert res["r"]["status"] == "ok", res["r"]
        rf1 = outputs.outputs["r"]["value"].value
        info = outputs.outputs["odb"]["odb"].info["steps"][0]
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert rf1 == pytest.approx(-1000, rel=0.01)
    names = {o["name"] for r in info["historyRegions"] for o in r["outputs"]}
    assert {"CTF1", "CU1"} <= names, info["historyRegions"]


def test_pipeline_solves_and_reads_back(session, monkeypatch):
    """Graph -> CAE model and input -> Run -> real ODB reader: the bar shortens by P L / E."""
    tmp, _ = session
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    runner = jobs.JobRunner()
    spec = jobs.JobSpec("Beam", str(tmp))
    assert runner.run([spec]) == ["Beam: queued"]
    end = time.time() + 600
    while time.time() < end and runner.status(spec)["state"] not in ("Completed", "Failed"):
        time.sleep(1)
    st = runner.status(spec)
    assert st["state"] == "Completed", (st, Path(spec.file(".log")).read_text(errors="replace")[-2000:])
    Worker._instance = None
    try:
        res, outputs = Engine().evaluate(graph(*pipeline(tmp)))
        assert res["r"]["status"] == "ok", res["r"]
        u1 = outputs.outputs["r"]["value"].value
        info = outputs.outputs["odb"]["odb"].info["steps"][0]
        # the Step's output requests reached the ODB: 4 evenly spaced frames (+ the initial one), and RF1
        # history at the nodes of the fixed face
        assert len(info["frames"]) == 5
        rf1 = [r for r in info["historyRegions"] if any(o["name"] == "RF1" for o in r["outputs"])]
        assert len(rf1) >= 4, info["historyRegions"]
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    assert u1 == pytest.approx(-1.0 * 100 / 210000, rel=0.05)


def test_a_solved_jobs_files_and_odb_update(session, monkeypatch):
    """After the pipeline's Beam run: its .sta parses into increments, and CAE's Odb.update (what a sync calls on a
    running job's ODB) exists on a session ODB."""
    tmp, _ = session
    beam = tmp / "Beam.odb"
    if not beam.exists():
        pytest.skip("needs the Beam run of test_pipeline_solves_and_reads_back")
    rows = jobs.parse_sta((tmp / "Beam.sta").read_text(errors="replace"))
    assert rows and rows[-1]["step"] == 1 and rows[-1]["total time"] == pytest.approx(1.0), rows
    script, result = tmp / "odb_update.py", tmp / "odb_update.txt"
    result.unlink(missing_ok=True)
    # noGUI's prints don't reach the console on Windows: the script writes what it saw to a file
    script.write_text("from abaqus import session\n"
                      f"odb = session.openOdb(name={str(beam)!r}, path={str(beam)!r}, readOnly=True)\n"
                      f"open({str(result)!r}, 'w').write('NOCODE-UPDATE %r' % (odb.update(),))\n")
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=odb_update.py"], cwd=tmp, capture_output=True, text=True,
                          errors="replace", timeout=600)
    out = result.read_text() if result.exists() else proc.stdout + proc.stderr
    assert "NOCODE-UPDATE" in out, out[-2000:]


POST_DRIVER = r'''
import visualization, part, sys, io, json, traceback
from abaqus import *
rep = {}
for name in json.load(open('post_cases.json')):
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        exec(compile(open(name + '.py').read(), name + '.py', 'exec'), {'__name__': '__main__'})
    except Exception:
        print(traceback.format_exc())
    sys.stdout = old
    rep[name] = buf.getvalue()
sodb = session.scratchOdbs[session.scratchOdbs.keys()[0]]
rep['session fields'] = sorted(sodb.steps['Session Step'].frames[0].fieldOutputs.keys())
rep['groups'] = sorted(session.displayGroups.keys())
rep['odbs'] = len(session.odbs.keys())
json.dump(rep, open('post_report.json', 'w'))
'''


OLD_LAUNCHER = os.environ.get("NOCODE_REAL_ABAQUS_OLD", "")  # an older release's launcher, e.g. abq2025.bat


@pytest.mark.parametrize("release", ["same", "older"])
def test_computed_fields_and_display_groups_show_on_the_beam_odb(session, monkeypatch, release):
    """After the pipeline's Beam run, the results scripts one after another in one CAE (as live updates): a Formula
    of an element field and of a nodal one become session fields CAE can contour (it refuses CENTROID data shown at
    CENTROID: ELEMENT_CENTROID), updated fields get new names, and a Compare's passing elements a display group.
    Every update reuses the one open ODB. Older: the Beam solved by an older Abaqus (NOCODE_REAL_ABAQUS_OLD), which
    CAE shows through an upgraded copy, made once."""
    tmp, _ = session
    beam = tmp / "Beam.odb"
    if not beam.exists():
        pytest.skip("needs the Beam run of test_pipeline_solves_and_reads_back")
    if release == "older":
        if not OLD_LAUNCHER:
            pytest.skip("set NOCODE_REAL_ABAQUS_OLD to an older Abaqus launcher to run")
        old = tmp / "older"
        old.mkdir(exist_ok=True)
        proc = subprocess.run([OLD_LAUNCHER, "job=BeamOld", f"input={tmp / 'Beam.inp'}", "interactive"], cwd=old,
                              capture_output=True, text=True, errors="replace", timeout=900)
        beam = old / "BeamOld.odb"
        assert beam.exists(), proc.stdout[-2000:]
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    base = [("odb", "odb.open", {"path": str(beam)}), ("s", "odb.field", {"variable": "S", "quantity": "Mises"}),
            ("u", "odb.field", {"variable": "U", "quantity": "Magnitude"})]
    be = [("odb", "odb", "s", "odb"), ("odb", "odb", "u", "odb")]
    cases = {
        "util": ([("f", "math.formula", {"expr": "a / 250", "label": "Utilisation"}), ("c", "view.contour", {})],
                 [("s", "field", "f", "a"), ("f", "value", "c", "field")]),
        "util_again": ([("f", "math.formula", {"expr": "a / 0.5", "label": "Utilisation"}),
                        ("c", "view.contour", {})], [("s", "field", "f", "a"), ("f", "value", "c", "field")]),
        "nodal": ([("f", "math.formula", {"expr": "a * 1000", "label": "U-micron"}), ("c", "view.contour", {})],
                  [("u", "field", "f", "a"), ("f", "value", "c", "field")]),
        "group": ([("f", "math.formula", {"expr": "a / 0.5", "label": "Utilisation"}),
                   ("hot", "logic.test", {"op": ">", "value": "1"}),
                   ("g", "view.group", {"name": "Yielded", "show": "Show only this group"})],
                  [("s", "field", "f", "a"), ("f", "value", "hot", "a"), ("hot", "kept", "g", "members")]),
    }
    Worker._instance = None
    try:
        for name, (nodes, edges) in cases.items():
            g = graph(base + nodes, be + edges)
            res, inputs = Engine().evaluate(g)
            assert all(r["status"] == "ok" for r in res.values()), (name, res)
            (tmp / f"{name}.py").write_text(build_script(g, res, inputs)["script"])
    finally:
        if Worker._instance is not None:
            Worker._instance.proc.kill()
            Worker._instance = None
    (tmp / "post_cases.json").write_text(json.dumps(list(cases)))
    (tmp / "post_driver.py").write_text(POST_DRIVER)
    report = tmp / "post_report.json"
    report.unlink(missing_ok=True)
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=post_driver.py"], cwd=tmp, capture_output=True, text=True,
                          errors="replace", timeout=600)
    assert report.exists(), proc.stdout[-3000:] + proc.stderr[-3000:]
    rep = json.loads(report.read_text())
    for name in cases:
        assert "failed" not in rep[name] and "Traceback" not in rep[name], (name, rep[name])
    assert rep["session fields"] == ["U-micron", "Utilisation", "Utilisation-2"]
    assert "Yielded" in rep["groups"] and "display group Yielded:" in rep["group"]
    assert rep["odbs"] == 1
    upgraded = [name for name in cases if "older Abaqus release" in rep[name]]
    assert upgraded == (["util"] if release == "older" else [])


def test_data_check_puts_abaqus_messages_on_the_nodes(session, monkeypatch):
    """The Job's Check: Abaqus' data check of the written input (no solve); its messages land on the nodes."""
    from nodecode_fea import checks
    tmp, report = session
    assert dict(report["flawed_model"]["checks"])[f"__import__('os').path.exists({str(tmp / 'Flawed.inp')!r})"]         == "True"
    monkeypatch.setenv("ABAQUS_CMD", LAUNCHER)
    spec = jobs.JobSpec("Flawed", str(tmp))
    assert jobs.runner.run([spec], check=True) == ["Flawed: data check queued"]
    end = time.time() + 300
    while time.time() < end and not jobs.runner.checked(spec):
        time.sleep(1)
    state = jobs.runner.check_state(spec)
    assert state.startswith("Checked:") and "error" in state, state
    assert not (tmp / "Flawed.odb").exists()  # the check's files are its own (Flawed-Check.*)
    g = graph(*flawed(tmp))
    res, inputs = Engine().evaluate(g)
    warnings = checks.lint(g, res, inputs)
    assert any("Data check" in w and "missing property" in w for w in warnings["bare"]), warnings
    assert any("Data check" in w and "STEEL" in w for w in warnings["sec"]), warnings
    assert any("Data check" in w and "GRAV" in w for w in warnings["g"]), warnings


def test_a_cae_model_opened_as_a_graph_rebuilds_the_same_model(tmp_path):
    """Open CAE model: a model made in CAE and saved as a .cae is read, turned into a graph, and the graph's build
    script (run next to the opened .cae, whose material with expansion it uses) makes the same model again."""
    from nodecode_fea.cae_import import graph_from_dump, payload
    from nodecode_fea.cae_reader import read_cae

    from .cae_roundtrip import rebuild_and_compare
    from .cae_source_model import SOURCE

    (tmp_path / "source.py").write_text(SOURCE, encoding="ascii")
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=source.py"], cwd=tmp_path, capture_output=True, text=True,
                          errors="replace", timeout=600)
    cae = tmp_path / "source.cae"
    assert cae.exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    dump = read_cae(str(cae), launcher=LAUNCHER, folder=str(tmp_path / "parts"))
    out = graph_from_dump(dump)
    g = payload(out["graph"])
    res, inputs = Engine().evaluate(g)
    assert not {k: v.get("message") for k, v in res.items() if v["status"] != "ok"}
    script = build_script(g, res, inputs)["script"]
    rep = rebuild_and_compare(LAUNCHER, tmp_path, str(cae), script, ["Bracket", out["model"]])
    assert rep["error"] is None, rep["error"]
    assert " failed: " not in rep["output"], rep["output"][-3000:]
    original, rebuilt = rep["models"]["Bracket"], rep["models"]["Bracket-rebuilt"]
    assert sorted(rebuilt["instances"]) == ["Block-1", "Plate-1", "Plate-2", "Ring-1", "Tri-1"]
    assert sorted(rebuilt["materials"]) == ["Alu", "Hot", "Steel"] and rebuilt["materials"]["Hot"]["expansion"]
    assert sorted(rebuilt["steps"]) == ["Initial", "Load", "Modes"]
    assert sorted(rebuilt["bcs"]) == ["Clamp", "Pull"] and sorted(rebuilt["loads"]) == ["Push", "Tip", "Weight"]
    assert rebuilt["constraints"] == {"Glue": "Tie"}
    assert rebuilt["parts"]["Plate"][1] > 0  # meshed
    # everything compared, including where every instance's vertices are and the parts' volumes and mesh sizes
    for key in original:
        assert original[key] == rebuilt[key], key


def test_a_set_made_in_cae_survives_the_part_being_rebuilt(tmp_path):
    """From a CAE set: a Set made in CAE on a part the graph built is listed as Find-at points; a Region taking them
    over makes the same named Set again after the graph rebuilt the part longer (indices would have gone stale)."""
    from nodecode_fea import cae_sets

    def script(nodes, edges):
        g = graph(nodes, edges)
        res, inputs = Engine().evaluate(g)
        assert not {k: v.get("message") for k, v in res.items() if v["status"] != "ok"}
        return build_script(g, res, inputs)["script"]

    (tmp_path / "build.py").write_text(script(BOX, BOX_EDGES))
    (tmp_path / "list_sets.py").write_text(cae_sets.script())
    (tmp_path / "first.py").write_text(
        "import visualization, part\nfrom abaqus import *\nfrom abaqusConstants import *\nimport sys, io\n"
        "exec(compile(open('build.py').read(), 'build.py', 'exec'), {'__name__': '__main__'})\n"
        f"p = {PART}\n"
        "p.Set(name='Top', faces=p.faces.findAt(((30.0, 50.0, 20.0),)))\n"
        "out, old = io.StringIO(), sys.stdout\nsys.stdout = out\n"
        "exec(compile(open('list_sets.py').read(), 'list_sets.py', 'exec'), {'__name__': '__main__'})\n"
        "sys.stdout = old\nopen('listed.txt', 'w').write(out.getvalue())\n")
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=first.py"], cwd=tmp_path, capture_output=True, text=True,
                          errors="replace", timeout=600)
    assert (tmp_path / "listed.txt").exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    listed = [s for s in cae_sets.parse((tmp_path / "listed.txt").read_text()) if s["name"] == "Top"]
    assert [(s["part"], s["kind"], s["count"]) for s in listed] == [("Part-1", "face", 1)]

    nodes = [BOX[0], ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 80.0}),
             ("r", "model.region", {"method": "Pick in CAE", "kind": "Face", "value": listed[0]["value"],
                                    "create": "Set", "name": "Top"})]
    (tmp_path / "second.py").write_text(script(nodes, BOX_EDGES + [("p", "part", "r", "source")]))
    (tmp_path / "check.py").write_text(
        "import visualization, part, json\nfrom abaqus import *\nfrom abaqusConstants import *\n"
        "exec(compile(open('second.py').read(), 'second.py', 'exec'), {'__name__': '__main__'})\n"
        f"p = {PART}\n"
        "json.dump({'faces': [f.pointOn[0] for f in p.sets['Top'].faces],\n"
        "           'length': max(v.pointOn[0][2] for v in p.vertices)}, open('checked.json', 'w'))\n")
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=check.py"], cwd=tmp_path, capture_output=True, text=True,
                          errors="replace", timeout=600)
    assert (tmp_path / "checked.json").exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    checked = json.loads((tmp_path / "checked.json").read_text())
    assert checked["length"] == pytest.approx(80.0)  # rebuilt longer
    assert len(checked["faces"]) == 1 and checked["faces"][0][1] == pytest.approx(50.0)  # still the top face


def test_an_input_file_is_rebuilt_as_it_was(session):
    _, report = session
    pa = "mdb.models['plate_assembly']"
    checks = dict(report["inp_rebuilt"]["checks"])
    assert checks[f"{pa}.parts.keys()"] == "['PLATE']"
    assert checks[f"sorted((k, len(s.nodes), len(s.elements)) for k, s in {pa}.parts['PLATE'].sets.items())"] ==         "[('FIXED', 6, 0), ('LEFT', 18, 4), ('RIGHT', 18, 4)]"
    assert checks[f"[(k, len(s.elements), sorted(set(str(x) for x in s.sides))) for k, s in "
                  f"{pa}.parts['PLATE'].surfaces.items()]"] == "[('TOP', 8, ['FACE2'])]"
    assert checks[f"sorted((k, len(s.nodes)) for k, s in {pa}.rootAssembly.sets.items())"] ==         "[('CORNER', 1), ('TIP', 6)]"
    assert checks[f"[(a.sectionName, sorted(e.label for e in a.getSet().elements)) for a in "
                  f"{pa}.parts['PLATE'].sectionAssignments]"] ==         "[('Section-1-LEFT', [1, 2, 5, 6]), ('Section-2-RIGHT', [3, 4, 7, 8])]"
    assert checks[f"sorted({pa}.loads.keys()) + sorted({pa}.boundaryConditions.keys())"] ==         "['CFORCE-1', 'SURFFORCE-1', 'Disp-BC-1', 'Disp-BC-2']"


def test_reading_the_input_file_gives_the_recorded_graph(tmp_path):
    """The recorded dump the tests use is what CAE gives for the file today."""
    read = read_cae(str(DATA / "plate_assembly.inp"), launcher=LAUNCHER, folder=str(tmp_path))
    assert graph_from_dump(read) == graph_from_dump(recorded_dump())


ODB_U = """
import json, sys
from odbAccess import openOdb
out = []
for path in sys.argv[1:]:
    odb = openOdb(path, readOnly=True)
    frame = odb.steps.values()[-1].frames[-1]
    out.append(sorted([v.instance.name, v.nodeLabel] + [float(c) for c in v.data] for v in frame.fieldOutputs['U'].values))
    odb.close()
json.dump(out, open('u.json', 'w'))
"""


def test_the_rebuilt_input_file_solves_to_the_same_displacements(session, tmp_path):
    tmp, _ = session
    (tmp_path / "original.inp").write_text((DATA / "plate_assembly.inp").read_text())
    (tmp_path / "rebuilt.inp").write_text((tmp / "plate_assembly.inp").read_text())
    for job in ("original", "rebuilt"):
        subprocess.run([LAUNCHER, f"job={job}", "interactive"], cwd=tmp_path, capture_output=True, timeout=600)
    (tmp_path / "u.py").write_text(ODB_U)
    proc = subprocess.run([LAUNCHER, "python", "u.py", "original.odb", "rebuilt.odb"], cwd=tmp_path,
                          capture_output=True, text=True, errors="replace", timeout=600)
    assert (tmp_path / "u.json").exists(), proc.stdout[-2000:] + proc.stderr[-2000:]
    original, rebuilt = json.loads((tmp_path / "u.json").read_text())
    assert len(original) == 30 and min(row[4] for row in original) < -1  # the corner is pushed down
    assert [r[:2] for r in rebuilt] == [r[:2] for r in original]
    assert [c for r in rebuilt for c in r[2:]] == pytest.approx([c for r in original for c in r[2:]], abs=1e-9)


def test_one_coupling_per_hole_found_by_shape(monkeypatch, tmp_path):
    """One per geometry on cylindrical faces: each hole's Region is located at its centre, on the axis, where findAt
    finds nothing; CAE takes the hole nearest it, so each hole gets its own coupling (and bushing to ground)."""
    monkeypatch.setattr(cae_queries, "value", lambda q, first, second=None: [[30.0, 25.0, 10.0], [70.0, 25.0, 10.0]])
    nodes = [("hole", "model.sketch", {"shape": "Circle", "x": 30, "y": 25, "radius": 10}),
             ("pin", "model.sketch", {"shape": "Circle", "x": 70, "y": 25, "radius": 5}),
             ("plate", "model.sketch", {}), ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 20}),
             ("i", "model.instance", {}),
             ("r", "model.region", {"method": "By shape", "members": "One per geometry", "create": "Set",
                                    "name": "Hole"}),
             ("c", "model.interaction", {"name": "Bolt", "kind": "Kinematic coupling",
                                         "point_from": "Slave Region point"}),
             ("b", "model.interaction", {"name": "Spring", "kind": "Connector", "connector": "Bushing",
                                         "second": "Ground", "stiffness": "1e4"})]
    edges = [("hole", "sketch", "plate", "add"), ("pin", "sketch", "plate", "add"), ("plate", "sketch", "p", "profile"),
             ("p", "part", "i", "part"), ("i", "instance", "r", "source"), ("r", "region", "c", "slave"),
             ("c", "interaction", "b", "master")]
    g = graph(nodes, edges)
    res, inputs = Engine().evaluate(g)
    assert all(res[n["id"]]["status"] == "ok" for n in g["nodes"]), res
    # each hole's Region (an assembly set, as it is named) holds that one hole's face; one coupling each
    check = (f"\n_a = {M}.rootAssembly\n"
             f"open('done.txt', 'w').write(repr(sorted((k, len(_a.sets[k].faces), "
             f"round(_a.sets[k].faces[0].getRadius(), 6)) for k in ('Hole-1', 'Hole-2'))"
             f" + sorted({M}.constraints.keys())))\n")
    (tmp_path / "driver.py").write_text("import visualization, part\nexec(open('build.py').read())\n")
    (tmp_path / "build.py").write_text(build_script(g, res, inputs)["script"] + check)
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=driver.py"], cwd=tmp_path, capture_output=True, text=True,
                          errors="replace", timeout=900)
    rpy = tmp_path / "abaqus.rpy"  # noGUI: what the script says goes to the replay file
    out = proc.stdout + proc.stderr + (rpy.read_text(errors="replace") if rpy.exists() else "")
    assert " failed: " not in out, out[-3000:]
    assert (tmp_path / "done.txt").exists(), out[-3000:]
    assert (tmp_path / "done.txt").read_text() == "[('Hole-1', 1, 10.0), ('Hole-2', 1, 5.0), 'Bolt-1', 'Bolt-2']"


def test_holes_are_measured_on_the_part_as_it_is_now(tmp_path):
    """A part rebuilt with more holes: its instance must be measured as it is now (the assembly regenerated), not as
    it was. Two updates in one session, as Live sends them: 4 holes, then 6."""
    def bolt_circle(count):
        nodes = [("i", "loop.for", {"by": "Count", "start": 0, "stop": 300, "count": count}),
                 ("x", "math.formula", {"expr": "60 + 40 * cos(a * pi / 180)"}),
                 ("y", "math.formula", {"expr": "60 + 40 * sin(a * pi / 180)"}),
                 ("holes", "model.sketch", {"shape": "Circle", "radius": 5}),
                 ("disc", "model.sketch", {"shape": "Circle", "x": 60, "y": 60, "radius": 60}),
                 ("p", "model.part", {"body": "Solid", "method": "Extrude", "depth": 10}),
                 ("inst", "model.instance", {}),
                 ("r", "model.region", {"method": "By shape", "radius": 5, "members": "One per geometry"})]
        edges = [("i", "i", "x", "a"), ("i", "i", "y", "a"), ("x", "value", "holes", "param:x"),
                 ("y", "value", "holes", "param:y"), ("holes", "sketch", "disc", "add"),
                 ("disc", "sketch", "p", "profile"), ("p", "part", "inst", "part"), ("inst", "instance", "r", "source")]
        return graph(nodes, edges)

    scripts, previous, keys = [], {}, {}
    for count in (4, 6):
        g = bolt_circle(count)
        g["tab"] = "model"
        res, inputs = Engine().evaluate(g)
        assert inputs.outputs["r"]["region"].__class__.__name__ == "PendingMeasure"
        out = build_script(g, res, inputs, previous=previous, previous_keys=keys)
        previous, keys = dict(out["fingerprints"]), dict(out["node_keys"])
        scripts.append(out["script"])
    for k, script in enumerate(scripts):
        (tmp_path / f"build{k}.py").write_text(script)
    (tmp_path / "driver.py").write_text("import visualization, part\nexec(open('build0.py').read())\n"
                                        "exec(open('build1.py').read())\n")
    proc = subprocess.run([LAUNCHER, "cae", "noGUI=driver.py"], cwd=tmp_path, capture_output=True, text=True,
                          errors="replace", timeout=900)
    rpy = tmp_path / "abaqus.rpy"
    out = proc.stdout + proc.stderr + (rpy.read_text(errors="replace") if rpy.exists() else "")
    lines = [ln for ln in out.splitlines() if MARKER in ln]
    answers = [list(json.loads(ln[ln.index(MARKER) + len(MARKER):]).values())[0] for ln in lines]
    assert [len(a) for a in answers] == [4, 6], out[-3000:]
