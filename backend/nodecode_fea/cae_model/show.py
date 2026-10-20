"""Which CAE module and viewport content each modelling node is shown with."""

from __future__ import annotations

from ..values import (AnalysisStep, Condition, DatumPlane, Instance, Interaction, Job, Model, Part, PartOperation,
    Region, Sketch)
from .core import ASSEMBLY, built, final_part, fingerprint, _flat, is_model_node, PART, SKETCH
from .regions import REPOS, selection_code
from .geometry import datum_model


# The CAE module each node's result is shown in, as a CAE user would look at it.
MODULES = {"model.model": ASSEMBLY, "model.instance": ASSEMBLY, "model.part": PART, "model.datum": PART,
           "model.feature": PART, "model.section": "Property", "model.mesh": "Mesh", "model.region": PART,
           "model.merge_instances": PART, "model.sketch": SKETCH, "model.step": "Step"}
MODULES["model.condition"] = "Load"
MODULES["model.interaction"] = "Interaction"
MODULES["model.job"] = "Job"

def _independent(part: Part, objects: list[dict]) -> bool:
    """Is the part placed as an independent instance (its mesh then lives on the assembly)?"""
    parts = [o["value"] for o in objects]
    mine = final_part(part, parts)
    return any(isinstance(o["value"], Instance) and not o["value"].dependent
               and final_part(o["value"].part, parts) == mine for o in objects)


def _regions_on_assembly(regions: list, objects: list[dict]) -> bool:
    """Are these Regions shown on the assembly (on an instance, or on more than one part/instance)?"""
    return (not isinstance(regions[0].source, Part)
            or len({built(r.source, objects)["key"] for r in regions}) > 1)


def module_for(node_type: str, first, objects: list[dict], vals: list | None = None) -> str:
    if node_type == "input.value":  # a sketch, part or instance picked in CAE
        return SKETCH if isinstance(first, Sketch) else PART if isinstance(first, Part) else ASSEMBLY
    if node_type == "model.region" and isinstance(first, Region) and _regions_on_assembly(
            [v for v in (vals or [first]) if isinstance(v, Region)], objects):
        return ASSEMBLY
    if node_type == "model.datum" and isinstance(first, DatumPlane) and not isinstance(first.part, Part):
        return ASSEMBLY
    return MODULES[node_type]


def _display(*settings: str) -> list[str]:
    """Viewport display options, each on its own (an option a CAE version lacks doesn't stop the rest)."""
    lines = []
    for setting in settings:
        lines += ["try:", f"    vp.{setting}", "except Exception:", "    pass"]
    return lines
SHOW_RANK = {"model.model": 0, "model.sketch": 1, "model.part": 2, "model.datum": 2, "model.feature": 2,
             "model.merge_instances": 2, "model.section": 3, "model.mesh": 3,
             "model.region": 3, "model.instance": 3}
SHOW_RANK["model.step"] = 4
SHOW_RANK["model.condition"] = 5
SHOW_RANK["model.interaction"] = 6
SHOW_RANK["model.job"] = 7


SHOWN_AS = ((Region, "model.region"), (Sketch, "model.sketch"), (Part, "model.part"), (Instance, "model.instance"),
            (DatumPlane, "model.datum"), (PartOperation, "model.feature"), (AnalysisStep, "model.step"),
            (Condition, "model.condition"), (Interaction, "model.interaction"), (Job, "model.job"),
            (Model, "model.model"))


def passed_on(graph: dict, node_inputs, objects: list[dict]) -> tuple | None:
    """(node type to show them as, values) of a clicked general node (Merge, Pick from List, Compare…) passing on
    modelling values already built: shown in CAE as the modelling node that made them would be (all of them)."""
    focus = graph.get("focus")
    node = next((n for n in graph.get("nodes", []) if n.get("id") == focus), None)
    if node is None or is_model_node(node) or node.get("type") == "input.value":
        return None
    vals = _flat(list(getattr(node_inputs, "outputs", {}).get(focus, {}).values()))
    kind = next((t for cls, t in SHOWN_AS if vals and isinstance(vals[0], cls)), None)
    if kind is None:
        return None
    try:
        for v in vals:
            if not isinstance(v, (Model, Sketch, Part, Instance)) or not v.existing:
                built(v.source if isinstance(v, Region) else v, objects)
    except (LookupError, StopIteration, AttributeError):
        return None
    return kind, vals


def shown_node(graph: dict, values: dict, previous: dict | None, stages: dict | None = None) -> str | None:
    """The modelling node CAE shows: the one clicked last, else the one that changed (furthest downstream)."""
    if graph.get("focus") in values or graph.get("focus") in (stages or {}):
        return graph["focus"]
    kinds = {n["id"]: n["type"] for n in graph.get("nodes", []) if n["id"] in values}
    changed = [nid for nid in kinds if (previous or {}).get(nid) != fingerprint(values[nid])]
    return max(changed, key=lambda nid: SHOW_RANK[kinds[nid]], default=None)


def shown_sketch(v, objects: list[dict]) -> tuple[str, str] | None:
    """(model, name) of the sketch a shown value is, as built in CAE; None for anything else. CAE's Sketch module
    only shows a sketch opened from its Sketch list (a GUI procedure: the kernel can't), so the link plug-in picks it
    there; the kernel's displayedObject alone leaves the module empty."""
    if not isinstance(v, Sketch):
        return None
    if v.existing:
        return v.model.name, v.name
    mine = [o for o in objects if o["value"] == v]
    o = next((o for o in mine if v.model and o["key"].split("|")[1] == v.model.name), mine[0])
    return o["key"].split("|")[1], o["name"]


