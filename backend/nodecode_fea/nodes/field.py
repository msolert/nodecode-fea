"""Field nodes: quantities, sets from fields, sampling and fields over time."""

from __future__ import annotations

import re
import numpy as np

from .. import ops
from ..mesh import surface_for
from ..values import Bool, EntitySet, Field, Number, Odb, Series, Text
from .core import (_fmt, FRAME_RANGE, _frames_in, MAX_CURVES, opt, Param, Port, register, _require_scalar, STEP_HELP,
    _step_hints, _step_or_last)
from .common import _as_list, as_set, ASSEMBLY, entity_name, _is_matrix, _size
from .data import _field_from, _point_word


# ------------------------------------------------------------------ field
def _scalar_options(f: Field | None) -> list:
    return [] if f is None else ops.available_invariants(f.kind, f.invariants)


def _check_invariant(name: str, kind: str, invariants: list[str] | None, wanted: str):
    allowed = ops.available_invariants(kind, invariants)
    if wanted in allowed:
        return
    if not allowed:
        raise ValueError(f"{name} has no invariants in this ODB; choose a component instead.")
    raise ValueError(f"Abaqus doesn't define '{wanted}' for {name}; choose {', '.join(allowed)} "
                     f"(or a component).")


@register("field.invariant", "Invariant", "Field", "Mises, principal values, Tresca, pressure or magnitude.",
          inputs=[Port("field", "field", "Field")], outputs=[Port("field", "field", "Scalar")],
          params=[Param("invariant", "select", "Mises", "Invariant", options=ops.INVARIANTS)],
          hints=lambda i, p: {"invariant": [opt(x) for x in _scalar_options(i.get("field"))]} if i.get("field") else {},
          hidden=True)
def field_invariant(inputs, params):
    f: Field = inputs["field"]
    kind = params["invariant"]
    _check_invariant(f.name, f.kind, f.invariants, kind)
    v = ops.invariant(kind, f.components, f.values, f.name, f.kind)
    return {"field": f.with_values(v[:, None], f"{f.name} {kind}", [f"{f.name} {kind}"], "SCALAR",
                                   refinement=("INVARIANT", kind))}


@register("field.component", "Component", "Field", "Pick one component, e.g. S11 or U2.",
          inputs=[Port("field", "field", "Field")], outputs=[Port("field", "field", "Scalar")],
          params=[Param("component", "select", "", "Component")],
          hints=lambda i, p: {"component": [opt(c) for c in i["field"].components]} if i.get("field") else {},
          hidden=True)
def field_component(inputs, params):
    f: Field = inputs["field"]
    comp = params["component"] or f.components[0]
    if comp not in f.components:
        raise ValueError(f"'{f.name}' has no component '{comp}' (has {', '.join(f.components)})")
    col = f.components.index(comp)
    return {"field": f.with_values(f.values[:, col:col + 1], comp, [comp], "SCALAR", refinement=("COMPONENT", comp))}


def _quantity_hints(i, p):
    f: Field | None = i.get("field")
    if f is None:
        return {}
    invs = _scalar_options(f)
    return {"quantity": [opt("", f"Auto ({invs[0] if invs else f.components[0]})")]
            + [opt(x, f"{x} (invariant)") for x in invs] + [opt(c) for c in f.components]}


def _axes_matrix(value) -> np.ndarray:
    if not (isinstance(value, list) and len(value) == 3 and
            all(isinstance(row, list) and len(row) == 3 for row in value)):
        raise ValueError("Local axes needs a 3×3 matrix: one global x,y,z direction per local axis row")
    try:
        axes = np.array([[float(x.value if isinstance(x, Number) else x) for x in row] for row in value])
    except (TypeError, ValueError) as exc:
        raise ValueError("Local axes must contain nine numbers") from exc
    if not np.isfinite(axes).all() or not np.allclose(axes @ axes.T, np.eye(3), atol=1e-6):
        raise ValueError("Local axes rows must be finite, perpendicular unit vectors")
    if np.linalg.det(axes) < 0.999999:
        raise ValueError("Local axes must form a right-handed coordinate system")
    return axes


def _field_coordinates(f: Field) -> np.ndarray:
    surf = surface_for(f.odb)
    idx = surf.lookup(f.entity, f.instances, f.labels)
    if np.any(idx < 0):
        raise ValueError("Some field rows have no position in this ODB mesh")
    return surf.coords[idx] if f.entity == "node" else surf.element_centers(idx, surf.coords)


