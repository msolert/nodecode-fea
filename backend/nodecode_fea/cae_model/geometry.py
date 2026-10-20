"""Models, sketches, parts, datums and Part Feature operations (partitions, repairs, fillets…)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from ..values import _turn, sketch_segments, DatumPlane, Instance, Model, Part, PartOperation, Sketch
from .core import _n, ORDER, _pt
from .material import native_sequence_lines, section_assignment_code, _selector_parts
from .regions import object_code


def sketch_code(sk: Sketch, name: str, var: str = "s", transform: str = "") -> list[str]:
    """Kernel lines that draw a sketch in model ``m`` as sketch ``name`` (replacing one of that name)."""
    size = max([200.0] + [4 * abs(c) for line in sk.outline() for p in line for c in p])
    transform_arg = f", transform={transform}" if transform else ""
    lines = [f"if {name!r} in m.sketches.keys():", f"    del m.sketches[{name!r}]",
             f"{var} = m.ConstrainedSketch(name={name!r}, sheetSize={_n(size)}{transform_arg})"]
    for it in sk.items:
        segments = sketch_segments(it)
        if segments:  # polygons, slots, rounded rectangles: lines and counterclockwise arcs
            for seg in segments:
                if seg[0] == "line":
                    lines.append(f"{var}.Line(point1={_pt(seg[1])}, point2={_pt(seg[2])})")
                else:
                    lines.append(f"{var}.ArcByCenterEnds(center={_pt(seg[1])}, point1={_pt(seg[2])}, "
                                 f"point2={_pt(seg[3])}, direction=COUNTERCLOCKWISE)")
        elif it[0] == "Ellipse":
            (cx, cy), w, h, rotation = it[1:]
            a1 = _turn(cx + w / 2, cy, cx, cy, rotation)
            a2 = _turn(cx, cy + h / 2, cx, cy, rotation)
            lines.append(f"{var}.EllipseByCenterPerimeter(center={_pt((cx, cy))}, axisPoint1={_pt(a1)}, "
                         f"axisPoint2={_pt(a2)})")
        elif it[0] == "Rectangle":
            lines.append(f"{var}.rectangle(point1={_pt(it[1])}, point2={_pt(it[2])})")
        elif it[0] == "Circle":
            (cx, cy), r = it[1], it[2]
            lines.append(f"{var}.CircleByCenterPerimeter(center={_pt((cx, cy))}, point1={_pt((cx + r, cy))})")
        elif it[0] == "Arc":
            (cx, cy), r, start, end, direction = it[1:]
            p1 = (cx + r * math.cos(math.radians(start)), cy + r * math.sin(math.radians(start)))
            p2 = (cx + r * math.cos(math.radians(end)), cy + r * math.sin(math.radians(end)))
            turn = "CLOCKWISE" if direction == "Clockwise" else "COUNTERCLOCKWISE"
            lines.append(f"{var}.ArcByCenterEnds(center={_pt((cx, cy))}, point1={_pt(p1)}, point2={_pt(p2)}, "
                         f"direction={turn})")
        elif it[0] == "Spline":
            lines.append(f"{var}.Spline(points={tuple(it[1])!r})")
            if it[2] and it[1][0] != it[1][-1]:
                lines.append(f"{var}.Line(point1={it[1][-1]!r}, point2={it[1][0]!r})")
        else:
            pts = list(it[1]) + ([it[1][0]] if it[2] and it[1][0] != it[1][-1] else [])
            lines.append(f"for p1, p2 in zip({tuple(pts)!r}[:-1], {tuple(pts)!r}[1:]):")
            lines.append(f"    {var}.Line(point1=p1, point2=p2)")
    return lines


def model_code(mo: Model) -> list[str]:
    if mo.file:  # the whole model of an input file (replacing one of that name)
        return [f"m = mdb.ModelFromInputFile(name={mo.name!r}, inputFileName={mo.file!r})"]
    return [f"m = mdb.Model(name={mo.name!r})", "m.rootAssembly.DatumCsysByDefault(CARTESIAN)"]


def datum_model(value: DatumPlane) -> Model:
    source = value.part
    return source.model if isinstance(source, Part) else source.part.model if isinstance(source, Instance) else source


def datum_code(value: DatumPlane, name: str, source_name: str, objects: list[dict]) -> list[str]:
    source = value.part
    model = datum_model(value)
    assembly = not isinstance(source, Part)
    lines = [f"m = mdb.models[{model.name!r}]"]
    if assembly:
        lines += ["a = m.rootAssembly", "owner = a"]
    else:
        lines += [f"part = m.parts[{source_name!r}]", "owner = part"]
    if value.kind == "Principal plane":
        lines.append(f"_feature = owner.DatumPlaneByPrincipalPlane(principalPlane={value.plane}PLANE, offset={_n(value.offset)})")
    elif value.kind == "Plane through 3 points":
        lines.append(f"_feature = owner.DatumPlaneByThreePoints(point1={_pt(value.points[0])}, "
                     f"point2={_pt(value.points[1])}, point3={_pt(value.points[2])})")
    elif value.kind == "Offset from face":
        selected = "part" if isinstance(value.region.source, Part) else object_code(value.region.source, objects)
        if value.region.indices:
            picked = f"tuple({selected}.faces[i] for i in {tuple(value.region.indices)!r})"
        else:
            picked = (f"_find_geometry({selected}.faces, {tuple(value.region.points)!r}, "
                      f"{_n(value.region.tolerance)}, 'Face Region for datum', owner={selected})")
        lines += [f"_faces = _geometry({picked}, 'Face Region for datum')",
                  f"_feature = owner.DatumPlaneByOffset(plane=_faces[0], "
                  f"flip={'SIDE1' if value.flip == 'Side 1' else 'SIDE2'}, offset={_n(value.offset)})"]
    elif value.kind == "Principal axis":
        lines.append(f"_feature = owner.DatumAxisByPrincipalAxis(principalAxis={value.plane}AXIS)")
    elif value.kind == "Axis through 2 points":
        lines.append(f"_feature = owner.DatumAxisByTwoPoint(point1={_pt(value.points[0])}, point2={_pt(value.points[1])})")
    elif value.kind == "Coordinate system":
        lines.append(f"_feature = owner.DatumCsysByThreePoints(coordSysType={value.plane.upper()}, "
                     f"origin={_pt(value.points[0])}, point1={_pt(value.points[1])}, point2={_pt(value.points[2])})")
    else:
        lines.append(f"_feature = owner.DatumPointByCoordinate(coords={_pt(value.points[0])})")
    return lines + [f"owner.features.changeKey(fromName=_feature.name, toName={name!r})"]


def operation_code(value: PartOperation, name: str, part_name: str, datum_name: str) -> list[str]:
    lines = [f"m = mdb.models[{value.part.model.name!r}]", f"part = m.parts[{part_name!r}]"]
    if value.kind in ("Fillet", "Chamfer"):
        _, indices, points, tolerance = _selector_parts(value.selector)
        lines += native_sequence_lines("targets", "part.edges", indices, points, tolerance, "Edge Region")
        method, argument = ("Round", "radius") if value.kind == "Fillet" else ("Chamfer", "length")
        lines += [f"_feature = part.{method}({argument}={_n(value.amount)}, edgeList=tuple(targets))",
                  f"part.features.changeKey(fromName=_feature.name, toName={name!r})"]
        return lines
    if value.kind in ("Extrude add", "Extrude cut"):
        _, indices, points, tolerance = _selector_parts(value.selector)
        lines += native_sequence_lines("targets", "part.faces", indices, points, tolerance, "Face Region")
        lines += ["sketch_plane = targets[0]", "_edge_ids = sketch_plane.getEdges()",
                  "if not len(_edge_ids): raise ValueError('The sketch face has no edge to orient its profile')",
                  "up_edge = part.edges[_edge_ids[0]]",
                  "transform = part.MakeSketchTransform(sketchPlane=sketch_plane, sketchUpEdge=up_edge, "
                  "sketchPlaneSide=SIDE1, sketchOrientation=RIGHT)"]
        temp = "__nocode_feature_profile"
        lines += sketch_code(value.sketch, temp, "feature_sketch", "transform")
        lines += ["part.projectReferencesOntoSketch(sketch=feature_sketch, filter=COPLANAR_EDGES)"]
        method = "SolidExtrude" if value.kind == "Extrude add" else "CutExtrude"
        lines += [f"_feature = part.{method}(sketchPlane=sketch_plane, sketchUpEdge=up_edge, "
                  f"sketchPlaneSide=SIDE1, sketchOrientation=RIGHT, sketch=feature_sketch, depth={_n(value.amount)})",
                  f"del m.sketches[{temp!r}]",
                  f"part.features.changeKey(fromName=_feature.name, toName={name!r})"]
        return lines
    if value.kind in ("Remove faces", "Remove wire", "Repair small edges", "Repair small faces",
                      "Remove redundant entities"):
        return lines + defeature_code(value, name)
    rename = f"part.features.changeKey(fromName=_feature.name, toName={name!r})"
    if value.kind == "Reference point":
        return lines + [f"_feature = part.ReferencePoint(point={_pt(value.point)})", rename]
    if value.kind in ("Solid to shell", "Midsurface"):
        lines.append("_before = list(part.features.keys())")
        if value.kind == "Solid to shell":
            lines.append("part.RemoveCells(cellList=part.cells[:])")
            if value.selector:
                _, indices, points, tolerance = _selector_parts(value.selector)
                lines += native_sequence_lines("targets", "part.faces", indices, points, tolerance,
                                               "Faces to leave out")
                lines.append("part.RemoveFaces(faceList=targets, deleteCells=False)")
        else:
            _, indices, points, tolerance = _selector_parts(value.selector)
            lines += native_sequence_lines("targets", "part.faces", indices, points, tolerance, "Midsurface Region")
            _, indices, points, tolerance = _selector_parts(value.tool)
            lines += native_sequence_lines("opposite", "part.faces", indices, points, tolerance, "Opposite faces")
            lines += ["part.AssignMidsurfaceRegion(cellList=part.cells[:])",
                      "part.OffsetFaces(faceList=targets, targetFaces=opposite, targetFacesMethod=CLOSEST_POINT_FRACTION, "
                      "fractionDistance=0.5, trimToReferenceRep=OFF)"]
        # one or more features: the last gets the node's name, the others <name>-step<k> (deleted with it)
        return lines + ["_new = [k for k in part.features.keys() if k not in _before]",
                        "for _k, _old in enumerate(_new[:-1], 1):",
                        f"    part.features.changeKey(fromName=_old, toName='%s-step%d' % ({name!r}, _k))",
                        "if _new:", f"    part.features.changeKey(fromName=_new[-1], toName={name!r})"]
    if value.kind == "Partition by extending a face":
        _, indices, points, tolerance = _selector_parts(value.tool)
        lines += native_sequence_lines("_tool", "part.faces", indices, points, tolerance, "Face to extend")
        if value.selector:
            _, indices, points, tolerance = _selector_parts(value.selector)
            lines += native_sequence_lines("targets", "part.cells", indices, points, tolerance, "Cell Region")
        else:
            lines.append("targets = part.cells[:]")
        return lines + ["if len(_tool) != 1: raise ValueError('Face to extend found %d faces, not one' % len(_tool))",
                        "_feature = part.PartitionCellByExtendFace(extendFace=_tool[0], cells=targets)", rename]
    if value.kind == "Split edges":
        _, indices, points, tolerance = _selector_parts(value.selector)
        lines += native_sequence_lines("targets", "part.edges", indices, points, tolerance, "Edge Region")
        return lines + [f"_feature = part.PartitionEdgeByParam(edges=targets, parameter={_n(value.amount)})", rename]
    if value.kind == "Partition by a sketch on a face":
        _, indices, points, tolerance = _selector_parts(value.selector)
        lines += native_sequence_lines("targets", "part.faces", indices, points, tolerance, "Face Region")
        lines += ["if len(targets) != 1: raise ValueError('the sketch goes on one face; the Region found %d' % "
                  "len(targets))",
                  "sketch_plane = targets[0]", "_edge_ids = sketch_plane.getEdges()",
                  "if not len(_edge_ids): raise ValueError('The sketch face has no edge to orient its profile')",
                  "up_edge = part.edges[_edge_ids[0]]",
                  "transform = part.MakeSketchTransform(sketchPlane=sketch_plane, sketchUpEdge=up_edge, "
                  "sketchPlaneSide=SIDE1, sketchOrientation=RIGHT)"]
        temp = "__nocode_partition_sketch"
        lines += sketch_code(value.sketch, temp, "feature_sketch", "transform")
        return lines + ["part.projectReferencesOntoSketch(sketch=feature_sketch, filter=COPLANAR_EDGES)",
                        "_feature = part.PartitionFaceBySketch(faces=targets, sketch=feature_sketch, "
                        "sketchUpEdge=up_edge, sketchOrientation=RIGHT)",
                        f"del m.sketches[{temp!r}]", rename]
    lines.append(f"datum = part.datums[part.features[{datum_name!r}].id]")
    if value.selector:
        kind, indices, points, tolerance = _selector_parts(value.selector)
        repo = {"face": "faces", "cell": "cells"}[kind]
        lines += native_sequence_lines("targets", f"part.{repo}", indices, points, tolerance,
                                       kind.title() + " Region")
        call = ("part.PartitionCellByDatumPlane(cells=targets, datumPlane=datum)" if kind == "cell" else
                "part.PartitionFaceByDatumPlane(faces=targets, datumPlane=datum)")
        lines.append(f"_feature = {call}")
    else:
        repo = "cells"
        lines += ["targets = part.cells[:]", "if not len(targets):", "    targets = part.faces[:]"]
        lines += ["_feature = (part.PartitionCellByDatumPlane(cells=targets, datumPlane=datum)"
                  " if len(part.cells) else part.PartitionFaceByDatumPlane(faces=targets, datumPlane=datum))"]
    lines.append(f"part.features.changeKey(fromName=_feature.name, toName={name!r})")
    return lines


def defeature_code(value: PartOperation, name: str) -> list[str]:
    """CAE's geometry-edit tools on part (already bound): the feature they add gets the node's name, and the volume
    change is reported (a note on the node)."""
    repo = {"Remove faces": "part.faces", "Repair small faces": "part.faces"}.get(value.kind, "part.edges")
    what = "faces" if repo == "part.faces" else "edges"
    lines = ["_before = list(part.features.keys())",
             "try:", "    _volume = part.getVolume()", "except Exception:", "    _volume = None"]
    if value.kind != "Remove redundant entities":
        if value.selector:
            _, indices, points, tolerance = _selector_parts(value.selector)
            lines += native_sequence_lines("targets", repo, indices, points, tolerance,
                                           f"{value.kind} Region")
        else:
            lines += [f"targets = {repo}[0:0]", f"for _entity in {repo}:",
                      f"    targets += {repo}[_entity.index:_entity.index + 1]"]
        if value.kind != "Remove faces" and value.amount > 0:  # only those under the size
            lines += [f"_small = {repo}[0:0]", "for _entity in targets:",
                      f"    if _entity.getSize(printResults=False) < {_n(value.amount)}:",
                      f"        _small += {repo}[_entity.index:_entity.index + 1]",
                      "targets = _small"]
        lines += ["if not len(targets):",
                  f"    raise ValueError('found no {what} to {value.kind.lower().split()[0]}"
                  f"{' smaller than ' + format(value.amount, 'g') if value.kind != 'Remove faces' and value.amount > 0 else ''}')"]
        call = {"Remove faces": "part.RemoveFacesAndStitch(faceList=targets)",
                "Remove wire": "part.RemoveWireEdges(wireEdgeList=targets)",
                "Repair small edges": "part.RepairSmallEdges(edgeList=targets)",
                "Repair small faces": "part.RepairSmallFaces(faceList=targets)"}[value.kind]
        lines += [call, f"_count = '%d {what}' % len(targets)"]
    else:
        # CAE refuses (adding no feature) when nothing is redundant: that is a clean part, not a failure
        lines += ["try:",
                  "    part.RemoveRedundantEntities(vertexList=part.vertices[:], edgeList=part.edges[:])",
                  "    _count = 'redundant edges and vertices'",
                  "except Exception as _error:",
                  "    if 'redundant' not in str(_error): raise",
                  "    _count = 'none were redundant'"]
    key = f"feature {value.part.model.name} {{}} {name}"
    lines += ["_new = [k for k in part.features.keys() if k not in _before]",
              "if _new:", f"    part.features.changeKey(fromName=_new[-1], toName={name!r})",
              "try:", "    _after = part.getVolume()", "except Exception:", "    _after = None",
              f"_key = {key!r}.format(part.name)",
              "if _volume and _after is not None and abs(_after - _volume) <= 1e-9 * abs(_volume):",
              f"    _say('%s note: {value.kind} ({{}}) left the volume as it was (%g)'.format(_count) % (_key, _after))",
              "elif _volume and _after is not None:",
              f"    _say('%s note: {value.kind} ({{}}) changed the volume by %+.3g %% (%g to %g)'.format(_count) % "
              "(_key, 100.0 * (_after - _volume) / _volume, _volume, _after))",
              "else:",
              f"    _say('%s note: {value.kind} ({{}})'.format(_count) % _key)"]
    return lines


DIMENSIONS = {"3D": "THREE_D", "2D planar": "TWO_D_PLANAR", "Axisymmetric": "AXISYMMETRIC"}
PART_TYPES = {"": "DEFORMABLE_BODY", "Discrete": "DISCRETE_RIGID_SURFACE", "Analytical": "ANALYTIC_RIGID_SURFACE"}


def part_code(p: Part, name: str | None = None, sketch: str | None = None,
              section_names: dict | None = None) -> list[str]:
    assignments = section_assignment_code(p, section_names or {})
    if p.method == "Import" and p.mesh_file:
        # the input file's parts go into a scratch model (their names can't clash), the chosen one is copied here
        return [f"m = mdb.models[{p.model.name!r}]", "_scratch = '__nocode_import__'",
                "if _scratch in mdb.models.keys():", "    del mdb.models[_scratch]",
                f"_found = mdb.Model(name=_scratch).PartFromInputFile(inputFileName={p.file!r})",
                "_names = sorted(_found.keys())",
                f"_want = [k for k in _names if k.upper() == {p.entry.upper()!r}] if {bool(p.entry)!r} else _names",
                "if not _want:",
                f"    raise ValueError('{os.path.basename(p.file)} has no part {p.entry} (it has: %s)' % ', '.join(_names))",
                "if len(_names) > 1:",
                f"    _say('{os.path.basename(p.file)} has parts %s: {name or p.name} is %s' % (', '.join(_names), _want[0]))",
                f"if {name or p.name!r} in m.parts.keys():", f"    del m.parts[{name or p.name!r}]",
                f"part = m.Part(name={name or p.name!r}, objectToCopy=mdb.models[_scratch].parts[_want[0]])",
                "del mdb.models[_scratch]"] + feature_code(p, assignments)
    if p.method == "Import":
        extension = os.path.splitext(p.file)[1].lower()
        if extension in (".step", ".stp"):
            opened = f"mdb.openStep(fileName={p.file!r}, scaleFromFile=OFF)"
        elif extension in (".iges", ".igs"):
            opened = f"mdb.openIges(fileName={p.file!r}, msbo=False, trimCurve=DEFAULT, scaleFromFile=OFF)"
        else:
            opened = f"mdb.openAcis(fileName={p.file!r})"
        return [f"m = mdb.models[{p.model.name!r}]", f"_geometry_file = {opened}",
                f"part = m.PartFromGeometryFile(name={name or p.name!r}, geometryFile=_geometry_file, "
                f"combine=False, dimensionality=THREE_D, type={PART_TYPES[p.rigid]})"] + feature_code(p, assignments)
    if p.source is not None:
        return [f"m = mdb.models[{p.model.name!r}]",
                f"part = m.Part(name={name or p.name!r}, objectToCopy=m.parts[{p.source.name!r}])"] + feature_code(p, assignments)
    head = [f"m = mdb.models[{p.model.name!r}]",
            f"part = m.Part(name={name or p.name!r}, dimensionality={DIMENSIONS[p.space]}, type={PART_TYPES[p.rigid]})"]
    if p.sketch is None:  # 3D wire through points
        segs = tuple((p.points[i], p.points[i + 1]) for i in range(len(p.points) - 1))
        return head + [f"part.WirePolyLine(points={segs!r}, mergeType=IMPRINT, meshable=ON)"] + feature_code(p, assignments)
    lines = [f"m = mdb.models[{p.model.name!r}]"]
    if p.sketch.name:
        lines.append(f"s = m.ConstrainedSketch(name='__nocode_profile', "
                     f"objectToCopy=m.sketches[{sketch or p.sketch.name!r}])")
    else:
        lines += sketch_code(p.sketch, "__nocode_profile")
    if p.method == "Revolve" or p.space == "Axisymmetric":
        lines.append("s.ConstructionLine(point1=(0.0, -100.0), point2=(0.0, 100.0))  # the y axis")
    lines += head[1:]
    if p.rigid == "Analytical":
        lines.append({"Planar": "part.AnalyticRigidSurf2DPlanar(sketch=s)",
                      "Extrude": f"part.AnalyticRigidSurfExtrude(sketch=s, depth={_n(p.depth)})",
                      "Revolve": "part.AnalyticRigidSurfRevolve(sketch=s)"}[p.method])
    else:
        lines.append({
            ("Wire", "Planar"): "part.BaseWire(sketch=s)",
            ("Shell", "Planar"): "part.BaseShell(sketch=s)",
            ("Shell", "Extrude"): f"part.BaseShellExtrude(sketch=s, depth={_n(p.depth)})",
            ("Shell", "Revolve"): f"part.BaseShellRevolve(sketch=s, angle={_n(p.angle)}, flipRevolveDirection=OFF)",
            ("Solid", "Extrude"): f"part.BaseSolidExtrude(sketch=s, depth={_n(p.depth)})",
            ("Solid", "Revolve"): f"part.BaseSolidRevolve(sketch=s, angle={_n(p.angle)}, flipRevolveDirection=OFF)",
        }[(p.body, p.method)])
    return lines + ["del m.sketches['__nocode_profile']"] + feature_code(p, assignments)


# Where a part's own code ends and its sections and mesh begin: a part with datums/partitions gets those after them
# (a partition made after meshing deletes the mesh), as a separate build step.
FINISH = "# sections and mesh"


@dataclass(frozen=True)
class PartFinish:
    """The sections and mesh of a part with datums/partitions, built after them."""
    part: Part


ORDER[PartFinish] = 6


def split_finish(code: list[str]) -> tuple[list[str], list[str]]:
    if FINISH not in code:
        return code, []
    i = code.index(FINISH)
    return code[:i], code[i + 1:]


def _edge_seed_lines(seeding: tuple, size: float, deviation: float, minimum: float) -> list[str]:
    """A local seed on ``mesh_edges``: by size, by number or biased; FINER so it isn't coarsened to fit."""
    if not seeding:
        return [f"part.seedEdgeBySize(edges=mesh_edges, size={_n(size)}, "
                f"deviationFactor={_n(deviation)}, minSizeFactor={_n(minimum)}, constraint=FINER)"]
    if seeding[0] == "By number":
        return [f"part.seedEdgeByNumber(edges=mesh_edges, number={int(seeding[1])}, constraint=FINER)"]
    _, number, ratio, bias = seeding
    method, edges = {"Fine at start": ("SINGLE", "end1Edges"), "Fine at end": ("SINGLE", "end2Edges"),
                     "Fine at both ends": ("DOUBLE", "endEdges"),
                     "Fine in the middle": ("DOUBLE", "centerEdges")}[bias]
    return [f"part.seedEdgeByBias(biasMethod={method}, {edges}=mesh_edges, ratio={_n(ratio)}, "
            f"number={int(number)}, constraint=FINER)"]


