"""Values that flow along the edges of a graph. Each class maps to one port type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .sources import ResultSource


@dataclass
class Odb:
    path: str
    version: float
    source: ResultSource
    info: dict[str, Any]

    def step(self, name: str) -> dict[str, Any]:
        for s in self.info["steps"]:
            if s["name"] == name:
                return s
        steps = self.info["steps"]
        if str(name).strip().lstrip("-").isdigit():  # a step number, e.g. from For or a Number: 1 = first, -1 = last
            i = int(str(name).strip())
            if 1 <= i <= len(steps) or -len(steps) <= i <= -1:
                return steps[i - 1 if i > 0 else i]
        names = ", ".join(s["name"] for s in steps)
        raise ValueError(f"Step '{name}' not found (available: {names}, or a number 1…{len(steps)})")


@dataclass
class Field:
    """Results at nodes or elements: one row per (instance, label), one column per component."""

    name: str
    odb: Odb
    position: str  # "NODAL" or "ELEMENT"
    components: list[str]
    instances: np.ndarray
    labels: np.ndarray
    values: np.ndarray
    step: str = ""
    frame: int = -1
    frame_value: float = 0.0
    kind: str = "TENSOR"  # SCALAR, VECTOR or TENSOR
    # How the field was derived, so it can be reproduced elsewhere (e.g. in Abaqus/CAE):
    # {"variable": "S", "refinement": ("INVARIANT", "Mises") | ("COMPONENT", "S11") | None,
    #  "sets": [set names it was filtered by], "math": True if a formula was applied}
    origin: dict = field(default_factory=dict)
    # Invariants Abaqus defines for this variable (None: the ODB didn't say; use the type).
    invariants: list[str] | None = None
    # Per-row location detail. Zero/empty means the ODB did not expose that detail (normal nodal/centroid data).
    integration_points: np.ndarray | None = None
    section_points: np.ndarray | None = None

    def with_values(self, values: np.ndarray, name: str, components: list[str], kind: str, **origin) -> "Field":
        return Field(name, self.odb, self.position, components, self.instances, self.labels,
                     values, self.step, self.frame, self.frame_value, kind, {**self.origin, **origin},
                     self.invariants, self.integration_points, self.section_points)

    def subset(self, mask: np.ndarray, suffix: str) -> "Field":
        return Field(f"{self.name} {suffix}", self.odb, self.position, self.components, self.instances[mask],
                     self.labels[mask], self.values[mask], self.step, self.frame, self.frame_value, self.kind,
                     {**self.origin, "sets": [*self.origin.get("sets", []), suffix.removeprefix("on ")]},
                     self.invariants,
                     None if self.integration_points is None else self.integration_points[mask],
                     None if self.section_points is None else self.section_points[mask])

    @property
    def entity(self) -> str:
        return "node" if self.position == "NODAL" else "element"


@dataclass
class EntitySet:
    name: str
    kind: str  # "node" or "element"
    instances: np.ndarray
    labels: np.ndarray


@dataclass
class Series:
    name: str
    x: np.ndarray
    y: np.ndarray
    xlabel: str = "Time"
    ylabel: str = ""
    # {"location": {"odb", "entity", "instance", "label"}}: the node/element the curve follows (Over Time, Each),
    # so a curve can stand for that node/element wherever sets are accepted.
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Number:
    value: float
    label: str = ""
    where: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bool:
    """A yes/no value, e.g. from Compare; drives If, And/Or and Filter List."""

    value: bool
    label: str = ""


@dataclass
class Text:
    """A piece of text (a string): names, labels, file paths…"""

    value: str
    label: str = ""


@dataclass(frozen=True)
class Model:
    """A model to build in Abaqus/CAE (preprocessing): the root that parts, instances (and later the rest) hang off."""

    name: str
    existing: bool = False
    file: str = ""  # an Abaqus input file the model starts from (its parts, materials, assembly, steps, loads)
    # Its position in the lists it was made from (one model per design of a study): what's made at the same
    # position inside it keeps its plain name, since it can't clash with the other designs'.
    path: tuple = ()

    def summary(self) -> str:
        return f"model {self.name}"


def _turn(px: float, py: float, cx: float, cy: float, degrees: float) -> tuple[float, float]:
    import math
    a = math.radians(degrees)
    dx, dy = px - cx, py - cy
    return (round(cx + dx * math.cos(a) - dy * math.sin(a), 9), round(cy + dx * math.sin(a) + dy * math.cos(a), 9))


def filleted(pts: list, radius: float, closed: bool) -> list[tuple]:
    """A polyline with its corners rounded by tangent arcs of one radius, as ("line", p1, p2) and ("arc", centre,
    p1, p2) pieces (arcs counterclockwise from p1 to p2). A corner too tight for the radius (the arc would need more
    than half of a neighbouring side) raises ValueError; straight-through points stay sharp."""
    import math
    pts = [tuple(map(float, p)) for p in pts]
    if closed and pts[0] == pts[-1]:
        pts = pts[:-1]
    n = len(pts)
    corners = range(n) if closed else range(1, n - 1)
    cut = {}  # corner index -> (tangent point on the incoming side, on the outgoing side, centre, turns left)
    for k in corners:
        p0, p1, p2 = pts[(k - 1) % n], pts[k], pts[(k + 1) % n]
        a = (p0[0] - p1[0], p0[1] - p1[1])
        b = (p2[0] - p1[0], p2[1] - p1[1])
        la, lb = math.hypot(*a), math.hypot(*b)
        if la == 0 or lb == 0:
            raise ValueError("A rounded profile can't have two equal points in a row")
        ua, ub = (a[0] / la, a[1] / la), (b[0] / lb, b[1] / lb)
        cos = max(-1.0, min(1.0, ua[0] * ub[0] + ua[1] * ub[1]))
        angle = math.acos(cos)  # between the two sides at the corner
        if angle > math.pi - 1e-9:
            continue  # straight on: nothing to round
        d = radius / math.tan(angle / 2)  # from the corner to each tangent point
        if d > la / 2 + 1e-9 or d > lb / 2 + 1e-9:
            raise ValueError(f"Corner radius {radius:g} is too big for the corner at {p1[0]:g}, {p1[1]:g}: "
                             f"at most {min(la, lb) / 2 * math.tan(angle / 2):g} there")
        t1 = (p1[0] + ua[0] * d, p1[1] + ua[1] * d)
        t2 = (p1[0] + ub[0] * d, p1[1] + ub[1] * d)
        bis = (ua[0] + ub[0], ua[1] + ub[1])
        lbis = math.hypot(*bis)
        h = radius / math.sin(angle / 2)
        centre = (p1[0] + bis[0] / lbis * h, p1[1] + bis[1] / lbis * h)
        # walking p0 -> p1 -> p2: a left turn rounds counterclockwise from t1 to t2
        left = (-a[0]) * b[1] - (-a[1]) * b[0] > 0
        cut[k] = tuple(round(c, 9) for c in t1), tuple(round(c, 9) for c in t2), tuple(round(c, 9) for c in centre), left
    segments = []
    last = range(n) if closed else range(n - 1)
    for k in last:
        j = (k + 1) % n
        start = cut[k][1] if k in cut else pts[k]
        end = cut[j][0] if j in cut else pts[j]
        if start != end:
            segments.append(("line", start, end))
        if j in cut:
            t1, t2, centre, left = cut[j]
            segments.append(("arc", centre, t1, t2) if left else ("arc", centre, t2, t1))
    return segments


def sketch_segments(item: tuple) -> list[tuple]:
    """A shape as ("line", p1, p2) and ("arc", centre, p1, p2) pieces, arcs counterclockwise from p1 to p2:
    polygons, slots, rounded rectangles and rounded profiles (what CAE draws and the editor previews)."""
    import math
    kind = item[0]
    if kind == "Polygon":
        (cx, cy), r, sides, rotation = item[1:5]
        pts = [_turn(cx + r * math.cos(2 * math.pi * k / sides + math.pi / 2), cy + r * math.sin(
            2 * math.pi * k / sides + math.pi / 2), cx, cy, rotation) for k in range(sides)]
        if len(item) > 5 and item[5] > 0:
            return filleted(pts, item[5], True)
        return [("line", pts[k], pts[(k + 1) % sides]) for k in range(sides)]
    if kind == "Profile" and len(item) > 3 and item[3] > 0:
        return filleted(list(item[1]), item[3], item[2])
    if kind == "Slot":
        (cx, cy), length, width, rotation = item[1:]
        if width > length:  # along the longer side
            length, width, rotation = width, length, rotation + 90.0
        r, half = width / 2.0, length / 2.0 - width / 2.0
        t = lambda x, y: _turn(cx + x, cy + y, cx, cy, rotation)
        return [("line", t(-half, -r), t(half, -r)), ("arc", t(half, 0), t(half, -r), t(half, r)),
                ("line", t(half, r), t(-half, r)), ("arc", t(-half, 0), t(-half, r), t(-half, -r))]
    if kind == "Rectangle" and len(item) > 3 and item[3] > 0:
        (x0, y0), (x1, y1), r = item[1], item[2], item[3]
        return [("line", (x0 + r, y0), (x1 - r, y0)), ("arc", (x1 - r, y0 + r), (x1 - r, y0), (x1, y0 + r)),
                ("line", (x1, y0 + r), (x1, y1 - r)), ("arc", (x1 - r, y1 - r), (x1, y1 - r), (x1 - r, y1)),
                ("line", (x1 - r, y1), (x0 + r, y1)), ("arc", (x0 + r, y1 - r), (x0 + r, y1), (x0, y1 - r)),
                ("line", (x0, y1 - r), (x0, y0 + r)), ("arc", (x0 + r, y0 + r), (x0, y0 + r), (x0 + r, y0))]
    return []


WALL_SIDES = ["Outside", "Inside", "Centred"]


def offset_polyline(pts: list, d: float, closed: bool) -> list[tuple[float, float]]:
    """The polyline moved sideways by d (to the left walking along it; negative: to the right), its corners
    mitred (sharp, as CAE's sketch offset makes them)."""
    import math
    pts = [tuple(map(float, p)) for p in pts]
    if closed and pts[0] == pts[-1]:
        pts = pts[:-1]
    n = len(pts)
    segs = list(range(n)) if closed else list(range(n - 1))
    normals = []
    for k in segs:
        (x1, y1), (x2, y2) = pts[k], pts[(k + 1) % n]
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            raise ValueError("A wall's outline can't have two equal points in a row")
        normals.append((-(y2 - y1) / length, (x2 - x1) / length))
    out = []
    for k in range(n):
        if not closed and k == 0:
            nx, ny = normals[0]
        elif not closed and k == n - 1:
            nx, ny = normals[-1]
        else:
            (ax, ay), (bx, by) = normals[(k - 1) % len(normals)], normals[k % len(normals)]
            mx, my = ax + bx, ay + by
            cos = mx * ax + my * ay  # |m| cos(half angle) along a normal: the mitre's length factor
            if abs(cos) < 1e-9:
                raise ValueError(f"The outline turns back on itself at {pts[k][0]:g}, {pts[k][1]:g}")
            nx, ny = mx / cos, my / cos
        out.append((round(pts[k][0] + d * nx, 9), round(pts[k][1] + d * ny, 9)))
    return out


def _signed_area(pts: list) -> float:
    return 0.5 * sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))


