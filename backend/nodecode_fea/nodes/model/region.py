"""Region and Measure: picking and finding geometry on a part or assembly."""

from __future__ import annotations

import math
import numpy as np
from dataclasses import replace

from ... import cae_queries
from ...values import Instance, Number, Part, PendingMeasure, Region
from ..core import Param, Port, register
from ..common import _finite
from ..field import parse_points
from ..maths import _matrix
from .geometry import _abaqus_name, _geometry_points, _model_value_display, _numbered


BY_SHAPE = ["Cylindrical faces", "Circular faces", "Circular edges", "Flat faces", "Small faces"]
RADIUS_MATCH = ["Equal to", "Up to", "At least"]
REGION_GROUPING = ["One per source", "One combined assembly region"]
REGION_MEMBERS = ["Together", "One per geometry"]
MESH_LABELS = ["Nodes", "Elements", "Element faces"]
ELEMENT_FACES = ("S1", "S2", "S3", "S4", "S5", "S6")


def _label_ranges(text: str, what: str) -> tuple:
    """Mesh labels as written in an input file: "1-9, 12" (nodes, elements), "S2: 1-8; S4: 12" (element faces,
    by face): ((face, ((first, last), ...)), ...), each range merged and sorted."""
    groups: dict = {}
    for chunk in text.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        face = ""
        if ":" in chunk:
            face, chunk = (x.strip() for x in chunk.split(":", 1))
            face = face.upper()
            if what != "faces" or face not in ELEMENT_FACES:
                raise ValueError("Only element faces take a face, S1 to S6: e.g. S2: 1-8; S4: 12"
                                 if what == "faces" else "Node and element labels are numbers and ranges: 1-9, 12")
        elif what == "faces":
            raise ValueError("Element faces are written by face, e.g. S2: 1-8; S4: 12 (S1 to S6)")
        for token in chunk.replace(",", " ").split():
            try:
                a, _, b = token.partition("-")
                first, last = int(a), int(b or a)
            except ValueError as exc:
                raise ValueError(f"Labels are whole numbers and ranges such as 1-9, 12 (not {token})") from exc
            if first < 1 or last < first:
                raise ValueError(f"Label range {token} must go up from 1")
            groups.setdefault(face, []).append((first, last))
    if not groups:
        raise ValueError("Mesh labels needs labels, e.g. 1-9, 12" + (" by face: S2: 1-8" if what == "faces" else ""))
    out = []
    for face, ranges in sorted(groups.items()):
        merged: list = []
        for a, b in sorted(ranges):
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        out.append((face, tuple(merged)))
    return tuple(out)


def _region_display(outputs, _inputs, _params):
    from ..previews import default_display
    value = outputs["region"]
    if isinstance(value, PendingMeasure):
        return {"kind": "info", "title": "Region", "items": [["CAE", "Finding geometry centres…"]]}
    return default_display(value) if isinstance(value, list) else _model_value_display(value)


