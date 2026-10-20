"""The mesh CAE generated for a part, and which of its element faces / edges / nodes a Region covers, for the
editor's 3D preview.

The build script asks CAE for the surface mesh of each meshed part the graph shows (and for each Region on one),
CAE prints them on one MARKER line, ``ingest`` caches them here by what the part is (its fingerprint), and
``attach`` puts them on the displays of the nodes whose output is that part or a Region on it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from .values import DatumPlane, Instance, Part, Region

MARKER = "__NODECODE_FEA_MESH__"
LIMIT = 30000  # element faces past which a mesh isn't sent to the editor (too heavy to draw in every run)
_meshes: dict[str, dict] = {}

# Python 2/3 code run in CAE: the surface of a part's mesh as corner-node polygons (faces) and segments (lines),
# with the coordinates of the nodes used.
CAE_CODE = r'''
_CORNERS = {3: 3, 4: 4, 6: 3, 8: 4, 9: 4}
class _Mesh(object):
    def __init__(self):
        self.nodes, self.faces, self.lines, self.points = {}, [], [], []
    def keep(self, ns):
        for n in ns:
            if n.label not in self.nodes:
                self.nodes[n.label] = [round(c, 6) for c in n.coordinates]
        return [n.label for n in ns]
    def face(self, ns):
        self.faces.append(self.keep(list(ns)[:_CORNERS.get(len(ns), len(ns))]))
        if len(self.faces) > _LIMIT:
            raise OverflowError('too big')
    def line(self, ns):
        ns = list(ns)
        self.lines.append(self.keep([ns[0], ns[-1]]))
    def of_face(self, owner, f):
        # a face with no elements (a midsurface part's solid faces: the shell is meshed) gives None
        if len(owner.cells):
            for ef in f.getElementFaces() or ():
                self.face(ef.getNodes())
        else:
            for el in f.getElements() or ():
                self.face(el.getNodes())
    def of_edge(self, e):
        on = set(n.label for n in e.getNodes())
        seen = set()
        for el in e.getElements():
            ns = el.getNodes()
            if str(el.type)[:1] in ('B', 'T', 'P') and len(ns) <= 3:
                self.line(ns)
                continue
            try:
                edges = el.getElemEdges()
            except Exception:
                edges = ()
            for me in edges:
                mn = [n for n in me.getNodes() if n.label in on]
                k = tuple(sorted(n.label for n in mn[:2]))
                if len(mn) >= 2 and k not in seen:
                    seen.add(k)
                    self.line(mn[:2])
        self.points += self.keep(e.getNodes())
    def answer(self, elements):
        return {'nodes': self.nodes, 'faces': self.faces, 'lines': self.lines, 'points': self.points,
                'elements': elements}
def _part_mesh(p):
    m = _Mesh()
    try:
        if len(p.faces):
            for f in p.faces:
                m.of_face(p, f)
        elif len(p.edges):
            for e in p.edges:
                for el in e.getElements():
                    m.line(el.getNodes())
        else:  # an orphan mesh (imported): solids by their outer element faces, the rest by their elements
            solid = [el for el in p.elements if str(el.type).startswith('C3D')]
            if solid:
                for mf in p.elementFaces:
                    if len(mf.getElements()) == 1:
                        m.face(mf.getNodes())
            for el in p.elements:
                t, ns = str(el.type), el.getNodes()
                if t.startswith('C3D'):
                    continue
                if t[:1] in ('B', 'T') or len(ns) == 2:
                    m.line(ns)
                else:
                    m.face(ns)
    except OverflowError:
        return {'too_big': len(p.elements)}
    except Exception:  # no mesh (not generated, or it failed: the Mesh node says why)
        return {}
    return m.answer(len(p.elements))
def _region_mesh(o, kind, items):
    if not len(o.elements):
        return {}
    m = _Mesh()
    try:
        for g in items:
            if hasattr(g, 'coordinates'):  # a mesh node (Mesh labels)
                m.points += m.keep([g])
            elif hasattr(g, 'getElemFaces'):  # a mesh element: a solid by its faces
                for ns in ([ef.getNodes() for ef in g.getElemFaces()] if str(g.type).startswith('C3D')
                           else [g.getNodes()]):
                    m.face(ns)
            elif hasattr(g, 'face') and not hasattr(g, 'pointOn'):  # an element face
                m.face(g.getNodes())
            elif kind == 'face':
                m.of_face(o, g)
            elif kind == 'cell':
                for i in g.getFaces():
                    m.of_face(o, o.faces[i])
            elif kind == 'edge':
                m.of_edge(g)
            else:
                m.points += m.keep(g.getNodes())
    except OverflowError:
        return {'too_big': len(o.elements)}
    try:
        count = len(set(el.label for g in items for el in g.getElements() or ()))
    except Exception:
        count = len(m.faces) + len(m.lines) or len(m.points)
    return m.answer(count)
'''


def _key(*what) -> str:
    return hashlib.sha1(repr(what).encode()).hexdigest()[:20]


def part_key(final) -> str:
    return _key("mesh", final)


def region_key(final, region: Region) -> str:
    return _key("region", final, replace(region, source=final, name=""))


def meshed(part: Part) -> bool:
    """Whether CAE holds a mesh for the part as built: a Mesh that generates it, an input file's mesh, or a part picked
    from CAE (which may have one)."""
    return (any(f[0] == "Mesh" and len(f) > 8 and f[8] for f in part.features) or part.existing
            or (part.method == "Import" and part.file.lower().endswith(".inp")))


def as_built(v, parts: list):
    """A Part or Instance as CAE holds it: the part with every edit made to it anywhere in the graph (an instance of
    an earlier stage places that part)."""
    from .cae_model import final_part

    if isinstance(v, Instance):
        return replace(v, part=final_part(v.part, parts))
    return final_part(v, parts)


def _wanted(node_inputs) -> list:
    """(shown as built, shown as the build script names it, Region or None) for the Parts, Instances and Regions on
    them the graph's outputs show, when their part is meshed. An instance's mesh is the part's, placed: dependent
    instances (CAE only instances a meshed part as dependent)."""
    from .cae_model import _flat

    outs = list(getattr(node_inputs, "outputs", {}).values())
    values = [v for out in outs for v in _flat(list(out.values()))]
    parts = [v for v in values if isinstance(v, Part)]
    found = {}
    for v in values:
        region = v if isinstance(v, Region) else None
        shown = region.source if region is not None else v.part if isinstance(v, DatumPlane) else v
        if not isinstance(shown, (Part, Instance)):
            continue
        built = as_built(shown, parts)
        if meshed(built.part if isinstance(built, Instance) else built):
            found.setdefault((built, built if isinstance(built, Part) else shown, region), None)
    return list(found)


def queries(node_inputs, objects: list[dict]) -> list[str]:
    """The build script lines that print the meshes (and Region highlights) not cached yet; [] when none."""
    from . import cae_model

    lines, asked = [], set()
    for built, named, region in _wanted(node_inputs):
        try:
            obj = cae_model.object_code(named, objects)
        except LookupError:
            continue
        k = part_key(built)
        if k not in _meshes and k not in asked:
            asked.add(k)
            lines.append(f"_meshes[{k!r}] = _part_mesh({obj})")
        if region is not None and region.kind != "reference point":  # a point, no elements: its marker shows it
            r = region_key(built, region)
            if r not in _meshes and r not in asked:
                asked.add(r)
                seq, _ = cae_model.selection_code(replace(region, source=named), objects, "")
                lines += ["try:", f"    _meshes[{r!r}] = _region_mesh({obj}, {region.kind!r}, tuple({seq}))",
                          "except Exception as exc:", f"    _meshes[{r!r}] = {{'error': str(exc)}}"]
    if not lines:
        return []
    return ([f"_LIMIT = {LIMIT}"] + CAE_CODE.strip("\n").splitlines() + ["_meshes = {}"] + lines +
            # cached for good by the part's fingerprint: not from a part another editor tab built (see _build)
            ["if not globals().get('_left'):",
             f"    print({MARKER!r} + __import__('json').dumps(_meshes, sort_keys=True))"])


def ingest(output: str) -> bool:
    """Cache the meshes CAE printed; True when one is new or changed. An empty mesh (not generated, or the build
    failed) isn't kept, so the next sync asks again."""
    changed = False
    for line in (output or "").splitlines():
        if not line.startswith(MARKER):
            continue
        for k, v in json.loads(line[len(MARKER):]).items():
            if not isinstance(v, dict) or not any(v.get(f) for f in ("elements", "faces", "lines", "points", "too_big", "error")):
                continue
            if _meshes.get(k) != v:
                _meshes[k] = v
                changed = True
    return changed


