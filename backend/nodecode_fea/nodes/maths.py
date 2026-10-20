"""Series and Math nodes: curves, Formula, Reduce."""

from __future__ import annotations

import ast
import re
import numpy as np
from dataclasses import dataclass

from .. import ops
from ..values import Bool, EntitySet, Field, Instance, Material, Model, Number, Part, Section, Series, Sketch, Text
from .core import _fmt, Param, Port, register
from .common import ASSEMBLY, _is_matrix
from .data import _field_row_keys


# ----------------------------------------------------------------- series
def _one(value, what: str) -> Series:
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError(f"{what} needs a single curve but got {len(value)}. Probe one label at a time.")
        return value[0]
    return value


def _flatten(values) -> list[Series]:
    out = []
    for v in values:
        out += v if isinstance(v, list) else [v]
    return out


@register("series.reduce", "Series Value", "Series", "Peak, minimum, first or last value of a curve.",
          inputs=[Port("series", "series", "Series")], outputs=[Port("value", "number", "Value")],
          params=[Param("op", "select", "Max", "Operation", options=["Max", "Min", "Abs Max", "Final", "Initial", "Mean"])],
          hidden=True)
def series_reduce(inputs, params):
    s: Series = _one(inputs["series"], "Series Value")
    op = params["op"]
    if len(s.y) == 0:
        raise ValueError("Series is empty")
    if op in ("Final", "Initial"):
        i = -1 if op == "Final" else 0
        return {"value": Number(float(s.y[i]), f"{op} {s.name}", f"{s.xlabel} = {_fmt(float(s.x[i]))}")}
    v, i = ops.reduce(op, s.y)
    where = f"{s.xlabel} = {_fmt(float(s.x[i]))}" if i is not None else ""
    return {"value": Number(v, f"{op} {s.name}", where)}


@register("series.xy", "Combine X–Y", "Series",
          "Plot one curve against another over time, e.g. force vs. displacement.",
          inputs=[Port("x", "series", "X"), Port("y", "series", "Y")], outputs=[Port("series", "series", "Series")],
          params=[Param("xscale", "number", 1.0, "X factor"), Param("yscale", "number", 1.0, "Y factor")], hidden=True)
def series_xy(inputs, params):
    sx: Series = _one(inputs["x"], "Combine X–Y")
    sy: Series = _one(inputs["y"], "Combine X–Y")
    y_on_x_time = np.interp(sx.x, sy.x, sy.y)
    return {"series": Series(f"{sy.name} vs {sx.name}", sx.y * float(params["xscale"]),
                             y_on_x_time * float(params["yscale"]), sx.ylabel or sx.name, sy.ylabel or sy.name,
                             dict(sy.extra))}


@register("series.expression", "Series Math", "Series", "Apply a formula to a curve's y values, e.g. -y or y / a.",
          inputs=[Port("series", "series", "Series"), Port("a", "number", "a", optional=True)],
          outputs=[Port("series", "series", "Series")],
          params=[Param("expr", "text", "y", "f(y) =", help="y is the curve value, t the x value")], hidden=True)
def series_expression(inputs, params):
    a = inputs.get("a")

    def apply(s: Series) -> Series:
        y = np.broadcast_to(ops.evaluate(params["expr"], {"y": s.y, "t": s.x, "a": a.value if a else None}),
                            s.y.shape).astype(float)
        return Series(f"f({s.name})", s.x, y, s.xlabel, params["expr"])

    v = inputs["series"]
    return {"series": [apply(s) for s in v] if isinstance(v, list) else apply(v)}


# ------------------------------------------------------------------- math
@register("math.expression", "Expression", "Math", "Combine numbers, e.g. a safety factor 355 / a.",
          inputs=[Port("a", "number", "a", optional=True), Port("b", "number", "b", optional=True),
                  Port("c", "number", "c", optional=True)],
          outputs=[Port("value", "number", "Value")],
          params=[Param("expr", "text", "a", "Formula"), Param("label", "text", "", "Name")], hidden=True)
def math_expression(inputs, params):
    names = {k: (inputs[k].value if inputs.get(k) else None) for k in ("a", "b", "c")}
    v = float(ops.evaluate(params["expr"], names))
    return {"value": Number(v, params["label"] or params["expr"])}


FORMULA_NAMES = ("a", "b", "c", "d")