@register("model.region", "Region", "Model",
          "Select faces, edges, vertices or cells on a Part or Instance, or turn an existing (including filtered) "
          "Region into a named CAE Set/Surface. Pick geometry directly in the CAE viewport, "
          "wire coordinates into Find at from text, a matrix or a curve, or find them By shape: every cylindrical "
          "face (holes, bores, pins, shafts, fillets), circular flat face (pads and partitioned discs), circular "
          "edge (hole edges, to fillet or chamfer), flat face facing a direction (top/bottom faces) or small face "
          "(area up to a size: chamfers, slivers, tiny details to defeature). A By-shape selection can keep matching "
          "geometry together or return one Region per geometry, located at its true CAE centre. Lists make one region per source or "
          "point table. A named Set or Surface mapped over Instances or filtered Regions can either make one per "
          "source or combine everything found by this node into one owner-level region. On a mesh (an imported "
          "input file .inp, an orphan mesh), Mesh labels selects nodes, elements or element faces by their "
          "label numbers (node set, element set, elset, nset, surface).",
          inputs=[Port("source", "any", "Part, instance or Region", depth=0),
                  Port("points", "any", "Find at", optional=True, depth=0, table=True,
                       help="Points x, y, z used by Find at.", when={"method": ["Find at"]})],
          outputs=[Port("region", "region", "Region")],
          params=[Param("method", "select", "Pick in CAE", "Select by",
                        options=["Pick in CAE", "Find at", "By shape", "Mesh labels"]),
                  Param("mesh", "select", MESH_LABELS[0], "Mesh entities", options=MESH_LABELS,
                        when={"method": ["Mesh labels"]},
                        help="Nodes (a node set: loads at points, BCs), elements (an element set: sections) or "
                             "element faces (an element surface: pressures, contact)."),
                  Param("labels", "text", "", "Labels", when={"method": ["Mesh labels"]},
                        help="Label numbers and ranges as in an input file: 1-9, 12. Element faces by face, S1 to "
                             "S6: S2: 1-8; S4: 12."),
                  Param("kind", "select", "Any geometry", "Geometry",
                        options=["Any geometry", "Vertex", "Face", "Cell", "Edge"],
                        when={"method": ["Pick in CAE", "Find at"]}),
                  Param("shape", "select", BY_SHAPE[0], "Shape", options=BY_SHAPE, when={"method": ["By shape"]}),
                  Param("radius", "number", 0.0, "Radius",
                        when={"method": ["By shape"], "shape": ["Cylindrical faces", "Circular faces", "Circular edges"]},
                        help="Only this radius (within Tolerance), or up to / at least it (Radius is); 0: any "
                             "radius"),
                  Param("match", "select", "Equal to", "Radius is", options=RADIUS_MATCH,
                        when={"method": ["By shape"], "shape": ["Cylindrical faces", "Circular faces", "Circular edges"]},
                        help="Equal to: that radius. Up to: holes and fillets no bigger (to defeature small "
                             "details). At least: no smaller."),
                  Param("members", "select", REGION_MEMBERS[0], "Matching geometry", options=REGION_MEMBERS,
                        when={"method": ["By shape"]},
                        help="Together returns one Region containing every match. One per geometry returns a list "
                             "of individual Regions, each located at its CAE centre (useful for one coupling, load "
                             "or operation per matching face)."),
                  Param("area", "number", 1.0, "Area up to", when={"method": ["By shape"], "shape": ["Small faces"]},
                        help="Every face of this area or less"),
                  Param("facing", "text", "0, 0, 1", "Facing",
                        when={"method": ["By shape"], "shape": ["Flat faces"]},
                        help="The direction x, y, z the faces' normals point to (within 1°)"),
                  Param("value", "text", "", "Selection",
                        help="Filled by Pick in CAE; indices can also be typed as 0, 1, 5.",
                        when={"method": ["Pick in CAE"]}),
                  Param("pick", "pick", "", "",
                        help="Pick geometry in the CAE viewport", when={"method": ["Pick in CAE"]}),
                  Param("tolerance", "number", 0.0, "Tolerance",
                        help="Find at: 0 uses Abaqus findAt; a positive value finds the nearest entity within this "
                             "distance. By shape: how far the radius may be from Radius.",
                        when={"method": ["Find at", "By shape"]}),
                  Param("create", "select", "Selection", "Create in CAE",
                        options=["Selection", "Set", "Surface"],
                        help="Selection stays in the graph; Set or Surface also creates a reusable named CAE region."),
                  Param("group", "select", REGION_GROUPING[0], "Mapped sources",
                        options=REGION_GROUPING, when={"create": ["Set", "Surface"]},
                        help="One per source gives each mapped Instance/Region its own named region. One combined "
                             "assembly region makes a single Set or Surface across mapped Instances, or on the "
                             "shared Part of mapped Regions."),
                  Param("name", "text", "Region-1", "Name", when={"create": ["Set", "Surface"]}),
                  Param("side", "select", "Side 1", "Surface side", options=["Side 1", "Side 2"],
                        when={"create": ["Surface"]},
                        help="Which side of shell faces or edges is the surface normal side.")],
          display=_region_display, version=cae_queries.revision)