def wall_items(item: tuple, thickness: float, side: str) -> tuple:
    """A shape drawn as a wall of a thickness: a closed shape as itself and its offset copy (the inner loop is a
    hole: a tube's section), an open profile as the closed band along it (a channel from its centreline).
    Outside / Inside: the wall grows out of / into a closed shape, to the left / right of an open profile walked from
    its first point; Centred: half each way. A rounded shape keeps its corner radius plus or minus the offset."""
    import math
    if side not in WALL_SIDES:
        raise ValueError(f"Wall side is one of {', '.join(WALL_SIDES)}")
    out_d, in_d = {"Outside": (thickness, 0.0), "Inside": (0.0, thickness),
                   "Centred": (thickness / 2, thickness / 2)}[side]  # how far each loop goes out and in
    kind = item[0]

    def rounded(corner, d):
        return () if corner <= 0 else ((corner + d,) if corner + d > 1e-9 else ())

    def small(what):
        return ValueError(f"A wall {thickness:g} thick doesn't fit inside this {what}")

    if kind == "Circle":
        c, r = item[1], item[2]
        if r - in_d <= 0:
            raise small("circle")
        return (("Circle", c, round(r + out_d, 9)), ("Circle", c, round(r - in_d, 9)))
    if kind == "Rectangle":
        (x0, y0), (x1, y1) = item[1], item[2]
        corner = item[3] if len(item) > 3 else 0.0
        if min(x1 - x0, y1 - y0) - 2 * in_d <= 0:
            raise small("rectangle")
        loop = lambda d: ("Rectangle", (round(x0 - d, 9), round(y0 - d, 9)), (round(x1 + d, 9), round(y1 + d, 9))) \
            + rounded(corner, d)
        return (loop(out_d), loop(-in_d))
    if kind == "Polygon":
        c, r, sides, rotation = item[1:5]
        corner = item[5] if len(item) > 5 else 0.0
        stretch = 1 / math.cos(math.pi / sides)  # circumradius per unit of offset across the sides
        if r - in_d * stretch <= 0:
            raise small("polygon")
        loop = lambda d: ("Polygon", c, round(r + d * stretch, 9), sides, rotation) + rounded(corner, d)
        return (loop(out_d), loop(-in_d))
    if kind == "Slot":
        c, length, width, rotation = item[1:]
        if min(length, width) - 2 * in_d <= 0:
            raise small("slot")
        loop = lambda d: ("Slot", c, round(length + 2 * d, 9), round(width + 2 * d, 9), rotation)
        return (loop(out_d), loop(-in_d))
    if kind == "Profile":
        pts, closed = list(item[1]), item[2]
        corner = item[3] if len(item) > 3 else 0.0
        if closed:
            ring = [tuple(p) for p in pts]
            if ring[0] == ring[-1]:
                ring = ring[:-1]
            left_is_in = _signed_area(ring) > 0  # counterclockwise: its inside is to the left
            sign = 1 if left_is_in else -1
            outer = offset_polyline(ring, -sign * out_d, True)
            inner = offset_polyline(ring, sign * in_d, True)
            if in_d > 0 and (_signed_area(inner) * _signed_area(ring) <= 0 or
                             abs(_signed_area(inner)) >= abs(_signed_area(ring))):
                raise small("profile")
            return (("Profile", tuple(outer), True) + rounded(corner, out_d),
                    ("Profile", tuple(inner), True) + rounded(corner, -in_d))
        if corner > 0:
            raise ValueError("A wall along an open profile has sharp corners: set its Corner radius to 0 (round the "
                             "part's edges afterwards with Part Feature Fillet)")
        left = offset_polyline(pts, out_d, False)
        right = offset_polyline(pts, -in_d, False)
        return (("Profile", tuple(left + right[::-1]), True),)
    raise ValueError("A wall can be drawn from a Rectangle, Circle, Polygon, Slot or Profile")