def _field_operand(f: Field) -> np.ndarray:
    """A field in a formula: one value per row (scalar), a row vector (vector), or a 3x3 matrix per row (tensor)."""
    if f.values.shape[1] == 1:
        return f.values[:, 0]
    if f.kind == "TENSOR":
        return ops.tensor_matrix(f.components, f.values, f.name)
    return f.values


_TENSOR_ORDER = [(0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2)]


def _field_result(f0: Field, out: np.ndarray, name: str, expr: str) -> Field:
    """A formula result with one entry per row of the first field, as a field."""
    out = np.asarray(out, dtype=float)
    if out.ndim == 1:
        return f0.with_values(out[:, None], name, [expr], "SCALAR", math=True)
    if out.ndim == 2:
        comps = f0.components if out.shape[1] == len(f0.components) and f0.kind != "TENSOR" else \
            [f"{name}{k + 1}" for k in range(out.shape[1])]
        return f0.with_values(out, name, comps, "VECTOR", math=True)
    if out.shape[1:] == (3, 3):  # one matrix per row: back to tensor components
        prefix = f0.origin.get("variable") or f0.name
        if np.allclose(out, np.swapaxes(out, 1, 2), equal_nan=True):
            shear = 2.0 if ops._is_strain(prefix) else 1.0  # Abaqus keeps engineering shear strains
            cols = [out[:, i, j] * (shear if i != j else 1.0) for i, j in _TENSOR_ORDER]
            comps = [f"{prefix}{i + 1}{j + 1}" for i, j in _TENSOR_ORDER]
            f = f0.with_values(np.stack(cols, axis=1), name, comps, "TENSOR", math=True)
            f.invariants = None
            return f
        comps = [f"{prefix}{i + 1}{j + 1}" for i in range(3) for j in range(3)]
        return f0.with_values(out.reshape(len(out), 9), name, comps, "MATRIX", math=True)
    raise ValueError(f"The formula gives {'×'.join(map(str, out.shape))} values per {f0.entity}; use one number, a "
                     "vector or a 3×3 matrix per row")


def _formula_operand(v, name: str, base: dict):
    """An input as something the formula can compute with; fields and curves are lined up with the first one."""
    if isinstance(v, Number):
        return v.value
    if isinstance(v, Bool):
        return v.value
    if isinstance(v, Text):
        try:
            return float(v.value.strip())
        except ValueError as exc:
            raise ValueError(f"'{name}' is the text '{v.value}', not a number") from exc
    if isinstance(v, Field):
        x = _field_operand(v)
        f0: Field | None = base.get("field")
        if f0 is None:
            base["field"] = v
            return x
        if v.position != f0.position:
            raise ValueError(f"'{name}' is a {v.entity} field but the first field is per {f0.entity}: "
                             "they can't be lined up")
        if _field_row_keys(v) == _field_row_keys(f0):
            return x
        # Line rows up by full result location; rows the other field doesn't have become NaN.
        where = {key: k for k, key in enumerate(_field_row_keys(v))}
        idx = np.array([where.get(key, -1) for key in _field_row_keys(f0)])
        out = np.full((len(idx),) + x.shape[1:], np.nan)
        out[idx >= 0] = x[idx[idx >= 0]]
        return out
    if isinstance(v, EntitySet):  # a set: its labels, as numbers
        return v.labels.astype(float)
    if isinstance(v, Series):
        s0: Series | None = base.get("series")
        if s0 is None:
            base["series"] = v
            return v.y
        return v.y if len(v.x) == len(s0.x) and np.allclose(v.x, s0.x) else np.interp(s0.x, v.x, v.y)
    if isinstance(v, list) and v and all(isinstance(r, list) for r in v):  # a matrix: a list of rows
        try:
            return np.array([[float(x.value) for x in row] for row in v])
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"'{name}' is a matrix whose rows aren't all numbers of the same length") from exc
    if isinstance(v, list):  # a list of numbers/yes-no values is a vector
        if not all(isinstance(i, (Number, Bool)) for i in v):
            raise ValueError(f"'{name}' is a list of {type(v[0]).__name__ if v else 'nothing'}s; only lists of "
                             "numbers work as vectors (lists of fields or curves run the formula once per item)")
        return np.array([i.value for i in v], dtype=float)
    raise ValueError(f"'{name}' is a {type(v).__name__}, which a formula can't use")


