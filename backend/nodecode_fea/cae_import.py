"""Turn a model read from a .cae or an input file (``cae_reader``'s dump) into an editor graph that rebuilds it with
the modelling nodes.

Only existing nodes are used: Model, Sketch, Part (sketched, or importing the part's own geometry or mesh that the
reader wrote out, or an input file's part from the file), Material & Section, Mesh, Instance, Region (Find at points
on the faces/edges/vertices/cells of each set or surface; Mesh labels for the nodes, elements and element faces of a
mesh's), Step, Load / Boundary Condition, Interaction, Job, and Inputs for materials and sections the
nodes can't describe (taken from the .cae, open in CAE). Whatever can't be expressed exactly is left out and listed in
the notes (also on a Text node in the graph): the graph never guesses.
"""

from __future__ import annotations

import math
import os
import re

from .nodes import ELEMENT_TYPES, ELEMENT_SHAPES

TOL = 1e-6
# CAE reports points on faces/edges to about 6 decimals: findAt then misses curved or turned faces by that much, so
# Regions take the nearest entity within this distance (cells: CAE only finds them exactly)
FIND_TOLERANCE = 1e-3
COL = 330  # editor layout: one column per stage, nodes stacked in it
ROW = 190
KINDS = {"cell": "Cell", "face": "Face", "edge": "Edge", "vertex": "Vertex"}
BODY_KIND = {"Solid": "cell", "Shell": "face", "Wire": "edge"}
FLAT_SPACES = {"TWO_D_PLANAR": "2D planar", "AXISYMMETRIC": "Axisymmetric"}
TECHNIQUES = {"FREE": "Free", "STRUCTURED": "Structured", "SWEEP": "Sweep"}
SHAPES = {"HEX": "Hex", "HEX_DOMINATED": "Hex-dominated", "TET": "Tet", "QUAD": "Quad",
          "QUAD_DOMINATED": "Quad-dominated", "TRI": "Tri"}
# Abaqus' symmetry / antisymmetry / pinned boundary conditions as the components they fix
TYPE_BCS = {"PINNED": ("u1", "u2", "u3"), "XSYMM": ("u1", "ur2", "ur3"), "YSYMM": ("u2", "ur1", "ur3"),
            "ZSYMM": ("u3", "ur1", "ur2"), "XASYMM": ("u2", "u3", "ur1"), "YASYMM": ("u1", "u3", "ur2"),
            "ZASYMM": ("u1", "u2", "ur3")}
DOFS = ("u1", "u2", "u3", "ur1", "ur2", "ur3")
# states of a load, BC or interaction in a later step that mean it carries on as made
QUIET = (None, "CREATED", "PROPAGATED", "NOT_YET_ACTIVE", "PROPAGATED_FROM_BASE_STATE", "BUILT_INTO_BASE_STATE",
         "INSTANCE_NOT_APPLICABLE")
MATERIAL_PARTS = {"elastic", "plastic", "density", "damping", "hyperelastic"}


# CAE amplitude classes the Amplitude node rebuilds, and its Type for each
AMPLITUDE_TYPES = {"TabularAmplitude": "Tabular", "SmoothStepAmplitude": "Smooth step",
                   "EquallySpacedAmplitude": "Equally spaced", "PeriodicAmplitude": "Periodic",
                   "ModulatedAmplitude": "Modulated", "DecayAmplitude": "Exponential decay",
                   "SolutionDependentAmplitude": "Solution dependent", "UserAmplitude": "User"}


def _g(x: float) -> str:
    """A number as text without losing what CAE gave (12 significant digits; 0.5, not 0.500000000000)."""
    return f"{float(x):.12g}"


def _pts(points) -> str:
    return "; ".join(", ".join(_g(c) for c in p) for p in points)


def _on(v) -> bool:
    return str(v).upper() in ("ON", "1", "TRUE")


def _ranges(labels) -> str:
    """Mesh labels as an input file writes them, in ranges: 1-9, 12."""
    out, run = [], []
    for n in sorted(set(int(x) for x in labels)):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


class _Graph:
    def __init__(self):
        self.nodes, self.edges, self.rows, self.notes = [], [], {}, []

    def add(self, nid: str, kind: str, col: int, params: dict | None = None, name: str = "") -> str:
        row = self.rows.get(col, 0)
        self.rows[col] = row + 1
        data = {"type": kind, "params": params or {}}
        if name:
            data["name"] = name
        self.nodes.append({"id": nid, "type": "op", "position": {"x": col * COL, "y": row * ROW}, "data": data})
        return nid

    def wire(self, source: str, handle: str, target: str, target_handle: str) -> None:
        self.edges.append({"id": f"{source}.{handle}-{target}.{target_handle}", "source": source,
                           "sourceHandle": handle, "target": target, "targetHandle": target_handle})

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)


def _id(prefix: str, name: str, taken: set) -> str:
    base = "cae-" + prefix + "-" + "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()
    nid, i = base, 2
    while nid in taken:
        nid, i = f"{base}-{i}", i + 1
    taken.add(nid)
    return nid


# ------------------------------------------------------------------ sketches
def _loops(curves: list) -> tuple[list, list, str]:
    """The sketch's lines chained into polylines ([(closed, points)]) and its circles ([(center, radius)]); or a
    reason it can't be drawn with Sketch (arcs, splines, branches)."""
    lines, circles = [], []
    for c in curves:
        if c.get("construction"):
            continue
        if c["type"] == "LINE" and len(c["points"]) == 2:
            lines.append([tuple(c["points"][0]), tuple(c["points"][1])])
        elif c["type"] == "CIRCLE" and len(c["points"]) == 2:
            centre = c["points"][1]
            circles.append((centre, math.dist(centre, c["points"][0])))
        else:
            return [], [], f"its sketch has {c['type'].lower()} curves (Sketch draws lines and circles here)"

    def key(p):
        return (round(p[0] / TOL), round(p[1] / TOL))

    chains, left = [], lines[:]
    while left:
        chain = list(left.pop(0))
        grown = True
        while grown:
            grown = False
            for seg in left:
                a, b = seg
                if key(a) == key(chain[-1]):
                    chain.append(b)
                elif key(b) == key(chain[-1]):
                    chain.append(a)
                elif key(b) == key(chain[0]):
                    chain.insert(0, a)
                elif key(a) == key(chain[0]):
                    chain.insert(0, b)
                else:
                    continue
                left.remove(seg)
                grown = True
                break
        closed = len(chain) > 3 and key(chain[0]) == key(chain[-1])
        chains.append((closed, chain[:-1] if closed else chain))
    ends = {}
    for closed, chain in chains:
        for p in ([] if closed else [chain[0], chain[-1]]) + (chain if closed else chain[1:-1]):
            ends[key(p)] = ends.get(key(p), 0) + 1
    if any(n > 1 for n in ends.values()):
        return [], [], "its sketch has lines that branch"
    return chains, circles, ""


