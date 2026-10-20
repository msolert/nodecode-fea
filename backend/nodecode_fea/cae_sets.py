"""The Sets and Surfaces defined in the running CAE, for a Region to take over ("From a CAE set").

A set made in CAE stores geometry by index ("face 12 of this part"), which points somewhere else once NodeCode FEA
rebuilds the part. So a set comes into the graph as Find-at points: one point on each of its faces, edges, vertices
or cells (CAE's own pointOn), which a Region finds again on the rebuilt geometry. A part's sets give points in the
part's coordinates and an assembly's in global ones, split per instance and per kind of geometry, since a Region is
one kind of geometry on one Part or Instance.
"""

from __future__ import annotations

import inspect
import json

from .cae_link import MACHINE_OUTPUT

MARK = MACHINE_OUTPUT[2]  # __NODECODE_FEA_OBJECTS__: kept out of CAE's message area and the Log


def defined_sets(mdb):
    """[{model, part, instance, name, form, kind, count, value}] for every geometry Set and Surface of every model:
    part ones (part set, instance "") and assembly ones (part "", one entry per instance). Runs inside CAE (Python 2
    or 3); mesh-only sets (nodes, elements) are left out."""
    kinds = (("face", "faces"), ("edge", "edges"), ("vertex", "vertices"), ("cell", "cells"))
    out = []

    def point(entity):
        p = entity.pointOn
        p = p[0] if isinstance(p[0], (tuple, list)) else p
        return ",".join("%.16g" % float(c) for c in p[:3])

    def add(model, part, repo, form):
        for name in sorted(repo.keys()):
            if name.startswith("_"):  # CAE's and NodeCode FEA's internal ones
                continue
            groups = {}
            for kind, attr in kinds:
                try:
                    entities = list(getattr(repo[name], attr) or ())
                except Exception:  # a Surface has no vertices or cells
                    continue
                for entity in entities:
                    instance = "" if part else (getattr(entity, "instanceName", None) or "")
                    points = groups.setdefault((instance, kind), [])
                    p = point(entity)
                    if p not in points:
                        points.append(p)
            for (instance, kind), points in sorted(groups.items()):
                out.append({"model": model, "part": part, "instance": instance, "name": name, "form": form,
                            "kind": kind, "count": len(points), "value": "@" + ";".join(points)})

    for model_name in sorted(mdb.models.keys()):
        model = mdb.models[model_name]
        for part_name in sorted(model.parts.keys()):
            part = model.parts[part_name]
            add(model_name, part_name, part.sets, "Set")
            add(model_name, part_name, part.surfaces, "Surface")
        add(model_name, "", model.rootAssembly.sets, "Set")
        add(model_name, "", model.rootAssembly.surfaces, "Surface")
    return out


def script() -> str:
    """The CAE script printing defined_sets(mdb) as one machine line."""
    return "\n".join(["# -*- coding: utf-8 -*-", "from __future__ import print_function", "import json",
                      "from abaqus import mdb", "", inspect.getsource(defined_sets),
                      f"print({MARK!r} + json.dumps(defined_sets(mdb)))", ""])


def parse(output: str) -> list[dict] | None:
    line = next((ln for ln in output.splitlines() if ln.startswith(MARK)), None)
    return None if line is None else json.loads(line[len(MARK):])