FORMULA_OUTPUTS = ("out1", "out2", "out3", "out4")  # the script's named outputs, after Result


def _script_outputs(params: dict) -> list[str]:
    """The variables a script gives as more outputs ("peak, note"), one per port after Result."""
    names = [n for n in re.split(r"[,;\s]+", str(params.get("outputs") or "")) if n]
    bad = [n for n in names if not n.isidentifier()]
    if bad:
        raise ValueError(f"More outputs are variable names separated by commas; '{bad[0]}' isn't one")
    if len(names) > len(FORMULA_OUTPUTS):
        raise ValueError(f"Up to {len(FORMULA_OUTPUTS)} more outputs ({len(names)} named)")
    return names


def _formula_name(expr: str, label: str) -> str:
    """What a formula's result is called: its Name, else the formula itself (a script's: 'result')."""
    text = expr.strip()
    return label or (text if "\n" not in text and not ops.is_script(text) else "result")


def _script_value(v, name: str):
    """A script's texts, lists of mixed things and the like, as values: a text, a number, a yes/no or lists."""
    if isinstance(v, np.ndarray):
        v = v.tolist()
    if isinstance(v, (list, tuple)):
        return [_script_value(x, f"{name}[{i}]") for i, x in enumerate(v)]
    if isinstance(v, str):
        return Text(v, name)
    if isinstance(v, (bool, np.bool_)):
        return Bool(bool(v), name)
    if isinstance(v, (int, float, np.number)):
        return Number(float(v), name)
    raise ValueError(f"'{name}' is a {ops._kind(v)}, which can't leave the formula" +
                     ("; give its values, e.g. list(d.values())" if isinstance(v, dict) else ""))


def _special(out, name: str):
    """Results that aren't arrays of numbers (texts, dicts, ragged lists) as values, else None."""
    if out is None or isinstance(out, (dict, str)):
        return _script_value(out, name)
    if isinstance(out, (list, tuple)):
        try:
            arr = np.asarray(out)
        except ValueError:  # rows of different lengths
            return _script_value(out, name)
        if arr.dtype.kind in "USO":
            return _script_value(out, name)
    elif isinstance(out, np.ndarray) and out.dtype.kind in "US":
        return _script_value(out, name)
    return None


def _formula_once(values: dict, expr: str, label: str, outputs=()):
    """The formula once: (its result, {out port: named output}), each following the inputs."""
    base: dict = {}
    names = {k: (None if values.get(k) is None else _formula_operand(values[k], k, base)) for k in FORMULA_NAMES}
    f0, s0 = base.get("field"), base.get("series")
    names["t"] = s0.x if s0 is not None else None
    names["x"] = names["a"]  # x and y keep old Field Math / Series Math formulas working
    names["y"] = names["a"]
    result, env = ops.run_script(expr, names)
    main = _formula_out(result, f0, s0, _formula_name(expr, label), bool(label), expr)
    return main, {k: _formula_out(_named(env, n), f0, s0, n, True, n) for k, n in zip(FORMULA_OUTPUTS, outputs)}


def _named(env: dict, n: str):
    if n not in env:
        raise ValueError(f"The output '{n}' isn't set: write a line like '{n} = …' in the formula")
    return env[n]


def _formula_out(out, f0, s0, name: str, named: bool, expr: str):
    """A formula's value as a node output: a field (one value per row of the first field), a curve, a number…"""
    special = _special(out, name)
    if special is not None:
        return special
    if "\n" in expr.strip():
        expr = name  # a script: its result is described by its name, not its whole text
    out = np.asarray(out)
    label = name if named else ""
    if out.ndim == 0:
        v = out.item()
        if isinstance(v, (bool, np.bool_)):
            return Bool(bool(v), name)
        return Number(float(v), name)
    if f0 is not None:
        if len(out) != len(f0.labels):
            if out.ndim == 2:  # not per row: a plain matrix (e.g. np.cov of two fields)
                return _matrix(out, name)
            raise ValueError(f"The formula must give one value per {f0.entity} ({len(f0.labels)}) or a single "
                             f"number, but gives {len(out)}")
        return _field_result(f0, out, name if label else f"f({f0.name})", expr)
    if out.ndim == 2:
        return _matrix(out, name)
    if out.ndim > 2:
        raise ValueError(f"The formula gives a {'×'.join(map(str, out.shape))} block; up to 2 dimensions "
                         "(a matrix) are supported")
    if s0 is not None:
        if len(out) != len(s0.x):
            raise ValueError(f"The formula must give one value per curve point ({len(s0.x)}) or a single number, "
                             f"but gives {len(out)}")
        return Series(name if label else f"f({s0.name})", s0.x, out.astype(float), s0.xlabel, expr, dict(s0.extra))
    if out.dtype == bool:
        return [Bool(bool(v), f"{name}[{i}]") for i, v in enumerate(out)]
    return [Number(float(v), f"{name}[{i}]") for i, v in enumerate(out)]