def _transform_field(f: Field, system: str, axes, origin: str, axis: str) -> Field:
    if f.kind not in ("VECTOR", "TENSOR"):
        raise ValueError("Coordinate transformation needs a vector or tensor field")
    if system == "Local axes":
        rotation = np.broadcast_to(_axes_matrix(axes), (len(f.labels), 3, 3))
    else:
        centre = parse_points(origin, 1)[0]
        axial = parse_points(axis, 1)[0]
        norm = np.linalg.norm(axial)
        if norm == 0:
            raise ValueError("Cylindrical axis direction cannot be zero")
        ez = axial / norm
        radial = _field_coordinates(f) - centre
        radial -= np.outer(radial @ ez, ez)
        lengths = np.linalg.norm(radial, axis=1)
        if np.any(lengths < 1e-12):
            raise ValueError("Cylindrical components are undefined for result locations on the axis")
        er = radial / lengths[:, None]
        et = np.cross(np.broadcast_to(ez, er.shape), er)
        rotation = np.stack([er, et, np.broadcast_to(ez, er.shape)], axis=1)
    if f.kind == "VECTOR":
        if f.values.shape[1] != 3:
            raise ValueError(f"'{f.name}' needs three components for a 3D coordinate transformation")
        values = np.einsum("nij,nj->ni", rotation, f.values)
    else:
        tensor = ops.tensor_matrix(f.components, f.values, f.name)
        local = np.einsum("nij,njk,nlk->nil", rotation, tensor, rotation)
        shear = 2.0 if ops._is_strain(f.origin.get("variable", f.name)) else 1.0
        values = np.stack([local[:, int(c[-2]) - 1, int(c[-1]) - 1] *
                           (shear if c[-2] != c[-1] else 1.0) for c in f.components], axis=1)
    label = "cylindrical" if system == "Cylindrical" else "local"
    return f.with_values(values, f"{f.name} ({label})", list(f.components), f.kind,
                         math=True, coordinate_system=system)


@register("field.quantity", "Quantity", "Field",
          "One value per node/element from a field: a component (S11, U2…) or an invariant (Mises, principal "
          "values, Tresca, pressure, magnitude). Components can be resolved in global, wired local, or cylindrical "
          "axes; component 1/2/3 then means local x/y/z or radial/tangential/axial.",
          inputs=[Port("field", "field", "Field"),
                  Port("axes", "any", "Local axes", optional=True, depth=2, table=True,
                       when={"coordinates": ["Local axes"]})], outputs=[Port("field", "field", "Scalar")],
          params=[Param("quantity", "select", "", "Quantity",
                        help="Auto = Mises for stresses/strains, Magnitude for vectors, the value for scalars"),
                  Param("coordinates", "select", "Global", "Coordinates",
                        options=["Global", "Local axes", "Cylindrical"]),
                  Param("origin", "text", "0, 0, 0", "Cylindrical origin",
                        when={"coordinates": ["Cylindrical"]}),
                  Param("axis", "text", "0, 0, 1", "Cylindrical axis",
                        help="Axis direction vector", when={"coordinates": ["Cylindrical"]})],
          hints=_quantity_hints)
def field_quantity(inputs, params):
    f: Field = inputs["field"]
    q = params["quantity"]
    if not q:
        invs = _scalar_options(f)
        q = invs[0] if invs and f.values.shape[1] > 1 else f.components[0]
    system = str(params.get("coordinates") or "Global")
    if system != "Global" and q in f.components:
        f = _transform_field(f, system, inputs.get("axes"), str(params.get("origin") or "0, 0, 0"),
                             str(params.get("axis") or "0, 0, 1"))
    if q in f.components:
        return field_component({"field": f}, {"component": q})
    if q in ops.INVARIANTS:
        return field_invariant({"field": f}, {"invariant": q})
    raise ValueError(f"'{q}' is not a component or invariant of {f.name} "
                     f"(components: {', '.join(f.components)}; invariants: {', '.join(_scalar_options(f)) or 'none'})")


@register("field.filter", "Filter by Set", "Field", "Keep only the values on a node or element set.",
          inputs=[Port("field", "field", "Field"), Port("set", "set", "Set")],
          outputs=[Port("field", "field", "Field")], hidden=True)
def field_filter(inputs, params):
    f: Field = inputs["field"]
    s: EntitySet = inputs["set"]
    mask = field_filter_mask(f, s)
    if not mask.any():
        raise ValueError(f"No values of '{f.name}' fall on set '{s.name}'")
    return {"field": f.subset(mask, f"on {s.name}")}


def convert_set(s: EntitySet, kind: str, odb: Odb) -> EntitySet:
    """A node set as the elements touching it, or an element set as its elements' nodes (like Abaqus)."""
    if s.kind == kind:
        return s
    surf = surface_for(odb)
    ok = surf.pair_node >= 0
    if kind == "node":
        elems = surf.lookup("element", s.instances, s.labels)
        idx = np.unique(surf.pair_node[ok & np.isin(surf.pair_elem, elems[elems >= 0])])
        return EntitySet(f"nodes of {s.name}", "node", surf.node_inst[idx], surf.node_label[idx])
    nodes = surf.lookup("node", s.instances, s.labels)
    idx = np.unique(surf.pair_elem[ok & np.isin(surf.pair_node, nodes[nodes >= 0])])
    return EntitySet(f"elements on {s.name}", "element", surf.elem_inst[idx], surf.elem_label[idx])


def field_filter_mask(f: Field, s: EntitySet) -> np.ndarray:
    s = convert_set(s, f.entity, f.odb)
    set_inst = s.instances.astype(str)
    field_inst = f.instances.astype(str)
    mask = np.zeros(len(f.labels), dtype=bool)
    for name in np.unique(set_inst):
        labels = s.labels[set_inst == name]
        # '' is the assembly level (e.g. reference points), matched exactly like any instance.
        mask |= (field_inst == name) & np.isin(f.labels, labels)
    return mask


