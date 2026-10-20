"""Output nodes: values, tables, charts, exports and the Contour Plot."""

from __future__ import annotations

import functools
import numpy as np

from ..mesh import surface_for
from ..values import EntitySet, Field, Number, Odb, Region, Series
from .core import _fmt, Param, Port, register, _require_scalar
from .common import entity_name, _flat_items, group_members, _merged_set, _numbers_table
from .data import _point_word
from .field import convert_set, field_filter, field_from_set, _field_title
from .maths import _flatten, value_table


# ----------------------------------------------------------------- output
def _value_display(inputs, params):
    from .previews import default_display, summarize
    v = inputs["value"]
    if isinstance(v, Field):
        return _table_display(v, params["sort"], int(params["limit"]))
    if isinstance(v, list) and v and all(isinstance(x, Number) for x in v):
        return _numbers_table(v, params["label"] or "Values")
    if not isinstance(v, Number):
        return default_display(v) or {"kind": "info", "title": params["label"] or "Value",
                                      "items": [[params["label"] or "Value", summarize(v)]]}
    return {"kind": "value", "value": v.value, "label": params["label"] or v.label, "unit": params["unit"],
            "where": v.where, "digits": int(params["digits"])}


@register("view.value", "Value", "Output",
          "Show any result: a number as a large readout, a list of numbers or a field as a table (sorted, with CSV "
          "export), anything else (texts, sets, Yes/No…) as a summary.",
          inputs=[Port("value", "any", "Value")], outputs=[],  # any depth: the data panel opens nested lists
          params=[Param("label", "text", "", "Label"), Param("unit", "text", "", "Unit"),
                  Param("digits", "int", 4, "Significant digits"),
                  Param("sort", "select", "Descending", "Table sort", options=["Descending", "Ascending", "Label"],
                        help="For fields"),
                  Param("limit", "int", 50, "Table rows", help="For fields")],
          display=lambda o, i, p: _value_display(i, p), lists=True)
def view_value(inputs, params):
    return {}


def _chart_display(series: list[Series], title="", xlabel="", ylabel="") -> dict:
    d = {"kind": "chart", "title": title,
         "xlabel": xlabel or (series[0].xlabel if series else ""),
         "ylabel": ylabel or (series[0].ylabel if len(series) == 1 else ""),
         "series": [{"name": s.name, "x": s.x, "y": s.y} for s in series]}
    try:  # the same curves as the table a Formula indexes: rows = curves, columns = Instance, Node, then frames
        t = value_table(series if len(series) > 1 else series[0])
    except ValueError:
        t = None
    if t is not None:
        rows = [[v.item() if hasattr(v, "item") else v for v in r] for r in t.data[:CSV_ROW_LIMIT]]
        d["table"] = {"kind": "table", "title": title or f"{len(series)} curves", "columns": t.columns,
                      "rows": rows[:200], "allRows": rows, "total": len(rows), "stats": [],
                      "indexed": True, "rowIndex": list(range(min(len(rows), 200)))}
    return d


@register("view.chart", "Chart", "Output", "Plot one or more curves.",
          inputs=[Port("series", "series", "Series", multi=True)], outputs=[],
          params=[Param("title", "text", "", "Title"), Param("xlabel", "text", "", "X label"),
                  Param("ylabel", "text", "", "Y label"),
                  Param("viewport", "select", "Current", "Viewport", options=["Current", "New"],
                        help="Where this shows up in Abaqus/CAE (live link and CAE script). Current: CAE's current "
                             "viewport, the safe choice. New: a viewport of its own, reused on every update. "
                             "Warning: on Abaqus/CAE 2026, showing an ODB in a new viewport can crash CAE.")],
          display=lambda o, i, p: _chart_display(_flatten(i["series"]), p["title"], p["xlabel"], p["ylabel"]))
def view_chart(inputs, params):
    return {}


CSV_ROW_LIMIT = 50_000