def _matrix(out: np.ndarray, name: str) -> list:
    """A matrix result: a list of rows, each a list of numbers (shown as a table; Pick from List takes a row)."""
    return [[Number(float(x), f"{name}[{i},{j}]") for j, x in enumerate(row)] for i, row in enumerate(out)]


VALUES, AS_TABLES = "Values (one by one)", "As tables (columns as shown)"
_LEGACY_USE = {"One by one": VALUES, "As tables (rows × columns)": AS_TABLES}


def _lined_up(v: Field, f0: Field) -> np.ndarray:
    """v's values (rows × components) in f0's row order; rows v doesn't have are NaN."""
    if _field_row_keys(v) == _field_row_keys(f0):
        return v.values
    where = {key: k for k, key in enumerate(_field_row_keys(v))}
    idx = np.array([where.get(key, -1) for key in _field_row_keys(f0)], dtype=int)
    out = np.full((len(idx), v.values.shape[1]), np.nan)
    out[idx >= 0] = v.values[idx[idx >= 0]]
    return out


def _all(v, kind) -> list | None:
    """v as a list of ``kind`` items (one item, or a list of only those), else None."""
    if isinstance(v, kind):
        return [v]
    return v if isinstance(v, list) and v and all(isinstance(i, kind) for i in v) else None


@dataclass
class ValueTable:
    """The table a field or curves show in the data panel, which is also what a Formula indexes: a[:, 0] is the
    first column shown. ``head`` leading columns name the rows (Instance and Element/Node, or Curve); the rest are
    values. ``rows`` are the fields or curves' own row order."""

    columns: list[str]
    data: np.ndarray  # object array, rows × columns
    head: int
    field: Field | None = None
    fields: list | None = None
    curves: list | None = None
    single: bool = True

    @property
    def values(self) -> np.ndarray:
        return self.data[:, self.head:].astype(float)


def value_table(v, ref: "ValueTable | None" = None) -> ValueTable | None:
    """Fields (one; or several one-value fields, one column each) and curves (rows = curves, one column per point)
    as the table the data panel shows. With ``ref``, rows are lined up with that table's (fields by instance and
    label, curves interpolated onto its x)."""
    fields, curves = _all(v, Field), _all(v, Series)
    if fields:
        f0 = ref.field if ref is not None and ref.field is not None else fields[0]
        if fields[0].position != f0.position:
            raise ValueError(f"A {fields[0].entity} field can't be lined up with a field per {f0.entity}")
        if len(fields) == 1:
            vals, comps = _lined_up(fields[0], f0), list(fields[0].components)
        else:
            if any(f.values.shape[1] != 1 for f in fields):
                raise ValueError("A list of fields becomes a table with one column per field, so each must have one "
                                 "value per row (use Field Output's Quantity: Mises, S11…)")
            vals = np.hstack([_lined_up(f, f0) for f in fields])
            comps = [f"{f.name} (t = {_fmt(f.frame_value)})" for f in fields]
        cols = ["Instance", f0.entity.capitalize(), *comps]
        data = np.empty((len(f0.labels), len(cols)), dtype=object)
        data[:, 0] = [str(i) or ASSEMBLY for i in f0.instances]
        data[:, 1] = [int(l) for l in f0.labels]
        data[:, 2:] = vals
        return ValueTable(cols, data, 2, field=f0, fields=fields, single=isinstance(v, Field))
    if curves:
        x0 = ref.curves[0].x if ref is not None and ref.curves else curves[0].x
        ys = [c.y if len(c.x) == len(x0) and np.allclose(c.x, x0) else np.interp(x0, c.x, c.y) for c in curves]
        locs = [c.extra.get("location") for c in curves]
        if all(locs):
            kinds = {l["entity"] for l in locs}
            head = ["Instance", kinds.pop().capitalize() if len(kinds) == 1 else "Label"]
            names = [[str(l["instance"]) or ASSEMBLY, int(l["label"])] for l in locs]
        else:
            head, names = ["Curve"], [[c.name] for c in curves]
        xl = curves[0].xlabel or "x"
        cols = head + [f"{'t' if 'time' in xl.lower() else xl} = {_fmt(float(x))}" for x in x0]
        data = np.empty((len(curves), len(cols)), dtype=object)
        for r, (name, y) in enumerate(zip(names, ys)):
            data[r, :len(head)] = name
            data[r, len(head):] = np.asarray(y, dtype=float)
        return ValueTable(cols, data, len(head), curves=curves, single=isinstance(v, Series))
    return None