def show_code(node_type: str, vals: list, objects: list[dict]) -> list[str]:
    """Kernel lines that show a modelling node's result (the first, for a list) in the current viewport ``vp``."""
    return _show_code(node_type, vals, objects)


def _step_name(step, objects: list[dict]) -> str:
    return built(step, objects)["name"] if step is not None else "Initial"


def _show_regions(regions: list, objects: list[dict]) -> list[str]:
    """Show and highlight every Region a node gave (a list or matrix of them, not only the first): on their part
    when they all are on one part, else on the assembly, a Region made on a part on each instance of it."""
    first = regions[0].source
    on_part = not _regions_on_assembly(regions, objects)
    if on_part:
        lines = [f"vp.setValues(displayedObject=mdb.models[{first.model.name!r}]"
                 f".parts[{built(first, objects)['name']!r}])"]
    else:
        model = first.model if isinstance(first, Part) else first.part.model
        lines = [f"vp.setValues(displayedObject=mdb.models[{model.name!r}].rootAssembly)"]
    lines += ["_highlight_state = _ledger()", "_highlight_module = sys.modules['nocode_ledger']",
              "for _old in getattr(_highlight_module, 'highlighted', []):",
              "    try:", "        unhighlight(object=_old)", "    except Exception:", "        pass",
              "_highlight_module.highlighted = []"]
    for n, region in enumerate(regions, 1):
        selected, _ = selection_code(region, objects, "")
        if not on_part and isinstance(region.source, Part):  # the part's entities, on each of its instances
            part = built(region.source, objects)
            repo = REPOS[region.kind]
            selected = (f"[_e for _s in _on_instances({part['key'].split('|')[1]!r}, {part['name']!r}, {repo!r}, "
                        f"{selected}, {repo!r}).values() for _e in _s]")
        which = f" {n} of {len(regions)}" if len(regions) > 1 else ""
        # a later feature (fillet, cut…) can consume what the Region found: show the rest anyway
        lines += ["try:", f"    _highlight_module.highlighted += list({selected})",
                  "except Exception as _gone:",
                  f"    _say('{region.kind.title()} Region{which} no longer matches the current geometry (%s); showing "
                  "it without highlighting' % _gone)"]
    return lines + ["for _selected in _highlight_module.highlighted:", "    highlight(object=_selected)",
                    "try:", "    vp.view.fitView()", "except Exception:", "    pass"]


def _show_code(node_type: str, vals: list, objects: list[dict]) -> list[str]:
    v = vals[0]
    regions = [x for x in vals if isinstance(x, Region)]
    if isinstance(v, Region):
        return _show_regions(regions, objects)
    if isinstance(v, AnalysisStep):  # the Step module, on this step
        return [f"vp.setValues(displayedObject=mdb.models[{v.model.name!r}].rootAssembly)",
                *_display(f"assemblyDisplay.setValues(step={_step_name(v, objects)!r})")]
    if isinstance(v, Interaction):  # the Interaction module: constraints and connectors live in Initial
        return [f"vp.setValues(displayedObject=mdb.models[{v.model.name!r}].rootAssembly)",
                *_display(f"assemblyDisplay.setValues(step={_step_name(v.step, objects)!r})",
                          "assemblyDisplay.setValues(interactions=ON, constraints=ON, connectors=ON)")]
    if isinstance(v, Condition):  # the Load module, on the condition's step
        return [f"vp.setValues(displayedObject=mdb.models[{v.step.model.name!r}].rootAssembly)",
                *_display(f"assemblyDisplay.setValues(step={_step_name(v.step, objects)!r})",
                          "assemblyDisplay.setValues(loads=ON, bcs=ON, fields=ON)")]
    if isinstance(v, Job):
        return [f"vp.setValues(displayedObject=mdb.models[{v.model.name!r}].rootAssembly)"]
    if isinstance(v, Sketch):  # the link plug-in then opens it from the Sketch module's Sketch list (shown_sketch)
        model, name = shown_sketch(v, objects)
        lines = [f"vp.setValues(displayedObject=mdb.models[{model!r}].sketches[{name!r}])"]
    elif isinstance(v, Part) and node_type == "model.mesh" and _independent(v, objects):
        # an independent instance carries its own mesh: the Mesh module shows it on the assembly
        lines = [f"vp.setValues(displayedObject=mdb.models[{v.model.name!r}].rootAssembly)",
                 *_display("assemblyDisplay.setValues(mesh=ON)")]
    elif isinstance(v, Part):
        name = v.name if v.existing else built(v, objects)["name"]
        lines = [f"vp.setValues(displayedObject=mdb.models[{v.model.name!r}].parts[{name!r}])"]
        if node_type == "model.mesh":
            lines += _display("partDisplay.setValues(mesh=ON)")
        elif node_type == "model.section":
            lines += _display("partDisplay.setValues(sectionAssignments=ON)")
    elif isinstance(v, DatumPlane) and not isinstance(v.part, Part):
        lines = [f"vp.setValues(displayedObject=mdb.models[{datum_model(v).name!r}].rootAssembly)"]
    elif isinstance(v, (DatumPlane, PartOperation)):
        part = built(v.part, objects)
        lines = [f"vp.setValues(displayedObject=mdb.models[{v.part.model.name!r}].parts[{part['name']!r}])"]
    else:
        model = v.name if isinstance(v, Model) else v.part.model.name
        lines = [f"vp.setValues(displayedObject=mdb.models[{model!r}].rootAssembly)"]
    return lines + ["try:", "    vp.view.fitView()", "except Exception:", "    pass"]