def _table_display(f: Field, sort: str = "Descending", limit: int = 100) -> dict:
    order = np.arange(len(f.labels))
    if sort != "Label" and len(order):
        key = f.values[:, 0] if f.values.shape[1] == 1 else np.linalg.norm(f.values, axis=1)
        order = np.argsort(np.nan_to_num(key, nan=-np.inf))
        if sort == "Descending":
            order = order[::-1]
    else:
        order = np.lexsort((f.labels, f.instances.astype(str)))
    show_ip = f.integration_points is not None and bool(np.any(f.integration_points))
    show_sp = f.section_points is not None and bool(np.any(f.section_points))
    rows = [[str(f.instances[i]), int(f.labels[i]),
             *([int(f.integration_points[i])] if show_ip else []),
             *([int(f.section_points[i])] if show_sp else []),
             *[float(v) for v in f.values[i]]] for i in order[:CSV_ROW_LIMIT]]
    stats = [{"name": c, "min": float(np.nanmin(f.values[:, k])), "max": float(np.nanmax(f.values[:, k]))}
             for k, c in enumerate(f.components)] if len(f.labels) else []
    # Same columns as the table a Formula indexes (a[:, 2] is the first value column); rowIndex is each row's
    # position there, since the rows here are sorted.
    return {"kind": "table", "title": _field_title(f),
            "columns": ["Instance", f.entity.capitalize(), *([_point_word(f).capitalize()] if show_ip else []),
                        *(["Section point"] if show_sp else []), *f.components], "rows": rows[:limit],
            "allRows": rows, "total": len(order), "stats": stats, "indexed": True,
            "rowIndex": [int(i) for i in order[:limit]]}


@register("view.table", "Table", "Output", "List field values, sorted, with CSV export.",
          inputs=[Port("field", "field", "Field")], outputs=[],
          params=[Param("sort", "select", "Descending", "Sort", options=["Descending", "Ascending", "Label"]),
                  Param("limit", "int", 50, "Rows shown")],
          display=lambda o, i, p: _table_display(i["field"], p["sort"], int(p["limit"])), hidden=True)
def view_table(inputs, params):
    return {}


MAX_MARKERS = 2000
LABELLED_MARKERS = 30


def _member_markers(item, odb: Odb, surf, coords: np.ndarray) -> tuple[list, list]:
    """A Field or Set on the Results input: a dot on each member (labelled with its value when there are few)."""
    kind = item.entity if isinstance(item, Field) else item.kind
    idx = surf.lookup(kind, item.instances, item.labels)
    ok = np.nonzero(idx >= 0)[0]
    note = f"{item.name}: {len(item.labels)} {kind}s marked"
    if len(ok) > MAX_MARKERS:
        ok = ok[:MAX_MARKERS]
        note += f" (first {MAX_MARKERS} shown)"
    pos = coords[idx[ok]] if kind == "node" else surf.element_centers(idx[ok], coords)
    labelled = len(ok) <= LABELLED_MARKERS
    vals = item.values[:, 0] if isinstance(item, Field) and item.values.shape[1] == 1 else None
    markers = []
    for j, row in enumerate(ok):
        where = f"{kind} {entity_name(str(item.instances[row]), item.labels[row])}"
        text = (_fmt(float(vals[row])) if vals is not None else where) if labelled else ""
        markers.append({"position": pos[j].astype(float), "text": text, "where": where})
    return markers, [note]


def _contour_markers(numbers: list, odb: Odb, surf, coords: np.ndarray) -> tuple[list, list]:
    """Numbers that carry a node/element location become 3D markers; the rest are listed as notes.

    Fields and Sets (e.g. from Filter by Value) put a marker on each of their nodes/elements.
    """
    markers, notes = [], []
    for num in numbers:
        if isinstance(num, (Field, EntitySet)):
            m, n = _member_markers(num, odb, surf, coords)
            markers += m
            notes += n
            continue
        if not isinstance(num, Number):
            raise ValueError(f"Results takes numbers, fields or sets, not a {type(num).__name__}")
        text = f"{num.label}: {_fmt(num.value)}"
        loc = num.extra.get("location")
        if loc and loc.get("odb") == odb.path:
            idx = surf.lookup(loc["entity"], np.array([loc["instance"]], dtype=object), np.array([loc["label"]]))[0]
            if idx >= 0:
                pos = coords[idx] if loc["entity"] == "node" else surf.element_center(int(idx), coords)
                markers.append({"position": pos.astype(float), "text": text,
                                "where": f"{loc['entity']} {loc['instance']}.{loc['label']}"})
                continue
        notes.append(text + (f" ({num.where})" if num.where else ""))
    return markers, notes


SPECTRA = ["Rainbow", "Reversed rainbow", "Blue to red", "Black to white", "White to black"]
EDGES = {"All edges": "ALL", "Feature edges": "FEATURE", "Outline": "EXTERIOR", "No edges": "NONE"}
VIEWS = ["Iso", "Front", "Back", "Top", "Bottom", "Left", "Right", "Keep"]


def _limit(text, fallback: float, name: str) -> tuple[float, bool]:
    """A colour-scale limit: (value, typed?) from 'auto' or a number."""
    t = str(text if text is not None else "auto").strip().lower()
    if t in ("", "auto"):
        return fallback, False
    try:
        return float(t), True
    except ValueError as exc:
        raise ValueError(f"{name} must be auto or a number, not '{text}'") from exc


