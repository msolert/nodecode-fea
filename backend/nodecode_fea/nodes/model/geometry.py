"""Model, Sketch, Part, Datum and Part Feature: the geometry of a part."""

from __future__ import annotations

import math
import os
import re
import numpy as np
from dataclasses import replace

from ...values import (WALL_SIDES, sketch_segments, wall_items, AnalysisStep, Condition, DatumPlane, Instance,
    Interaction, Job, MergedPart, Model, Number, Part, PartOperation, Region, Series, Sketch, Text)
from ..core import Param, Port, register
from ..common import _finite, _flat_items, _is_matrix
from ..field import parse_points


# ------------------------------------------------------------- modelling
BODIES = ["Shell", "Wire", "Solid"]
METHODS = ["Planar", "Extrude", "Revolve", "Import CAD"]
SPACES = ["3D", "2D planar", "Axisymmetric"]
PART_TYPES = ["Deformable", "Discrete rigid", "Analytical rigid"]
CAD_EXTENSIONS = (".step", ".stp", ".iges", ".igs", ".sat", ".sab")
IMPORT_EXTENSIONS = CAD_EXTENSIONS + (".inp",)  # an input file brings a mesh (no geometry)
SHAPES = ["Rectangle", "Circle", "Arc", "Polygon", "Ellipse", "Slot", "Spline", "Profile"]
AXES = ["X", "Y", "Z"]
DEFAULT_MODEL = "NoCode-Model"


def _abaqus_name(text: str, fallback: str) -> str:
    """A name CAE accepts: letters, digits, - and _, starting with a letter."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(text or "").strip()).strip("-")
    return name if name and name[0].isalpha() else fallback


def _profile_points(v, text: str = "") -> np.ndarray:
    """Profile points as (n, 3): typed 'x, y; x, y; …' (text or a wired Text), a matrix (Input, Formula) or a curve
    (its x, y points)."""
    if v is None:
        return parse_points(text, 2)
    if isinstance(v, Text):
        return parse_points(v.value, 2)
    if isinstance(v, Series):
        pts = np.column_stack([v.x, v.y])
        pts = pts[np.isfinite(pts).all(axis=1)]
    elif _is_matrix(v):
        pts = np.array([[float(x.value) for x in r] for r in v])
    else:
        raise ValueError("Points take a matrix (one row x, y[, z] per point), a curve, or text like "
                         "0, 0; 100, 0; 100, 50")
    if pts.ndim != 2 or pts.shape[1] not in (2, 3) or len(pts) < 2:
        raise ValueError("Points need at least two rows of x, y (or x, y, z)")
    return np.hstack([pts, np.zeros((len(pts), 3 - pts.shape[1]))])


def _geometry_points(v) -> np.ndarray:
    """One or more 2D/3D points for CAE findAt (unlike profiles, a single point is valid)."""
    if isinstance(v, Text):
        return parse_points(v.value, 1)
    if isinstance(v, Series):
        pts = np.column_stack([v.x, v.y])
    elif _is_matrix(v):
        pts = np.array([[float(x.value) for x in row] for row in v])
    elif isinstance(v, list) and v and all(isinstance(x, Number) for x in v):
        pts = np.array([[float(x.value) for x in v]])
    else:
        raise ValueError("Find at takes text or a matrix with one x, y[, z] point per row")
    if pts.ndim != 2 or pts.shape[1] not in (2, 3) or not len(pts):
        raise ValueError("Find at needs one or more rows of x, y (or x, y, z)")
    pts = np.hstack([pts, np.zeros((len(pts), 3 - pts.shape[1]))])
    if not np.isfinite(pts).all():
        raise ValueError("Find at points must be finite")
    return pts


def _flat_points(pts: np.ndarray, what: str) -> tuple:
    if not np.isfinite(pts).all():
        raise ValueError(f"{what} has points that are infinite or NaN: a division by zero upstream?")
    if np.ptp(pts[:, 2]) > 0 or pts[0, 2] != 0:
        raise ValueError(f"{what} is drawn in the XY plane (z = 0); for a 3D wire (beam frame), wire the points "
                         "straight into Part's Profile")
    return tuple((round(float(x), 9), round(float(y), 9)) for x, y, _ in pts)


def _numbered(base: str, params: dict, name_param: str = "name", model: Model | None = None) -> str:
    """A name for something made once per item of a list: Part-1 run over 10 profiles makes Part-1-1 ... Part-1-10
    (a grid: Part-1-2-3), unless the names themselves come from a list, or it goes into a model made for this
    same item (a study with one model per design: each has its own Part-1)."""
    path = params.get("__path__") or ()
    if not path or name_param in (params.get("__varied__") or ()):
        return base
    if model is not None and model.path and tuple(path) == tuple(model.path):
        return base
    return base + "-" + "-".join(str(i + 1) for i in path)


def _item(part: Part, params: dict) -> tuple:
    """The list item an edit of a part (Mesh, Material & Section, Part Feature) is made for: run over a list (sizes
    10, 5) each item's edit makes its own part; one edit, or edits on side-by-side branches, stay that one part."""
    path = tuple(params.get("__path__") or ())
    return path if len(path) >= len(part.item) else part.item


def _sketch_display(sk: Sketch, title: str = "") -> dict:
    return {"kind": "sketch", "title": title or sk.summary(),
            "lines": [[[float(x), float(y)] for x, y in line] for line in sk.outline()]}


# Features that change a part's shape; the 3D preview draws the base shape and names these.
SHAPE_FEATURES = ("Fillet", "Chamfer", "Extrude add", "Extrude cut")