def _sketch_vertices(chains, circles, curves) -> list:
    pts = [p for _, chain in chains for p in chain]
    pts += [tuple(c["points"][0]) for c in curves if c["type"] == "CIRCLE" and not c.get("construction")]
    return pts


def _same_points(a, b) -> bool:
    key = lambda p: tuple(round(c / 1e-4) for c in p)  # noqa: E731
    return {key(p) for p in a} == {key(p) for p in b}


def _base_matches(part: dict, base: dict, chains, circles) -> str:
    """'' when the part's vertices are exactly what Part builds from this sketch (so it is the same shape);
    else why not."""
    flat = _sketch_vertices(chains, circles, base["curves"])
    method = base["method"]
    if method == "Planar":
        expected = [(x, y, 0.0) for x, y in flat]
    elif method == "Extrude":
        d = base.get("depth")
        if not d:
            return "its extrusion depth isn't readable"
        expected = [(x, y, z) for x, y in flat for z in (0.0, d)]
    else:
        angle = base.get("angle")
        if not angle:
            return "its revolution angle isn't readable"
        if any(x < -TOL for x, _ in flat):
            return "its profile crosses the axis"
        t = math.radians(angle)
        expected = [(x, y, 0.0) for x, y in flat]
        if angle < 360 - 1e-9:
            expected += [(x * math.cos(t), y, x * math.sin(t)) for x, y in flat]
    if not _same_points(expected, part.get("vertices") or []):
        return ("it isn't the plain " + method.lower() + " of its sketch about the axes Part uses "
                "(the sketch is placed or revolved differently, or other features changed it)")
    return ""


# ------------------------------------------------------------------ materials and sections
def _material_params(mat: dict) -> tuple[dict, str]:
    """Material & Section settings for a material, or why the node can't hold it."""
    extra = sorted(set(mat.get("behaviours", [])) - MATERIAL_PARTS)
    if extra:
        return {}, "it has " + ", ".join(extra)
    params = {"material": mat["name"]}
    for key in ("elastic", "plastic", "density", "hyperelastic"):
        if (mat.get(key) or {}).get("temperature"):
            return {}, f"its {key} data depends on temperature"
    hyper, elastic = mat.get("hyperelastic"), mat.get("elastic")
    if hyper:
        if elastic or mat.get("plastic"):
            return {}, "it mixes hyperelastic with elastic or plastic data"
        row = hyper["table"][0] if len(hyper.get("table") or []) == 1 else None
        if hyper.get("testData") or row is None or hyper["type"] not in ("NEO_HOOKE", "MOONEY_RIVLIN"):
            return {}, "its hyperelastic model isn't Neo-Hooke or Mooney-Rivlin from coefficients"
        if hyper["type"] == "NEO_HOOKE":
            params.update(behavior="Neo-Hooke", c10=row[0], d1=row[1])
        else:
            params.update(behavior="Mooney-Rivlin", c10=row[0], c01=row[1], d1=row[2])
    elif elastic:
        table = elastic.get("table") or []
        if len(table) != 1 or elastic.get("noCompression") or elastic.get("moduli") not in (None, "LONG_TERM"):
            return {}, "its elasticity is more than one constant row"
        row = table[0]
        if elastic["type"] == "ISOTROPIC":
            params.update(behavior="Elastic", young=row[0], poisson=row[1])
        elif elastic["type"] == "LAMINA":
            params.update(behavior="Lamina", **dict(zip(("e1", "e2", "nu12", "g12", "g13", "g23"), row)))
        elif elastic["type"] == "ENGINEERING_CONSTANTS":
            params.update(behavior="Engineering constants", **dict(zip(
                ("e1", "e2", "e3", "nu12", "nu13", "nu23", "g12", "g13", "g23"), row)))
        else:
            return {}, f"its elasticity is {elastic['type'].lower()}"
    else:
        return {}, "it has no elasticity"
    plastic = mat.get("plastic")
    if plastic:
        if plastic.get("hardening") != "ISOTROPIC" or plastic.get("rate") or params.get("behavior") != "Elastic":
            return {}, "its plasticity isn't isotropic hardening on an isotropic elastic material"
        rows = plastic["table"]
        if not rows or rows[0][1] != 0:
            return {}, "its plastic table doesn't start at 0 plastic strain"
        params["plastic"] = "; ".join(f"{_g(e)}, {_g(s)}" for s, e in rows)
    if mat.get("density"):
        if len(mat["density"]["table"]) != 1:
            return {}, "its density is a table"
        params["density"] = mat["density"]["table"][0][0]
    damping = mat.get("damping")
    if damping:
        if damping.get("composite") or damping.get("structural"):
            return {}, "it has composite or structural damping"
        params.update(alpha=damping.get("alpha") or 0.0, beta=damping.get("beta") or 0.0)
    return params, ""


def _section_params(sec: dict, profiles: dict) -> tuple[dict, str]:
    kind = sec["type"]
    params = {"section": sec["name"]}
    if kind == "HomogeneousSolidSection":
        params["kind"] = "Solid"
    elif kind == "HomogeneousShellSection":
        if sec.get("thicknessType") not in (None, "UNIFORM") or sec.get("nodalThicknessField") or sec.get(
                "thicknessField") or sec.get("idealization") not in (None, "NO_IDEALIZATION"):
            return {}, "its thickness isn't one uniform value"
        params.update(kind="Shell", thickness=sec.get("thickness"))
    elif kind == "CompositeShellSection":
        layup = sec.get("layup") or []
        if not layup or len({p["material"] for p in layup}) != 1:
            return {}, "its plies are of several materials"
        params.update(kind="Composite shell", symmetric="Yes" if _on(sec.get("symmetric")) else "No",
                      plies="; ".join(f"{_g(p['angle'])}, {_g(p['thickness'])}" for p in layup))
        sec = dict(sec, material=layup[0]["material"])
    elif kind == "BeamSection":
        prof = profiles.get(sec.get("profile") or "")
        if prof and prof["type"] == "RectangularProfile":
            params.update(kind="Beam", profile="Rectangle", width=prof["a"], height=prof["b"])
        elif prof and prof["type"] == "CircularProfile":
            params.update(kind="Beam", profile="Circle", radius=prof["r"])
        else:
            return {}, "its beam profile isn't a rectangle or a circle"
    else:
        return {}, f"it is a {kind}"
    return params, ""