def _contour_display(inputs, params):
    f: Field = inputs["field"]
    values = _require_scalar(f, "Contour Plot")
    surf = surface_for(f.odb)
    nodal = surf.nodal(f.position, f.instances, f.labels, values)
    coords = surf.coords.copy()
    scale = 0.0
    d: Field | None = inputs.get("deform")
    if d is not None:
        if d.position != "NODAL":
            raise ValueError("Deformation needs a nodal vector field such as U")
        disp = np.nan_to_num(surf.nodal("NODAL", d.instances, d.labels, d.values[:, :3]))
        if disp.shape[1] < 3:
            disp = np.hstack([disp, np.zeros((len(disp), 3 - disp.shape[1]))])
        scale = float(params["scale"])
        if scale <= 0:  # auto: largest displacement drawn at 10% of the model size, like Abaqus
            size = float(np.ptp(surf.coords, axis=0).max()) or 1.0
            peak = float(np.abs(disp).max())
            scale = 0.1 * size / peak if peak > 0 else 1.0
        coords = coords + scale * disp
    markers, notes = _contour_markers(_flat_items(inputs.get("markers") or []), f.odb, surf, coords)
    v = nodal[surf.vertices]
    finite = v[~np.isnan(v)]
    lo, lo_set = _limit(params.get("min"), float(finite.min()) if finite.size else 0.0, "Min")
    hi, hi_set = _limit(params.get("max"), float(finite.max()) if finite.size else 0.0, "Max")
    if hi < lo:
        raise ValueError(f"Max ({_fmt(hi)}) is below Min ({_fmt(lo)})")
    if params.get("minmax") == "On" and finite.size:  # where the extremes are, like CAE's min/max locations
        for label, pick in (("Min", np.nanargmin), ("Max", np.nanargmax)):
            i = int(pick(np.where(np.isnan(nodal), np.nan, nodal)))
            markers.append({"position": coords[i].astype(float), "text": f"{label} {_fmt(float(nodal[i]))}",
                            "where": f"node {entity_name(str(surf.node_inst[i]), surf.node_label[i])}",
                            "extreme": True})
    edges = params.get("edges", "All edges")
    return {"kind": "contour", "markers": markers, "notes": notes, "title": _field_title(f),
            "label": f.name, "positions": coords[surf.vertices].astype(np.float32).ravel(),
            "triangles": surf.triangles.astype(np.int32).ravel(),
            "lines": surf.lines.astype(np.int32).ravel() if edges != "No edges" else np.zeros(0, np.int32),
            "values": v.astype(np.float32), "min": lo, "max": hi, "limits": [lo_set, hi_set],
            "spectrum": params.get("spectrum", "Rainbow"), "view": params.get("view", "Iso"),
            "deformScale": scale, "bands": int(params["bands"])}


def _group_display(inputs, params):
    kind, sets = group_members(inputs["members"])
    odb = inputs.get("odb") or next((x.odb for x in _flat_items(inputs["members"]) if isinstance(x, Field)), None)
    total = len({(str(i), int(l)) for s in sets for i, l in zip(s.instances, s.labels)})
    if odb is None or not total:  # an empty group (e.g. nothing passed a Compare) has nothing to highlight
        return {"kind": "info", "title": params["name"], "items": [
            ["Members", f"{total} {kind}s"],
            ["Note", "Connect the ODB to see them on the model" if total else "No nodes or elements to group"]]}
    f = field_from_set({"odb": odb, "set": sets, "a": None},
                       {"expr": "1", "rest": "Empty elsewhere", "name": params["name"] or "Group"})["field"]
    d = _contour_display({"field": f}, {"scale": 0, "bands": 2})
    d["title"] = f"{params['name'] or 'Group'} · {total} {kind}s"
    return d


@register("view.group", "Display Group", "Output",
          "Make a group of nodes or elements in Abaqus/CAE's ODB display (View > ODB Display Groups): from sets, "
          "fields (e.g. Compare's Passing, the hot spots) or lists of them (Sort List + Pick from List: the 10 "
          "worst). Optionally show only that group. Here it highlights them on the model. Its Group output is "
          "the same nodes/elements as a set, to restrict a field to them (Field Output's Where), plot them "
          "over time, or combine them with other sets.",
          # Members at any depth make one group (one display group in CAE).
          inputs=[Port("members", "any", "Nodes/elements", multi=True), Port("odb", "odb", "ODB", optional=True, depth=0)],
          outputs=[Port("set", "set", "Group")],
          params=[Param("name", "text", "NoCode group", "Group name"),
                  Param("show", "select", "Create only", "In CAE",
                        options=["Create only", "Show only this group", "Add to what is shown"],
                        help="Create only: the group appears in CAE's display group list. Show only: the current "
                             "viewport shows just these nodes/elements. Add: they're shown too.")],
          display=lambda o, i, p: _group_display(i, p), lists=True)