def _body3d(part: Part, instance: Instance | None = None) -> dict | None:
    """What the editor's 3D preview draws of a part (placed by an instance): its profile and how it is made, else
    None (an imported or merged part, or one picked from CAE: its shape isn't known here)."""
    if part.existing or part.method == "Import" or isinstance(part, MergedPart) or (part.sketch is None and not part.points):
        return None
    return {
        "part": part.name, "body": part.body, "method": part.method,
        "loops": [[[float(x), float(y)] for x, y in line] for line in part.sketch.outline()] if part.sketch else [],
        "points": [[float(c) for c in p] for p in part.points], "depth": float(part.depth), "angle": float(part.angle),
        "steps": [[st[0], *([[float(c) for c in st[1]]] if st[0] == "move" else
                            [st[1], float(st[2]), [float(c) for c in st[3]]])]
                  for st in (instance.steps if instance else ()) if st[0] != "align"],
        "instance": (instance.origin or instance.name) if instance else "",
        "features": [op.name for op in part.operations if op.kind in SHAPE_FEATURES] + [
            f"{st[1]} to {st[3].source.name}" for st in (instance.steps if instance else ()) if st[0] == "align"],
    }


def _model3d(v: Part | Instance, title: str) -> dict | None:
    """A part or instance as the editor's 3D preview: the instance with the model's other instances around it."""
    part = v.part if isinstance(v, Instance) else v
    body = _body3d(part, v if isinstance(v, Instance) else None)
    if body is None:
        return None
    return {"kind": "model3d", "title": title, "model": part.model.name,
            "focus": body["instance"], "bodies": [body], "markers": []}


def _reference_points(part: Part) -> list:
    """A part's reference points, for the 3D preview (drawn like datum points, named)."""
    return [{"name": op.name, "kind": "point", "origin": [float(c) for c in op.point]}
            for op in part.operations if op.kind == "Reference point"]


def _model_value_display(v) -> dict:
    if isinstance(v, Model):
        return {"kind": "info", "title": f"Model {v.name}", "items": [["In CAE", "Built as you add parts to it"]]}
    if isinstance(v, Sketch):
        return _sketch_display(v)
    if isinstance(v, Part):
        shown = _model3d(v, f"{v.name} in {v.model.name}: {v.summary()}")
        if shown:
            shown["datums"] = _reference_points(v)
            return shown
        if v.sketch is not None:
            return _sketch_display(v.sketch, f"{v.name} in {v.model.name}: {v.summary()}")
        return {"kind": "info", "title": f"Part {v.name}", "items": [
            ["Model", v.model.name], ["Body", v.effective_body + (f" (from a {v.body.lower()})"
                                                                  if v.effective_body != v.body else "")],
            ["Shape", v.summary()],
            ["Points", "; ".join(", ".join(f"{c:g}" for c in p) for p in v.points[:20])]]}
    if isinstance(v, Region):
        # Picked by points: the points on the part / assembly in 3D, so a wrong pick shows.
        shown = None if v.dynamic or v.indices else _model3d(v.source, f"{v.summary()} on {v.source.name}")
        if shown and v.kind == "reference point":
            shown["datums"] = [d for d in _reference_points(v.source) if d["name"] == v.feature.name]
            return shown
        if shown:
            shown["markers"] = [[float(c) for c in p] for p in v.points if len(p) == 3]
            return shown
        return {"kind": "info", "title": v.summary(), "items": [
            ["On", v.source.name], ["Geometry", v.kind.title()],
            ["Selection", ", ".join(map(str, v.indices)) if v.indices else
             v.summary() if v.dynamic else "; ".join(", ".join(f"{c:g}" for c in p) for p in v.points)]]}
    if isinstance(v, DatumPlane):
        shown = _model3d(v.part, f"{v.name}: {v.summary()}") if isinstance(v.part, (Part, Instance)) else None
        if shown:
            shown["datums"] = [v.geometry()]
            return shown
        detail = ([["Plane", v.plane], ["Offset", f"{v.offset:g}"]] if v.kind == "Principal plane" else
                  [["Axis", v.plane]] if v.kind == "Principal axis" else
                  [["Face", v.region.summary()], ["Offset", f"{v.offset:g}"]] if v.kind == "Offset from face" else
                  [["Points", "; ".join(", ".join(f"{c:g}" for c in p) for p in v.points)]])
        level = "Part" if isinstance(v.part, Part) else "Assembly"
        return {"kind": "info", "title": v.name, "items": [[level, v.part.name], ["Type", v.kind], *detail]}
    if isinstance(v, AnalysisStep):
        return {"kind": "info", "title": f"Step {v.name}", "items": [
            ["Model", v.model.name], ["Procedure", v.kind], ["Previous", v.previous.name if v.previous else "Initial"],
            ["Time", f"{v.time:g}" if not v.perturbation else f"{v.modes} modes"]]}
    if isinstance(v, Condition):
        return {"kind": "info", "title": v.name, "items": [
            ["Step", v.step.name], ["Type", v.kind], ["Target", v.region.summary() if v.region else "Whole model"]]}
    if isinstance(v, Interaction):
        return {"kind": "info", "title": v.name, "items": [
            ["Step", v.step.name if v.step is not None else "Model constraint"], ["Type", v.kind],
            *([["Control point", v.master.summary() if v.master is not None else
                ", ".join(f"{c:g}" for c in v.point)], ["Surface", v.slave.summary()], ["DOFs", ", ".join(v.dofs)]]
              if v.coupling else [["Connection", v.summary()],
                                  ["Stiffness", ", ".join(f"{c}: {k:g}" for c, k in v.stiffness) or "None"],
                                  ["Damping", ", ".join(f"{c}: {k:g}" for c, k in v.damping) or "None"]]
              if v.kind == "Connector" else [["Contact", v.summary()], ["Friction", f"{v.friction:g}"]]
              if v.kind == "General contact" else [["Constraint", v.summary()]]
              if v.kind in ("Rigid body", "Point mass", "Equation") else
              [["Master", v.master.summary()], ["Slave", v.slave.summary()]])]}
    if isinstance(v, Job):
        return {"kind": "info", "title": f"Job {v.name}", "items": [
            ["Model", v.model.name], ["Runs", v.action], ["CPUs", str(v.cpus)],
            ["ODB", os.path.join(v.directory, v.name + ".odb")]]}
    if isinstance(v, Instance) and v.removed:
        return {"kind": "info", "title": f"Removed instance {v.name}", "items": [
            ["Model", v.part.model.name], ["Part", v.part.name], ["Assembly", "Not placed"]]}
    return _model3d(v, f"Instance {v.name} of {v.part.name}" + (f": {v.summary().split(', ', 1)[1]}" if v.steps else "")) or {
        "kind": "info", "title": f"Instance {v.name}", "items": [
            ["Model", v.part.model.name], ["Part", v.part.name], ["Placed", v.summary()]]}