@register("field.expression", "Field Math", "Field",
          "Apply a formula to every value, e.g. x / 1e6, abs(x), np.clip(x, 0, 355) or x - np.mean(x). "
          "Comparisons, where(), indexing and many NumPy functions (np.…) are allowed.",
          inputs=[Port("field", "field", "Scalar"), Port("a", "number", "a", optional=True)],
          outputs=[Port("field", "field", "Scalar")],
          params=[Param("expr", "text", "x", "f(x) =", help="x is the field value; a is the optional number input")],
          hidden=True)
def field_expression(inputs, params):
    f: Field = inputs["field"]
    x = _require_scalar(f, "Field Math")
    a = inputs.get("a")
    out = np.asarray(ops.evaluate(params["expr"], {"x": x, "a": a.value if a else None}), dtype=float)
    if out.ndim > 1 or (out.ndim == 1 and out.shape != x.shape):
        raise ValueError(f"Field Math must give one value per {f.entity} ({len(x)}), but the formula gives "
                         f"{out.size}. For a single number (max, percentile…) use Expression or Reduce.")
    v = np.broadcast_to(out, x.shape).astype(float)
    return {"field": f.with_values(v[:, None], f"f({f.name})", [params["expr"]], "SCALAR", math=True)}


CUSTOM_REST = ["Only the sets", "0 elsewhere", "Empty elsewhere"]


def _custom_operand(a, n_sets: int, set_index: np.ndarray):
    """The value input of Field from Sets: one number, one per set, or one per node/element."""
    if a is None:
        return None
    if isinstance(a, (Number, Bool)):
        return float(a.value)
    if isinstance(a, list) and all(isinstance(i, (Number, Bool)) for i in a):
        v = np.array([float(i.value) for i in a])
        if len(v) == n_sets:
            return v[set_index]
        if len(v) == len(set_index):
            return v
        raise ValueError(f"'a' has {len(v)} numbers: give one, one per set ({n_sets}) or one per member "
                         f"({len(set_index)})")
    raise ValueError("'a' must be a number or a list of numbers")


def _field_title(f: Field) -> str:
    if f.frame < 0:  # made by hand (Field from Sets), not read from a frame
        return f.name
    return f"{f.name} · {f.step} frame {f.frame} (t = {_fmt(f.frame_value)})"


@register("field.from_set", "Field from Sets", "Field",
          "Make your own field on node or element sets: a constant, one value per set, or a formula of the "
          "position (x, y, z), the label or the set number. Contour it, filter it, or combine it with results "
          "in a Formula.",
          inputs=[Port("odb", "odb", "ODB"), Port("set", "set", "Sets", multi=True, depth=1),
                  Port("a", "any", "a", optional=True, depth=1)],
          outputs=[Port("field", "field", "Field")],
          params=[Param("expr", "text", "1", "Value",
                        help="A number or formula: x, y, z (node position or element centre), label, "
                             "set (1 for the first set, 2 for the second…), a (the a input). E.g. set, a, "
                             "y / 10, sqrt(x**2 + z**2)"),
                  Param("rest", "select", "Only the sets", "Elsewhere", options=CUSTOM_REST,
                        help="Only the sets: the field has just their nodes/elements. 0 or Empty elsewhere: "
                             "every node/element of the model, so fields from different sets add up in a Formula"),
                  Param("name", "text", "Custom", "Name")])
def field_from_set(inputs, params):
    odb: Odb = inputs["odb"]
    sets: list[EntitySet] = _as_list(inputs.get("set"))
    if not sets:
        raise ValueError("Connect one or more node or element sets")
    kinds = {s.kind for s in sets}
    if len(kinds) > 1:
        raise ValueError("Mix of node and element sets: a field is either per node or per element")
    kind = kinds.pop()
    inst = np.concatenate([np.asarray(s.instances, dtype=object).astype(str) for s in sets])
    lab = np.concatenate([np.asarray(s.labels, dtype=np.int64) for s in sets])
    set_no = np.concatenate([np.full(len(s.labels), i + 1) for i, s in enumerate(sets)])
    # A member of several sets takes its value from the last one.
    _, last = np.unique(np.stack([inst, lab.astype(str)], axis=1)[::-1], axis=0, return_index=True)
    keep = np.sort(len(lab) - 1 - last)
    inst, lab, set_no = inst[keep], lab[keep], set_no[keep]
    surf = surface_for(odb)
    idx = surf.lookup(kind, inst, lab)
    found = idx >= 0
    if not found.any():
        raise ValueError(f"None of the set members are in this ODB's mesh, e.g. {entity_name(inst[0], lab[0])}")
    # Members outside the drawn mesh (e.g. reference points) are kept, with an unknown position (NaN).
    xyz = np.full((len(lab), 3), np.nan)
    xyz[found] = surf.coords[idx[found]] if kind == "node" else surf.element_centers(idx[found], surf.coords)
    names = {"x": xyz[:, 0], "y": xyz[:, 1], "z": xyz[:, 2], "label": lab.astype(float),
             "set": set_no.astype(float), "a": _custom_operand(inputs.get("a"), len(sets), set_no - 1)}
    v = np.asarray(ops.evaluate(str(params["expr"]), names), dtype=float)
    if v.ndim > 1 or (v.ndim == 1 and len(v) != len(lab)):
        raise ValueError(f"The value must be one number or one per {kind} ({len(lab)}), not {v.size}")
    v = np.broadcast_to(v, lab.shape).astype(float)
    if params["rest"] != CUSTOM_REST[0]:
        all_inst, all_lab = (surf.node_inst, surf.node_label) if kind == "node" else (surf.elem_inst, surf.elem_label)
        full = np.full(len(all_lab), 0.0 if params["rest"] == "0 elsewhere" else np.nan)
        full[idx[found]] = v[found]
        inst = np.concatenate([np.asarray(all_inst).astype(str), inst[~found]])
        lab = np.concatenate([np.asarray(all_lab), lab[~found]])
        v = np.concatenate([full, v[~found]])
    name = params["name"] or "Custom"
    return {"field": Field(name, odb, "NODAL" if kind == "node" else "ELEMENT", [name], inst.astype(object),
                           np.asarray(lab), v[:, None], kind="SCALAR",
                           origin={"custom": True, "set_names": [s.name for s in sets]})}