def model_region(inputs, params):
    source = inputs["source"]
    if isinstance(source, Region):
        selected = source
        owner = selected.source
        form = str(params.get("create") or "Selection")
        if form == "Surface" and selected.kind not in ("face", "edge"):
            raise ValueError("A Surface needs faces or edges; vertices and cells can only make a Set")
        owner_model = owner.model if isinstance(owner, Part) else owner.part.model
        # A list of already-selected Regions maps this node. The combined choice turns those mapped outputs into
        # one named owner-level Set/Surface; a single Region is simply named directly.
        mapped = "source" in set(params.get("__varied__") or ())
        combine = (form != "Selection" and mapped
                   and params.get("group") == REGION_GROUPING[1])
        base_name = _abaqus_name(str(params.get("name") or "Region-1"), "Region-1")
        name = ((base_name if combine else _numbered(base_name, params, model=owner_model))
                if form != "Selection" else "")
        return {"region": replace(selected, name=name, form=form,
                                  side=str(params.get("side") or "Side 1"), combine=combine)}
    if not isinstance(source, (Part, Instance)):
        raise ValueError("Region needs a Part, Instance or existing Region")
    method = str(params.get("method") or "Pick in CAE")
    if method == "Mesh labels":
        mesh = str(params.get("mesh") or MESH_LABELS[0])
        if mesh not in MESH_LABELS:
            raise ValueError(f"Mesh entities is one of {', '.join(MESH_LABELS)}")
        what = {"Nodes": "nodes", "Elements": "elements", "Element faces": "faces"}[mesh]
        body = (source.part if isinstance(source, Instance) else source).effective_body
        kind = ("vertex" if what == "nodes" else "face" if what == "faces" else
                {"Solid": "cell", "Shell": "face", "Wire": "edge"}.get(body, "cell"))
        form = str(params.get("create") or "Selection")
        if form == "Surface" and kind not in ("face", "edge"):
            raise ValueError("A Surface needs element faces (or shell elements); nodes and solid elements can only "
                             "make a Set")
        owner_model = source.model if isinstance(source, Part) else source.part.model
        combine = form != "Selection" and params.get("group") == REGION_GROUPING[1]
        if combine and not isinstance(source, Instance):
            raise ValueError("One combined assembly region needs an Instance or a list of Instances")
        base_name = _abaqus_name(str(params.get("name") or "Region-1"), "Region-1")
        name = ((base_name if combine else _numbered(base_name, params, model=owner_model))
                if form != "Selection" else "")
        spec = ("@labels", what, _label_ranges(str(params.get("labels") or ""), what))
        return {"region": Region(source, kind, points=(spec,), name=name, form=form,
                                 side=str(params.get("side") or "Side 1"), combine=combine)}
    if method == "By shape":
        form = str(params.get("create") or "Selection")
        owner_model = source.model if isinstance(source, Part) else source.part.model
        combine = form != "Selection" and params.get("group") == REGION_GROUPING[1]
        if combine and not isinstance(source, Instance):
            raise ValueError("One combined assembly region needs an Instance or a list of Instances")
        base_name = _abaqus_name(str(params.get("name") or "Region-1"), "Region-1")
        name = ((base_name if combine else _numbered(base_name, params, model=owner_model))
                if form != "Selection" else "")
        shape = str(params.get("shape") or BY_SHAPE[0])
        kind = "edge" if shape == "Circular edges" else "face"
        if shape not in BY_SHAPE:
            raise ValueError(f"Shape is one of {', '.join(BY_SHAPE)}")
        if shape == "Small faces":
            _finite(params, {"area": "Area up to"})
            area = float(params.get("area") or 0.0)
            if area <= 0:
                raise ValueError("Area up to must be positive")
            spec = ("@shape", "small", area)
        elif shape == "Flat faces":
            facing = parse_points(str(params.get("facing") or ""), 1)
            if facing.shape[1] < 3 or not np.any(facing[0][:3]):
                raise ValueError("Facing is a direction x, y, z, e.g. 0, 0, 1")
            spec = ("@shape", "plane", *(round(float(c), 9) for c in facing[0][:3]))
        else:
            _finite(params, {"radius": "Radius", "tolerance": "Tolerance"})
            radius, tolerance = float(params.get("radius") or 0.0), float(params.get("tolerance") or 0.0)
            if radius < 0 or tolerance < 0:
                raise ValueError("Radius and tolerance can't be negative (radius 0: any radius)")
            match = str(params.get("match") or "Equal to")
            if match not in RADIUS_MATCH:
                raise ValueError(f"Radius is one of {', '.join(RADIUS_MATCH)}")
            if match != "Equal to" and radius <= 0:
                raise ValueError(f"Radius {match.lower()} needs a positive Radius")
            what = "disk" if shape == "Circular faces" else "cylinder" if kind == "face" else "circle"
            spec = ("@shape", what, radius, tolerance,
                    *(() if match == "Equal to" else (match,)))
        if form == "Surface" and kind != "face" and kind != "edge":
            raise ValueError("A Surface needs faces or edges")
        selected = Region(source, kind, points=(spec,), name=name, form=form,
                          side=str(params.get("side") or "Side 1"), combine=combine)
        if str(params.get("members") or REGION_MEMBERS[0]) == REGION_MEMBERS[1]:
            centers = cae_queries.value("Center", selected)
            if isinstance(centers, PendingMeasure):
                return {"region": centers}
            rows = np.asarray(centers, dtype=float).reshape(-1, 3)
            regions = []
            for i, point in enumerate(rows):
                item_name = name if combine else (f"{name}-{i + 1}" if name else "")
                at = tuple(round(float(c), 9) for c in point)
                # a hole's or circle's centre is in empty space, where findAt finds nothing: CAE takes the match
                # nearest it instead; a flat face's centre is on the face itself
                located = (spec + ("@at",) + at,) if what in ("cylinder", "circle") else (at,)
                regions.append(replace(selected, points=located, name=item_name))
            return {"region": regions}
        return {"region": selected}
    find_at = inputs.get("points") is not None or method == "Find at"
    kind = str(params["kind"]).lower()
    if kind == "any geometry":
        if find_at:
            raise ValueError("Choose Face, Edge, Vertex or Cell when using Find at points")
        raise ValueError("Pick geometry in CAE; its vertex, face, cell or edge type will be detected")
    form = str(params.get("create") or "Selection")
    owner_model = source.model if isinstance(source, Part) else source.part.model
    combine = form != "Selection" and params.get("group") == REGION_GROUPING[1]
    if combine and not isinstance(source, Instance):
        raise ValueError("One combined assembly region needs an Instance or a list of Instances")
    base_name = _abaqus_name(str(params.get("name") or "Region-1"), "Region-1")
    name = ((base_name if combine else _numbered(base_name, params, model=owner_model))
            if form != "Selection" else "")
    side = str(params.get("side") or "Side 1")
    if form == "Surface" and kind not in ("face", "edge"):
        raise ValueError("A Surface needs faces or edges; vertices and cells can only make a Set")
    if find_at:
        if inputs.get("points") is None:
            raise ValueError("Find at needs one or more x, y, z points")
        pts = _geometry_points(inputs["points"])
        points = tuple(tuple(round(float(c), 9) for c in p) for p in pts)
        tolerance = float(params.get("tolerance") or 0.0)
        if tolerance < 0 or not math.isfinite(tolerance):
            raise ValueError("Find at tolerance must be zero or a positive finite number")
        return {"region": Region(source, kind, points=points, name=name, form=form, side=side,
                                 tolerance=tolerance, combine=combine)}
    raw = str(params.get("value") or "").strip()
    if not raw:
        raise ValueError("Nothing selected yet: press Pick in CAE")
    if raw.startswith("@"):
        pts = parse_points(raw[1:], 1)
        points = tuple(tuple(round(float(c), 9) for c in p) for p in pts)
        return {"region": Region(source, kind, points=points, name=name, form=form, side=side, combine=combine)}
    labels = raw.split("|")[-1]
    try:
        indices = tuple(dict.fromkeys(int(x.strip()) for x in labels.split(",") if x.strip()))
    except ValueError as exc:
        raise ValueError("Selection needs geometry indices such as 0, 1, 5") from exc
    if not indices:
        raise ValueError("Nothing selected yet: press Pick in CAE")
    return {"region": Region(source, kind, indices=indices, name=name, form=form, side=side, combine=combine)}