def _graph_model(inputs, fallback=None):
    """The graph's Model (the engine passes it to a Sketch / Part / Step whose own isn't wired; with no Model node,
    a sketch, part or instance chosen from CAE, whose model it is), else fallback."""
    model = inputs.get("__default_model")
    if isinstance(model, (Sketch, Part)):
        model = model.model
    elif isinstance(model, Instance):
        model = model.part.model
    return model if isinstance(model, Model) else fallback


@register("model.model", "Model", "Model",
          "Start a model to build in Abaqus/CAE. Wire it into Part (and later materials, steps…): what you add "
          "is built in CAE as you go, and a change rebuilds only what depends on it. It can start from an Abaqus "
          "input file (.inp: import a whole model to check, change and run). Parts with no Model go into "
          "NoCode-Model. Needs Abaqus/CAE (Launch picks CAE automatically).",
          inputs=[], outputs=[Port("model", "model", "Model")],
          params=[Param("name", "text", DEFAULT_MODEL, "Name"),
                  Param("file", "file", "", "From input file", options=[".inp"],
                        help="Start from an Abaqus input file (.inp): its parts, materials, sections, assembly, steps "
                             "and loads, imported as this model (choose its parts or instances with an Input set "
                             "to Part / Instance in CAE). Empty: a new, empty model.")])
def model_model(inputs, params):
    typed = str(params.get("file") or "").strip()
    path = os.path.abspath(os.path.expanduser(typed)) if typed else ""
    if path:
        if not path.lower().endswith(".inp"):
            raise ValueError("A model starts from an Abaqus input file (.inp)")
        if not os.path.isfile(path):
            raise ValueError(f"Input file not found: {path}")
    return {"model": Model(_abaqus_name(params["name"], DEFAULT_MODEL), path=tuple(params.get("__path__") or ()),
                           file=path)}


@register("model.sketch", "Sketch", "Model",
          "Draw a flat profile in the XY plane for Part: rectangle (sharp or rounded corners), circle, arc, regular "
          "polygon (triangle, square, hexagon… n sides), ellipse, slot (obround), spline, or polyline through "
          "points (typed, a matrix, a curve). Lists of positions or sizes make patterns of shapes (with For and "
          "Formula). Add other sketches to combine them: a loop inside "
          "a closed loop becomes a hole (a plate with a hole is a rectangle with a circle added). Wall thickness "
          "offsets the shape into a wall (tube, hollow or thin-walled section, channel or C-section from a "
          "centreline). Model: the model "
          "it is built in (parts made from it go there too, unless their own Model is wired).",
          inputs=[Port("model", "model", "Model", optional=True,
                       help="The model to build the sketch in; Part takes it from the sketch when its own Model "
                            "isn't wired. None: the graph's Model node, else the parts' model, or NoCode-Model."),
                  Port("add", "sketch", "Add", optional=True, multi=True),
                  Port("points", "any", "Points", optional=True, depth=0, table=True,
                       when={"shape": ["Profile", "Spline"]})],
          outputs=[Port("sketch", "sketch", "Sketch")],
          params=[Param("name", "text", "Sketch-1", "Name",
                        help="Its name in CAE; a list of sizes makes Sketch-1-1, Sketch-1-2…"),
                  Param("shape", "select", "Rectangle", "Shape", options=SHAPES),
                  Param("x", "number", 0.0, "X",
                        help="Where it goes: a rectangle's lower-left corner, the centre of the other shapes; a "
                             "profile's points are moved by X, Y"),
                  Param("y", "number", 0.0, "Y"),
                  Param("width", "number", 100.0, "Width (x)", when={"shape": ["Rectangle", "Ellipse", "Slot"]},
                        help="Overall size along x (a slot rounds the ends of its longer side)"),
                  Param("height", "number", 50.0, "Height (y)", when={"shape": ["Rectangle", "Ellipse", "Slot"]}),
                  Param("corner", "number", 0.0, "Corner radius", when={"shape": ["Rectangle", "Polygon", "Profile"]},
                        help="Rounds every corner with a tangent arc (fillet; 0: sharp). Rectangle: at most half the "
                             "shorter side; polygon and profile: as far as each corner allows"),
                  Param("radius", "number", 25.0, "Radius", when={"shape": ["Circle", "Arc", "Polygon"]},
                        help="Polygon: to its corners (circumscribed)"),
                  Param("sides", "int", 6, "Sides", when={"shape": ["Polygon"]}),
                  Param("rotation", "number", 0.0, "Rotation (°)", when={"shape": ["Polygon", "Ellipse", "Slot"]},
                        help="Turns the shape about its centre, counterclockwise (a polygon starts with a corner up)"),
                  Param("start", "number", 0.0, "Start angle (°)", when={"shape": ["Arc"]}),
                  Param("end", "number", 90.0, "End angle (°)", when={"shape": ["Arc"]}),
                  Param("direction", "select", "Counterclockwise", "Direction",
                        options=["Counterclockwise", "Clockwise"], when={"shape": ["Arc"]}),
                  Param("points", "text", "0, 0; 100, 0; 100, 50", "Points",
                        help="Profile/spline control points x, y, separated by semicolons; used when Points isn't wired",
                        when={"shape": ["Profile", "Spline"]}),
                  Param("closed", "select", "Open", "Profile", options=["Open", "Closed"],
                        help="Closed joins the last point to the first (needed for flat shells and solids)",
                        when={"shape": ["Profile", "Spline"]}),
                  Param("wall", "number", 0.0, "Wall thickness",
                        when={"shape": ["Rectangle", "Circle", "Polygon", "Slot", "Profile"]},
                        help="Offset: draw the shape as a wall this thick. A closed shape gets an offset copy "
                             "(the space between is the wall: a tube's section); an open profile becomes a closed "
                             "band along it (a channel from its centreline). 0: no wall"),
                  Param("wall_side", "select", "Outside", "Wall side", options=WALL_SIDES,
                        when={"shape": ["Rectangle", "Circle", "Polygon", "Slot", "Profile"]},
                        help="Outside / Inside the shape (an open profile: to the left / right walking from its "
                             "first point), or Centred on it")],
          display=lambda o, i, p: _sketch_display(o["sketch"]))
