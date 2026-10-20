"""Helpers several node modules share: collections, members, matrices, names."""

from __future__ import annotations

import math
import numpy as np

from ..properties import as_name
from ..values import Bool, EntitySet, Field, Number, Odb, Part, Series, Text
from .core import _fmt


ASSEMBLY = "ASSEMBLY"  # how assembly-level nodes/elements (instance '') are written in labels and names


def entity_name(inst: str, label: int) -> str:
    return f"{inst or ASSEMBLY}.{int(label)}"


def as_set(v) -> EntitySet | None:
    """Nodes/elements from anything that names some: sets, fields (their rows), values found at a node or element
    (Reduce Max/Min), and lists of these, merged into one set."""
    if v is None:
        return None
    items = []
    for x in _flat_items(v):
        if isinstance(x, (Number, Series)):
            loc = x.extra.get("location")
            if not loc:
                raise ValueError(f"'{getattr(x, 'label', '') or x.name}' is a {_kind_name(x)} with no node/element "
                                 "behind it (Reduce Max/Min results and Over Time's Each curves have one)")
            x = EntitySet(f"{loc['entity']} {entity_name(loc['instance'], loc['label'])}", loc["entity"],
                          np.array([loc["instance"]], dtype=object), np.array([loc["label"]]))
        items.append(x)
    return _merged_set(items, "item") if items else None


# ------------------------------------------------------------------- list
# A Python list is a list of items. A single Set or Field is also a collection: of its
# nodes/elements (one row each), so List Item on a set gives one node, as a one-member set.
def _as_list(v) -> list:
    return v if isinstance(v, list) else ([] if v is None else [v])


def _is_collection(v) -> bool:
    return isinstance(v, (EntitySet, Field))


def _size(v) -> int:
    if isinstance(v, Series):  # a single curve: its points
        return len(v.x)
    return len(v.labels) if _is_collection(v) else len(_as_list(v))


def _kind_name(v) -> str:
    return {Series: "curve", Number: "number", Field: "field", EntitySet: "set"}.get(type(v), type(v).__name__)


# ------------------------------------------------------------------- text
def _as_text(v) -> str:
    """How a value reads when put into text."""
    if isinstance(v, Text):
        return v.value
    if isinstance(v, Number):
        return _fmt(v.value)
    if isinstance(v, Bool):
        return "Yes" if v.value else "No"
    if isinstance(v, (EntitySet, Field, Series)):
        return v.name
    if isinstance(v, Odb):
        return v.path
    name = as_name(v)
    if name is not None:
        return name
    return "" if v is None else str(v)


def group_members(items) -> tuple[str, list[EntitySet]]:
    """Nodes or elements from sets, fields and lists of them, as sets (all of one kind)."""
    sets = []
    for item in _flat_items(items):
        if isinstance(item, EntitySet):
            sets.append(item)
        elif isinstance(item, Field):
            sets.append(EntitySet(item.name, item.entity, item.instances, item.labels))
        elif isinstance(item, (Number, Series)) and item.extra.get("location"):  # Reduce Max/Min, an Over Time curve
            loc = item.extra["location"]
            sets.append(EntitySet(getattr(item, "label", "") or getattr(item, "name", "") or "value", loc["entity"],
                                  np.array([loc["instance"]], dtype=object), np.array([loc["label"]])))
        else:
            raise ValueError("Give nodes or elements: sets, fields, a Reduce Max/Min result (where it was found) or "
                             f"an Over Time curve, not a {type(item).__name__}"
                             + (" without a location" if isinstance(item, (Number, Series)) else ""))
    if not sets:
        raise ValueError("Connect sets, fields or a Reduce Max/Min (e.g. Compare's Passing, a Set, Pick from List)")
    kinds = {s.kind for s in sets}
    if len(kinds) > 1:
        raise ValueError("Mix of nodes and elements: use one Display Group for each")
    return kinds.pop(), sets


def _flat_items(v) -> list:
    out = []
    for x in v if isinstance(v, list) else [v]:
        out += _flat_items(x) if isinstance(x, list) else ([] if x is None else [x])
    return out


def _merged_set(v, what: str) -> EntitySet:
    kind, sets = group_members(v)
    inst = np.concatenate([np.asarray(s.instances, dtype=object).astype(str) for s in sets])
    lab = np.concatenate([np.asarray(s.labels, dtype=np.int64) for s in sets])
    keys = np.unique(np.stack([inst, lab.astype(str)], axis=1), axis=0, return_index=True)[1]
    keep = np.sort(keys)
    name = sets[0].name if len(sets) == 1 else f"{len(sets)} {what}s"
    return EntitySet(name, kind, inst[keep].astype(object), lab[keep])


def _finite(params: dict, labels: dict) -> None:
    """Modelling numbers must be real numbers: CAE can't build infinity (a division by zero upstream) or NaN."""
    for k, label in labels.items():
        v = float(params[k])
        if not math.isfinite(v):
            raise ValueError(f"{label} is {'infinite' if math.isinf(v) else 'not a number (NaN)'}: "
                             "a division by zero upstream?")


def _region_model(region):
    return region.source.model if isinstance(region.source, Part) else region.source.part.model


# ------------------------------------------------------- default previews
def _numbers_table(items: list[Number], title: str) -> dict:
    vals = [n.value for n in items]
    if not any(n.label or n.where for n in items):  # plain numbers (e.g. a Formula's a[:, 2]): just the values
        rows = [[i, float(n.value)] for i, n in enumerate(items)]
        return {"kind": "table", "title": title, "columns": ["#", "Value"], "rows": rows, "allRows": rows,
                "total": len(rows), "stats": [{"name": "Value", "min": min(vals), "max": max(vals)}] if vals else []}
    rows = [[i, n.label, float(n.value), n.where] for i, n in enumerate(items)]
    return {"kind": "table", "title": title, "columns": ["#", "Name", "Value", "Where"], "rows": rows,
            "allRows": rows, "total": len(rows),
            "stats": [{"name": "Value", "min": min(vals), "max": max(vals)}] if vals else []}


def _is_matrix(v) -> bool:
    """A list of rows of numbers (or of a table's cells: names and numbers, e.g. a Formula's a[:, :])."""
    return isinstance(v, list) and bool(v) and all(isinstance(r, list) and r and all(isinstance(x, (Number, Text))
                                                                                     for x in r) for r in v)