def view_group(inputs, params):
    s = _merged_set(inputs["members"], "item")
    # The group goes on as a set: restrict a field to it (Field Output's Where), plot it over time, combine it…
    return {"set": EntitySet(params["name"] or s.name, s.kind, s.instances, s.labels)}


SET_OPS = ["Field on set", "Nodes of elements", "Elements touching any node", "Elements touching all nodes",
           "Elements inside (all nodes)", "Neighbour elements", "Union", "Intersection", "Difference"]


def _odb_for(inputs) -> Odb:
    odb = inputs.get("odb") or next((x.odb for x in _flat_items([inputs.get("a"), inputs.get("b")])
                                     if isinstance(x, Field)), None)
    if odb is None:
        raise ValueError("Connect the ODB (needed to know which nodes belong to which elements)")
    return odb


def _set_from_index(surf, kind: str, idx: np.ndarray, name: str) -> EntitySet:
    idx = np.unique(idx)
    if kind == "node":
        return EntitySet(name, "node", surf.node_inst[idx], surf.node_label[idx])
    return EntitySet(name, "element", surf.elem_inst[idx], surf.elem_label[idx])


def _region_boolean(op: str, a, b) -> Region:
    """Union / intersection / difference of model Regions (a list on one side: its union first), resolved in CAE
    by entity on their shared Part or Instance; a Region node can name the result as a Set or Surface."""
    sides = []
    for what, v in (("a", a), ("b", b)):
        items = _flat_items(v)
        if not items:
            raise ValueError(f"{op} needs a Region in {what}")
        if not all(isinstance(x, Region) for x in items):
            raise ValueError(f"{op} of model Regions needs Regions in both a and b (not ODB sets or fields)")
        sides.append(functools.reduce(lambda x, y: _region_boolean("Union", x, y), items))
    a, b = sides
    if "reference point" in (a.kind, b.kind):
        raise ValueError(f"{op} works on faces, edges, vertices or cells, not reference points")
    if a.source != b.source:
        raise ValueError(f"{op} combines Regions on the same Part or Instance: a is on {a.source.name}, "
                         f"b on {b.source.name}")
    if a.kind != b.kind:
        raise ValueError(f"{op} needs the same geometry in a and b: a selects {a.kind}s, b {b.kind}s")
    label = f"{op.lower()} of ({a.summary()}) and ({b.summary()})"
    return Region(a.source, a.kind, points=(("@bool", op, a.operand(), b.operand(), label),), side=a.side)


@register("set.op", "Set Operation", "Field",
          "Work with nodes and elements: a field on a set (Field on set), the nodes of elements, the elements "
          "touching any or all of some nodes, the elements lying inside a node set, the neighbours of elements "
          "(sharing a node), or union / intersection / difference (boolean: add, combine, subtract, common) of "
          "two sets. Sets and fields both work as a and b; so do model Regions (faces, edges, vertices or cells "
          "on one Part or Instance), giving a Region to load, mesh or name as a CAE Set/Surface.",
          inputs=[Port("a", "any", "a", depth=1), Port("b", "any", "b", optional=True, depth=1),
                  Port("odb", "odb", "ODB", optional=True, depth=0)],
          outputs=[Port("result", "any", "Result")],
          params=[Param("op", "select", "Field on set", "Operation", options=SET_OPS,
                        help="Field on set: a = a field, b = a set (node sets on element fields keep the elements "
                             "touching them, and vice versa).")],
          lists=True)