def _segments_outline(segments: list[tuple], closed: bool = True) -> list[tuple[float, float]]:
    import math
    pts = []
    for seg in segments:
        if seg[0] == "line":
            pts += [seg[1], seg[2]]
            continue
        (cx, cy), (x1, y1), (x2, y2) = seg[1:]
        r = math.hypot(x1 - cx, y1 - cy)
        a1, a2 = math.atan2(y1 - cy, x1 - cx), math.atan2(y2 - cy, x2 - cx)
        while a2 <= a1:
            a2 += 2 * math.pi
        n = max(4, int(math.ceil((a2 - a1) / (math.pi / 32))))  # 64 chords to a full turn, like Circle
        arc = [(cx + r * math.cos(a1 + (a2 - a1) * k / n), cy + r * math.sin(a1 + (a2 - a1) * k / n))
               for k in range(n + 1)]
        # a clockwise turn is the counterclockwise arc walked backwards
        if pts and math.dist(pts[-1], arc[-1]) < math.dist(pts[-1], arc[0]):
            arc.reverse()
        pts += arc
    return pts + pts[:1] if closed else pts


@dataclass(frozen=True)
class Sketch:
    """A flat drawing in the XY plane: a tuple of items, each ("Rectangle", (x0, y0), (x1, y1)[, corner radius]),
    ("Circle", (cx, cy), r), ("Arc", center, radius, start°, end°, direction), ("Polygon", center, radius, sides,
    rotation°), ("Ellipse", center, width, height, rotation°), ("Slot", center, length, width, rotation°),
    ("Spline", points, closed), or ("Profile", points, closed). Loops inside a closed loop become holes.
    name: the sketch's name in CAE ("" for a profile made straight from points). model: the model it is built in
    (None: the model of each part made from it, or NoCode-Model when no part uses it)."""

    name: str
    items: tuple
    model: Model | None = None
    existing: bool = False

    @property
    def closed(self) -> bool:
        return all(it[0] not in ("Profile", "Spline", "Arc") or
                   (it[0] in ("Profile", "Spline") and it[2]) for it in self.items)

    def outline(self) -> list[list[tuple[float, float]]]:
        """Each item as a polyline (for previews and bounds)."""
        import math
        lines = []
        for it in self.items:
            if sketch_segments(it):
                lines.append(_segments_outline(sketch_segments(it), it[0] != "Profile" or it[2]))
            elif it[0] == "Ellipse":
                (cx, cy), w, h, rotation = it[1:]
                lines.append([_turn(cx + w / 2 * math.cos(t * math.pi / 32), cy + h / 2 * math.sin(t * math.pi / 32),
                                    cx, cy, rotation) for t in range(65)])
            elif it[0] == "Rectangle":
                (x0, y0), (x1, y1) = it[1], it[2]
                lines.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])
            elif it[0] == "Circle":
                (cx, cy), r = it[1], it[2]
                lines.append([(cx + r * math.cos(t * math.pi / 32), cy + r * math.sin(t * math.pi / 32))
                              for t in range(65)])
            elif it[0] == "Arc":
                (cx, cy), r, start, end, direction = it[1:]
                if direction == "Clockwise":
                    while end >= start:
                        end -= 360.0
                else:
                    while end <= start:
                        end += 360.0
                angles = [math.radians(start + (end - start) * k / 32.0) for k in range(33)]
                lines.append([(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles])
            elif it[0] == "Spline":
                pts = list(it[1])
                # CAE draws a smooth spline; the lightweight editor preview uses its control polygon.
                lines.append(pts + [pts[0]] if it[2] and pts[0] != pts[-1] else pts)
            else:
                pts = list(it[1])
                lines.append(pts + [pts[0]] if it[2] and pts[0] != pts[-1] else pts)
        return lines

    def summary(self) -> str:
        kinds = [it[0].lower() for it in self.items]
        return ", ".join(f"{kinds.count(k)} {k}{'s' if kinds.count(k) > 1 else ''}" for k in dict.fromkeys(kinds))


@dataclass(frozen=True)
class Material:
    name: str
    model: Model
    young: float
    poisson: float
    density: float = 0.0
    plastic: tuple = ()  # isotropic hardening: ((yield stress, plastic strain), ...) as Abaqus takes it
    hyper: tuple = ()  # hyperelastic instead of elastic: ("NEO_HOOKE", C10, D1) or ("MOONEY_RIVLIN", C10, C01, D1)
    damping: tuple = ()  # Rayleigh damping: (alpha (mass), beta (stiffness))
    # orthotropic elasticity instead of isotropic: ("LAMINA", E1, E2, Nu12, G12, G13, G23) or
    # ("ENGINEERING_CONSTANTS", E1, E2, E3, Nu12, Nu13, Nu23, G12, G13, G23)
    elastic: tuple = ()
    existing: bool = False  # picked in CAE: used by name, its properties unknown here and never rebuilt
    copy_from: str = ""  # copied by name from this model (a material library) into its own model
    # thermal: conductivity, specific heat, expansion coefficient (0: undefined)
    conductivity: float = 0.0
    specific_heat: float = 0.0
    expansion: float = 0.0
    # how plastic hardens: "" isotropic (plastic's table), "KINEMATIC", "COMBINED" (the same table), or
    # ("JOHNSON_COOK", A, B, n, m, melting, transition)
    hardening: tuple = ()
    damage: tuple = ()  # ductile damage: (fracture strain, displacement at failure)

    @property
    def known(self) -> bool:
        """Are its properties the editor's (not picked or copied from CAE)?"""
        return not (self.existing or self.copy_from)


@dataclass(frozen=True)
class Section:
    name: str
    material: Material
    kind: str
    thickness: float = 0.0
    profile: str = ""
    width: float = 0.0
    height: float = 0.0
    radius: float = 0.0
    n1: tuple = (0.0, 0.0, -1.0)
    plies: tuple = ()  # a composite shell's layup: ((angle in degrees, thickness), ...) from the bottom
    symmetric: bool = False  # the layup mirrored about its mid-plane (written once)
    orientation: tuple = ()  # the material's 1-direction (x, y, z), projected onto shells; () Abaqus' default
    existing: bool = False  # picked in CAE: assigned by name, never rebuilt
    copy_from: str = ""  # copied by name (with its material) from this model into its material's model

    @property
    def known(self) -> bool:
        return not (self.existing or self.copy_from) and self.material.known


SHELLING = ("Solid to shell", "Midsurface")  # Part Feature operations after which a solid part is a shell


@dataclass(frozen=True)
class Part:
    """A part to build in CAE: its model, the profile it is made from and how.

    body: "Shell", "Wire" (beams) or "Solid". method: "Planar", "Extrude" or "Revolve" (about the y axis).
    sketch: the flat profile; points: (x, y, z) tuples instead, for a 3D wire (beam frame).
    """

    name: str
    model: Model
    body: str
    method: str
    sketch: Sketch | None = None
    points: tuple = ()
    depth: float = 0.0
    angle: float = 360.0
    existing: bool = False
    features: tuple = ()
    source: Part | None = None
    sections: tuple = ()  # (Section, selector or None), applied after geometric features and before meshing
    operations: tuple = ()  # independently managed Datum / partition operations; never rebuild the owning part
    file: str = ""  # STEP / IGES / SAT geometry, or an input file's mesh (.inp), when method == Import
    entry: str = ""  # which part of an input file with several (empty: the only or first one)
    # the list item its edits were made for (Mesh over sizes 10, 5: (0,) and (1,)): edits for different items of a
    # list are separate parts (a study); everything else done to a part is that one part
    item: tuple = ()
    space: str = "3D"  # "3D", "2D planar" or "Axisymmetric" (2D: drawn in the XY plane, revolved about y)
    rigid: str = ""  # "" deformable, "Discrete" or "Analytical" rigid surface

    @property
    def flat(self) -> bool:
        """A 2D planar or axisymmetric part (its regions are faces and edges in the XY plane)."""
        return self.space != "3D"

    @property
    def effective_body(self) -> str:
        """What the part is now: "Shell" once a Part Feature turned the solid into a shell (Solid to shell,
        Midsurface); body stays what it was built as."""
        if any(getattr(o, "kind", "") in SHELLING for o in self.operations):
            return "Shell"
        return self.body

    @property
    def mesh_file(self) -> bool:
        """Imported from an Abaqus input file: a mesh without geometry (already meshed)."""
        return self.method == "Import" and self.file.lower().endswith(".inp")

    def summary(self) -> str:
        how = {"Planar": "", "Extrude": f", extruded {self.depth:g}", "Revolve": f", revolved {self.angle:g}°"}
        if self.method == "Import":
            return f"imported geometry from {self.file}"
        what = self.sketch.summary() if self.sketch else f"3D polyline of {len(self.points)} points"
        extra = f", {len(self.features)} feature{'s' if len(self.features) != 1 else ''}" if self.features else ""
        kind = f"{self.rigid.lower()} rigid " if self.rigid else ""
        space = f" ({self.space.lower()})" if self.flat else ""
        return f"{kind}{self.body.lower()}{space} from {what}{how[self.method]}{extra}"


@dataclass(frozen=True)
class MergedPart(Part):
    """A Part made by an Abaqus boolean operation on assembly Instances."""

    instances: tuple = ()
    keep_intersections: bool = False
    keep_originals: bool = True
    operation: str = "Merge"
    target: Instance | None = None

    def summary(self) -> str:
        return (f"boolean merge of {len(self.instances)} instances" if self.operation == "Merge" else
                f"boolean cut of {self.target.name} with {len(self.instances)} instances")


@dataclass(frozen=True)
class Region:
    """Faces, edges, vertices or cells on one CAE part/instance, selected by index or findAt points."""

    source: Part | Instance
    kind: str
    indices: tuple = ()
    points: tuple = ()
    name: str = ""
    form: str = "Selection"
    side: str = "Side 1"
    tolerance: float = 0.0
    feature: Any = None  # kind "reference point": the Part Feature (PartOperation) that made it; points: where
    combine: bool = False  # named regions mapped over instances: make one assembly Set/Surface for the whole node
    # Positions within the geometry selected above. None means all; an empty tuple is a deliberately empty result.
    # This lets If apply a Yes/No mask to a dynamic By-shape selection whose CAE entity indices are not known here.
    positions: tuple | None = None
    position_count: int | None = None  # original selection size expected by that mask; checked when CAE resolves it

    @property
    def by_shape(self) -> tuple:
        """("@shape", "cylinder" | "circle" | "disk", radius, tolerance[, "Equal to" | "Up to" | "At least"]),
        ("@shape", "plane", nx, ny, nz) or ("@shape", "small", largest area), else (). One match of a cylinder or
        circle search (Region's One per geometry) ends with "@at", x, y, z: the match whose centre is there."""
        return self.points[0] if self.points and self.points[0] and self.points[0][0] == "@shape" else ()

    @property
    def at(self) -> tuple:
        """Where a By shape Region of one match is: (x, y, z) of its centre, else ()."""
        spec = self.by_shape
        return tuple(spec[spec.index("@at") + 1:][:3]) if "@at" in spec else ()

    @property
    def boolean(self) -> tuple:
        """("@bool", "Union" | "Intersection" | "Difference", a, b, description), a and b each (indices, points,
        tolerance, positions, position_count) on the same source: resolved in CAE (Set Operation), else ()."""
        return self.points[0] if self.points and self.points[0] and self.points[0][0] == "@bool" else ()

    @property
    def labels(self) -> tuple:
        """("@labels", "nodes" | "elements" | "faces", ((face, ((first, last), ...)), ...)): mesh nodes, elements
        or element faces (face "S1".."S6"; "" for nodes and elements) by label ranges, else ()."""
        return self.points[0] if self.points and self.points[0] and self.points[0][0] == "@labels" else ()

    @property
    def dynamic(self) -> bool:
        """Its members are only known once CAE resolves it (By shape, a Set Operation, or mesh labels)."""
        return bool(self.by_shape or self.boolean or self.labels)

    def operand(self) -> tuple:
        """This Region's selection as one side of a boolean Region."""
        return (tuple(self.indices), tuple(self.points), self.tolerance, self.positions, self.position_count)

    def summary(self) -> str:
        if self.kind == "reference point":
            return f"reference point {self.feature.name if self.feature is not None else ''}".rstrip()
        if self.boolean and self.positions is None:
            named = f" as {self.form} {self.name}" if self.name else ""
            return f"{self.boolean[4]}{named}"
        if self.labels and self.positions is None:
            n = sum(b - a + 1 for _, ranges in self.labels[2] for a, b in ranges)
            what = {"nodes": "node", "elements": "element", "faces": "element face"}[self.labels[1]]
            named = f" as {self.form} {self.name}" if self.name else ""
            return f"{n} mesh {what}{'' if n == 1 else 's'} by label{named}"
        spec = self.by_shape
        if spec and self.positions is None:
            what = {"cylinder": "cylindrical faces", "circle": "circular edges", "disk": "circular faces",
                    "plane": "flat faces", "small": "faces"}[spec[1]]
            if spec[1] == "plane":
                detail = " facing " + ", ".join(f"{c:g}" for c in spec[2:5])
            elif spec[1] == "small":
                detail = f" of area up to {spec[2]:g}"
            else:
                match = spec[4] if len(spec) > 4 and spec[4] != "@at" else "Equal to"
                detail = (f" of radius {'' if match == 'Equal to' else match.lower() + ' '}{spec[2]:g}"
                          if spec[2] > 0 else "")
            named = f" as {self.form} {self.name}" if self.name else ""
            if self.at:
                return f"the {what[:-1]}{detail} at {', '.join(f'{c:g}' for c in self.at)}{named}"
            return f"all {what}{detail}{named}"
        n = len(self.positions) if self.positions is not None else len(self.indices) or len(self.points)
        method = ("kept by condition" if self.positions is not None else
                  "picked" if self.indices else "requested with findAt")
        named = f" as {self.form} {self.name}" if self.name else ""
        return f"{n} {self.kind}{'' if n == 1 else 's'} {method}{named}"


@dataclass(frozen=True)
class DatumPlane:
    name: str
    # Kept as ``part`` so saved graphs keep their existing port handle. It can also be an Instance (an assembly
    # datum tied to that instance) or a Model (a datum in the root assembly's global coordinates).
    part: Part | Instance | Model
    plane: str
    offset: float = 0.0
    kind: str = "Principal plane"
    points: tuple = ()
    region: Region | None = None
    flip: str = "Side 1"
    # kind "Coordinate system": plane is its type (Cartesian, Cylindrical, Spherical) and points its origin, a point
    # on its first axis and one in its 1-2 plane

    def summary(self) -> str:
        level = "part" if isinstance(self.part, Part) else "assembly"
        return f"{self.kind} datum on {self.part.name} ({level})"

    def geometry(self) -> dict:
        """Where the datum is, in part coordinates, for the 3D preview: a plane (origin, normal), an axis (origin,
        direction) or a point. An offset from a face isn't known without CAE: the face's picked points stand in."""
        unit = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}
        p = [tuple(float(c) for c in q) + (0.0,) * (3 - len(q)) for q in self.points]
        base = {"name": self.name}
        if self.kind == "Principal plane":
            normal = unit[{"XY": "Z", "YZ": "X", "XZ": "Y"}.get(self.plane, "Z")]
            return {**base, "kind": "plane", "origin": [c * self.offset for c in normal], "normal": list(normal)}
        if self.kind == "Principal axis":
            return {**base, "kind": "axis", "origin": [0.0, 0.0, 0.0], "direction": list(unit.get(self.plane, unit["Z"]))}
        if self.kind == "Plane through 3 points" and len(p) == 3:
            a, b, c = p
            u, v = [b[i] - a[i] for i in range(3)], [c[i] - a[i] for i in range(3)]
            n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
            return {**base, "kind": "plane", "origin": list(a), "normal": n, "points": [list(q) for q in p]}
        if self.kind == "Axis through 2 points" and len(p) == 2:
            return {**base, "kind": "axis", "origin": list(p[0]), "direction": [p[1][i] - p[0][i] for i in range(3)],
                    "points": [list(q) for q in p]}
        if self.kind in ("Point", "Coordinate system") and p:
            return {**base, "kind": "point", "origin": list(p[0])}
        face = [list(q) + [0.0] * (3 - len(q)) for q in (self.region.points if self.region else ())]
        return {**base, "kind": "face", "points": face}