MESH_MEASURES = ["Nodes", "Elements", "Worst aspect ratio", "Mean aspect ratio", "Failed elements"]


PER_ITEM = ["Count", "Radius", "Center"]  # of what a Region found: how many, and one value / point each


def _measure_display(o, i, p):
    from ..previews import default_display
    return ({"kind": "value", "title": p["quantity"], "value": o["value"].value}
            if isinstance(o["value"], Number) else default_display(o["value"])
            if isinstance(o["value"], list) else
            {"kind": "info", "title": p["quantity"], "items": [["CAE", "Syncing measurement…"]]})


@register("model.measure", "Measure", "Model",
          "Measure CAE geometry: area of faces, length of edges, volume of cells, or distance/angle between two "
          "objects; what a Region found: how many faces/edges (Count), the Radius of each (holes, fillets) and the "
          "Center of each (x, y, z: one point is a row, several points a table, e.g. to list holes found By shape); or its "
          "mesh: the number of nodes or elements (mesh size, count), the worst or mean element "
          "aspect ratio, and how many elements fail Abaqus' analysis checks (mesh quality, distorted elements). "
          "Inputs can be Parts, Instances or Regions; lists produce matching lists of measurements. "
          "The result is read from the live CAE model and cached until the geometry or mesh changes.",
          inputs=[Port("first", "any", "First", depth=0), Port("second", "any", "Second", optional=True, depth=0)],
          outputs=[Port("value", "number", "Value")],
          params=[Param("quantity", "select", "Distance", "Quantity",
                        options=["Distance", "Area", "Length", "Volume", "Angle"] + PER_ITEM + MESH_MEASURES)],
          display=_measure_display,
          version=cae_queries.revision)