def _tidy(out) -> np.ndarray:
    """A formula result as numbers where it can be (table columns come out as objects), else texts, else mixed."""
    return ops.tidy(np.asarray(out))


def _table_names(expr: str) -> set[str]:
    """Inputs the formula indexes by row and column (a[:, 2], a[0, 3]): those are used as their tables."""
    try:
        tree = ops.parse(expr)
    except ValueError:
        return set()
    return {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
            and n.value.id in FORMULA_NAMES and isinstance(n.slice, ast.Tuple) and len(n.slice.elts) == 2}


def _formula_tables(values: dict, expr: str, label: str, tabled: set[str], outputs=()):
    """The formula with the ``tabled`` inputs as their tables (columns as shown); other fields and curves are their
    values, lined up with the first table's rows. Returns the outputs, as the node gives them."""
    tables: dict = {}
    names: dict = {}
    for k in FORMULA_NAMES:  # tables first: the first one sets the rows
        if k in tabled and values.get(k) is not None:
            tbl = value_table(values[k], tables.get("base"))
            if tbl is not None:
                tables.setdefault("base", tbl)
                tables[k] = tbl
                names[k] = tbl.data
    base: ValueTable | None = tables.get("base")
    seed = {} if base is None else ({"field": base.field} if base.field is not None else {"series": base.curves[0]})
    for k in FORMULA_NAMES:
        if k not in names:
            names[k] = None if values.get(k) is None else _formula_operand(values[k], k, seed)
    names["t"] = None if base is None else (base.curves[0].x if base.curves else
                                            np.array([f.frame_value for f in base.fields]))
    names["x"] = names["y"] = names["a"]
    try:
        result, env = ops.run_script(expr, names)
    except ValueError as exc:
        heads = [f"'{k}': {', '.join(f'[{j}] {c}' for j, c in enumerate(t.columns[:t.head]))}"
                 for k, t in tables.items() if k != "base"]
        if heads and ("unsupported operand" in str(exc) or "ufunc" in str(exc) or "convert" in str(exc)):
            raise ValueError(f"{exc}. The first columns name the rows ({'; '.join(heads)}), they aren't values: take "
                             f"the value columns, e.g. a[:, {base.head}] or a[:, {base.head}:]") from exc
        raise
    out = {"value": _table_out(result, base, _formula_name(expr, label))}
    for k, n in zip(FORMULA_OUTPUTS, outputs):
        out[k] = _table_out(_named(env, n), base, n)
    return out