def model_sketch(inputs, params):
    _finite(params, {"x": "X", "y": "Y", "width": "Width", "height": "Height", "radius": "Radius",
                     "start": "Start angle", "end": "End angle", "corner": "Corner radius", "rotation": "Rotation"})
    shape, x, y = params["shape"], float(params["x"]), float(params["y"])
    rotation = float(params.get("rotation") or 0.0)
    if shape in ("Rectangle", "Ellipse", "Slot"):
        w, h = float(params["width"]), float(params["height"])
        if w <= 0 or h <= 0:
            raise ValueError("Width and height must be positive")
    if shape == "Rectangle":
        corner = float(params.get("corner") or 0.0)
        if corner < 0 or corner > min(w, h) / 2 + 1e-12:
            raise ValueError(f"Corner radius goes from 0 to half the shorter side ({min(w, h) / 2:g})")
        if math.isclose(corner, min(w, h) / 2) and math.isclose(w, h):
            raise ValueError("That corner radius makes a circle: use Circle")
        item = ("Rectangle", (x, y), (x + w, y + h)) + ((corner,) if corner > 0 else ())
    elif shape == "Ellipse":
        item = ("Ellipse", (x, y), w, h, rotation)
    elif shape == "Slot":
        if math.isclose(w, h):
            raise ValueError("A slot with equal width and height is a circle: use Circle")
        item = ("Slot", (x, y), w, h, rotation)
    elif shape == "Polygon":
        r, sides = float(params["radius"]), int(params.get("sides") or 0)
        if r <= 0:
            raise ValueError("Radius must be positive")
        if not 3 <= sides <= 360:
            raise ValueError("A polygon has 3 to 360 sides")
        corner = float(params.get("corner") or 0.0)
        if corner < 0:
            raise ValueError("Corner radius can't be negative")
        item = ("Polygon", (x, y), r, sides, rotation) + ((corner,) if corner > 0 else ())
    elif shape == "Circle":
        r = float(params["radius"])
        if r <= 0:
            raise ValueError("Radius must be positive")
        item = ("Circle", (x, y), r)
    elif shape == "Arc":
        r, start, end = float(params["radius"]), float(params["start"]), float(params["end"])
        if r <= 0:
            raise ValueError("Radius must be positive")
        if math.isclose((end - start) % 360.0, 0.0, abs_tol=1e-9):
            raise ValueError("Arc start and end angles must be different; use Circle for a full loop")
        item = ("Arc", (x, y), r, start, end, str(params["direction"]))
    else:
        pts = _flat_points(_profile_points(inputs.get("points"), str(params["points"])), "A sketch")
        pts = tuple((round(px + x, 9), round(py + y, 9)) for px, py in pts)
        closed = params["closed"] == "Closed"
        if closed and len(pts) < 3:
            raise ValueError("A closed profile needs at least 3 points")
        item = (shape, pts, closed)
        corner = float(params.get("corner") or 0.0) if shape == "Profile" else 0.0
        if corner < 0:
            raise ValueError("Corner radius can't be negative")
        if corner > 0:
            item += (corner,)
    sketch_segments(item)  # a corner radius too big for a corner says so here, not in CAE
    items = (item,)
    _finite(params, {"wall": "Wall thickness"})
    wall = float(params.get("wall") or 0.0)
    if wall < 0:
        raise ValueError("Wall thickness can't be negative")
    if wall > 0:
        items = wall_items(item, wall, str(params.get("wall_side") or "Outside"))
        for it in items:
            sketch_segments(it)
    added = [it for sk in _flat_items(inputs.get("add")) for it in sk.items]
    model = inputs.get("model") or _graph_model(inputs)
    return {"sketch": Sketch(_numbered(_abaqus_name(params["name"], "Sketch-1"), params, model=model),
                             tuple(added) + items, model)}


@register("model.part", "Part", "Model",
          "Make a part in Abaqus/CAE from a profile: a Shell (plates, walls), a Wire (beams, frames) or a Solid; "
          "Planar (flat), Extrude (along z), Revolve (about the y axis), or import STEP, IGES or SAT geometry, or a "
          "mesh from an Abaqus input file (.inp, an orphan mesh: load and section it, find its holes By shape). The "
          "profile is a Sketch, or points (a matrix, a curve, text); 3D points make a wire frame. Modelling space "
          "2D planar (plane stress / plane strain) or Axisymmetric (2D axisymmetric about y: pipes, vessels, seals) "
          "makes a flat part from the profile. Type Discrete or Analytical rigid makes a rigid surface (a punch, "
          "die, tool or rigid wall for contact; give it a reference point with Part Feature). Built in CAE "
          "as soon as it's valid; place it in the assembly with Instance.",
          inputs=[Port("model", "model", "Model", optional=True,
                       help="The model to build the part in. Not wired: its sketch's model, else the graph's Model node, "
                            "else NoCode-Model."),
                  Port("profile", "any", "Profile", optional=True, depth=0, table=True,
                       when={"method": ["Planar", "Extrude", "Revolve"]},
                       help="A Sketch, or points (a matrix, a curve, text). A list of them makes one part each.")],
          outputs=[Port("part", "part", "Part")],
          params=[Param("name", "text", "Part-1", "Name",
                        help="Its name in CAE; a list of profiles makes Part-1-1, Part-1-2…"),
                  Param("space", "select", "3D", "Modelling space", options=SPACES,
                        help="3D, or a 2D part from the flat profile: 2D planar (plane stress or plane strain, by the "
                             "element type) or Axisymmetric (the profile at x ≥ 0 turned about the y axis). A 2D part "
                             "is a Shell (its area, Method Planar) or a Wire."),
                  Param("type", "select", "Deformable", "Type", options=PART_TYPES,
                        help="Deformable, or a rigid surface for contact (tools, dies, walls): Discrete rigid is "
                             "meshed (a 3D shell, a 2D wire); Analytical rigid is exact and unmeshed (3D: a profile "
                             "extruded or revolved 360°; 2D: a wire). A rigid part moves by its reference point "
                             "(Part Feature → Reference point). An analytical surface's Side 1 is on the left "
                             "walking along its profile (a line drawn left to right faces up): to touch what is on "
                             "the other side, make its Region a Surface with Side 2."),
                  Param("body", "select", "Shell", "Body", options=BODIES,
                        help="Shell: plates and walls (thickness comes with the section, later); in 2D the area "
                             "itself. Wire: beams and frames (profile comes with the section). Solid: 3D solid."),
                  Param("method", "select", "Planar", "Method", options=METHODS,
                        help="Planar: the flat profile itself (shell or wire). Extrude: along z by Depth. "
                             "Revolve: about the y axis by Angle (keep the profile at x ≥ 0)."),
                  Param("file", "file", "", "File", options=list(IMPORT_EXTENSIONS), when={"method": ["Import CAD"]},
                        help="STEP/STP, IGES/IGS or SAT/ACIS geometry, or an Abaqus input file (.inp): its mesh, "
                             "without geometry or sections."),
                  Param("entry", "text", "", "Part in file", when={"method": ["Import CAD"]},
                        help="Which part of an input file that has several (e.g. PLATE). Empty: the only one, else "
                             "the first (CAE says which it took)."),
                  Param("depth", "number", 50.0, "Depth (z)", help="Extrusion length (Extrude)",
                        when={"method": ["Extrude"]}),
                  Param("angle", "number", 360.0, "Angle (°)", help="Revolution angle (Revolve)",
                        when={"method": ["Revolve"]})],
          display=lambda o, i, p: _model_value_display(o["part"]))
