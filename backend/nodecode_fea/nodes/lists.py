"""List and Logic nodes: pick, sort, count, set operations, Compare and If."""

from __future__ import annotations

import re
import numpy as np
from dataclasses import replace

from .. import ops
from ..values import Bool, EntitySet, Field, Number, Region, Series, Text
from .core import _fmt, Param, Port, register, _require_scalar
from .common import _as_list, entity_name, _is_collection, _size


def _take(v, idx, suffix: str):
    """The items at positions ``idx``: same kind as ``v`` (a list, or a smaller set/field)."""
    idx = np.asarray(idx, dtype=int)
    if isinstance(v, EntitySet):
        return EntitySet(f"{v.name}{suffix}", v.kind, v.instances[idx], v.labels[idx])
    if isinstance(v, Field):
        return replace(v, name=f"{v.name}{suffix}", instances=v.instances[idx], labels=v.labels[idx],
                       values=v.values[idx])
    if isinstance(v, Series):
        return replace(v, name=f"{v.name}{suffix}", x=v.x[idx], y=v.y[idx], extra=dict(v.extra))
    items = _as_list(v)
    return [items[i] for i in idx]


def _member(v, i: int) -> str:
    return entity_name(str(v.instances[i]), v.labels[i])


def _flatten_all(v) -> list:
    return [x for item in v for x in _flatten_all(item)] if isinstance(v, list) else ([] if v is None else [v])


@register("list.merge", "Merge", "List",
          "Join items or lists into one list, in order A, B, C, D. E.g. several Reduce results into one list. "
          "Flatten turns lists of lists (a grid, nested results) into one flat list of all their items.",
          # Any depth: it joins the outer lists as they are (a grid stays rows) and Flatten works on every level.
          inputs=[Port("a", "any", "A", optional=True), Port("b", "any", "B", optional=True),
                  Port("c", "any", "C", optional=True), Port("d", "any", "D", optional=True)],
          outputs=[Port("list", "any", "List")],
          params=[Param("levels", "select", "Keep levels", "Levels", options=["Keep levels", "Flatten"],
                        help="Keep levels: a grid stays a list of lists. Flatten: one list of every item, row by row "
                             "(a 3 × 4 grid gives 12 items).")],
          lists=True)
def list_merge(inputs, params):
    items = [x for k in ("a", "b", "c", "d") for x in _as_list(inputs.get(k))]
    if params.get("levels") == "Flatten":
        items = _flatten_all(items)
    if not items:
        raise ValueError("Connect at least one input")
    return {"list": items}


@register("list.item", "List Item", "List",
          "Pick one item by position: 0 is the first, -1 the last. On a Set it picks one node/element "
          "(as a one-member set); on a Field, one row.",
          inputs=[Port("list", "any", "List")], outputs=[Port("item", "any", "Item")],
          params=[Param("index", "int", 0, "Index")], lists=True, hidden=True)
def list_item(inputs, params):
    v = inputs["list"]
    n, i = _size(v), int(params["index"])
    if not -n <= i < n:
        raise ValueError(f"Index {i} is out of range (there are {n} items)")
    if _is_collection(v):
        i %= n
        return {"item": _take(v, [i], f"[{i}] {_member(v, i)}")}
    if isinstance(v, Series):  # one point of a curve: its y value, at its x
        i %= n
        return {"item": Number(float(v.y[i]), f"{v.name} [{i}]", f"{v.xlabel} = {_fmt(float(v.x[i]))}",
                               {**v.extra, "x": float(v.x[i])})}
    return {"item": _as_list(v)[i]}


@register("list.length", "Count", "List",
          "The size of anything: how many items a list has (its length), members a set has, nodes/elements a "
          "field has, or points a curve has.",
          inputs=[Port("list", "any", "List", depth=1)], outputs=[Port("value", "number", "Count")], lists=True)
def list_length(inputs, params):
    v = inputs["list"]
    n = len(v.x) if isinstance(v, Series) else _size(v)
    return {"value": Number(float(n), "Count")}


def _parse_slice(text: str) -> slice:
    parts = [p.strip() for p in text.split(":")]
    if not 1 <= len(parts) <= 3:
        raise ValueError("Use start:end, e.g. 0:3 or -2:")
    try:
        nums = [int(p) if p else None for p in parts]
    except ValueError as exc:
        raise ValueError("Use whole numbers, e.g. 0:3 or -2:") from exc
    if len(nums) == 1:
        return slice(nums[0], None if nums[0] == -1 else nums[0] + 1)
    return slice(*nums)