def model_measure(inputs, params):
    first, second = inputs["first"], inputs.get("second")
    if not isinstance(first, (Part, Instance, Region)):
        raise ValueError("Measure needs a Part, Instance or Region")
    q = str(params["quantity"])
    if any(isinstance(v, Region) and v.kind == "reference point" for v in (first, second)):
        raise ValueError("Measure takes geometry (faces, edges, vertices, cells), not a reference point")
    if q in ("Distance", "Angle"):
        if not isinstance(second, (Part, Instance, Region)):
            raise ValueError(f"{q} needs First and Second geometry")
        if q == "Angle":
            if not isinstance(first, Region) or not isinstance(second, Region):
                raise ValueError("Angle needs two Edge or Face Regions")
            if first.kind not in ("edge", "face") or second.kind not in ("edge", "face"):
                raise ValueError("Angle needs Edge or Face Regions")
        def geometry_model(value):
            source = value.source if isinstance(value, Region) else value
            return source.part.model if isinstance(source, Instance) else source.model
        if geometry_model(first) != geometry_model(second):
            raise ValueError(f"{q} geometry must be in the same CAE model")
    elif second is not None:
        raise ValueError(f"{q} uses only First")
    if q in PER_ITEM and not isinstance(first, Region):
        raise ValueError(f"{q} needs a Region (e.g. Region By shape: the holes found)")
    expected = {"Area": "face", "Length": "edge", "Volume": "cell"}.get(q)
    if expected and isinstance(first, Region) and first.kind != expected:
        raise ValueError(f"{q} needs a {expected} Region")
    if q in MESH_MEASURES:
        source = first.source if isinstance(first, Region) else first
        part = source.part if isinstance(source, Instance) else source
        if not any(f[0] == "Mesh" for f in part.features) and not part.existing:
            raise ValueError(f"{q} needs a meshed Part: connect Mesh before Measure")
        if q not in ("Nodes", "Elements") and isinstance(first, Region) and first.kind == "vertex":
            raise ValueError(f"{q} needs a Part, Instance or an Edge, Face or Cell Region")
    out = cae_queries.value(q, first, second)
    if isinstance(out, list):  # one per face / edge… the Region found
        if q == "Center":
            points = _matrix(np.array(out, dtype=float).reshape(len(out), 3), "Center")
            # A mapped list of one-entity Regions should be N × 3, not N × 1 × 3. A Region that found several
            # entities still returns its full point table.
            out = points[0] if len(points) == 1 else points
        else:
            out = [Number(float(x), f"{q}[{i}]") for i, x in enumerate(out)]
    return {"value": out}