@dataclass(frozen=True)
class PartOperation:
    name: str
    part: Part
    kind: str
    datum: DatumPlane | None
    selector: tuple | None = None
    amount: float = 0.0
    sketch: Sketch | None = None
    tool: tuple | None = None  # selector of the face a partition extends
    point: tuple = ()  # a Reference point's x, y, z

    def summary(self) -> str:
        return (f"{self.kind} on {self.part.name} using {self.datum.name}" if self.datum else
                f"{self.kind} on {self.part.name}")


@dataclass(frozen=True)
class PendingMeasure:
    """A CAE geometry query awaiting the next live synchronization."""

    key: str
    quantity: str
    first: Any
    second: Any = None


@dataclass(frozen=True)
class AnalysisStep:
    name: str
    model: Model
    kind: str
    previous: AnalysisStep | None = None
    time: float = 1.0
    nlgeom: bool = False
    initial: float = 0.1
    maximum: float = 1.0
    minimum: float = 1e-5
    increments: int = 100
    modes: int = 10
    conditions: tuple = ()
    scale: float = 1.0  # Dynamic explicit: time scaling factor on the stable increment
    mass_dt: float = 0.0  # Dynamic explicit: target increment of (semi-automatic) mass scaling, 0 none
    transient: bool = False  # Heat transfer / coupled temperature-displacement: transient (else steady state)
    max_temperature_change: float = 0.0  # transient: largest temperature change per increment (0: fixed increments)
    max_load_factor: float = 0.0  # Static Riks: stop at this load proportionality factor (0: no limit)

    @property
    def explicit(self) -> bool:
        return self.kind == "Dynamic explicit"

    @property
    def perturbation(self) -> bool:
        """A linear perturbation step (eigenvalues about the state of the general step before it)."""
        return self.kind in ("Frequency", "Buckling")

    @property
    def thermal(self) -> bool:
        """Has temperature degrees of freedom: heat transfer, or coupled temperature-displacement."""
        return self.kind in ("Heat transfer", "Coupled temperature-displacement")

    def summary(self) -> str:
        return f"{self.kind} step after {self.previous.name if self.previous else 'Initial'}"