@register("list.slice", "Sub-list", "List",
          "Part of a list, Python style: 0:3 is the first three, -2: the last two. Works on sets and fields too.",
          inputs=[Port("list", "any", "List")], outputs=[Port("list", "any", "List")],
          params=[Param("range", "text", "0:3", "Range")], lists=True, hidden=True)
def list_slice(inputs, params):
    v = inputs["list"]
    idx = np.arange(_size(v))[_parse_slice(params["range"])]
    if not len(idx):
        raise ValueError("The range selects nothing")
    return {"list": _take(v, idx, f"[{params['range']}]")}


def _sort_key(v):
    if isinstance(v, Number):
        return v.value
    if isinstance(v, Series):
        return float(np.nanmax(np.abs(v.y))) if len(v.y) else 0.0
    return str(getattr(v, "name", v))


@register("list.pick", "Pick from List", "List",
          "Take items by position: 0 is the first, -1 the last; 0:3 the first three, -5: the last five, ::2 every "
          "other one, ::-1 all of them reversed; 0, 2, 7 those three. One position gives the item itself, the "
          "others a shorter list. On a Set it picks nodes/elements; on a Field, rows; on one curve, points (one "
          "point is its y value, e.g. -1 the last frame's value; a range is a shorter curve). A list of curves "
          "picks curves.",
          inputs=[Port("list", "any", "List", depth=1)], outputs=[Port("list", "any", "Picked")],
          params=[Param("which", "text", "0", "Which",
                        help="An index (0, -1), a range (0:3, -5:, ::-1) or several indices (0, 2, 7). Takes a wire, "
                             "e.g. For's i.")],
          lists=True)
def list_pick(inputs, params):
    v = inputs["list"]
    n = _size(v)
    text = str(params["which"]).strip()
    if re.fullmatch(r"-?\d+(\.0+)?", text):  # one index: the item itself
        return {"list": list_item({"list": v}, {"index": int(float(text))})["item"]}
    if ":" in text:
        idx = np.arange(n)[_parse_slice(text)]
    else:
        try:
            idx = np.array([int(float(t)) for t in re.split(r"[,;\s]+", text) if t], dtype=int)
        except ValueError as exc:
            raise ValueError("Use an index (2), a range (0:3, ::-1) or indices (0, 2, 7)") from exc
        bad = [int(i) for i in idx if not -n <= i < n]
        if bad:
            raise ValueError(f"Index {bad[0]} is out of range (there are {n} items)")
        idx = idx % max(n, 1)
    if not len(idx):
        raise ValueError("That selects nothing")
    return {"list": _take(v, idx, f"[{text}]")}


@register("list.sort", "Sort List", "List",
          "Sort numbers by value, curves by their peak, set members by label, field rows by value; "
          "anything else by name.",
          inputs=[Port("list", "any", "List", depth=1)], outputs=[Port("list", "any", "List")],
          params=[Param("order", "select", "Descending", "Order", options=["Descending", "Ascending"])], lists=True)
def list_sort(inputs, params):
    v, desc = inputs["list"], params["order"] == "Descending"
    if isinstance(v, EntitySet):
        idx = np.lexsort((v.labels, v.instances.astype(str)))
    elif isinstance(v, Field):
        key = v.values[:, 0] if v.values.shape[1] == 1 else np.linalg.norm(v.values, axis=1)
        idx = np.argsort(np.nan_to_num(key, nan=-np.inf), kind="stable")
    else:
        return {"list": sorted(_as_list(v), key=_sort_key, reverse=desc)}
    return {"list": _take(v, idx[::-1] if desc else idx, " sorted")}


@register("list.reverse", "Reverse List", "List", "Reverse the order of a list (or of a set's members).",
          inputs=[Port("list", "any", "List")], outputs=[Port("list", "any", "List")], lists=True, hidden=True)
def list_reverse(inputs, params):
    v = inputs["list"]
    return {"list": _take(v, np.arange(_size(v))[::-1], " reversed")}


MAX_SPLIT = 1000


@register("list.split", "Split into Items", "List",
          "Turn a Set into a list of one-member sets (one per node/element), or a Field into a list of one-row "
          "fields, so the next nodes run once per member.",
          inputs=[Port("list", "any", "Set / Field")], outputs=[Port("list", "any", "List")], lists=True, hidden=True)
def list_split(inputs, params):
    v = inputs["list"]
    if not _is_collection(v):
        raise ValueError("Connect a Set or a Field")
    n = _size(v)
    if n > MAX_SPLIT:
        raise ValueError(f"That's {n} members; the limit is {MAX_SPLIT}. Use Sub-list first.")
    return {"list": [_take(v, [i], f" {_member(v, i)}") for i in range(n)]}