REDUCE_OPS = ops.REDUCERS + ["Product", "Count", "First", "Last"]
_RUNNING = {"Sum": np.cumsum, "Product": np.cumprod, "Max": np.maximum.accumulate, "Min": np.minimum.accumulate,
            "Abs Max": lambda a: np.maximum.accumulate(np.abs(a)),
            "Mean": lambda a: np.cumsum(a) / np.arange(1, len(a) + 1),
            "Count": lambda a: np.arange(1, len(a) + 1, dtype=float),
            "First": lambda a: np.full(len(a), a[0]), "Last": lambda a: a}


def _running(v, op: str):
    """The value after each item (lists) or point (curves), e.g. the running sum; nothing for fields."""
    op = _REDUCE_ALIASES.get(op, op)
    if isinstance(v, Series):
        return Series(f"running {op} {v.name}", v.x, _RUNNING[op](v.y.astype(float)), v.xlabel, v.ylabel,
                      dict(v.extra))
    items = _as_list(v)
    if items and all(isinstance(x, (Number, Bool)) for x in items):
        r = _RUNNING[op](np.array([float(x.value) for x in items]))
        return [Number(float(x), f"{op} after #{i}") for i, x in enumerate(r)]
    return []
_REDUCE_ALIASES = {"Final": "Last", "Initial": "First"}  # Series Value's names


def _reduce_any(v, op: str) -> Number:
    """One number from a field (its rows), a curve (its points), a list of numbers, or a set/list (Count)."""
    op = _REDUCE_ALIASES.get(op, op)
    if op == "Count":
        return Number(float(_size(v)), "Count")
    if isinstance(v, Field):
        if op == "Product":
            return Number(float(np.nanprod(_require_scalar(v, "Reduce"))), f"Product {v.name}")
        return field_reduce({"field": v}, {"op": op})["value"]
    if isinstance(v, Series):
        if len(v.y) == 0:
            raise ValueError("The curve is empty")
        if op == "Product":
            return Number(float(np.prod(v.y)), f"Product {v.name}")
        if op in ("First", "Last"):
            i = -1 if op == "Last" else 0
            return Number(float(v.y[i]), f"{op} {v.name}", f"{v.xlabel} = {_fmt(float(v.x[i]))}")
        val, i = ops.reduce(op, v.y)
        return Number(val, f"{op} {v.name}", f"{v.xlabel} = {_fmt(float(v.x[i]))}" if i is not None else "")
    items = _as_list(v)
    if not items or not all(isinstance(x, (Number, Bool)) for x in items):
        raise ValueError("Reduce takes a field, a curve or a list of numbers (Count works on anything)")
    if op == "Product":
        return Number(float(np.prod([float(n.value) for n in items])), f"Product of {len(items)}")
    if op in ("First", "Last"):
        src = items[-1 if op == "Last" else 0]
        return Number(float(src.value), f"{op}: {src.label}", getattr(src, "where", ""), dict(getattr(src, "extra", {})))
    val, i = ops.reduce(op, np.array([float(n.value) for n in items]))
    if i is None:
        return Number(val, f"{op} of {len(items)}")
    src = items[i]
    return Number(val, f"{op}: {src.label}", getattr(src, "where", ""), dict(getattr(src, "extra", {})))


@register("math.reduce", "Reduce", "Math",
          "Collapse many values into one number: max, min, abs max, mean, sum, count, first or last. Takes a field "
          "(its nodes/elements; Max/Min remember where, for Contour Plot), a curve (its points), a list of numbers, "
          "or anything for Count. Several fields or curves give one number each. Running gives the value after "
          "each item of a list (or point of a curve): the running sum, max… of a loop.",
          inputs=[Port("field", "any", "Values", depth=1)],
          outputs=[Port("value", "number", "Value"), Port("running", "any", "Running")],
          params=[Param("op", "select", "Max", "Operation", options=REDUCE_OPS)], lists=True)
def math_reduce(inputs, params):
    v = inputs["field"]
    if params["op"] != "Count" and isinstance(v, list) and v and all(isinstance(x, (Field, Series)) for x in v):
        return {"value": [_reduce_any(x, params["op"]) for x in v], "running": [_running(x, params["op"]) for x in v]}
    return {"value": _reduce_any(v, params["op"]), "running": _running(v, params["op"])}