def set_op(inputs, params):
    op, a, b = params["op"], inputs["a"], inputs.get("b")
    if op == "Field on set":
        if b is None:
            raise ValueError("Connect the set to b")
        fields = [x for x in _flat_items(a) if isinstance(x, Field)]
        if not fields:
            raise ValueError("Field on set needs a field in a")
        sb = _merged_set(b, "set")
        out = [field_filter({"field": f, "set": sb}, {})["field"] for f in fields]
        return {"result": out[0] if not isinstance(a, list) else out}
    if any(isinstance(x, Region) for x in _flat_items(a) + _flat_items(b)):
        if op not in ("Union", "Intersection", "Difference"):
            raise ValueError(f"{op} works on ODB sets; model Regions take Union, Intersection or Difference")
        return {"result": _region_boolean(op, a, b)}
    sa = _merged_set(a, "set")
    if op in ("Union", "Intersection", "Difference"):
        if b is None:
            raise ValueError(f"{op} needs a second set in b")
        sb = _merged_set(b, "set")
        if sb.kind != sa.kind:
            sb = convert_set(sb, sa.kind, _odb_for(inputs))
        ka = list(zip(sa.instances.astype(str), sa.labels.astype(int)))
        kb = set(zip(sb.instances.astype(str), sb.labels.astype(int)))
        if op == "Union":
            keys = ka + [k for k in zip(sb.instances.astype(str), sb.labels.astype(int)) if k not in set(ka)]
        elif op == "Intersection":
            keys = [k for k in ka if k in kb]
        else:
            keys = [k for k in ka if k not in kb]
        name = f"{sa.name} {dict(Union='+', Intersection='&', Difference='-')[op]} {sb.name}"
        return {"result": EntitySet(name, sa.kind, np.array([k[0] for k in keys], dtype=object),
                                    np.array([k[1] for k in keys], dtype=np.int64))}
    surf = surface_for(_odb_for(inputs))
    ok = surf.pair_node >= 0
    pe, pn = surf.pair_elem[ok], surf.pair_node[ok]
    if op == "Nodes of elements":
        if sa.kind == "node":
            raise ValueError("a is already nodes")
        el = surf.lookup("element", sa.instances, sa.labels)
        return {"result": _set_from_index(surf, "node", pn[np.isin(pe, el[el >= 0])], f"nodes of {sa.name}")}
    if op == "Neighbour elements":
        if sa.kind != "element":
            raise ValueError("Neighbour elements needs elements in a")
        el = surf.lookup("element", sa.instances, sa.labels)
        el = el[el >= 0]
        nodes = np.unique(pn[np.isin(pe, el)])
        around = np.setdiff1d(np.unique(pe[np.isin(pn, nodes)]), el)
        return {"result": _set_from_index(surf, "element", around, f"neighbours of {sa.name}")}
    if sa.kind != "node":
        raise ValueError(f"{op} needs nodes in a (use Nodes of elements first)")
    nodes = surf.lookup("node", sa.instances, sa.labels)
    nodes = np.unique(nodes[nodes >= 0])
    hit = np.isin(pn, nodes)
    n_el = len(surf.elem_label)
    in_set = np.bincount(pe[hit], minlength=n_el)
    if op == "Elements touching any node":
        idx = np.nonzero(in_set > 0)[0]
    elif op == "Elements touching all nodes":
        idx = np.nonzero(in_set == len(nodes))[0] if len(nodes) else np.array([], dtype=int)
    else:  # inside: every node of the element is in the set
        total = np.bincount(pe, minlength=n_el)
        idx = np.nonzero((in_set == total) & (total > 0))[0]
    if not len(idx):
        raise ValueError(f"No element is {op[len('Elements '):]} of {sa.name}")
    return {"result": _set_from_index(surf, "element", idx, f"elements {op[len('Elements '):]} {sa.name}")}


@register("view.contour", "Contour Plot", "Output", "Colour the model by a scalar field, optionally deformed.",
          inputs=[Port("field", "field", "Scalar"), Port("deform", "field", "Deform (U)", optional=True),
                  # Results at any depth: all of them marked on the one plot.
                  Port("markers", "any", "Results", optional=True, multi=True)], outputs=[],
          params=[Param("scale", "number", 0, "Deform scale", help="0 = automatic"),
                  Param("bands", "int", 12, "Colour bands"),
                  Param("min", "text", "auto", "Min", help="Lowest value of the colour scale: auto, or a number "
                        "(lower values get the first colour)"),
                  Param("max", "text", "auto", "Max", help="Highest value of the colour scale: auto, or a number"),
                  Param("spectrum", "select", "Rainbow", "Colours", options=SPECTRA),
                  Param("minmax", "select", "Off", "Show min/max", options=["Off", "On"],
                        help="Mark where the smallest and largest values are"),
                  Param("edges", "select", "All edges", "Mesh edges", options=list(EDGES)),
                  Param("view", "select", "Iso", "View", options=VIEWS,
                        help="Orientation, here and in CAE. Keep: leave CAE's view as it is"),
                  Param("viewport", "select", "Current", "Viewport", options=["Current", "New"],
                        help="Where this shows up in Abaqus/CAE (live link and CAE script). Current: CAE's current "
                             "viewport, the safe choice. New: a viewport of its own, reused on every update. "
                             "Warning: on Abaqus/CAE 2026, showing an ODB in a new viewport can crash CAE.")],
          display=lambda o, i, p: _contour_display(i, p))
def view_contour(inputs, params):
    return {}