@register("list.stats", "List Stats", "List",
          "Max, min, mean or sum of a list of numbers. Max/Min keep where the value was found.",
          inputs=[Port("list", "number", "Numbers")], outputs=[Port("value", "number", "Value")],
          params=[Param("op", "select", "Max", "Operation", options=ops.REDUCERS)], lists=True, hidden=True)
def list_stats(inputs, params):
    items: list[Number] = _as_list(inputs["list"])
    v, i = ops.reduce(params["op"], np.array([n.value for n in items]))
    if i is None:
        return {"value": Number(v, f"{params['op']} of {len(items)}")}
    src = items[i]
    return {"value": Number(v, f"{params['op']}: {src.label}", src.where, dict(src.extra))}


# ------------------------------------------------------------------ logic
COMPARE_OPS = [">", ">=", "<", "<=", "==", "!="]
_CMP = {">": np.greater, ">=": np.greater_equal, "<": np.less, "<=": np.less_equal, "==": np.isclose,
        "!=": lambda a, b: ~np.isclose(a, b)}


def _truth(v) -> bool:
    return bool(v.value) if isinstance(v, (Bool, Number)) else bool(v)


@register("logic.compare", "Compare", "Logic",
          "Is a greater than / less than / equal to b? Gives Yes or No. With a list, gives one answer per item.",
          inputs=[Port("a", "number", "a"), Port("b", "number", "b", optional=True)],
          outputs=[Port("result", "bool", "Yes/No")],
          params=[Param("op", "select", ">", "Test", options=COMPARE_OPS),
                  Param("value", "number", 0.0, "b (if not connected)")], hidden=True)
def logic_compare(inputs, params):
    a, b = inputs["a"], inputs.get("b")
    bv = b.value if b is not None else float(params["value"])
    result = bool(_CMP[params["op"]](a.value, bv))
    return {"result": Bool(result, f"{a.label or 'a'} {params['op']} {_fmt(bv)}")}


@register("logic.gate", "And / Or / Not", "Logic", "Combine Yes/No values.",
          inputs=[Port("a", "bool", "a"), Port("b", "bool", "b", optional=True)],
          outputs=[Port("result", "bool", "Yes/No")],
          params=[Param("op", "select", "And", "Operation", options=["And", "Or", "Xor", "Not a"])])
def logic_gate(inputs, params):
    a, b, op = inputs["a"], inputs.get("b"), params["op"]
    if op == "Not a":
        return {"result": Bool(not a.value, f"not ({a.label})")}
    if b is None:
        raise ValueError(f"'{op}' needs b connected")
    value = {"And": a.value and b.value, "Or": a.value or b.value, "Xor": a.value != b.value}[op]
    return {"result": Bool(bool(value), f"({a.label}) {op.lower()} ({b.label})")}


def _region_mask(region: Region, conditions: list) -> Region:
    """Keep the selected geometry at the positions whose matching condition is true."""
    if region.positions is not None:
        available = region.positions
        expected = len(available)
        position_count = region.position_count
    else:
        available = tuple(range(len(conditions)))
        # By-shape selections are resolved only in CAE. For picked indices and Find-at points the expected count is
        # already known, so catch a mask from some unrelated list before generating a misleading CAE selection.
        expected = None if region.dynamic else len(region.indices) or len(region.points)
        position_count = len(conditions)
    if expected is not None and expected != len(conditions):
        raise ValueError(f"Then has {expected} region members but there are {len(conditions)} conditions")
    kept = tuple(available[i] for i, condition in enumerate(conditions) if _truth(condition))
    # A subset is a graph selection, not the original named Set/Surface. A downstream Region can name it anew.
    return replace(region, name="", form="Selection", combine=False, positions=kept,
                   position_count=position_count)


@register("logic.if", "If", "Logic",
          "Pass on Then when the condition is Yes, else Else. With a list of conditions it decides item by item: "
          "items (or set members, field rows, Region faces/edges/cells) with No and no Else are dropped, so it "
          "also filters lists and geometry Regions.",
          inputs=[Port("condition", "bool", "Condition", depth=1), Port("then", "any", "Then", depth=1),
                  Port("else", "any", "Else", optional=True, depth=1)],
          outputs=[Port("result", "any", "Result")], lists=True)