@register("field.reduce", "Reduce (field)", "Field", "Collapse a scalar field to one number: max, min, mean...",
          inputs=[Port("field", "field", "Scalar")], outputs=[Port("value", "number", "Value")],
          params=[Param("op", "select", "Max", "Operation", options=ops.REDUCERS)], hidden=True)
def field_reduce(inputs, params):
    f: Field = inputs["field"]
    def where(row):
        detail = ""
        if f.integration_points is not None and int(f.integration_points[row]):
            detail += f", {_point_word(f)} {int(f.integration_points[row])}"
        if f.section_points is not None and int(f.section_points[row]):
            detail += f", section point {int(f.section_points[row])}"
        return f"{f.entity} {entity_name(str(f.instances[row]), f.labels[row])}{detail}"
    if params["op"] in ("First", "Last"):
        i = -1 if params["op"] == "Last" else 0
        v = float(_require_scalar(f, "Reduce")[i])
        return {"value": Number(v, f"{params['op']} {f.name}", where(i))}
    v, i = ops.reduce(params["op"], _require_scalar(f, "Reduce"))
    if i is None:
        return {"value": Number(v, f"{params['op']} {f.name}", f"{len(f.labels)} {f.entity}s")}
    loc = {"odb": f.odb.path, "entity": f.entity, "instance": str(f.instances[i]), "label": int(f.labels[i]),
           "integrationPoint": int(f.integration_points[i]) if f.integration_points is not None else 0,
           "sectionPoint": int(f.section_points[i]) if f.section_points is not None else 0}
    return {"value": Number(v, f"{params['op']} {f.name}", where(i),
                            {"location": loc})}


def _quantity(f: Field, q: str) -> np.ndarray:
    """One scalar per row: a named component or an invariant."""
    if q in f.components:
        return f.values[:, f.components.index(q)]
    if q in ops.INVARIANTS:
        _check_invariant(f.name, f.kind, f.invariants, q)
        return ops.invariant(q, f.components, f.values, f.name, f.kind)
    raise ValueError(f"'{q}' is not an invariant or a component of {f.name} ({', '.join(f.components)})")


def _quantity_label(variable: str, q: str) -> str:
    return q if q.startswith(variable) else f"{variable} {q}"


def _over_frames_hints(i, p):
    h = _step_hints(i.get("odb"), p)
    h.pop("frame", None)
    if i.get("odb") is None:
        return h
    try:
        step = _step_or_last(i["odb"], p.get("step", ""))
    except ValueError:
        return h
    for fdesc in step["fields"]:
        if fdesc["name"] == p.get("variable"):
            kind = ops.classify_type(fdesc["type"], len(fdesc["components"]))
            h["quantity"] = [opt(x) for x in ops.available_invariants(kind, fdesc.get("invariants"))] + [
                opt(c) for c in fdesc["components"]]
    return h


@register("field.over_frames", "Field vs Time", "Field",
          "Reduce a field at every frame of a step (or From–To), e.g. max Mises over time. Optionally restricted to a set.",
          inputs=[Port("odb", "odb", "ODB"), Port("set", "set", "Set", optional=True)],
          outputs=[Port("series", "series", "Series")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP), Param("variable", "select", "S", "Variable"),
                  Param("quantity", "select", "Mises", "Quantity"),
                  Param("op", "select", "Max", "Operation", options=ops.REDUCERS + ["Each (one curve per item)"],
                        help="Max/Min/… give one curve. Each gives one curve per node or element "
                             f"(up to {MAX_CURVES}); connect a Set to choose which."), *FRAME_RANGE],
          hints=_over_frames_hints, hidden=True)
def field_over_frames(inputs, params):
    odb: Odb = inputs["odb"]
    if params["op"].startswith("Each"):
        the_set = inputs.get("set")
        pick = (lambda f: _set_rows(f, the_set)) if the_set is not None else (lambda f: np.arange(len(f.labels)))
        return {"series": _curves(odb, params["step"], params["variable"], params["quantity"], pick, params)}
    s = _step_or_last(odb, params["step"])
    xs, ys = [], []
    for fr in _frames_in(s, params):
        f = _field_from(odb, s["name"], fr["index"], params["variable"])
        if inputs.get("set") is not None:
            f = field_filter({"field": f, "set": inputs["set"]}, {})["field"]
        v = _quantity(f, params["quantity"])
        xs.append(fr["value"])
        ys.append(ops.reduce(params["op"], v)[0])
    q = params["quantity"]
    label = f"{params['op']} {q if q.startswith(params['variable']) else params['variable'] + ' ' + q}"
    return {"series": Series(label, np.array(xs), np.array(ys), "Step time", label)}


_LABEL_RE = re.compile(r"^(?:(?P<inst>[^\s,]+)\.)?(?P<a>\d+)(?::(?P<b>\d+))?$")