# ------------------------------------------------------------------ instances
def _euler(R) -> tuple[float, float, float]:
    """Angles (degrees) of turns about X, then Y, then Z (fixed axes) that make the rotation matrix R."""
    sb = max(-1.0, min(1.0, -R[2][0]))
    b = math.asin(sb)
    if abs(math.cos(b)) > 1e-9:
        a, c = math.atan2(R[2][1], R[2][2]), math.atan2(R[1][0], R[0][0])
    else:  # gimbal lock: the X turn folds into Z
        a, c = 0.0, math.atan2(-R[0][1], R[1][1])
    return tuple(0.0 if abs(x) < 1e-9 else round(x, 9) for x in map(math.degrees, (a, b, c)))


def graph_from_dump(dump: dict) -> dict:
    """{"graph": {"nodes", "edges"} (the editor's format), "notes": [what wasn't rebuilt], "model": name}."""
    m = dump["model"]
    source = os.path.basename(dump.get("file") or "the .cae")
    inp = source.lower().endswith(".inp")
    g = _Graph()
    for note in dump.get("notes", []):  # the reader's own (e.g. an older release's file read through an upgrade)
        g.note(note)
    taken: set = set()
    materials = {x["name"]: x for x in m.get("materials", [])}
    sections = {x["name"]: x for x in m.get("sections", [])}
    profiles = {x["name"]: x for x in m.get("profiles", [])}
    picked_from_cae = []  # materials/sections the nodes can't hold: used from the .cae, open in CAE

    # ---- materials and sections first: one the nodes can't hold makes the graph build into its own model
    section_settings = {}
    for name, sec in sections.items():
        params, why = _section_params(sec, profiles)
        mat_name = sec.get("material") if not params else (
            sec["material"] if sec["type"] != "CompositeShellSection" else sec["layup"][0]["material"])
        mat = materials.get(mat_name or "")
        if params and mat is None:
            why = f"its material {mat_name} isn't in the model"
        mparams, mwhy = (_material_params(mat) if mat else ({}, ""))
        if why:
            section_settings[name] = ("section", why)
            picked_from_cae.append(f"section {name} ({why})")
        elif mwhy:
            section_settings[name] = ("material", params, mat_name, mwhy)
            picked_from_cae.append(f"material {mat_name} ({mwhy})")
        else:
            section_settings[name] = ("ok", {**params, **mparams})
    model_name = m["name"] + ("-rebuilt" if picked_from_cae else "")
    model = g.add("cae-model", "model.model", 0, {"name": model_name}, name=model_name)
    if picked_from_cae:
        g.note(f"Open {source} in Abaqus/CAE before building: " + "; ".join(dict.fromkeys(picked_from_cae)) +
               f": used from its model {m['name']} (the nodes can't describe them), so this graph builds "
               f"model {model_name} beside it")
    cae_inputs: dict = {}

    def from_cae(kind: str, name: str) -> str:
        if (kind, name) not in cae_inputs:
            nid = _id("in", name, taken)
            cae_inputs[(kind, name)] = g.add(nid, "input.value", 2, {
                "type": f"{kind} in CAE", "value": f"{m['name']}|{name}", "label": name}, name=f"{kind} {name}")
        return cae_inputs[(kind, name)]

    # ---- regions: Find at points, one Region per instance (or part) and kind
    regions: dict = {}
    part_of = {i["name"]: i["part"] for i in m.get("instances", [])}

    def mesh_region_nodes(ref: dict, owner_node, what: str) -> list:
        """[(kind, region node id)] for a set or surface of a mesh (no geometry): its node, element or element-face
        labels, one Region (Mesh labels) per instance."""
        mesh = ref["mesh"]
        surface = ref.get("kind") == "surface"
        faces = mesh.get("faces") if surface else None
        entries = faces or mesh.get("elements") or mesh.get("nodes") or []
        by_inst: dict = {}
        for e in entries:
            by_inst.setdefault(e[0], []).append(e[1:])
        named = not ref.get("internal")
        out = []
        for inst, rows in sorted(by_inst.items()):
            src = owner_node(inst)
            if src is None:
                g.note(f"{what}: its mesh on {inst or 'the part'} can't be selected (that isn't rebuilt)")
                continue
            body = part_body.get(ref.get("part") or part_of.get(inst, ""), "Solid")
            entity, side = ("Element faces" if faces else "Elements" if mesh.get("elements") else "Nodes"), "Side 1"
            if faces:
                sides = {r[1] for r in rows}
                if sides <= {"SIDE1", "SIDE2", "SPOS", "SNEG"} and len(sides) == 1:  # a side of shell elements
                    entity, side = "Elements", "Side 2" if sides <= {"SIDE2", "SNEG"} else "Side 1"
                    labels = _ranges(r[0] for r in rows)
                elif all(re.fullmatch(r"FACE[1-6]", x) for x in sides):
                    labels = "; ".join(f"S{x[4:]}: {_ranges(r[0] for r in rows if r[1] == x)}" for x in sorted(sides))
                else:
                    g.note(f"{what}: surface {ref['name']} is on element sides {', '.join(sorted(sides))} (Mesh labels "
                           "takes element faces S1 to S6, or one side of shell elements)")
                    continue
            else:
                labels = _ranges(r[0] for r in rows)
            kind = ("vertex" if entity == "Nodes" else "face" if entity == "Element faces" else BODY_KIND.get(body, "cell"))
            create = ("Surface" if surface and kind in ("face", "edge") else "Set") if named else "Selection"
            name = ref["name"] if len(by_inst) == 1 else f"{ref['name']}-{inst}"
            if named and len(by_inst) > 1:
                g.note(f"{what}: {ref['name']} spans {len(by_inst)} instances: rebuilt as one region each ({name}…)")
            rid = _id("region", f"{ref['name']}-{inst}", taken)
            g.add(rid, "model.region", 6 if ref.get("owner") == "assembly" else 3, {
                "method": "Mesh labels", "mesh": entity, "labels": labels, "create": create, "name": name,
                "side": side}, name=ref["name"])
            g.wire(src[0], src[1], rid, "source")
            out.append((kind, rid))
        return out

    def region_nodes(ref: dict | None, owner_node, what: str, form: str = "Set") -> list:
        """[(kind, region node id)] for a set or surface; [] with a note when it can't be selected again."""
        if not ref:
            g.note(f"{what}: its region isn't readable")
            return []
        if ref.get("missing"):
            g.note(f"{what}: its region {ref['name']} isn't in the model")
            return []
        key = (ref.get("owner"), ref.get("part"), ref["name"])
        if key in regions:
            return regions[key]
        if ref.get("mesh") and not ref.get("items"):
            regions[key] = mesh_region_nodes(ref, owner_node, what)
            return regions[key]
        if ref.get("other"):
            g.note(f"{what}: {ref['name']} holds {', '.join(ref['other'])}, not geometry (Region selects faces, "
                   "edges, vertices or cells)")
            regions[key] = []
            return []
        sides = set(ref.get("sides") or [])
        side = "Side 2" if sides == {"SIDE2"} else "Side 1"
        if sides - {"SIDE1", "SIDE2"} or len(sides) > 1:
            g.note(f"{what}: surface {ref['name']} uses sides {', '.join(sorted(sides))}; rebuilt on Side 1")
        surface = ref.get("kind") == "surface"
        out = []
        groups: dict = {}
        for kind, items in ref.get("items", {}).items():
            for inst, point in items:
                groups.setdefault((inst, kind), []).append(point)
        named = not ref.get("internal")
        for (inst, kind), points in sorted(groups.items()):
            src = owner_node(inst)
            if src is None:
                g.note(f"{what}: its {kind}s on {inst or 'the part'} can't be selected (that isn't rebuilt)")
                continue
            create = ("Surface" if surface and kind in ("face", "edge") else "Set") if named else "Selection"
            name = ref["name"] if len(groups) == 1 else f"{ref['name']}-{inst or kind}"
            if named and len(groups) > 1:
                g.note(f"{what}: {ref['name']} spans {len(groups)} instances or kinds: rebuilt as one region each "
                       f"({name}…)")
            rid = _id("region", f"{ref['name']}-{inst}-{kind}", taken)
            at = g.add(rid + "-at", "input.value", 5 if ref.get("owner") == "assembly" else 2, {
                "type": "Text", "value": _pts(points), "label": f"{ref['name']} at"}, name=f"{ref['name']} points")
            g.add(rid, "model.region", 6 if ref.get("owner") == "assembly" else 3, {
                "method": "Find at", "kind": KINDS[kind], "create": create if form != "Selection" else "Selection",
                "name": name, "side": side,
                "tolerance": 0.0 if kind == "cell" else FIND_TOLERANCE}, name=ref["name"])
            g.wire(src[0], src[1], rid, "source")
            g.wire(at, "value", rid, "points")
            out.append((kind, rid))
        regions[key] = out
        return out

    # ---- parts
    part_out: dict = {}  # part name -> (node, handle) of the finished part (sections, mesh)
    part_base: dict = {}  # part name -> (node, handle) of the bare part (where its Regions are found)
    part_body: dict = {}
    for p in m.get("parts", []):
        name = p["name"]
        if p.get("error"):
            g.note(f"Part {name}: couldn't be read ({p['error'].strip().splitlines()[-1]})")
            continue
        space = FLAT_SPACES.get(p.get("space"), "3D")
        flat_sketched = space != "3D" and p.get("base") and p["base"].get("method") == "Planar"
        if (p.get("space") != "THREE_D" and not flat_sketched) or p.get("type") != "DEFORMABLE_BODY":
            g.note(f"Part {name}: a {str(p.get('space')).lower()} {str(p.get('type')).lower()} part (the nodes make "
                   "it from a sketch, not from CAE)")
            continue
        pid = _id("part", name, taken)
        base = p.get("base")
        chains, circles, why = _loops(base["curves"]) if base and base.get("curves") else ([], [], "")
        if base and base.get("curves") and not why:
            later = p.get("features", [])[1:]
            why = (f"it has features after its sketch ({', '.join(later)})" if later else
                   _base_matches(p, base, chains, circles))
        if base and base.get("curves") and not why and (chains or circles):
            sketches = []
            for i, (closed, chain) in enumerate(chains):
                sketches.append(g.add(_id("sketch", f"{name}-{i + 1}", taken), "model.sketch", 1, {
                    "name": f"{name}-profile" + (f"-{i + 1}" if i else ""), "shape": "Profile",
                    "points": _pts(chain), "closed": "Closed" if closed else "Open"}, name=f"{name} profile"))
            for i, (centre, radius) in enumerate(circles):
                sketches.append(g.add(_id("sketch", f"{name}-circle-{i + 1}", taken), "model.sketch", 1, {
                    "name": f"{name}-circle-{i + 1}", "shape": "Circle", "x": centre[0], "y": centre[1],
                    "radius": round(radius, 9)}, name=f"{name} circle"))
            for s in sketches:
                g.wire(model, "model", s, "model")
            for s in sketches[1:]:
                g.wire(s, "sketch", sketches[0], "add")
            params = {"name": name, "body": base["body"], "method": base["method"]}
            if space != "3D":
                params["space"] = space
            if base["method"] == "Extrude":
                params["depth"] = base["depth"]
            if base["method"] == "Revolve":
                params["angle"] = base["angle"]
            g.add(pid, "model.part", 2, params, name=name)
            g.wire(sketches[0], "sketch", pid, "profile")
            body = base["body"]
        elif p.get("inp"):
            types = p.get("element_types") or []
            body = ("Solid" if all(t.startswith("C3D") for t in types) else "Shell" if all(
                t.startswith("S") for t in types) else "Wire" if all(t.startswith("B") for t in types) else "")
            if not body:
                g.note(f"Part {name}: its mesh mixes element types {', '.join(types)}")
                continue
            g.add(pid, "model.part", 2, {"name": name, "body": body, "method": "Import CAD", "file": p["inp"],
                                         "entry": name}, name=name)
            if os.path.abspath(p["inp"]) != os.path.abspath(dump.get("file") or ""):  # a .cae's: written out
                g.note(f"Part {name}: only a mesh in {source}; imported from {p['inp']} (written from it)")
        elif p.get("sat") and p.get("body"):
            body = p["body"]
            g.add(pid, "model.part", 2, {"name": name, "body": body, "method": "Import CAD", "file": p["sat"]},
                  name=name)
            how = (why or "it isn't made from one sketch") if base else "its first feature isn't a sketch"
            g.note(f"Part {name}: imported as geometry from {p['sat']} (written from {source}), since {how}; "
                   f"its features ({', '.join(p.get('features', [])) or 'none'}) aren't editable here")
        else:
            g.note(f"Part {name}: neither sketched nor exportable "
                   f"({p.get('sat_error') or p.get('inp_error') or 'no geometry or mesh'})")
            continue
        g.wire(model, "model", pid, "model")
        part_base[name] = (pid, "part")
        part_body[name] = body
        current = (pid, "part")
        for what in ("reference_points",):
            if p.get(what):
                g.note(f"Part {name}: {p[what]} reference point(s) not rebuilt")
        if p.get("orientations"):
            g.note(f"Part {name}: material orientations not rebuilt (Fibre direction on Material & Section sets "
                   "one by a direction)")
        # sections, in the order they were assigned
        for k, sa in enumerate(p.get("sections", [])):
            sname = sa["section"]
            what = f"Section assignment {k + 1} of {name} ({sname})"
            setting = section_settings.get(sname)
            if setting is None:
                g.note(f"{what}: section {sname} isn't in the model")
                continue
            ref = sa.get("region")
            target = current
            whole = (ref and (ref.get("counts") or {}).get("elements") == p.get("elements") if p.get("inp") else
                     ref and ref.get("whole"))  # a mesh has no geometry to compare: its element count
            if not whole:
                found = region_nodes(ref, lambda inst: part_base[name], what)
                found = [r for r in found if r[0] == BODY_KIND[body]]
                if len(found) != 1:
                    g.note(f"{what}: its region isn't one set of {BODY_KIND[body]}s")
                    continue
                target = None
            sid = _id("section", f"{name}-{sname}", taken)
            if setting[0] == "ok":
                params = dict(setting[1])
            elif setting[0] == "material":
                params = dict(setting[1])
            else:
                params = {"section": sname}
            if params.get("kind") == "Beam":
                n1 = next((o["n1"] for o in p.get("beam_n1", []) if (o.get("region") or {}).get("name") == (
                    ref or {}).get("name")), None) or (p["beam_n1"][0]["n1"] if p.get("beam_n1") else None)
                if n1 is None:
                    g.note(f"{what}: no beam orientation in {source}; n1 is the node's default (0, 0, -1)")
                else:
                    params.update(n1x=n1[0], n1y=n1[1], n1z=n1[2])
            g.add(sid, "model.section", 4, params, name=f"{sname} on {name}")
            if target is None:
                g.wire(found[0][1], "region", sid, "target")
            else:
                g.wire(current[0], current[1], sid, "target")
            if setting[0] == "material":
                g.wire(from_cae("Material", setting[2]), "value", sid, "existing")
            elif setting[0] == "section":
                g.wire(from_cae("Section", sname), "value", sid, "existing")
            current = (sid, "part")
        # mesh
        mesh = p.get("mesh") or {}
        if mesh.get("size") and not p.get("inp"):
            params = {"size": mesh["size"], "deviation": mesh.get("deviation") or 0.1,
                      "minimum": mesh.get("minimum") or 0.1, "generate": "Yes" if p.get("elements") else "No"}
            for key, table, label in (("technique", TECHNIQUES, "technique"), ("shape", SHAPES, "element shape")):
                seen = mesh.get(key) or []
                if len(seen) == 1 and seen[0] in table:
                    params[key] = table[seen[0]]
                elif seen:
                    g.note(f"Mesh of {name}: {label} {', '.join(seen).lower()} isn't one the Mesh node sets; left "
                           "Automatic")
            types = mesh.get("element_types") or []
            if len(types) == 1 and types[0] in ELEMENT_TYPES:
                params["element"] = types[0]
                family = ELEMENT_SHAPES.get(types[0])
                if family and params.get("shape") not in (None, "Automatic", family, family + "-dominated"):
                    params["shape"] = family
            elif types:
                g.note(f"Mesh of {name}: element types {', '.join(types)} aren't ones the Mesh node offers; left "
                       "Automatic")
            mid = _id("mesh", name, taken)
            g.add(mid, "model.mesh", 5, params, name=f"Mesh {name}")
            g.wire(current[0], current[1], mid, "target")
            current = (mid, "part")
            g.note(f"Mesh of {name}: global seed, controls and element type rebuilt; local edge seeds aren't read")
        part_out[name] = current
        # the part's own named sets and surfaces (those not already used)
        for kind_list in ("sets", "surfaces"):
            for r in p.get(kind_list, []):
                ref = dict(r, owner="part", part=name, kind=kind_list[:-1], internal=r["name"].startswith("_"))
                if (("part", name, r["name"]) not in regions and not ref["internal"]
                        and (r.get("items") or r.get("mesh"))):
                    region_nodes(ref, lambda inst, n=name: part_base[n], f"{kind_list[:-1].title()} {r['name']} of {name}")

    # ---- instances
    inst_out: dict = {}
    for inst in m.get("instances", []):
        name, part = inst["name"], inst["part"]
        if part not in part_out:
            g.note(f"Instance {name}: its part {part} isn't rebuilt")
            continue
        if "rotation" not in inst:
            g.note(f"Instance {name}: its position isn't readable")
            continue
        a, b, c = _euler(inst["rotation"])
        t = [0.0 if abs(x) < 1e-9 else round(x, 9) for x in inst["translation"]]
        turns = [(axis, ang) for axis, ang in (("X", a), ("Y", b), ("Z", c)) if ang]
        steps = turns or [("Z", 0.0)]
        prev = part_out[part]
        for k, (axis, ang) in enumerate(steps):
            nid = _id("inst", f"{name}-{k + 1}" if k else name, taken)
            params = {"axis": axis, "angle": ang}
            if k == 0:
                params.update(name=name, dependency="Dependent" if inst.get("dependent") else "Independent")
            if k == len(steps) - 1:
                params.update(x=t[0], y=t[1], z=t[2])
            g.add(nid, "model.instance", 6 if k == 0 else 7, params, name=name)
            g.wire(prev[0], prev[1], nid, "part")
            prev = (nid, "instance")
        inst_out[name] = prev
        if not inst.get("dependent") and inst.get("elements"):
            g.note(f"Instance {name}: independent, meshed in the assembly; the mesh is the part's here (Mesh works "
                   "on parts)")
    inst_of = lambda n: inst_out.get(n)  # noqa: E731

    # ---- steps
    steps: dict = {}
    chain = []
    prev = (model, "model")
    stopped = ""
    for st in m.get("steps", []):
        name = st["name"]
        if st["type"] == "InitialStep":
            continue
        if stopped:
            g.note(f"Step {name}: comes after {stopped}, which isn't rebuilt")
            continue
        params = {"name": name}
        if st["type"] in ("StaticStep", "ImplicitDynamicsStep"):
            params.update(kind="Static" if st["type"] == "StaticStep" else "Dynamic implicit",
                          time=st.get("timePeriod"), nlgeom="Yes" if _on(st.get("nlgeom")) else "No",
                          initial=st.get("initialInc"), maximum=st.get("maxInc"), minimum=st.get("minInc"),
                          increments=st.get("maxNumInc"))
            if st.get("timeIncrementationMethod") not in (None, "AUTOMATIC"):
                g.note(f"Step {name}: fixed time increments; rebuilt with automatic ones")
            if st.get("stabilizationMethod") not in (None, "NONE"):
                g.note(f"Step {name}: stabilization not rebuilt")
        elif st["type"] == "ExplicitDynamicsStep":
            params.update(kind="Dynamic explicit", time=st.get("timePeriod"),
                          nlgeom="Yes" if _on(st.get("nlgeom")) else "No", scale=st.get("scaleFactor") or 1.0)
            scaling = st.get("massScaling") or []
            if len(scaling) == 1 and scaling[0].get("objective") == "SEMI_AUTOMATIC" and scaling[0].get(
                    "type") == "BELOW_MIN" and scaling[0].get("occurs") == "AT_BEGINNING" and not scaling[0].get(
                    "region", "").strip("(),' 0"):
                params["mass_dt"] = scaling[0]["dt"]
            elif scaling:
                g.note(f"Step {name}: mass scaling other than at the start, to a target increment for the whole "
                       "model, not rebuilt")
            if st.get("timeIncrementationMethod") not in (None, "AUTOMATIC_GLOBAL"):
                g.note(f"Step {name}: {st['timeIncrementationMethod']} time incrementation; rebuilt with global "
                       "automatic increments")
        elif st["type"] in ("FrequencyStep", "BuckleStep") and isinstance(st.get("numEigen"), (int, float)):
            params.update(kind="Frequency" if st["type"] == "FrequencyStep" else "Buckling", modes=int(st["numEigen"]))
        else:
            stopped = name
            g.note(f"Step {name}: a {st['type']} (Step makes static, implicit/explicit dynamic and frequency steps)")
            continue
        sid = _id("step", name, taken)
        g.add(sid, "model.step", 7, params, name=name)
        g.wire(prev[0], prev[1], sid, "after")
        prev = (sid, "step")
        steps[name] = sid
        chain.append(name)
    first = chain[0] if chain else ""

    def step_params(sid):
        return next(n for n in g.nodes if n["id"] == sid)["data"]["params"]

    for fo in m.get("field_outputs", []):
        for state in fo.get("states", []):
            if state.get("status") not in ("CREATED", "MODIFIED") or state["step"] not in steps:
                continue
            variables = state.get("variables")
            freq, n = state.get("frequency"), state.get("numIntervals")
            if variables == "PRESELECT" and re.fullmatch(r"F-Output-\d+", fo["name"]) and freq in (0, 1):
                continue  # CAE's own default for the step, which the rebuilt step gets too
            if fo["name"] != "F-Output-1" or fo.get("region") not in (None, "MODEL"):
                g.note(f"Field output {fo['name']} in {state['step']}: only the model-wide F-Output-1 is rebuilt")
                continue
            params = step_params(steps[state["step"]])
            if isinstance(variables, list):
                params["field"] = ", ".join(variables)
            elif variables == "ALL":
                params["field"] = "ALL"
            if state.get("frequencyType") not in (None, "FREQUENCY") and n:
                params["intervals"] = int(n)
            elif state.get("frequencyType") not in (None, "FREQUENCY") or freq not in (None, 1, 1.0):
                g.note(f"Field output in {state['step']}: every {_g(freq)} increments isn't a Step setting; "
                       "written every increment")
    for ho in m.get("history_outputs", []):
        for state in ho.get("states", []):
            if state.get("status") not in ("CREATED", "MODIFIED") or state["step"] not in steps:
                continue
            variables = state.get("variables")
            if re.fullmatch(r"H-Output-\d+", ho["name"]) and variables == "PRESELECT":
                continue  # Abaqus' own default, which the rebuilt step gets too
            if ho.get("region") not in (None, "MODEL") or not isinstance(variables, list):
                g.note(f"History output {ho['name']} in {state['step']}: only model-wide variable lists are rebuilt")
                continue
            params = step_params(steps[state["step"]])
            params["history"] = ", ".join(dict.fromkeys(
                [*filter(None, params.get("history", "").split(", ")), *variables]))

    # ---- loads and boundary conditions
    amplitudes = {a["name"]: a for a in m.get("amplitudes", [])}
    col_rows = 8
    amp_nodes: dict[str, str] = {}

    def amplitude_node(a: dict) -> str:
        """An Amplitude node for CAE amplitude `a` (one per amplitude, shared by the conditions using it)."""
        if a["name"] in amp_nodes:
            return amp_nodes[a["name"]]
        kind = AMPLITUDE_TYPES[a["type"]]
        params = {"kind": kind, "time_span": "Total time" if a.get("timeSpan") == "TOTAL" else "Step time"}
        data = a.get("data") or []
        if kind in ("Tabular", "Smooth step"):
            params["points"] = _pts(data)
        elif kind == "Equally spaced":
            params.update(values=", ".join(_g(v) for v in data), interval=a.get("fixedInterval"),
                          begin=a.get("begin") or 0.0)
        elif kind == "Periodic":
            params.update(terms=_pts(data), frequency=a.get("frequency"), start=a.get("start") or 0.0,
                          initial=a.get("a_0") or 0.0)
        elif kind == "Modulated":
            params.update({k: a.get(k) for k in ("initial", "magnitude", "start", "frequency1", "frequency2")})
        elif kind == "Exponential decay":
            params.update(initial=a.get("initial"), magnitude=a.get("maximum"), start=a.get("start"),
                          decay=a.get("decayTime"))
        elif kind == "Solution dependent":
            params.update(factor=a.get("initial"), minimum=a.get("minimum"), maximum=a.get("maximum"))
        else:
            params["variables"] = int(a.get("numVariables") or 0)
        if kind in ("Tabular", "Equally spaced") and isinstance(a.get("smooth"), (int, float)):
            params["smoothing"] = _g(a["smooth"])
        params = {k: v for k, v in params.items() if v is not None}
        aid = _id("amp", a["name"], taken)
        g.add(aid, "model.amplitude", col_rows - 1, params, name=a["name"])
        amp_nodes[a["name"]] = aid
        return aid

    def owner(inst_name: str):
        return inst_of(inst_name)

    def condition(entry: dict, what: str, params: dict, needs_region: bool = True,
                  kinds: tuple = ("face", "edge", "vertex", "cell")) -> None:
        states = entry.get("states", [])
        created = next((s for s in states if s.get("status") == "CREATED"), None)
        if created is None:
            g.note(f"{what}: never created in a step")
            return
        step = created["step"]
        if step == "Initial":
            if not first:
                g.note(f"{what}: made in Initial, and no step is rebuilt")
                return
            g.note(f"{what}: made in Initial in {source}; rebuilt in the first step, {first} (same effect)")
            step = first
        if step not in steps:
            g.note(f"{what}: its step {step} isn't rebuilt")
            return
        later = [s for s in states if s.get("status") not in QUIET
                 and not (s.get("status") == "MODIFIED" and s["values"].get("boltMethod") == "FIX_LENGTH")]
        if later:
            g.note(f"{what}: changed in later steps ({', '.join(s['step'] + ' ' + str(s['status']).lower() for s in later)}); "
                   "only how it was made is rebuilt")
        amp = created["values"].get("amplitude")
        amp_node = None
        if amp and amp not in ("", "UNSET", "Ramp", "RAMP"):
            a = amplitudes.get(amp)
            if (a and a.get("timeSpan") == "STEP" and a["type"] in ("TabularAmplitude", "SmoothStepAmplitude")
                    and a.get("smooth") in (None, "SOLVER_DEFAULT")):
                params["amplitude"] = _pts(a["data"])
                params["amplitude_shape"] = "Smooth step" if a["type"] == "SmoothStepAmplitude" else "Tabular"
            elif a and a["type"] in AMPLITUDE_TYPES:
                amp_node = amplitude_node(a)
            else:
                g.note(f"{what}: amplitude {amp} ({a['type'] if a else 'not found'}) isn't rebuilt; rebuilt "
                       "without it")
        if entry.get("distributionType") not in (None, "UNIFORM"):
            g.note(f"{what}: not uniform over space ({entry['distributionType'].lower()}); rebuilt uniform")
        if entry.get("localCsys") not in (None, "None"):
            g.note(f"{what}: in a local coordinate system, which isn't rebuilt")
            return
        targets = [None]
        if needs_region:
            found = region_nodes(entry.get("region"), owner, what)
            targets = [r for r in found if r[0] in kinds]
            if len(targets) != len(found):
                g.note(f"{what}: acts on {', '.join(sorted({r[0] for r in found} - set(kinds)))}s, which this kind "
                       "can't take; left out")
            if not targets:
                if found or entry.get("region"):
                    g.note(f"{what}: nothing it can act on was rebuilt; left out")
                return
        for k, target in enumerate(targets):
            name = entry["name"] if len(targets) == 1 else f"{entry['name']}-{k + 1}"
            cid = _id("cond", name, taken)
            g.add(cid, "model.condition", col_rows, dict(params, name=name), name=name)
            g.wire(steps[step], "step", cid, "step")
            if amp_node is not None:
                g.wire(amp_node, "amplitude", cid, "amplitude")
            if target is not None:
                g.wire(target[1], "region", cid, "region")

    for bc in m.get("bcs", []):
        what = f"Boundary condition {bc['name']}"
        created = next((s for s in bc.get("states", []) if s.get("status") == "CREATED"), {"values": {}})
        values = created["values"]
        if bc["type"] == "EncastreBC" or (bc["type"] == "TypeBC" and values.get("typeName") == "ENCASTRE"):
            condition(bc, what, {"kind": "Fixed"})
        elif bc["type"] == "TypeBC" and values.get("typeName") in TYPE_BCS:
            from .nodes import FIXES
            fix = next(label for label, name in FIXES.items() if name == values["typeName"])
            condition(bc, what, {"kind": "Fixed", "fix": fix})
        elif bc["type"] in ("DisplacementBC", "PinnedBC") or bc["type"].endswith("SymmBC"):
            if bc.get("fixed") == "ON":
                g.note(f"{what}: holds the displacement at the start of the step, which isn't rebuilt")
                continue
            comps = {k: values.get(k) for k in DOFS}
            if all(v is None for v in comps.values()):
                g.note(f"{what}: sets no component")
                continue
            condition(bc, what, {"kind": "Displacement / rotation",
                                 **{k: "Free" if v is None else _g(v) for k, v in comps.items()}})
        else:
            g.note(f"{what}: a {bc['type']} (Load / Boundary Condition makes fixed and displacement ones)")
    for ld in m.get("loads", []):
        what = f"Load {ld['name']}"
        created = next((s for s in ld.get("states", []) if s.get("status") == "CREATED"), {"values": {}})
        v = created["values"]
        if ld["type"] == "ConcentratedForce":
            condition(ld, what, {"kind": "Concentrated force", "x": v.get("cf1") or 0.0, "y": v.get("cf2") or 0.0,
                                 "z": v.get("cf3") or 0.0}, kinds=("vertex",))
        elif ld["type"] == "Pressure":
            condition(ld, what, {"kind": "Pressure", "magnitude": v.get("magnitude")}, kinds=("face",))
        elif ld["type"] == "Gravity":
            if ld.get("region"):
                g.note(f"{what}: on a region; rebuilt on the whole model")
            condition(ld, what, {"kind": "Gravity", "x": v.get("comp1") or 0.0, "y": v.get("comp2") or 0.0,
                                 "z": v.get("comp3") or 0.0}, needs_region=False)
        elif ld["type"] == "BoltLoad" and v.get("boltMethod") in (None, "APPLY_FORCE"):
            fixed = any(s["values"].get("boltMethod") == "FIX_LENGTH" for s in ld.get("states", []))
            condition(ld, what, {"kind": "Bolt load", "force": v.get("magnitude"),
                                 "then": "Fix its length" if fixed else "Keep the force"}, kinds=("face",))
        else:
            g.note(f"{what}: a {ld['type']} (Load / Boundary Condition makes forces, pressures, gravity and bolt "
                   "loads)")

    # ---- interactions and constraints
    props = {p["name"]: p for p in m.get("interaction_properties", [])}

    def faces(ref, what):
        found = region_nodes(ref, owner, what, "Surface")
        return found[0][1] if len(found) == 1 and found[0][0] == "face" else None

    def interaction(name: str, params: dict, step: str | None, main=None, secondary=None):
        iid = _id("int", name, taken)
        g.add(iid, "model.interaction", 8, dict(params, name=name), name=name)
        if step:
            g.wire(steps[step], "step", iid, "step")
        for handle, rid in (("master", main), ("slave", secondary)):
            if rid:
                g.wire(rid, "region", iid, handle)

    for c in m.get("constraints", []):
        what = f"Constraint {c['name']}"
        if c["type"] == "Tie":
            main, sec = faces(c.get("main"), what), faces(c.get("secondary"), what)
            if not (main and sec):
                g.note(f"{what}: a tie between surfaces other than one set of faces each")
                continue
            interaction(c["name"], {"kind": "Tie", "adjust": "Yes" if _on(c.get("adjust")) else "No"}, None,
                        main, sec)
        elif c["type"] == "Coupling":
            pts = (c.get("controlPoint") or {}).get("reference_points") or []
            surf = faces(c.get("surface"), what)
            kind = {"KINEMATIC": "Kinematic coupling", "DISTRIBUTING": "Distributing coupling"}.get(
                c.get("couplingType") or "")
            radius = c.get("influenceRadius")
            try:
                radius = 0.0 if radius in (None, "WHOLE_SURFACE") else float(radius)
            except (TypeError, ValueError):
                radius = None
            if len(pts) != 1 or not surf or not kind or radius is None:
                g.note(f"{what}: a coupling the Interaction node can't rebuild (one reference point to a face "
                       "surface, kinematic or distributing)")
                continue
            dofs = [d.upper() for d in DOFS if _on(c.get(d))]
            interaction(c["name"], {"kind": kind, "point": _pts(pts), "dofs": ", ".join(dofs) or "All",
                                    "influence": radius},
                        None, None, surf)
        else:
            g.note(f"{what}: a {c['type']} (Interaction rebuilds ties and couplings)")
    for it in m.get("interactions", []):
        what = f"Interaction {it['name']}"
        step = it.get("step") or it.get("createStepName")
        if step == "Initial":
            step = first
        prop = props.get(it.get("property") or it.get("interactionProperty") or it.get("global_property") or "")
        friction, why = None, ""
        if prop:
            fr = prop.get("friction")
            if fr is None or fr.get("formulation") in (None, "FRICTIONLESS"):
                friction = 0.0
            elif fr.get("formulation") == "PENALTY" and len(fr.get("table") or []) == 1 and len(fr["table"][0]) == 1:
                friction = fr["table"][0][0]
            else:
                why = "its friction isn't one penalty coefficient"
            other = set(prop.get("behaviours", [])) - {"tangentialBehavior", "normalBehavior"}
            normal = prop.get("normal") or {}
            if other or normal.get("pressureOverclosure") not in (None, "HARD") or normal.get(
                    "allowSeparation") not in (None, "ON"):
                why = why or "its property has more than hard contact and friction"
        if it["type"] in ("SurfaceToSurfaceContactStd", "SurfaceToSurfaceExp"):  # Exp: its CAE type name
            main, sec = faces(it.get("main"), what), faces(it.get("secondary"), what)
            if why or friction is None or not (main and sec) or step not in steps:
                g.note(f"{what}: " + (why or "not between two face surfaces in a rebuilt step"))
                continue
            interaction(it["name"], {"kind": "Frictional contact" if friction else "Frictionless contact",
                                     "friction": friction, "sliding": "Small" if it.get("sliding") == "SMALL" else "Finite",
                                     "adjust": "No" if it.get("adjustMethod") in ("NONE", None) else "Yes"},
                        step, main, sec)
        elif it["type"] in ("ContactStd", "ContactExp"):
            if why or friction is None or not first or it.get("included") not in (None, "ON", "True"):
                g.note(f"{what}: " + (why or "general contact other than everything with everything"))
                continue
            interaction(it["name"], {"kind": "General contact", "friction": friction}, first)
        else:
            g.note(f"{what}: a {it['type']} (Interaction rebuilds surface contact and general contact)")

    # ---- what else the model has
    a = m.get("assembly", {})
    for kind_list in ("sets", "surfaces"):
        for r in a.get(kind_list, []):
            if ("assembly", "", r["name"]) not in regions and (r.get("items") or r.get("mesh")):
                region_nodes(dict(r, owner="assembly", part="", kind=kind_list[:-1], internal=False), owner,
                             f"Assembly {kind_list[:-1]} {r['name']}")
    features = [f for f in a.get("features", []) if not (inp and f == "Datum csys-1")]  # the import's own csys
    if features:
        g.note(f"Assembly features not rebuilt: {', '.join(features)}")
    for repo, names in (m.get("other") or {}).items():
        g.note(f"Not rebuilt ({repo}): {', '.join(names)}")
    assigned = {sa["section"] for p in m.get("parts", []) for sa in p.get("sections", [])}
    if set(sections) - assigned:
        g.note(f"Sections not assigned to any part (not rebuilt): {', '.join(sorted(set(sections) - assigned))}")
    used = {sections[n].get("material") for n in assigned if n in sections} | {
        ply["material"] for n in assigned if n in sections for ply in sections[n].get("layup") or []}
    if set(materials) - used:
        g.note(f"Materials not used by an assigned section (not rebuilt): {', '.join(sorted(set(materials) - used))}")

    # ---- jobs
    last = (steps[chain[-1]], "step") if chain else (model, "model")
    for job in dump.get("jobs", []):
        params = {"name": job["name"], "cpus": job.get("cpus", 1)}
        if job.get("memoryUnits") in (None, "PERCENTAGE"):
            params["memory"] = job.get("memory", 90)
        if job.get("type") not in (None, "ANALYSIS"):
            g.note(f"Job {job['name']}: a {job['type'].lower()} job; rebuilt as an analysis")
        jid = _id("job", job["name"], taken)
        g.add(jid, "model.job", 9, params, name=job["name"])
        g.wire(last[0], last[1], jid, "analysis")

    if g.notes:
        g.nodes.insert(0, {"id": "cae-notes", "type": "op", "position": {"x": 0, "y": -2 * ROW},
                           "data": {"type": "input.value", "name": f"Not rebuilt from {source}",
                                    "params": {"type": "Text", "value": "\n".join(g.notes),
                                               "label": "Not rebuilt"}}})
    return {"graph": {"nodes": g.nodes, "edges": g.edges}, "notes": g.notes, "model": model_name}


def payload(graph: dict) -> dict:
    """The editor graph as the engine takes it (what the editor sends)."""
    return {"nodes": [{"id": n["id"], "type": n["data"]["type"], "params": n["data"]["params"]}
                      for n in graph["nodes"] if n.get("type") == "op"],
            "edges": [{k: e[k] for k in ("source", "sourceHandle", "target", "targetHandle")} for e in graph["edges"]]}
