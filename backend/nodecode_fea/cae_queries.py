"""Small in-process cache for geometry measurements returned by the live CAE kernel."""

from __future__ import annotations

import hashlib
import json

from .values import Number, PendingMeasure

_values: dict[str, object] = {}  # a number, or a list (one value / point per face, edge… of a Region)
_revision = 0
MARKER = "__NODECODE_FEA_MEASURE__"


def key(quantity, first, second=None) -> str:
    return hashlib.sha1(repr((quantity, first, second)).encode()).hexdigest()[:20]


def value(quantity, first, second=None):
    """A Number, a list (as CAE answered it: numbers, or [x, y, z] rows), or a PendingMeasure until CAE answers."""
    k = key(quantity, first, second)
    if k in _values:
        v = _values[k]
        return Number(v, quantity) if isinstance(v, float) else v
    return PendingMeasure(k, quantity, first, second)


def _number(v):
    """CAE's answer as floats (None, e.g. the radius of a straight edge: NaN)."""
    if isinstance(v, list):
        return [_number(x) for x in v]
    return float("nan") if v is None else float(v)


def revision(*_args):
    return _revision


def ingest(output: str) -> bool:
    global _revision
    changed = False
    for line in (output or "").splitlines():
        if not line.startswith(MARKER):
            continue
        data = json.loads(line[len(MARKER):])
        for k, v in data.items():
            value = _number(v)
            if repr(_values.get(k)) != repr(value):
                _values[k] = value
                changed = True
    if changed:
        _revision += 1
    return changed