def parse_labels(text: str) -> list[tuple[str, int]]:
    """'BEAM-1.533, 540, 10:12' -> [('BEAM-1', 533), ('', 540), ('', 10), ('', 11), ('', 12)]."""
    out = []
    for token in re.split(r"[,;\s]+", text.strip()):
        if not token:
            continue
        m = _LABEL_RE.match(token)
        if not m:
            raise ValueError(f"Can't read '{token}'. Use labels like 533, BEAM-1.533 or 10:20")
        a, b = int(m["a"]), int(m["b"] or m["a"])
        if b < a or b - a > 500:
            raise ValueError(f"Bad range '{token}'")
        out += [(m["inst"] or "", n) for n in range(a, b + 1)]
    return out


def _set_rows(f: Field, s: EntitySet) -> np.ndarray:
    return np.nonzero(field_filter_mask(f, s))[0]


def _label_rows(f: Field, wanted: list[tuple[str, int]], exact: bool = False) -> list[int]:
    """Field rows for (instance, label) pairs. Without ``exact``, a blank instance matches any instance."""
    rows = []
    insts = f.instances.astype(str)
    for inst, label in wanted:
        if inst.upper() in (ASSEMBLY, "ROOTASSEMBLY"):
            inst, exact_one = "", True
        else:
            exact_one = exact or bool(inst)
        hit = np.nonzero((f.labels == label) & ((insts == inst) if exact_one else True))[0]
        if hit.size == 0:
            where = entity_name(inst, label) if exact_one else str(label)
            raise ValueError(f"{f.entity.capitalize()} {where} has no {f.name} values "
                             f"({f.name} is stored at {f.entity}s)")
        if hit.size > 1:
            names = ", ".join(sorted({n or ASSEMBLY for n in insts[hit]}))
            raise ValueError(f"Label {label} exists in several instances ({names}); write INSTANCE.{label}")
        rows.append(int(hit[0]))
    return rows


def _curves(odb: Odb, step: str, variable: str, q: str, pick_rows, params: dict | None = None) -> list[Series]:
    """One curve per picked row of a field, over every frame of a step.

    ``pick_rows(field)`` is called on the first frame and returns the row indices to follow.
    Rows are looked up by label on later frames, in case their order changes.
    """
    s = _step_or_last(odb, step)
    keys, xs, ys = None, [], []
    for fr in _frames_in(s, params or {}):
        f = _field_from(odb, s["name"], fr["index"], variable)
        if keys is None:
            rows = list(dict.fromkeys(int(r) for r in pick_rows(f)))
            if not rows:
                raise ValueError("Nothing to probe")
            if len(rows) > MAX_CURVES:
                raise ValueError(f"That's {len(rows)} curves; the limit is {MAX_CURVES}. "
                                 "Use a smaller set, or Field vs Time with Max/Mean to get one curve.")
            keys = [(str(f.instances[r]), int(f.labels[r])) for r in rows]
        else:
            rows = _label_rows(f, keys, exact=True)
        xs.append(fr["value"])
        ys.append(_quantity(f, q)[rows])
    ys = np.array(ys).reshape(len(xs), len(keys or []))
    label = _quantity_label(variable, q)
    return [Series(f"{label} @ {entity_name(inst, lab)}", np.array(xs), ys[:, k], "Step time", label,
                   {"location": {"odb": odb.path, "entity": f.entity, "instance": inst, "label": lab}})
            for k, (inst, lab) in enumerate(keys or [])]


def _probe_hints(i, p):
    h = _over_frames_hints(i, p)
    the_set: EntitySet | None = i.get("set")
    if the_set is not None and i.get("odb") is not None:
        try:  # list the set's members, as nodes or elements to match the chosen variable
            step = _step_or_last(i["odb"], p.get("step", ""))
            fdesc = next((f for f in step["fields"] if f["name"] == p.get("variable")), None)
            kind = "node" if fdesc is None or "NODAL" in fdesc["positions"] else "element"
            members = convert_set(the_set, kind, i["odb"])
            h["labels"] = [opt(entity_name(inst, lab))
                           for inst, lab in zip(members.instances[:1000], members.labels[:1000])]
        except Exception:
            pass
    return h


@register("field.probe", "Probe vs Time", "Field",
          "Values at chosen nodes or elements over the frames of a step (all, or From–To): one curve per node/element. "
          "Pick them by label, by connecting a Set, or both.",
          inputs=[Port("odb", "odb", "ODB"), Port("set", "set", "Set", optional=True)],
          outputs=[Port("series", "series", "Series")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP), Param("variable", "select", "U", "Variable"),
                  Param("quantity", "select", "U2", "Quantity"),
                  Param("labels", "text", "", "Labels",
                        help="Node labels for nodal fields (U, RF), element labels for element fields (S, E). "
                             "Comma separated; use INSTANCE.label when there are several instances, "
                             "and a:b for ranges, e.g. BEAM-1.533, 10:12. With a Set connected, leave it empty "
                             "for every member, or pick members from the list."), *FRAME_RANGE],
          hints=lambda i, p: _probe_hints(i, p), hidden=True)
def field_probe(inputs, params):
    wanted = parse_labels(params["labels"])
    the_set: EntitySet | None = inputs.get("set")
    if not wanted and the_set is None:
        raise ValueError("Enter labels or connect a Set")

    def pick(f: Field):
        # Typed labels win; with none, follow every member of the set.
        return _label_rows(f, wanted) if wanted else _set_rows(f, the_set).tolist()

    curves = _curves(inputs["odb"], params["step"], params["variable"], params["quantity"], pick, params)
    return {"series": curves[0] if len(curves) == 1 else curves}