@dataclass(frozen=True)
class OutputRequests:
    """What a Step writes to the ODB, kept apart from the step itself so changing it doesn't rebuild the step (and
    every load in it) in CAE. field: variables every frame (empty: Abaqus' preselected ones); intervals: that many
    evenly spaced frames (0: every increment); history: variables at region (an Instance Region), or for the
    whole model (energies) without one."""

    step: AnalysisStep
    field: tuple = ()
    intervals: int = 0
    history: tuple = ()
    region: Region | None = None

    @property
    def name(self) -> str:
        return self.step.name

    def summary(self) -> str:
        parts = []
        if self.field or self.intervals:
            parts.append(f"field {', '.join(self.field) or 'preselected'}"
                         + (f" at {self.intervals} intervals" if self.intervals else ""))
        if self.history:
            parts.append(f"history {', '.join(self.history)}" + (f" at {self.region.summary()}" if self.region else ""))
        return "; ".join(parts)


AMPLITUDE_KINDS = ["Tabular", "Smooth step", "Equally spaced", "Periodic", "Modulated", "Exponential decay",
                   "Solution dependent", "User"]


@dataclass(frozen=True)
class Amplitude:
    """A time history multiplying a load or boundary condition (Abaqus' amplitude types).
    data: Tabular / Smooth step ((time, value), ...); Equally spaced (value, ...); Periodic ((A, B), ...).
    settings: the type's numbers by Abaqus' argument names, e.g. (("frequency", 6.28), ("start", 0.0), ("a_0", 0.0)).
    smooth: Tabular / Equally spaced smoothing (None: the solver's default, 0.05)."""
    kind: str
    data: tuple = ()
    settings: tuple = ()
    time_span: str = "Step"  # "Step" (each step's own time) or "Total" (the analysis' total time)
    smooth: float | None = None

    def get(self, key: str, default: float = 0.0) -> float:
        return dict(self.settings).get(key, default)

    def points(self) -> tuple:
        """((time, value), ...) where the type is given by points (Tabular, Smooth step, Equally spaced), else ()."""
        if self.kind in ("Tabular", "Smooth step"):
            return self.data
        if self.kind == "Equally spaced":
            begin, dt = self.get("begin"), self.get("fixedInterval", 1.0)
            return tuple((begin + i * dt, a) for i, a in enumerate(self.data))
        return ()

    def sample(self, t):
        """The amplitude at times t (a numpy array), or None where it depends on the solution or a subroutine."""
        t = np.asarray(t, dtype=float)
        pts = self.points()
        if pts:
            ts, a = np.array([p[0] for p in pts]), np.array([p[1] for p in pts])
            if self.kind != "Smooth step" or len(ts) < 2:
                return np.interp(t, ts, a)
            i = np.clip(np.searchsorted(ts, t, side="right") - 1, 0, len(ts) - 2)
            span = np.where(ts[i + 1] > ts[i], ts[i + 1] - ts[i], 1.0)
            xi = np.clip((t - ts[i]) / span, 0.0, 1.0)
            out = a[i] + (a[i + 1] - a[i]) * xi ** 3 * (10 - 15 * xi + 6 * xi ** 2)
            return np.where(t <= ts[0], a[0], np.where(t >= ts[-1], a[-1], out))
        t0, a0 = self.get("start"), self.get("a_0") if self.kind == "Periodic" else self.get("initial")
        s = t - t0
        if self.kind == "Periodic":
            w = self.get("frequency")
            out = np.full_like(t, a0) + sum(A * np.cos(n * w * s) + B * np.sin(n * w * s)
                                            for n, (A, B) in enumerate(self.data, 1))
        elif self.kind == "Modulated":
            out = a0 + self.get("magnitude") * np.sin(self.get("frequency1") * s) * np.sin(self.get("frequency2") * s)
        elif self.kind == "Exponential decay":
            out = a0 + self.get("maximum") * np.exp(-s / self.get("decayTime", 1.0))
        elif self.kind == "Solution dependent":
            return np.full_like(t, self.get("initial", 1.0))
        else:
            return None
        return np.where(s >= 0, out, a0)

    def summary(self) -> str:
        over = " over total time" if self.time_span == "Total" else ""
        if self.kind in ("Tabular", "Smooth step", "Equally spaced"):
            return f"{self.kind} amplitude, {len(self.data)} points{over}"
        return f"{self.kind} amplitude{over}"