def _table_out(out, base: "ValueTable | None", name: str):
    """A formula's value over tables as a node output: like Python gives it (a column or row is a list, a block a
    matrix), and one value per row still knows its node/element."""
    if out is None or isinstance(out, dict):
        return _script_value(out, name)
    try:
        out = _tidy(out)
    except ValueError:  # rows of different lengths
        return _script_value(out, name)
    names_shown = set() if base is None else {str(v) for v in base.data[:, :base.head].flat}
    if base is not None and out.ndim and any(isinstance(v, str) and v not in names_shown for v in out.flat):
        raise ValueError(f"That works on the whole table, whose first {base.head} column(s) "
                         f"({', '.join(base.columns[:base.head])}) name the rows and aren't values: take the value "
                         f"columns, e.g. a[:, {base.head}:] * 2 or a[:, {base.head}]")
    if out.ndim == 0:
        v = out.item()
        if isinstance(v, str):
            return Text(v, name)
        return Bool(bool(v), name) if isinstance(v, (bool, np.bool_)) else Number(float(v), name)
    if out.ndim > 2:
        raise ValueError(f"The formula gives a {'×'.join(map(str, out.shape))} block; up to 2 dimensions "
                         "(a table) are supported")
    # Like Python: what the indexing gives, as plain values (a column or a row is a list, a block a matrix).
    # One value per row of the table still knows its node/element (not shown), for Contour markers.
    extras = _row_extras(base) if base is not None and out.ndim == 1 and len(out) == len(base.data) else None
    if out.dtype.kind in "USO":  # texts, or names and numbers mixed (a[:, :], a[0]): like the table's cells
        def cell(v):
            return Text(v) if isinstance(v, str) else Number(float(v))
        return [[cell(v) for v in row] for row in out] if out.ndim == 2 else [cell(v) for v in out]
    if out.ndim == 2:
        return _matrix(out, name)
    if out.dtype == bool:
        return [Bool(bool(v)) for v in out]
    return [Number(float(v), "", "", dict(extras[i]) if extras else {}) for i, v in enumerate(out)]


def _row_extras(t: ValueTable) -> list[dict]:
    """Each table row's node/element, as Reduce Max/Min results carry it."""
    if t.curves:
        return [c.extra for c in t.curves]
    f = t.field
    return [{"location": {"odb": f.odb.path, "entity": f.entity, "instance": str(i), "label": int(l)}}
            for i, l in zip(f.instances, f.labels)]


_DIMENSION_WORDS = ("dimensional", "too many indices", "out of bounds for array of dimension")


def _table_hint(exc: Exception, inputs: dict) -> ValueError:
    """What the tables look like, when indexing one went wrong."""
    parts = []
    for k in FORMULA_NAMES:
        try:
            t = value_table(inputs.get(k))
        except ValueError:
            t = None
        if t is not None:
            cols = ", ".join(f"[{j}] {c}" for j, c in enumerate(t.columns[:6])) + (" …" if len(t.columns) > 6 else "")
            parts.append(f"'{k}' is a table of {len(t.data)} rows × {len(t.columns)} columns ({cols})")
        elif _is_matrix(inputs.get(k)):
            m = inputs[k]
            parts.append(f"'{k}' is a {len(m)}×{max(len(r) for r in m)} matrix: a[row, column]")
        elif isinstance(inputs.get(k), list):
            parts.append(f"'{k}' is a list of {len(inputs[k])} values (1-D, one column): use one index, e.g. "
                         f"{k}[2:] or {k}[0]")
    return ValueError(f"{exc}. " + "; ".join(parts) + ". Index like the data panel shows it, counting from 0: "
                      "a[row, column] for tables and matrices, a[i] for lists.")