EACH = "Each (one curve per node/element)"


def _safe_set(v):
    try:
        return as_set(v)
    except ValueError:
        return None


@register("field.time", "Over Time", "Field",
          "A quantity over the frames of a step (all, or From–To). Each gives one curve per node/element: the "
          "typed Labels, or the nodes/elements connected to Where: a Set, a single node (Pick from List, For "
          "Each), Compare's Passing, a list of them, or a Reduce result (the node/element where the max is). "
          "Max/Min/Mean/… give one curve: that value over the whole model, or over Where, at every frame. "
          "Nodes/elements gives the labels behind the curves, in curve order, as a Set (for Set Operation, "
          "Display Group, Format Text, Count…). Make Curve's X/Y values give the times and values as numbers.",
          inputs=[Port("odb", "odb", "ODB", depth=0), Port("set", "any", "Where", optional=True, depth=1)],
          outputs=[Port("series", "series", "Series"), Port("set", "set", "Nodes/elements")],
          params=[Param("step", "select", "", "Step", help=STEP_HELP), Param("variable", "select", "U", "Variable"),
                  Param("quantity", "select", "U2", "Quantity"),
                  Param("op", "select", EACH, "Curves", options=[EACH] + ops.REDUCERS,
                        help=f"Each: one curve per node/element (up to {MAX_CURVES}). Max/Min/…: one curve, "
                             "reducing all nodes/elements (or the Set's) at each frame."),
                  Param("labels", "text", "", "Labels",
                        help="For Each: node labels for nodal fields (U, RF), element labels for element fields "
                             "(S, E), e.g. BEAM-1.533, 10:12. Empty: every member of the Set."),
                  *FRAME_RANGE],
          hints=lambda i, p: _probe_hints({**i, "set": _safe_set(i.get("set"))}, p), lists=True)
def field_time(inputs, params):
    inputs = {**inputs, "set": as_set(inputs.get("set"))}
    if params["op"] == EACH:
        if not params["labels"].strip() and inputs.get("set") is None:
            raise ValueError("Each needs nodes/elements: type Labels or connect a Set (or choose Max, Mean… "
                             "for one curve over the whole model)")
        out = field_probe(inputs, params)
        locs = [c.extra["location"] for c in _as_list(out["series"])]
        kind = locs[0]["entity"] if locs else "node"
        name = f"{_quantity_label(params['variable'], params['quantity'])} curves"
        return {**out, "set": EntitySet(name, kind, np.array([l["instance"] for l in locs], dtype=object),
                                        np.array([l["label"] for l in locs]))}
    if params["labels"].strip():
        raise ValueError("Labels are for Each; to reduce over chosen nodes/elements, connect a Set")
    out = field_over_frames(inputs, params)
    # The nodes/elements reduced over (at the first frame): the whole model, or Where's members.
    s = _step_or_last(inputs["odb"], params["step"])
    f = _field_from(inputs["odb"], s["name"], _frames_in(s, params)[0]["index"], params["variable"])
    if inputs.get("set") is not None:
        f = field_filter({"field": f, "set": inputs["set"]}, {})["field"]
    return {**out, "set": EntitySet(f"{out['series'].name} over", f.entity, f.instances, f.labels)}


def parse_points(text: str, minimum: int = 2) -> np.ndarray:
    pts = []
    for chunk in re.split(r"[;\n]+", text.strip()):
        if not chunk.strip():
            continue
        try:
            xyz = [float(v) for v in re.split(r"[,\s]+", chunk.strip()) if v]
        except ValueError as exc:
            raise ValueError(f"Can't read point '{chunk.strip()}'. Use x, y, z") from exc
        if len(xyz) not in (2, 3):
            raise ValueError(f"Point '{chunk.strip()}' needs 2 or 3 coordinates")
        pts.append(xyz + [0.0] * (3 - len(xyz)))
    if len(pts) < minimum:
        raise ValueError("A path needs at least two points, e.g. 0, 5, 0; 100, 5, 0" if minimum > 1
                         else "Give at least one point, e.g. 0, 5, 0")
    return np.array(pts)


def sample_path(points: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    seg = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)])
    if cum[-1] == 0:
        raise ValueError("Path points are all the same")
    dist = np.linspace(0, cum[-1], n)
    xyz = np.stack([np.interp(dist, cum, points[:, k]) for k in range(3)], axis=1)
    return dist, xyz


SAMPLE_MODES = ["Along a path through the points", "At the points"]