@dataclass(frozen=True)
class Condition:
    name: str
    step: AnalysisStep
    kind: str
    region: Region | None = None
    values: tuple = ()
    previous: Condition | None = None  # region: a Region, or a coupling (its reference point)
    # Over time: ((time, factor), ...) and "Tabular" / "Smooth step", or an Amplitude (any of Abaqus' types); empty:
    # the step's default ramp/instant.
    amplitude: tuple | Amplitude = ()
    amplitude_shape: str = "Tabular"
    # Over space, multiplying the magnitude: ("expression", "1 + X / 100") or ("points", ((x, y, z, v), ...)).
    distribution: tuple = ()
    # The steps it is active in (stepwise.parse_active; empty: from `step` on, a temperature from Initial) and its
    # settings' changes from step to step: ((setting, ((step name, value), ...)), ...), e.g. ("z", (("Horizon",
    # 0.0),)) or ("amplitude", (("Wind", ((0, 0), (1, 1))),)). Step names are resolved once the model is known.
    active: tuple = ()
    changes: tuple = ()
    fix: str = ""  # Fixed: "" encastre (all), or "Pinned", "XSYMM", "YSYMM", "ZSYMM", "XASYMM", "YASYMM", "ZASYMM"
    csys: DatumPlane | None = None  # an assembly coordinate system Datum its components are in (else global)

    @property
    def stepwise(self) -> bool:
        return bool(self.active or self.changes)

    @property
    def predefined(self) -> bool:
        return self.kind in ("Temperature", "Initial velocity")

    def summary(self) -> str:
        extra = (" over time" if self.amplitude else "") + (" varying in space" if self.distribution else "")
        when = (" in Initial" if self.kind == "Initial velocity" else " changing by step" if self.stepwise
                else " from Initial" if self.kind == "Temperature" else f" in {self.step.name}")
        return f"{self.kind}{when}" + (f" on {self.region.summary()}" if self.region else "") + extra


