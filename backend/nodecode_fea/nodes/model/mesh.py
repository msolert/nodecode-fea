"""Mesh: seeds, mesh controls and element types."""

from __future__ import annotations

import math
from dataclasses import replace

from ...values import Instance, Part, Region
from ..core import Param, Port, register
from ..common import _finite
from .geometry import _abaqus_name, _item, _model_value_display, _numbered
from .material import (ELEMENT_FAMILIES, ELEMENT_SHAPES, ELEMENT_TYPES, MESH_BIAS, MESH_SEEDS, MESH_SHAPES,
                       MESH_TECHNIQUES)


@register("model.mesh", "Mesh", "Model",
          "Seed, control, type and generate a mesh on a Part or Region. An Edge Region gets a local seed: by size, "
          "by number of elements, or biased (graded, finer toward an end, both ends or the middle: mesh refinement "
          "near a hole, fillet or contact); a Face or Cell Region restricts controls and element type, and can seed "
          "its own edges (Face/Cell Region seed: Its edges) to refine a local area. A picked CAE "
          "part is copied before meshing. Lists of parts, regions, sizes or settings create corresponding meshes "
          "(mesh convergence studies). Measure gives the node and element counts and the mesh quality.",
          inputs=[Port("target", "any", "Part or region", depth=0)], outputs=[Port("part", "part", "Part")],
          params=[Param("name", "text", "", "Copy name",
                        help="For a picked CAE part, the editable copy. Empty: original-name-copy."),
                  Param("seed", "select", "By size", "Seed", options=MESH_SEEDS,
                        help="By number and Biased seed edges: the Edge Region's, or By number on every edge of the "
                             "part. Biased needs an Edge Region."),
                  Param("size", "number", 10.0, "Seed size", when={"seed": ["By size"]}),
                  Param("number", "int", 10, "Elements per edge", when={"seed": ["By number", "Biased"]}),
                  Param("seed_on", "select", "Whole part", "Face/Cell Region seed", options=["Whole part", "Its edges"],
                        when={"seed": ["By size", "By number"]},
                        help="With a Face or Cell Region: Whole part seeds the part as usual (the Region only gets "
                             "the controls and element type); Its edges seeds just the edges around those faces or "
                             "cells, finer (local refinement). An Edge Region always seeds its edges."),
                  Param("ratio", "number", 4.0, "Bias ratio", when={"seed": ["Biased"]},
                        help="Largest over smallest element along the edge"),
                  Param("bias", "select", "Fine at start", "Finer", options=MESH_BIAS, when={"seed": ["Biased"]},
                        help="Start and end follow each edge's own direction: swap them if it comes out backwards"),
                  Param("deviation", "number", 0.1, "Deviation factor", when={"seed": ["By size"]}),
                  Param("minimum", "number", 0.1, "Minimum size factor", when={"seed": ["By size"]}),
                  Param("technique", "select", "Automatic", "Technique", options=MESH_TECHNIQUES),
                  Param("shape", "select", "Automatic", "Element shape", options=MESH_SHAPES),
                  Param("element", "select", "Automatic", "Element type", options=ELEMENT_TYPES),
                  Param("generate", "select", "Yes", "Generate", options=["Yes", "No"])],
          display=lambda o, i, p: _model_value_display(o["part"]))
def model_mesh(inputs, params):
    target = inputs["target"]
    region = target if isinstance(target, Region) else None
    part = region.source if region is not None else target
    if isinstance(part, Instance):
        raise ValueError("Mesh works on Parts or Regions on a Part, not assembly instances")
    if not isinstance(part, Part):
        raise ValueError("Mesh needs a Part or Region")
    if part.mesh_file:
        raise ValueError(f"{part.name} is already a mesh (imported from an input file): use it directly")
    _finite(params, {"size": "Seed size", "deviation": "Deviation factor", "minimum": "Minimum size factor"})
    size, deviation, minimum = float(params["size"]), float(params["deviation"]), float(params["minimum"])
    if size <= 0:
        raise ValueError("Seed size must be positive")
    if not 0 < deviation <= 1 or not 0 < minimum <= 1:
        raise ValueError("Deviation and minimum size factors must be greater than 0 and at most 1")
    seed = str(params.get("seed") or "By size")
    number, ratio, bias = int(params.get("number") or 0), float(params.get("ratio") or 0), str(params.get("bias") or "")
    if seed != "By size":
        if number < 1:
            raise ValueError("Elements per edge must be at least 1")
        if seed == "Biased":
            if region is None or region.kind != "edge":
                raise ValueError("Biased seeding needs an Edge Region (the edges to grade)")
            if not math.isfinite(ratio) or ratio < 1:
                raise ValueError("Bias ratio is the largest over the smallest element: 1 or more")
            if bias in ("Fine at both ends", "Fine in the middle") and number < 2:
                raise ValueError("Double bias needs at least 2 elements per edge")
    if part.rigid == "Analytical":
        raise ValueError(f"{part.name} is an analytical rigid surface: it is exact and has no mesh")
    shape, element = str(params["shape"]), str(params["element"])
    allowed_shapes = {"Wire": {"Automatic"}, "Shell": {"Automatic", "Quad", "Quad-dominated", "Tri"},
                      "Solid": {"Automatic", "Hex", "Hex-dominated", "Tet"}}[part.effective_body]
    if shape not in allowed_shapes:
        raise ValueError(f"{part.effective_body} parts take {' or '.join(sorted(allowed_shapes))} element shapes")
    space = getattr(part, "space", "3D")
    prefixes, automatic, automatic_tri = ELEMENT_FAMILIES[(space, part.effective_body, part.rigid)]
    if element == "Automatic":
        element = automatic_tri if shape == "Tri" else automatic
        if part.effective_body == "Solid" and shape == "Tet":
            element = "C3D4"
    if not element.startswith(prefixes):
        what = (f"{part.rigid.lower()} rigid " if part.rigid else "") + (
            f"{space.lower()} " if space != "3D" else "") + part.effective_body.lower()
        raise ValueError(f"Element type {element} does not fit a {what} part: use "
                         f"{' or '.join(p + '…' for p in prefixes)} elements")
    family = ELEMENT_SHAPES.get(element)
    if family and shape not in ("Automatic", family, family + "-dominated"):
        raise ValueError(f"{element} is a {family.lower()} element: it doesn't fit a {shape.lower()} mesh (CAE would "
                         f"ignore it and use its default element). Choose a {shape.split('-')[0].lower()} element, or "
                         f"the {family} shape")
    selector = None if region is None else (region.kind, region.indices, region.points, region.tolerance)
    if region is not None and region.kind in ("vertex", "reference point"):
        raise ValueError("Mesh targeting needs an Edge, Face or Cell Region")
    name, source, existing = part.name, part.source, part.existing
    if part.existing:  # a picked CAE part is copied, not changed: the copy gets a name (one per item of a list)
        name = _numbered(_abaqus_name(str(params.get("name") or f"{part.name}-copy"), f"{part.name}-copy"), params,
                         model=part.model)
        source, existing = part, False
    seeding = () if seed == "By size" else (seed, number) + ((ratio, bias) if seed == "Biased" else ())
    on_edges = params.get("seed_on") == "Its edges" and region is not None and region.kind in ("face", "cell")
    feature = ("Mesh", selector, size, deviation, minimum, str(params["technique"]), shape, element,
               params["generate"] == "Yes", seeding) + (("edges",) if on_edges else ())
    return {"part": replace(part, name=name, existing=existing, source=source,
                            features=part.features + (feature,), item=_item(part, params))}