def _points_from(v, odb: Odb, minimum: int) -> tuple[np.ndarray, list[str]]:
    """Points as (n, 3) coordinates and a name for each: typed text, a matrix (Input / Formula), or nodes and
    elements (sets, fields, Reduce results: their positions, element centres for elements)."""
    if isinstance(v, Text):
        pts = parse_points(v.value, minimum)
        return pts, [f"({', '.join(_fmt(c) for c in p)})" for p in pts]
    if _is_matrix(v) or (isinstance(v, list) and v and all(isinstance(x, Number) for x in v)):
        rows = v if _is_matrix(v) else [v]
        pts = np.array([[float(x.value) for x in r] for r in rows])
        if pts.shape[1] not in (2, 3):
            raise ValueError("Points need 2 or 3 coordinates each (one row per point)")
        pts = np.hstack([pts, np.zeros((len(pts), 3 - pts.shape[1]))])
        return pts, [f"({', '.join(_fmt(c) for c in p)})" for p in pts]
    members = as_set(v)
    surf = surface_for(odb)
    idx = surf.lookup(members.kind, members.instances, members.labels)
    ok = idx >= 0
    if not ok.any():
        raise ValueError("None of those nodes/elements are in the mesh")
    pts = surf.coords[idx[ok]] if members.kind == "node" else surf.element_centers(idx[ok], surf.coords)
    names = [f"{members.kind} {entity_name(str(i), l)}" for i, l in zip(members.instances[ok], members.labels[ok])]
    return pts, names


def _point_lists(v) -> bool:
    """A list of typed points or point tables (a list of nodes/elements is one group of points instead)."""
    return isinstance(v, list) and bool(v) and all(isinstance(x, Text) or _is_matrix(x) for x in v)


@register("field.path", "Sample Field", "Field",
          "Read a field anywhere in the model (undeformed positions): along a path through points (a curve "
          "against the distance along it), or at the points themselves (one value each). Points can be typed, "
          "a matrix (Input, Formula), or nodes/elements (a Set, Compare's Passing, a Reduce result…).",
          inputs=[Port("field", "field", "Scalar", depth=0), Port("points", "any", "Points", optional=True, depth=1, table=True)],
          outputs=[Port("series", "any", "Result")],
          params=[Param("points", "text", "0, 5, 0; 100, 5, 0", "Points",
                        help="x, y, z of each point, separated by semicolons (used when Points isn't wired)"),
                  Param("mode", "select", SAMPLE_MODES[0], "Sample", options=SAMPLE_MODES),
                  Param("samples", "int", 50, "Samples", help="Along a path: how many points along it"),
                  Param("method", "select", "Interpolate", "Method", options=["Interpolate", "Nearest node"],
                        help="Interpolate uses hex/tet shape functions; Nearest node works for any element type")],
          lists=True)
def field_path(inputs, params):
    f: Field = inputs["field"]
    given = inputs.get("points")
    if _point_lists(given):  # several paths / groups of points: one result each
        return {"series": [field_path({**inputs, "points": p}, params)["series"] for p in given]}
    values = _require_scalar(f, "Sample Field")
    along = params["mode"] == SAMPLE_MODES[0]
    pts, names = _points_from(given if given is not None else Text(str(params["points"])), f.odb, 2 if along else 1)
    surf = surface_for(f.odb)
    nodal = surf.nodal(f.position, f.instances, f.labels, values)
    if along:
        n = int(params["samples"])
        if not 2 <= n <= 5000:
            raise ValueError("Samples must be between 2 and 5000")
        if len(pts) < 2:
            raise ValueError("A path needs at least two points")
        dist, xyz = sample_path(pts, n)
    else:
        xyz = pts
    if params["method"] == "Nearest node":
        y = surf.nearest(xyz, nodal)
    else:
        y = surf.interpolate(xyz, nodal)
        if np.isnan(y).all():
            raise ValueError("The points aren't inside any hex or tet element. Check the coordinates, "
                             "or use Method = Nearest node for shells and other element types.")
    if along:
        return {"series": Series(f"{f.name} along path", dist, y, "Distance along path", f.name)}
    return {"series": [Number(float(v), f"{f.name} at {nm}", nm) for v, nm in zip(y, names)]}


@register("cae.pick", "Picked in CAE", "Data",
          "Nodes or elements picked in the Abaqus/CAE viewport (needs CAE started with Launch CAE). "
          "Press Pick in CAE, pick in the viewport, then click Done there. You can also type labels.",
          inputs=[Port("odb", "odb", "ODB")], outputs=[Port("set", "set", "Set")],
          params=[Param("kind", "select", "node", "Pick", options=["node", "element"]),
                  Param("labels", "text", "", "Labels", help="Filled in by Pick in CAE, or typed: BEAM-1.12, 13, …"),
                  Param("pick", "pick", "", "", help="Pick in the CAE viewport")])
def cae_pick(inputs, params):
    wanted = parse_labels(params["labels"])
    if not wanted:
        raise ValueError("Nothing picked yet: press Pick in CAE")
    odb: Odb = inputs["odb"]
    kind = params["kind"]
    surf = surface_for(odb)
    all_inst, all_lab = (surf.node_inst, surf.node_label) if kind == "node" else (surf.elem_inst, surf.elem_label)
    instances = []
    for inst, label in wanted:
        if inst.upper() in (ASSEMBLY, "ROOTASSEMBLY"):
            inst = ""
        elif not inst:  # CAE didn't say which instance: find it from the label
            owners = sorted({str(i) for i in all_inst[all_lab == label]})
            if len(owners) > 1:
                raise ValueError(f"{kind.capitalize()} {label} exists in {', '.join(owners)}; write INSTANCE.{label}")
            inst = owners[0] if owners else ""
        instances.append(inst)
    return {"set": EntitySet(f"picked {kind}s", kind, np.array(instances, dtype=object),
                             np.array([lab for _, lab in wanted]))}