@dataclass(frozen=True)
class Interaction:
    name: str
    step: AnalysisStep | None  # ties and couplings are model constraints: no step needed
    kind: str
    # Tie/contact: the two faces. Coupling: master an optional control vertex, slave the faces. Connector: its two
    # ends, each a vertex Region or a coupling (its reference point), or None for a typed point / second-end ground.
    # General contact:
    # none (the whole model), one (self-contact) or two faces.
    master: Region | Interaction | None
    slave: Region | Interaction | None
    friction: float = 0.0
    adjust: bool = True
    sliding: str = "Finite"
    previous: Interaction | None = None
    point: tuple = ()  # a coupling's reference point, or a connector's first end (x, y, z)
    dofs: tuple = ()  # a coupling's constrained degrees of freedom, e.g. ("U1", "U2", "U3")
    influence: float = 0.0  # a coupling's radius of influence (0: the whole surface)
    point2: tuple = ()  # a connector's second end
    ground: bool = False  # a connector's second end is fixed ground rather than a point
    connector: str = ""  # "Beam", "Join", "Hinge", "Axial", "Bushing"
    stiffness: tuple = ()  # ((component, stiffness), ...) for a connector's free components
    damping: tuple = ()  # ((component, damping coefficient), ...) likewise (viscous: force per velocity)
    # Pair contact: the steps it is active in; contact: friction changes by step (as Condition's)
    active: tuple = ()
    changes: tuple = ()
    mass: float = 0.0  # Point mass: its mass, and rotary inertia (I11, I22, I33) or ()
    inertia: tuple = ()
    dof: int = 0  # Equation: Slave's degree of freedom dof = ratio × Master's
    ratio: float = 0.0

    @property
    def stepwise(self) -> bool:
        return bool(self.active or self.changes)

    @property
    def coupling(self) -> bool:
        return self.kind.endswith("coupling")

    @property
    def point_owner(self) -> bool:
        """Has its own reference point (an assembly set <name>-RP) to load, hold, connect or record."""
        return self.coupling or self.kind == "Rigid body"

    @property
    def model(self) -> Model:
        if self.step is not None:
            return self.step.model
        for end in (self.slave, self.master):
            if isinstance(end, Interaction):
                return end.model
            if isinstance(end, Region):
                return end.source.model if isinstance(end.source, Part) else end.source.part.model
        raise ValueError(f"{self.kind} needs a Step or a Region to know its model")

    def summary(self) -> str:
        def end(e, point):
            if e is None:
                return "RP at " + ", ".join(f"{c:g}" for c in point) if point else "the whole model"
            return e.name if isinstance(e, Interaction) else e.summary()

        if self.coupling:
            within = f" within {self.influence:g}" if self.influence else ""
            return f"{self.kind}: {end(self.master, self.point)} to {self.slave.summary()}{within}"
        if self.kind == "Rigid body":
            return f"Rigid body: {self.slave.summary()} moving with {end(self.master, self.point)}"
        if self.kind == "Point mass":
            return f"Point mass {self.mass:g} at {end(self.master, ())}"
        if self.kind == "Equation":
            return (f"Equation: {end(self.slave, ())} DOF {self.dof} = {self.ratio:g} × {end(self.master, ())} "
                    f"DOF {self.dof}")
        if self.kind == "Connector":
            second = "ground" if self.ground else end(self.slave, self.point2)
            return f"{self.connector} connector: {end(self.master, self.point)} to {second}"
        if self.kind == "General contact":
            if self.master is None and self.slave is None:
                return "General contact: the whole model"
            return "General contact: " + " with ".join(e.summary() for e in (self.master, self.slave) if e is not None)
        return f"{self.kind}: {self.master.summary()} to {self.slave.summary()}"