def _payload(mesh: dict, region: dict | None) -> dict:
    """What the 3D preview draws: node coordinates, faces and lines as indices into them, and the highlight."""
    if "too_big" in mesh:
        return {"too_big": int(mesh["too_big"])}
    coords = {**mesh.get("nodes", {}), **((region or {}).get("nodes") or {})}
    index = {label: i for i, label in enumerate(coords)}
    out = {"nodes": [list(c) for c in coords.values()], "elements": int(mesh.get("elements", 0)),
           "faces": [[index[str(n)] for n in f] for f in mesh.get("faces", [])],
           "lines": [[index[str(n)] for n in ln] for ln in mesh.get("lines", [])]}
    if region is not None:
        if "error" in region or "too_big" in region:
            out["highlight"] = {"note": ("Region not on the meshed part (a later Part Feature changed it?): "
                                         + region["error"]) if "error" in region else "Region too big to highlight"}
        else:
            out["highlight"] = {"faces": [[index[str(n)] for n in f] for f in region.get("faces", [])],
                                "lines": [[index[str(n)] for n in ln] for ln in region.get("lines", [])],
                                "points": sorted({index[str(n)] for n in region.get("points", [])}),
                                "elements": int(region.get("elements", 0))}
    return out


def mesh_for(shown, parts: list, region: Region | None = None) -> dict | None:
    """The cached mesh of a Part or Instance as built (with ``region`` highlighted), as the preview's payload, else
    None."""
    built = as_built(shown, parts)
    mesh = _meshes.get(part_key(built))
    if mesh is None:
        return None
    return _payload(mesh, _meshes.get(region_key(built, region)) if region is not None else None)