@register("math.formula", "Formula", "Math",
          "One formula for any kind of input: numbers, fields, curves, lists or yes/no values, e.g. 355 / a, "
          "a / 1e6 (field), -a (curve), a - b (two fields or curves), np.percentile(a, 95), a > 200. "
          "The output follows the inputs: a field if a field comes in (one value per node/element), a curve if a "
          "curve comes in, else a number, a list or a yes/no. Matrices: a.T (transpose), a @ b, inv(a), det(a), "
          "eigvals(a), trace(a); a tensor field (S, E…) comes in as one 3×3 matrix per node/element. "
          "Tables: index a field or curves like the data panel shows them, a[row, column] counting from 0 "
          "(columns, rows): a[:, 2] is the third column (e.g. S Mises after Instance and Element), a[0] the first "
          "row. A set comes in as its labels. "
          "Scripts: open the code window (⤢) to write several lines, with variables, if/elif/else, for and "
          "while loops, def functions, lists and dicts, f-texts; the last line (or result = …) is the Result, and "
          "variables named in More outputs become outputs of their own (a field, a curve, a number, a text…). "
          "Script, code, program, Python, custom calculation.",
          # Any depth: NumPy takes a list as a vector and a grid as a matrix (a.T, a @ b, a[:, 0]), so nothing loops.
          inputs=[Port(k, "any", k, optional=True) for k in FORMULA_NAMES],
          outputs=[Port("value", "any", "Result"),
                   *[Port(k, "any", f"Output {i + 2}", when={"named:outputs": [str(i)]})
                     for i, k in enumerate(FORMULA_OUTPUTS)]],
          params=[Param("expr", "text", "a", "Formula",
                        help="Inputs are a, b, c, d (t is the curve's x). Use + - * / **, comparisons, "
                             "'p if c else q', x[x > 0], a[:, 2] (a column of the table shown) and NumPy "
                             "functions (np.max, np.clip, np.interp…). ⤢ opens a code window for scripts of "
                             "several lines: variables, if/else, for, def; the last line (or result = …) is the "
                             "Result"),
                  Param("label", "text", "", "Name"),
                  Param("outputs", "text", "", "More outputs",
                        help=f"Variables the script sets that become outputs too, separated by commas (up to "
                             f"{len(FORMULA_OUTPUTS)}), e.g. peak, note"),
                  Param("use", "select", VALUES, "Use inputs", options=[VALUES, AS_TABLES],
                        help="Values: a field is its values (one per node/element; a 3×3 matrix per row for "
                             "tensors), a curve its y values, and a list of curves or fields runs the formula once "
                             "per item. As tables: every field or curve input is the table the data panel shows, "
                             "with the same columns in the same order (Instance, Element/Node, then the values; "
                             "for curves one column per frame). Even in Values, an input indexed by row and "
                             "column (a[:, 2], a[0, 3]) is its table. Results with one value per row give a "
                             "field / one number per curve (with where it was); the value columns of curves give "
                             "curves back.")],
          lists=True)
def math_formula(inputs, params):
    use = _LEGACY_USE.get(params.get("use"), params.get("use") or VALUES)
    # Fields and curves indexed by row and column (a[:, 2]) are their tables, as the data panel shows them.
    tabled = set(FORMULA_NAMES) if use == AS_TABLES else _table_names(params["expr"])
    tabled = {k for k in tabled if _all(inputs.get(k), Field) or _all(inputs.get(k), Series)}
    try:
        if tabled:
            return _formula_tables(inputs, params["expr"], params["label"], tabled, _script_outputs(params))
        return _formula_values(inputs, params)
    except ValueError as exc:
        if any(w in str(exc) for w in _DIMENSION_WORDS):  # say what shape each input has
            raise _table_hint(exc, inputs) from exc
        raise


def _formula_values(inputs, params):
    def is_group(v):  # several fields or curves: one result per item
        return isinstance(v, list) and v and all(isinstance(i, (Field, Series)) for i in v)

    outputs = _script_outputs(params)
    groups = {k: v for k, v in inputs.items() if k in FORMULA_NAMES and is_group(v)}
    if not groups:
        main, extras = _formula_once(inputs, params["expr"], params["label"], outputs)
        return {"value": main, **extras}
    lengths = {len(v) for v in groups.values()}
    n = max(lengths)
    if lengths - {1, n}:
        raise ValueError(f"Lists have different lengths ({', '.join(map(str, sorted(lengths)))})")
    runs = []
    for i in range(n):
        item = {k: (v[i if len(v) > 1 else 0] if k in groups else v) for k, v in inputs.items()}
        runs.append(_formula_once(item, params["expr"], params["label"], outputs))
    return {"value": [m for m, _ in runs], **{k: [e[k] for _, e in runs] for k in FORMULA_OUTPUTS[:len(outputs)]}}


CAE_OBJECTS = ["Sketch in CAE", "Part in CAE", "Instance in CAE", "Material in CAE", "Section in CAE"]
INPUT_TYPES = ["Number", "Text", "Yes/No", "List / matrix", *CAE_OBJECTS]


def _typed_list(v, name: str, hint: str):
    """A typed-in list at any depth (numbers, texts, Yes/No; rows can differ in length) as values."""
    if isinstance(v, (list, tuple)):
        return [_typed_list(x, f"{name}[{i}]", hint) for i, x in enumerate(v)]
    if isinstance(v, bool):
        return Bool(v, name)
    if isinstance(v, (int, float)):
        return Number(float(v), name)
    if isinstance(v, str):
        return Text(v, name)
    raise ValueError(hint)