def logic_if(inputs, params):
    cond, then, other = inputs["condition"], inputs["then"], inputs.get("else")
    if not isinstance(cond, list):
        if _truth(cond):
            return {"result": then}
        if other is None:
            raise ValueError("The condition is No and Else isn't connected")
        return {"result": other}

    if isinstance(then, Region) and other is None:
        return {"result": _region_mask(then, cond)}

    if _is_collection(then) and other is None:  # keep the members/rows whose condition is Yes
        if _size(then) != len(cond):
            raise ValueError(f"Then has {_size(then)} members but there are {len(cond)} conditions")
        return {"result": _take(then, [i for i, c in enumerate(cond) if _truth(c)], " (kept)")}

    def pick(v, i):
        if isinstance(v, list):
            if len(v) != len(cond):
                raise ValueError(f"Then/Else have {len(v)} items but there are {len(cond)} conditions")
            return v[i]
        return v

    out = []
    for i, c in enumerate(cond):
        if _truth(c):
            out.append(pick(then, i))
        elif other is not None:
            out.append(pick(other, i))
    return {"result": out}


def _value_of(v) -> float:
    if isinstance(v, (Number, Bool)):
        return float(v.value)
    if isinstance(v, Series):
        return float(np.nanmax(np.abs(v.y))) if len(v.y) else float("nan")
    raise ValueError(f"Can't compare a {type(v).__name__}; connect Keep instead")


@register("list.filter", "Filter List", "List",
          "Keep the items that pass a test (numbers by value, curves by their peak), or where Keep is Yes. "
          "On a Field, keeps the rows that pass.",
          inputs=[Port("list", "any", "List"), Port("keep", "bool", "Keep", optional=True)],
          outputs=[Port("list", "any", "List")],
          params=[Param("op", "select", ">", "Keep if", options=COMPARE_OPS),
                  Param("value", "number", 0.0, "Value")], lists=True)
def list_filter(inputs, params):
    v, keep = inputs["list"], inputs.get("keep")
    v = [v] if isinstance(v, Series) else v
    n = _size(v)
    if keep is not None:
        mask = np.array([_truth(k) for k in _as_list(keep)], dtype=bool)
        if len(mask) != n:
            raise ValueError(f"Keep has {len(mask)} Yes/No values but the list has {n} items")
    elif isinstance(v, Field):
        mask = _CMP[params["op"]](_require_scalar(v, "Filter List"), float(params["value"]))
    elif isinstance(v, EntitySet):
        raise ValueError("A Set has no values to test; connect Keep, or filter the Field instead")
    else:
        mask = np.array([bool(_CMP[params["op"]](_value_of(x), float(params["value"]))) for x in _as_list(v)],
                        dtype=bool)
    if not mask.any():
        raise ValueError("No item passes the filter")
    return {"list": _take(v, np.nonzero(mask)[0], f" ({params['op']} {_fmt(float(params['value']))})")}


@register("field.where", "Filter by Value", "Field",
          "Keep the nodes/elements whose value passes a test, e.g. Mises > 355 (where it yields).",
          inputs=[Port("field", "field", "Scalar")], outputs=[Port("field", "field", "Field")],
          params=[Param("op", "select", ">", "Keep if", options=COMPARE_OPS), Param("value", "number", 0.0, "Value")],
          hidden=True)
def field_where(inputs, params):
    f: Field = inputs["field"]
    mask = _CMP[params["op"]](_require_scalar(f, "Filter by Value"), float(params["value"]))
    if not mask.any():
        raise ValueError(f"No {f.entity} of {f.name} is {params['op']} {_fmt(float(params['value']))}")
    return {"field": f.subset(mask, f"{params['op']} {_fmt(float(params['value']))}")}


TEXT_OPS = ["contains", "starts with", "ends with"]
TEST_OPS = COMPARE_OPS + TEXT_OPS


def _compare_values(a, b, op: str, case: str):
    """Element-wise test of numbers (arrays) or texts; == on numbers is 'close to'."""
    if op in TEXT_OPS or isinstance(a, str) or (isinstance(a, np.ndarray) and a.dtype.kind in "US"):
        if op in (">", ">=", "<", "<="):
            raise ValueError(f"'{op}' compares numbers; for texts use ==, !=, contains, starts with or ends with")
        aa, bb = np.atleast_1d(np.asarray(a, dtype=str)), np.broadcast_to(np.asarray(b, dtype=str), np.shape(np.atleast_1d(a)))
        if case == "Ignore case":
            aa, bb = np.char.lower(aa), np.char.lower(bb)
        fn = {"==": lambda x, y: x == y, "!=": lambda x, y: x != y, "contains": lambda x, y: y in x,
              "starts with": lambda x, y: x.startswith(y), "ends with": lambda x, y: x.endswith(y)}[op]
        out = np.array([fn(str(x), str(y)) for x, y in zip(aa, bb)], dtype=bool)
        return out if np.ndim(a) else bool(out[0])
    if op in TEXT_OPS:
        raise ValueError(f"'{op}' works on texts")
    return _CMP[op](np.asarray(a, dtype=float), np.asarray(b, dtype=float))