def model_part(inputs, params):
    _finite(params, {"depth": "Depth", "angle": "Angle"})
    body, method = params["body"], params["method"]
    depth, angle = float(params["depth"]), float(params["angle"])
    space, kind = str(params.get("space") or "3D"), str(params.get("type") or "Deformable")
    if space not in SPACES:
        raise ValueError(f"Modelling space is one of {', '.join(SPACES)}")
    if kind not in PART_TYPES:
        raise ValueError(f"Type is one of {', '.join(PART_TYPES)}")
    rigid = kind.split()[0] if kind != "Deformable" else ""
    flat = space != "3D"
    if flat and method != "Planar":
        raise ValueError(f"A {space.lower()} part is the flat profile itself: use Method Planar (an axisymmetric "
                         "part is already turned about y)")
    if flat and body == "Solid":
        raise ValueError(f"A {space.lower()} part's area is Body Shell (with a solid section); a Wire is its edges")
    if rigid == "Analytical":
        if method == "Import CAD":
            raise ValueError("An analytical rigid surface is drawn from a profile: import a Discrete rigid part")
        if flat and body != "Wire":
            raise ValueError("A 2D analytical rigid surface is a Wire (lines and arcs)")
        if not flat and (body != "Shell" or method not in ("Extrude", "Revolve")):
            raise ValueError("A 3D analytical rigid surface is a Shell extruded or revolved from its profile")
        if method == "Revolve" and angle != 360:
            raise ValueError("An analytical rigid surface is revolved all the way round (Angle 360)")
    if rigid == "Discrete" and (body == "Solid" or (flat and body != "Wire") or (not flat and body != "Shell")):
        raise ValueError("A discrete rigid part is a surface: a Shell in 3D, a Wire in 2D")
    prof = inputs.get("profile")
    if method == "Import CAD":
        path = os.path.abspath(os.path.expanduser(str(params.get("file") or "")))
        extension = os.path.splitext(path)[1].lower()
        if extension not in IMPORT_EXTENSIONS:
            raise ValueError("Import needs a STEP, IGES or SAT file, or an Abaqus input file (.inp)")
        if not os.path.isfile(path):
            raise ValueError(f"File not found: {path}")
        model = inputs.get("model") or _graph_model(inputs, Model(DEFAULT_MODEL))
        entry = str(params.get("entry") or "").strip() if extension == ".inp" else ""
        if rigid and extension == ".inp":
            raise ValueError("An input file's mesh is imported as it is: make rigid parts from geometry")
        return {"part": Part(_numbered(_abaqus_name(params["name"], "Part-1"), params, model=model), model, body,
                             "Import", file=path, entry=entry, rigid=rigid)}
    if prof is None:
        raise ValueError(f"{method} needs a Profile")
    # its own Model, else its sketch's, else the graph's, else NoCode-Model
    model = (inputs.get("model") or (prof.model if isinstance(prof, Sketch) else None)
             or _graph_model(inputs, Model(DEFAULT_MODEL)))
    sketch, points = None, ()
    if isinstance(prof, Sketch):
        sketch = prof
    else:
        pts = _profile_points(prof)
        if np.ptp(pts[:, 2]) > 0 or pts[0, 2] != 0:
            if flat:
                raise ValueError(f"A {space.lower()} part is drawn in the XY plane (z = 0)")
            if body != "Wire":
                raise ValueError("Shell and solid profiles are drawn in the XY plane (z = 0); only wires take 3D "
                                 "points")
            points = tuple(tuple(round(float(c), 9) for c in p) for p in pts)
        else:
            sketch = Sketch("", (("Profile", _flat_points(pts, "A profile"), body != "Wire"),))
    if body == "Wire" and method != "Planar":
        raise ValueError("A wire is the profile itself: use Method Planar (extrude or revolve a Shell or Solid)")
    if body == "Solid" and method == "Planar":
        raise ValueError("A solid needs Extrude or Revolve")
    if sketch is not None and not sketch.closed and (body == "Solid" or (body == "Shell" and method == "Planar")):
        raise ValueError(f"A {'2D part' if flat else 'flat shell or a solid'} needs a closed profile (Sketch: Profile "
                         "Closed, or a rectangle/circle)")
    if method == "Extrude" and depth <= 0:
        raise ValueError("Depth must be positive to extrude")
    if method == "Revolve":
        if not 0 < angle <= 360:
            raise ValueError("Angle must be between 0 and 360 degrees")
        if min(x for line in sketch.outline() for x, _ in line) < -1e-9:
            raise ValueError("Revolve turns the profile about the y axis: keep it at x ≥ 0")
    if space == "Axisymmetric" and min(x for line in sketch.outline() for x, _ in line) < -1e-9:
        raise ValueError("An axisymmetric part turns about the y axis: keep its profile at x ≥ 0")
    return {"part": Part(_numbered(_abaqus_name(params["name"], "Part-1"), params, model=model), model, body, method,
                         sketch, points,
                         depth if method == "Extrude" else 0.0, angle if method == "Revolve" else 0.0,
                         space=space, rigid=rigid)}