@register("input.value", "Input", "Input",
          "A value you type in: a number (e.g. a yield stress), a text (a name, a path, a folder), Yes/No (a "
          "switch for If), or a list: numbers (1, 2, 3), a matrix ([[1, 2], [3, 4]]), texts ([\"A\", \"B\"]), or "
          "lists of lists at any depth (a grid of sizes, a list of point tables). Wire it into any input or setting; "
          "a list into a one-value input runs that node once per item. It can also reference a sketch, part, "
          "instance, material or section selected from the running CAE model, so complicated existing geometry and "
          "an existing material library can enter the graph.",
          outputs=[Port("value", "any", "Value")],
          params=[Param("type", "select", "Number", "Type", options=INPUT_TYPES),
                  Param("value", "text", "355", "Value", help="Number: 355 or 2.1e5. Yes/No: Yes or No."),
                  Param("pick", "pick", "", "", help="Choose it from the list of the sketches, parts, instances, "
                             "materials or sections in CAE (⟳ lists them again)",
                        when={"type": CAE_OBJECTS}),
                  Param("label", "text", "", "Name")])
def input_value(inputs, params):
    raw, label = str(params["value"]).strip(), params["label"]
    cae_type = str(params["type"])
    if cae_type in CAE_OBJECTS:
        try:
            model_name, object_name = raw.split("|", 1)
        except ValueError as exc:
            raise ValueError("Nothing chosen yet: choose it from the list of CAE's objects") from exc
        model_name, object_name = model_name.strip(), object_name.strip()
        model = Model(model_name, existing=True)
        if not object_name:
            raise ValueError("CAE did not return an object name; select it again")
        if cae_type == "Sketch in CAE":
            return {"value": Sketch(object_name, (), model, existing=True)}
        if cae_type in ("Material in CAE", "Section in CAE"):  # used by name: their properties stay in CAE
            material = Material(object_name if cae_type == "Material in CAE" else "", model, 0.0, 0.0, existing=True)
            if cae_type == "Material in CAE":
                return {"value": material}
            return {"value": Section(object_name, material, "", existing=True)}
        part = Part(object_name if cae_type == "Part in CAE" else "", model, "Shell", "Planar", existing=True)
        if cae_type == "Part in CAE":
            return {"value": part}
        # The part name is filled by the CAE picker after the instance name, separated by a second pipe.
        bits = raw.split("|")
        if len(bits) != 3:
            raise ValueError("CAE did not return the selected instance's part; select it again")
        model = Model(bits[0].strip(), existing=True)
        part = Part(bits[2].strip(), model, "Shell", "Planar", existing=True)
        return {"value": Instance(bits[1].strip(), part, origin=bits[1].strip(), existing=True)}
    if params["type"] == "Text":
        return {"value": Text(str(params["value"]), label or "Text")}
    if params["type"] == "List / matrix":
        import ast
        hint = ("Type numbers like 1, 2, 3, a matrix like [[1, 2], [3, 4]], texts like [\"Left\", \"Right\"], or lists "
                "of lists at any depth")
        try:
            v = ast.literal_eval(raw if raw.startswith("[") else f"[{raw}]")
        except (ValueError, SyntaxError, TypeError) as exc:
            raise ValueError(hint) from exc
        name = label or "List"
        try:
            arr = np.array(v, dtype=float)
        except (ValueError, TypeError):
            arr = None
        if arr is not None and arr.ndim == 1:
            return {"value": [Number(float(x), f"{name}[{i}]") for i, x in enumerate(arr)]}
        if arr is not None and arr.ndim == 2:
            return {"value": _matrix(arr, name)}
        return {"value": _typed_list(v, name, hint)}
    if params["type"] == "Yes/No":
        if raw.lower() not in ("yes", "no", "true", "false", "1", "0", "y", "n"):
            raise ValueError("Type Yes or No")
        return {"value": Bool(raw.lower() in ("yes", "true", "1", "y"), label or "Switch")}
    try:
        return {"value": Number(float(raw), label or "Number")}
    except ValueError as exc:
        raise ValueError(f"'{raw}' isn't a number") from exc


@register("math.number", "Number", "Input", "A real number you type in, e.g. a yield stress.",
          outputs=[Port("value", "number", "Value")],
          params=[Param("value", "number", 355.0, "Value"), Param("label", "text", "", "Name")])
def math_number(inputs, params):
    return {"value": Number(float(params["value"]), params["label"] or "Number")}
