"""Settings that change from step to step, and the steps something is active in.

A per-step setting is typed as its value from where the object is created, then `Step: value` changes, each
holding until the next: `-9810; Horizon: 0` (an amplitude's own `;`-separated rows work the same way:
`0, 0; 1, 1; Wind: 0, 1; 1, 0`). Active in lists steps and ranges: `Zenith, Wind`, `Zenith to Horizon`,
`Horizon to end`, `Initial` (boundary conditions and predefined fields). Empty: from the node's Step on.

Step names are only known once the whole graph has run (a load wired to its first step doesn't see the later
ones), so nodes keep names as typed; ``plan()`` resolves them against the model's ordered step list (with
"Initial" first), both to check them (the engine's late pass) and to write the CAE calls.
"""

from __future__ import annotations

import re

INITIAL = "Initial"
# a step change starts at the beginning or after a ';' with a name (it starts with a letter) and a ':'
_MARK = re.compile(r"(?:^|;)\s*([A-Za-z][^;:]*?)\s*:")


def split(text) -> tuple[str, tuple[tuple[str, str], ...]]:
    """'0; Horizon: -9810; Wind: 0' -> ('0', (('Horizon', '-9810'), ('Wind', '0')))."""
    raw = "" if text is None else str(text)
    marks = list(_MARK.finditer(raw))
    if not marks:
        return raw.strip(), ()
    base = raw[:marks[0].start()].strip().strip(";").strip()
    changes = []
    for k, m in enumerate(marks):
        end = marks[k + 1].start() if k + 1 < len(marks) else len(raw)
        changes.append((m.group(1).strip(), raw[m.end():end].strip().strip(";").strip()))
    return base, tuple(changes)


def has_changes(text) -> bool:
    return bool(split(text)[1])


def join(base: str, changes) -> str:
    """The text for a base value and (step, value) changes (the inverse of split)."""
    parts = [str(base).strip()] if str(base).strip() else []
    parts += [f"{step}: {value}".rstrip() for step, value in changes]
    return "; ".join(parts)


def parse_active(text) -> tuple:
    """'Zenith to Horizon, Wind' -> (('range', 'Zenith', 'Horizon'), ('step', 'Wind')); 'X to end' / 'X to' / 'X on'
    give ('range', 'X', None); 'all' (every step) gives ('range', None, None). Empty: ()."""
    items = []
    for item in (s.strip() for s in re.split(r"[,;\n]+", str(text or "")) if s.strip()):
        low = item.lower()
        if low in ("all", "every step", "always"):
            items.append(("range", None, None))
            continue
        m = re.fullmatch(r"(.+?)\s+(?:to|-|–|until)\s+(.+)", item, flags=re.I)
        if m:
            last = m.group(2).strip()
            items.append(("range", m.group(1).strip(), None if last.lower() in ("end", "the end", "last") else last))
            continue
        m = re.fullmatch(r"(?:from\s+)?(.+?)(?:\s+(?:to|on|onwards))", item, flags=re.I)
        if m or low.startswith("from "):
            items.append(("range", (m.group(1) if m else item[5:]).strip(), None))
            continue
        items.append(("step", item))
    return tuple(items)


def format_active(active: list[str], names: list[str]) -> str:
    """The shortest Active in text for these step names, in model order (runs become ranges)."""
    on = [n in active for n in names]
    out, i = [], 0
    while i < len(names):
        if not on[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(names) and on[j + 1]:
            j += 1
        if j == len(names) - 1 and j > i:
            out.append(f"{names[i]} to end")
        elif j > i:
            out.append(f"{names[i]} to {names[j]}")
        else:
            out.append(names[i])
        i = j + 1
    return ", ".join(out)


def _index(name: str, names: list[str], what: str) -> int:
    folded = [n.casefold() for n in names]
    key = name.strip().casefold()
    if key in folded:
        return folded.index(key)
    if key.lstrip("+-").isdigit():  # a step number: 1 = the first analysis step
        k = int(key)
        if 1 <= k < len(names):
            return k
    raise ValueError(f"{what}: Step '{name}' not found (steps: {', '.join(names)})")


def active_mask(items: tuple, names: list[str], start: str) -> list[bool]:
    """Which of the model's steps (names, Initial first) it is active in; no items: from start to the end."""
    if not items:
        first = _index(start, names, "Step")
        return [i >= first for i in range(len(names))]
    mask = [False] * len(names)
    for item in items:
        if item[0] == "step":
            mask[_index(item[1], names, "Active in")] = True
            continue
        _, a, b = item
        lo = 1 if a is None else _index(a, names, "Active in")
        hi = len(names) - 1 if b is None else _index(b, names, "Active in")
        if hi < lo:
            raise ValueError(f"Active in: '{a} to {b}' runs backwards")
        for i in range(lo, hi + 1):
            mask[i] = True
    return mask


def plan(names: list[str], start: str, active: tuple, base: dict, changes: tuple, what: str = "",
         initial: bool = False) -> list[tuple]:
    """What to do in each step, in order: (step, "create", values in effect, copy number 0, 1, ...),
    (step, "change", {setting: value}) and (step, "off", None).

    names: the model's steps, Initial first. start: the node's own Step (where it starts without Active in).
    changes: ((setting, ((step, value), ...)), ...), each holding from its step on (also through steps it is off
    in). initial: may it exist in the Initial step."""
    mask = active_mask(active, names, start)
    if mask[0] and not initial:
        raise ValueError(f"{what or 'It'} can't be in the Initial step (Abaqus makes loads in analysis steps); "
                         "start it in the first step")
    if not any(mask):
        raise ValueError("Active in names no step")
    by_step: dict[int, dict] = {}
    for key, seq in changes:
        for step, value in seq:
            by_step.setdefault(_index(step, names, what or key), {})[key] = value
    current, out, on, copies = dict(base), [], False, 0
    for i, name in enumerate(names):
        here = by_step.get(i, {})
        current.update(here)  # a change in a step it is off in holds for when it is on again
        if mask[i]:
            if not on:
                out.append((name, "create", dict(current), copies))
                copies += 1
            elif here:
                out.append((name, "change", dict(here)))
            on = True
        elif on:
            out.append((name, "off", None))
            on = False
    return out


def copy_name(name: str, step: str, copy: int) -> str:
    """The CAE name of a condition turned on again in a later step (the first one keeps its own name)."""
    return name if copy == 0 else f"{name}-from-{step}"


def model_steps(values) -> dict[str, list[str]]:
    """Each model's analysis steps in order (Initial first), from every AnalysisStep among values."""
    from .values import AnalysisStep

    found: dict[str, dict] = {}

    def walk(v):
        if isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, AnalysisStep):
            while v is not None:
                found.setdefault(v.model.name, {}).setdefault(v.name, v)
                v = v.previous
    walk(list(values))
    out = {}
    for model, steps in found.items():
        depth = {}

        def d(s):
            if s.name not in depth:
                depth[s.name] = 0 if s.previous is None else d(s.previous) + 1
            return depth[s.name]
        ordered = sorted(steps.values(), key=d)
        out[model] = [INITIAL] + [s.name for s in ordered]
    return out