def _plain(v):
    """A value as what Compare tests: a number, a text, or a curve's peak (largest absolute value)."""
    if isinstance(v, (Number, Bool)):
        return float(v.value)
    if isinstance(v, Text):
        return v.value
    if isinstance(v, Series):
        return float(np.nanmax(np.abs(v.y))) if len(v.y) else float("nan")
    raise ValueError(f"Compare can't test a {type(v).__name__}")


def _compare_one(a, b, params) -> dict:
    op, case = params["op"], params["case"]
    if b is None:
        raw = str(params["value"]).strip()
        try:
            bv = float(raw) if op not in TEXT_OPS else raw
        except ValueError:
            bv = raw  # a text to compare with
    elif isinstance(b, list):
        bv = np.array([_plain(x) for x in b])
    else:
        bv = _plain(b)
    shown = _fmt(bv) if isinstance(bv, float) else ("…" if isinstance(bv, np.ndarray) else f"'{bv}'")
    if isinstance(a, Field):
        if isinstance(b, (list, Field)):
            raise ValueError("Compare a field with a number (to compare two fields, use a Formula like a > b)")
        mask = np.asarray(_compare_values(_require_scalar(a, "Compare"), bv, op, case), dtype=bool)
        mask &= ~np.isnan(a.values[:, 0]) if op not in ("!=",) else True
        n = int(mask.sum())
        return {"kept": a.subset(mask, f"{op} {shown}"),
                "result": Bool(bool(n), f"any {a.entity} of {a.name} {op} {shown}"),
                "count": Number(float(n), f"{a.entity}s {op} {shown}")}
    if isinstance(a, EntitySet):
        raise ValueError("A Set has no values to compare: compare the Field (filtered by the set) instead")
    items = _as_list(a)
    av = np.array([_plain(x) for x in items], dtype=object)
    if all(isinstance(x, float) for x in av):
        av = av.astype(float)
    if isinstance(bv, np.ndarray) and len(bv) != len(items):
        raise ValueError(f"a has {len(items)} items but b has {len(bv)}")
    mask = np.atleast_1d(np.asarray(_compare_values(av if av.dtype != object else av.astype(str), bv, op, case),
                                    dtype=bool))
    if not isinstance(a, list):
        ok = bool(mask[0])
        label = f"{getattr(a, 'label', '') or 'a'} {op} {shown}"
        return {"kept": [a] if ok else [], "result": Bool(ok, label), "count": Number(float(ok), label)}
    return {"kept": [x for x, k in zip(items, mask) if k],
            "result": [Bool(bool(k), f"{getattr(x, 'label', '') or f'#{i}'} {op} {shown}")
                       for i, (x, k) in enumerate(zip(items, mask))],
            "count": Number(float(mask.sum()), f"items {op} {shown}")}


@register("logic.test", "Compare", "Logic",
          "Test values: numbers, lists, fields (every node/element), curves (their peak) or texts, against b or "
          "a typed value. Passing gives what passes (the nodes/elements of a field, e.g. Mises > 355; the items "
          "of a list), Yes/No the answer (one per item for a list; for a field, whether any passes), Count how "
          "many pass.",
          inputs=[Port("a", "any", "a", depth=1), Port("b", "any", "b", optional=True, depth=1)],
          outputs=[Port("kept", "any", "Passing"), Port("result", "bool", "Yes/No"), Port("count", "number", "Count")],
          params=[Param("op", "select", ">", "Test", options=TEST_OPS),
                  Param("value", "text", "0", "b (if not connected)",
                        help="A number, or a text for text tests. Takes a wire."),
                  Param("case", "select", "Ignore case", "Text case", options=["Ignore case", "Match case"],
                        help="For texts only")],
          lists=True)
def logic_test(inputs, params):
    a, b = inputs["a"], inputs.get("b")
    if isinstance(a, list) and a and all(isinstance(x, Field) for x in a):  # several fields: one result each
        outs = [_compare_one(f, b, params) for f in a]
        return {k: [o[k] for o in outs] for k in ("kept", "result", "count")}
    return _compare_one(a, b, params)