@dataclass(frozen=True)
class Job:
    """An Abaqus analysis job attached to the last relevant object in a model graph."""

    name: str
    model: Model
    source: Any
    action: str = "Write input"
    directory: str = "."
    cpus: int = 1
    memory: int = 90
    user: str = ""  # a user subroutine file (.f / .for / .obj), e.g. a UAMP for a User amplitude

    def summary(self) -> str:
        return f"{self.action.lower()} {self.name} for {self.model.name}"


@dataclass(frozen=True)
class Instance:
    """A part placed in its model's assembly, then turned and moved by its steps, in order: ("rotate", axis "X" /
    "Y" / "Z", angle in degrees, pivot (x, y, z)), ("move", (x, y, z)) or ("align", how, moving Region, fixed
    Region, flip, clearance): an Abaqus position constraint (Face to face, Edge to edge, Coaxial, Point to point)
    moving this instance so its geometry meets another instance's. An Instance node fed an instance adds steps to it
    (the same instance, moved further). origin: the name it was first placed with (one chain)."""

    name: str
    part: Part
    steps: tuple = ()
    origin: str = ""
    existing: bool = False
    dependent: bool = True
    removed: bool = False  # a downstream Instance node removes this graph-owned placement from the assembly

    def summary(self) -> str:
        def step(s):
            if s[0] == "align":
                return f"{s[1][0].lower() + s[1][1:]} to {s[3].source.name}"
            if s[0] == "move":
                return "moved " + ", ".join(f"{c:g}" for c in s[1])
            about = f" about {s[3][0]:g}, {s[3][1]:g}, {s[3][2]:g}" if any(s[3]) else ""
            return f"turned {s[2]:g}° about {s[1]}" + about
        prefix = "removed instance of" if self.removed else "instance of"
        return f"{prefix} {self.part.name}" + "".join(", " + step(s) for s in self.steps)


PORT_TYPES = {
    "model": Model,
    "sketch": Sketch,
    "part": Part,
    "instance": Instance,
    "region": Region,
    "datum": DatumPlane,
    "analysis_step": AnalysisStep,
    "condition": Condition,
    "amplitude": Amplitude,
    "interaction": Interaction,
    "job": Job,
    "bool": Bool,
    "text": Text,
    "odb": Odb,
    "field": Field,
    "set": EntitySet,
    "series": Series,
    "number": Number,
}