@register("model.datum", "Datum", "Model",
          "Create a reusable plane, axis, point or coordinate system (local csys: cartesian, cylindrical, "
          "spherical) Datum on a Part or in an assembly. A Part makes a part-level "
          "datum; an Instance or Model makes an assembly-level datum (coordinates are assembly coordinates). "
          "Points can come from text, matrices or curves; a Face Region can define an offset plane. Deleting the "
          "node deletes only this CAE feature.",
          # Keep the old handle ``part`` so existing graphs load without migration; it now accepts all owners.
          inputs=[Port("part", "any", "Part, instance or model", depth=0),
                  Port("points", "any", "Points", optional=True, depth=0, table=True,
                       when={"kind": ["Point", "Plane through 3 points", "Axis through 2 points",
                                      "Coordinate system"]},
                       help="Coordinate system: its origin, a point on its first axis (x, or r), and a point in "
                            "its 1-2 plane."),
                  Port("region", "region", "Face", optional=True, when={"kind": ["Offset from face"]})],
          outputs=[Port("datum", "datum", "Datum")],
          params=[Param("name", "text", "Datum-1", "Name"),
                  Param("kind", "select", "Principal plane", "Type",
                        options=["Principal plane", "Plane through 3 points", "Offset from face",
                                 "Principal axis", "Axis through 2 points", "Point", "Coordinate system"]),
                  Param("csys", "select", "Cartesian", "Coordinate system", options=["Cartesian", "Cylindrical",
                                                                                      "Spherical"],
                        when={"kind": ["Coordinate system"]},
                        help="Cylindrical: components 1, 2, 3 are radial, around (θ) and along the axis (the "
                             "origin's 3-direction). Loads and boundary conditions given it act in it."),
                  Param("plane", "select", "XY", "Principal plane", options=["XY", "YZ", "XZ"],
                        when={"kind": ["Principal plane"]}),
                  Param("axis", "select", "Z", "Principal axis", options=["X", "Y", "Z"],
                        when={"kind": ["Principal axis"]}),
                  Param("offset", "number", 0.0, "Offset",
                        when={"kind": ["Principal plane", "Offset from face"]}),
                  Param("flip", "select", "Side 1", "Direction", options=["Side 1", "Side 2"],
                        when={"kind": ["Offset from face"]})],
          display=lambda o, i, p: _model_value_display(o["datum"]))
def model_datum(inputs, params):
    source = inputs["part"]
    if not isinstance(source, (Part, Instance, Model)):
        raise ValueError("Datum needs a Part (part level), or an Instance or Model (assembly level)")
    model = source.model if isinstance(source, Part) else source.part.model if isinstance(source, Instance) else source
    kind = str(params.get("kind") or "Principal plane")
    offset = float(params["offset"])
    if not math.isfinite(offset):
        raise ValueError("Offset must be finite")
    points = ()
    required = {"Point": 1, "Plane through 3 points": 3, "Axis through 2 points": 2, "Coordinate system": 3}.get(kind)
    if required:
        if inputs.get("points") is None:
            raise ValueError(f"{kind} needs {required} point{'s' if required != 1 else ''}")
        raw = _geometry_points(inputs["points"])
        if len(raw) != required:
            raise ValueError(f"{kind} needs exactly {required} point{'s' if required != 1 else ''}")
        points = tuple(tuple(float(c) for c in p) for p in raw)
    region = inputs.get("region")
    if kind == "Offset from face":
        if not isinstance(region, Region) or region.kind != "face":
            raise ValueError("Offset from face needs a Face Region on this Part or in this assembly")
        if isinstance(source, Part) and not _same_geometry(region.source, source):
            raise ValueError("A part-level offset datum needs a Face Region on this Part")
        if isinstance(source, Instance) and region.source != source:
            raise ValueError("An instance-level offset datum needs a Face Region on this Instance")
        region_model = region.source.model if isinstance(region.source, Part) else region.source.part.model
        if not isinstance(source, Part) and region_model != model:
            raise ValueError("The Face Region and assembly datum must belong to the same Model")
    plane = str(params.get("axis") if kind == "Principal axis" else
                params.get("csys") or "Cartesian" if kind == "Coordinate system" else params.get("plane") or "XY")
    return {"datum": DatumPlane(_numbered(_abaqus_name(str(params["name"]), "Datum-1"), params, model=model),
                                source, plane, offset, kind, points, region,
                                str(params.get("flip") or "Side 1"))}


def _same_geometry(a, b) -> bool:
    """The same part, ignoring what doesn't change its geometry (sections, meshing, datums and partitions made
    alongside): a Datum made on the Part works on its sectioned copy too."""
    if not isinstance(a, Part) or not isinstance(b, Part):
        return a == b

    def bare(p):
        return replace(p, sections=(), operations=(), features=tuple(f for f in p.features if f[0] != "Mesh"))
    return bare(a) == bare(b)


EXTEND, SKETCHED, SPLIT = "Partition by extending a face", "Partition by a sketch on a face", "Split edges"
FEATURE_OPERATIONS = ["Partition", EXTEND, SKETCHED, SPLIT, "Fillet", "Chamfer", "Extrude add", "Extrude cut",
                      "Remove faces", "Remove wire", "Repair small edges", "Repair small faces",
                      "Remove redundant entities", "Solid to shell", "Midsurface", "Reference point"]
DEFEATURE = ("Remove faces", "Remove wire", "Repair small edges", "Repair small faces", "Remove redundant entities")