def attach(results: dict, node_inputs) -> None:
    """Put the cached mesh on the 3D preview of every node whose output is a meshed part, an instance of one, a Datum on
    a part or a Region on either (a Mesh node on a Region highlights that Region). An instance's mesh is drawn where it is in the
    assembly, among the model's other instances. A part not meshed yet where the node takes it (the Part node feeding
    the Mesh) shows its geometry, even though CAE's part is meshed later in the graph."""
    from .cae_model import _flat

    outs = getattr(node_inputs, "outputs", {})
    parts = [v for out in outs.values() for v in _flat(list(out.values())) if isinstance(v, Part)]
    for nid, out in outs.items():
        res = results.get(nid)
        first = next(iter(out.values()), None) if out else None
        if not res or res.get("status") != "ok" or not isinstance(first, (Part, Instance, Region, DatumPlane)):
            continue
        region = first if isinstance(first, Region) else None
        target = (node_inputs.get(nid) or {}).get("target")
        if region is None and isinstance(target, Region) and isinstance(target.source, Part):
            region = target
        shown = first.source if isinstance(first, Region) else first.part if isinstance(first, DatumPlane) else first
        if not isinstance(shown, (Part, Instance)):
            continue
        # only once the node's own part is meshed: the Part before its Mesh node (or an instance of it) is geometry
        if not meshed(shown.part if isinstance(shown, Instance) else shown):
            continue
        mesh = mesh_for(shown, parts, region)
        if mesh is None:
            continue
        display = dict(res.get("display") or {})
        if display.get("kind") != "model3d":
            part = shown.part if isinstance(shown, Instance) else shown
            display = {"kind": "model3d", "title": display.get("title") or getattr(first, "name", ""),
                       "model": part.model.name, "bodies": [], "markers": [],
                       "focus": (shown.origin or shown.name) if isinstance(shown, Instance) else ""}
        if isinstance(first, DatumPlane):
            display["datums"] = [first.geometry()]
        results[nid] = {**res, "display": {**display, "mesh": mesh}}