def feature_code(p: Part, assignments: list[str] | None = None) -> list[str]:
    lines, mesh_lines = [], []
    generate_mesh = False
    for feature in p.features:
        if feature[0] == "Mesh":
            start = len(lines)
            _, selector, size, deviation, minimum, technique, shape, element, generate = feature[:9]
            seeding = feature[9] if len(feature) > 9 else ()
            repo = {"Wire": "edges", "Shell": "faces", "Solid": "cells"}[p.effective_body]
            lines.append("import mesh")
            on_edges = len(feature) > 10 and feature[10] == "edges"  # a Face/Cell Region seeding its own edges
            if selector and selector[0] == "edge":
                kind, indices, points, tolerance = _selector_parts(selector)
                if indices:
                    lines.append(f"mesh_edges = tuple(part.edges[i] for i in {tuple(indices)!r})")
                else:
                    lines.append(f"mesh_edges = _find_geometry(part.edges, {tuple(points)!r}, {_n(tolerance)}, 'Edge Region', owner=part)")
                lines += _edge_seed_lines(seeding, size, deviation, minimum)
                domain = f"part.{repo}[:]"
            elif seeding and not on_edges:  # By number on every edge of the part
                lines.append(f"part.seedEdgeByNumber(edges=part.edges[:], number={int(seeding[1])}, constraint=FINER)")
                domain = f"part.{repo}[:]"
            else:
                if not on_edges:
                    lines.append(f"part.seedPart(size={_n(size)}, deviationFactor={_n(deviation)}, "
                                 f"minSizeFactor={_n(minimum)})")
                if selector:
                    kind, indices, points, tolerance = _selector_parts(selector)
                    selected_repo = {"face": "faces", "cell": "cells"}[kind]
                    if indices:
                        lines.append(f"mesh_domain = tuple(part.{selected_repo}[i] for i in {tuple(indices)!r})")
                    else:
                        lines.append(f"mesh_domain = _find_geometry(part.{selected_repo}, {tuple(points)!r}, "
                                     f"{_n(tolerance)}, {kind.title() + ' Region'!r}, owner=part)")
                    if on_edges:  # the edges bounding those faces or cells
                        lines.append("mesh_edges = tuple(part.edges[k] for k in "
                                     "sorted(set(k for _d in mesh_domain for k in _d.getEdges())))")
                        lines += _edge_seed_lines(seeding, size, deviation, minimum)
                    if kind == "face" and repo == "cells":  # a solid's controls and element type go on its cells
                        lines.append("mesh_domain = tuple(part.cells[k] for k in "
                                     "sorted(set(k for _d in mesh_domain for k in _d.getCells())))")
                    domain = "mesh_domain"
                else:
                    domain = f"part.{repo}[:]"
            controls = []
            if technique != "Automatic":
                controls.append(f"technique={technique.upper()}")
            if shape != "Automatic":
                controls.append(f"elemShape={shape.upper().replace('-', '_')}")
            if controls and p.effective_body != "Wire":
                lines.append(f"part.setMeshControls(regions={domain}, {', '.join(controls)})")
            lines += [f"elem_type = mesh.ElemType(elemCode={element}, elemLibrary=STANDARD)",
                      f"part.setElementType(regions=({domain},), elemTypes=(elem_type,))"]
            generate_mesh = generate_mesh or generate
            mesh_lines.extend(lines[start:])
            del lines[start:]
            continue
        _, plane, offset = feature[:3]
        selector = feature[3] if len(feature) > 3 else None
        lines += [f"datum = part.DatumPlaneByPrincipalPlane(principalPlane={plane}PLANE, offset={_n(offset)})",
                  "datum = part.datums[datum.id]"]
        if selector:
            kind, indices, points, tolerance = _selector_parts(selector)
            repo = {"face": "faces", "edge": "edges", "vertex": "vertices", "cell": "cells"}[kind]
            if indices:
                lines.append(f"targets = tuple(part.{repo}[i] for i in {tuple(indices)!r})")
            else:
                lines.append(f"targets = _find_geometry(part.{repo}, {tuple(points)!r}, {_n(tolerance)}, "
                             f"{kind.title() + ' Region'!r}, owner=part)")
            if kind == "cell":
                lines.append("part.PartitionCellByDatumPlane(cells=targets, datumPlane=datum)")
            elif kind == "face":
                lines.append("part.PartitionFaceByDatumPlane(faces=targets, datumPlane=datum)")
            else:
                lines.append(f"raise ValueError('A datum plane partitions faces or cells, not {kind}s')")
        else:
            lines += ["if len(part.cells):", "    part.PartitionCellByDatumPlane(cells=part.cells[:], datumPlane=datum)",
                      "elif len(part.faces):", "    part.PartitionFaceByDatumPlane(faces=part.faces[:], datumPlane=datum)"]
    if generate_mesh:
        mesh_lines.append("part.generateMesh()")
    finish = list(assignments or []) + mesh_lines
    return lines + ([FINISH] + finish if finish else [])