@register("model.feature", "Part Feature", "Model",
          "Modify a part with a reversible CAE feature. Partition (split, cut into hex-meshable / sweepable "
          "regions) uses a Datum plane and optionally a Region of the cells or faces to cut; Partition by extending "
          "a face cuts cells along the extension of one face (e.g. a step or a hole's side); Partition by a sketch "
          "on a face draws a closed or open Sketch on one face and splits it there (a load or contact patch); Split "
          "edges cuts edges at a fraction of their length (a point for a load, a seed). The "
          "same node also fillets/chamfers edges and adds or cuts a closed Sketch from a selected planar face. "
          "Defeaturing (simplify, clean up or heal CAD geometry): Remove faces takes out the faces of a Region "
          "and closes the gap by extending the faces around them (small holes, fillets, chamfers, bosses: Region "
          "By shape with Radius up to, or Small faces); Remove wire deletes the edges of an Edge Region (wire edges: lines of a "
          "wire part, or loose edges not bounding a face); Repair small edges / small faces merges tiny edges or "
          "faces into their neighbours (shorter / smaller than a size, or those of a Region); Remove redundant "
          "entities cleans up edges and vertices that split nothing. CAE reports how much the volume changed. "
          "Thin solids as shells: Solid to shell keeps a solid's faces as a shell (its skin; a Face Region's faces "
          "are left out, to open it); Midsurface makes the shell halfway between the Region's faces and the "
          "Opposite faces (a plate's top and bottom, a wall's two sides). The part is a shell from then on: give it "
          "a Shell section (thickness) and shell elements. Reference point (RP, remote point, control point) adds "
          "one to the part at x, y, z: every instance of the part carries it, and its Reference point output takes "
          "loads, boundary conditions, a coupling (RBE2 / RBE3, MPC) or a connector, and history output. To "
          "suppress a feature, deactivate the node that makes it. "
          "The original Part is modified through its "
          "feature tree, including picked CAE parts; no copy is made.",
          inputs=[Port("part", "part", "Part"),
                  Port("datum", "datum", "Datum", optional=True, when={"operation": ["Partition"]}),
                  Port("region", "region", "Region", optional=True),
                  Port("tool", "region", "Face to extend", optional=True, when={"operation": [EXTEND]}),
                  Port("opposite", "region", "Opposite faces", optional=True, when={"operation": ["Midsurface"]}),
                  Port("sketch", "sketch", "Sketch", optional=True,
                       when={"operation": ["Extrude add", "Extrude cut", SKETCHED]})],
          outputs=[Port("part", "part", "Part"),
                   Port("point", "region", "Reference point", when={"operation": ["Reference point"]})],
          params=[Param("operation", "select", "Partition", "Operation", options=FEATURE_OPERATIONS),
                  Param("name", "text", "", "Feature name",
                        help="Name in the Part feature tree. Empty: Partition-1."),
                  Param("plane", "select", "XY", "Fallback datum plane", options=["XY", "YZ", "XZ"],
                        help="Used only when Datum is not connected, including older saved graphs.",
                        when={"operation": ["Partition"]}),
                  Param("offset", "number", 0.0, "Fallback offset",
                        help="Used only when Datum is not connected.", when={"operation": ["Partition"]}),
                  Param("radius", "number", 1.0, "Radius / length",
                        when={"operation": ["Fillet", "Chamfer"]}),
                  Param("depth", "number", 10.0, "Depth",
                        when={"operation": ["Extrude add", "Extrude cut"]}),
                  Param("fraction", "number", 0.5, "At (fraction of length)", when={"operation": [SPLIT]},
                        help="Where each edge is cut, from its start (0) to its end (1)"),
                  Param("size", "number", 0.0, "Smaller than",
                        when={"operation": ["Repair small edges", "Repair small faces"]},
                        help="Edges shorter than this length / faces smaller than this area (among the Region's, "
                             "when one is wired). 0: exactly the Region's."),
                  Param("point", "text", "0, 0, 0", "At (x, y, z)", when={"operation": ["Reference point"]},
                        help="Where the reference point is, in part coordinates")],
          display=lambda o, i, p: _model_value_display(o["part"]))
def model_feature(inputs, params):
    part = inputs["part"]
    if any(feature[0] == "Mesh" for feature in part.features):
        raise ValueError("Geometry features must come before Mesh; connect Part Feature before Mesh")
    region = inputs.get("region")
    if region is not None and region.kind == "reference point":
        raise ValueError("Part Feature's Region is faces, edges or cells, not a reference point")
    if region is not None and not _same_geometry(region.source, part):
        raise ValueError("Region belongs to a different part or instance")
    if region is not None and isinstance(region.source, Instance):
        raise ValueError("Part Feature needs a Region selected on the Part, not on an assembly instance")
    operation_kind = str(params.get("operation") or "Partition")
    datum = inputs.get("datum")
    if operation_kind == "Partition" and datum is None:  # saved graphs before the standalone Datum node
        offset = float(params["offset"])
        if not math.isfinite(offset):
            raise ValueError("Offset must be finite")
        datum = DatumPlane("Datum-1", part, str(params["plane"]), offset)
    if operation_kind == "Partition" and datum is not None:
        if not isinstance(datum.part, Part):
            raise ValueError("Partitioning a Part needs a part-level Datum, not an assembly-level Datum")
        if not _same_geometry(datum.part, part):
            raise ValueError("Datum belongs to a different Part")
    if operation_kind == "Partition" and datum is not None and datum.kind not in ("Principal plane", "Plane through 3 points", "Offset from face"):
        raise ValueError("Partition needs a plane Datum, not a point or axis Datum")
    selector = None if region is None else (region.kind, region.indices, region.points, region.tolerance)
    amount = float(params.get("radius") or 0.0)
    if operation_kind in ("Fillet", "Chamfer"):
        if region is None or region.kind != "edge":
            raise ValueError(f"{operation_kind} needs an Edge Region")
        if amount <= 0 or not math.isfinite(amount):
            raise ValueError("Radius / length must be a positive finite number")
        datum = None
    if operation_kind not in FEATURE_OPERATIONS:
        raise ValueError(f"Operation is one of {', '.join(FEATURE_OPERATIONS)}")
    tool = None
    if operation_kind in (EXTEND, SKETCHED, SPLIT):
        datum = None
        if part.mesh_file:
            raise ValueError(f"{operation_kind} needs geometry; this part is only a mesh")
    if operation_kind == EXTEND:
        face = inputs.get("tool")
        if part.effective_body != "Solid":
            raise ValueError("Partition by extending a face cuts cells: it needs a Solid Part (for a shell, use a "
                             "Datum plane or a sketch on a face)")
        if not isinstance(face, Region) or face.kind != "face" or not _same_geometry(face.source, part):
            raise ValueError("Partition by extending a face needs Face to extend: one Face Region on this Part")
        if not face.dynamic and len(face.indices or face.points) != 1:
            raise ValueError("Face to extend is one face")
        if region is not None and region.kind != "cell":
            raise ValueError("Region restricts which cells are cut: a Cell Region (or none: every cell)")
        tool = (face.kind, face.indices, face.points, face.tolerance)
    if operation_kind == SKETCHED:
        if region is None or region.kind != "face" or (not region.dynamic and len(region.indices or region.points) != 1):
            raise ValueError("Partition by a sketch on a face needs one Face Region: the face the Sketch is drawn on")
        if not isinstance(inputs.get("sketch"), Sketch):
            raise ValueError("Partition by a sketch on a face needs a Sketch (its lines split the face)")
    if operation_kind in ("Solid to shell", "Midsurface"):
        datum = None
        if part.effective_body != "Solid" or part.mesh_file:
            raise ValueError(f"{operation_kind} turns a solid into a shell: it needs a Solid Part with geometry")
        if any(f and f[0] == "Mesh" for f in part.features) or part.sections:
            raise ValueError(f"{operation_kind} changes what the part is: put it before Material & Section and Mesh")
        if operation_kind == "Solid to shell" and region is not None and region.kind != "face":
            raise ValueError("Solid to shell's Region is optional: the faces to leave out (a Face Region)")
        if operation_kind == "Midsurface":
            opposite = inputs.get("opposite")
            for label, faces in (("Region", region), ("Opposite faces", opposite)):
                if not isinstance(faces, Region) or faces.kind != "face" or not _same_geometry(faces.source, part):
                    raise ValueError(f"Midsurface needs {label}: Face Regions on this Part, one on each side of "
                                     "the wall (e.g. a plate's top and bottom)")
            tool = (opposite.kind, opposite.indices, opposite.points, opposite.tolerance)
    if operation_kind == SPLIT:
        if region is None or region.kind != "edge":
            raise ValueError("Split edges needs an Edge Region: the edges to cut")
        _finite(params, {"fraction": "At (fraction of length)"})
        amount = float(params.get("fraction") if params.get("fraction") is not None else 0.5)
        if not 0 < amount < 1:
            raise ValueError("At is a fraction of each edge's length, between 0 and 1 (not at its ends)")
    if operation_kind in DEFEATURE:
        datum = None
        if part.mesh_file:
            raise ValueError(f"{operation_kind} needs geometry; this part is only a mesh")
        if operation_kind == "Remove faces" and (region is None or region.kind != "face"):
            raise ValueError("Remove faces needs a Face Region: the faces to take out (e.g. By shape: cylindrical "
                             "faces with Radius up to, or Small faces)")
        if operation_kind == "Remove wire" and (region is None or region.kind != "edge"):
            raise ValueError("Remove wire needs an Edge Region: the wire edges to delete")
        if operation_kind == "Remove wire":
            amount = 0.0
        if operation_kind in ("Repair small edges", "Repair small faces"):
            want = "edge" if operation_kind == "Repair small edges" else "face"
            if region is not None and region.kind != want:
                raise ValueError(f"{operation_kind} takes an {want.title()} Region (or none: the whole part)")
            _finite(params, {"size": "Smaller than"})
            amount = float(params.get("size") or 0.0)
            if amount < 0:
                raise ValueError("Smaller than can't be negative")
            if amount == 0 and region is None:
                raise ValueError(f"{operation_kind} needs a size (Smaller than) or a Region")
        if operation_kind == "Remove redundant entities":
            selector = None
    sketch = inputs.get("sketch")
    if operation_kind == SKETCHED:
        pass
    elif operation_kind in ("Extrude add", "Extrude cut"):
        if part.effective_body != "Solid":
            raise ValueError(f"{operation_kind} currently needs a Solid Part")
        if not isinstance(sketch, Sketch) or not sketch.closed:
            raise ValueError(f"{operation_kind} needs a closed Sketch")
        if region is None or region.kind != "face" or len(region.indices or region.points) != 1:
            raise ValueError(f"{operation_kind} needs one planar Face Region as its sketch plane")
        amount = float(params.get("depth") or 0.0)
        if amount <= 0 or not math.isfinite(amount):
            raise ValueError("Extrude depth must be a positive finite number")
        datum = None
    else:
        sketch = None
    point = ()
    if operation_kind == "Reference point":
        datum, selector, amount = None, None, 0.0
        given = params.get("point")
        try:
            raw = parse_points(given, 1) if isinstance(given, str) else _geometry_points(given)
        except ValueError:
            raw = []
        if len(raw) != 1 or not 2 <= len(raw[0]) <= 3:
            raise ValueError("A reference point is at one x, y, z (e.g. 0, 0, 100)")
        point = tuple(float(c) for c in raw[0]) + (0.0,) * (3 - len(raw[0]))
        if not all(math.isfinite(c) for c in point):
            raise ValueError("The reference point's x, y, z must be finite numbers")
    fallback_name = "RP-1" if operation_kind == "Reference point" else operation_kind + "-1"
    if operation_kind in (EXTEND, SKETCHED, SPLIT):
        fallback_name = "Split-1" if operation_kind == SPLIT else "Partition-1"
    operation = PartOperation(_numbered(_abaqus_name(str(params.get("name") or fallback_name), fallback_name),
                                        params, model=part.model), part, operation_kind, datum, selector, amount, sketch,
                              tool, point)
    out = replace(part, operations=part.operations + (operation,), item=_item(part, params))
    if operation_kind == "Reference point":
        return {"part": out, "point": Region(out, "reference point", points=(point,), feature=operation)}
    return {"part": out}
