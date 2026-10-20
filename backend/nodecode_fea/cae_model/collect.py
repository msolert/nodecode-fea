"""Collecting the graph's model values into CAE objects to build (collect), and their node keys."""

from __future__ import annotations

from dataclasses import replace

from ..nodes import DEFAULT_MODEL, REGISTRY
from ..values import (SHELLING, AnalysisStep, Condition, DatumPlane, Instance, Interaction, Job, Material,
    MergedPart, Model, OutputRequests, Part, PartOperation, Region, Section, Sketch)
from .core import final_part, fingerprint, _flat, is_model_node, ORDER
from .material import copy_code, material_code, section_code
from .regions import combined_region_code, CombinedRegion, region_code
from .geometry import (datum_code, datum_model, model_code, operation_code, part_code, PartFinish, sketch_code,
    split_finish)
from .step import output_code, step_code
from .loads import condition_code
from .interaction import CONTACTS, explicit_model, interaction_code
from .assembly import instance_code, job_code, merge_code, _superseded


def collect(graph: dict, results: dict, node_inputs: dict,
            stages: dict | None = None) -> tuple[list[dict], dict[str, list], list[str]]:
    """The CAE objects the graph's modelling nodes make, in build order: [{key, fp, deps, code, label, value, name}],
    each node's values, and the modelling nodes with an error (or waiting on one), whose objects are kept.

    CAE names must be unique per model: when two different sketches (parts, instances) ask for the same name in one
    model, the first keeps it and the next gets -2, -3... (in graph order); obj["renamed"] says so."""
    outs = getattr(node_inputs, "outputs", {})
    objs: dict[tuple, dict] = {}  # by (kind, model, fingerprint of the value)
    taken: dict[tuple, set] = {}  # (kind, model) -> names in use
    values: dict[str, list] = {}
    broken = []

    all_parts: list = []  # every Part made, once collected: earlier stages resolve to the one built

    def final(part: Part) -> Part:
        """A datum or partition on a part that is then sectioned/meshed belongs on the part as built."""
        return final_part(part, all_parts)

    def finish_after_operations(v: Part) -> None:
        """Sections and mesh of a part with datums/partitions: a step built after all of them."""
        if not v.operations or isinstance(v, MergedPart):
            return
        part = objs[ident(v)]
        code, finish = split_finish(part["code"])
        if not finish:
            return
        where = part["key"].split("|")[1]
        deps = [part["key"]] + [objs[ident(op)]["key"] for op in v.operations if ident(op) in objs]
        part["code"] = code
        me = ("PartFinish",) + ident(v)[1:]
        objs[me] = {"key": f"finish|{where}|{part['name']}", "fp": fingerprint((v, part["name"], deps)), "deps": deps,
                    "code": [f"m = mdb.models[{where!r}]", f"part = m.parts[{part['name']!r}]"] + finish,
                    "label": f"Sections and mesh of {part['name']}", "value": PartFinish(v), "name": part["name"],
                    "renamed": False, "external": False}

    def ident(v, model: str = "") -> tuple:
        if isinstance(v, CombinedRegion):
            source = v.regions[0].source
            where = (source.model if isinstance(source, Part) else source.part.model).name
        else:
            where = (v.name if isinstance(v, Model) else model if isinstance(v, Sketch) else
                     v.model.name if isinstance(v, (Part, Material)) else
                     v.material.model.name if isinstance(v, Section) else
                     v.model.name if isinstance(v, AnalysisStep) else
                     v.step.model.name if isinstance(v, Condition) else
                     v.model.name if isinstance(v, Interaction) else
                     v.model.name if isinstance(v, Job) else
                     v.step.model.name if isinstance(v, OutputRequests) else
                     datum_model(v).name if isinstance(v, DatumPlane) else
                     v.part.model.name if isinstance(v, PartOperation) else
                     ((v.source.model.name if isinstance(v.source, Part) else v.source.part.model.name)
                      if isinstance(v, Region) else v.part.model.name))
        identity = replace(v, operations=()) if isinstance(v, Part) else v
        return type(v).__name__, where, fingerprint(identity)

    def add(v, model: str = "", include_operations: bool = True):
        if isinstance(v, MergedPart):
            add(v.model)
            if v.target is not None:
                add(v.target)
            for instance in v.instances:
                add(instance)
        elif isinstance(v, Part):
            add(v.model)
            for section, _ in v.sections:
                add(section)
            if v.source is not None:
                add(v.source)
            if v.sketch is not None and v.sketch.name:
                add(v.sketch, v.model.name)
        elif isinstance(v, DatumPlane):
            if isinstance(v.part, Part):
                add(final(v.part), include_operations=False)
            else:
                add(v.part)
        elif isinstance(v, PartOperation):
            add(final(v.part), include_operations=False)
            if v.datum is not None:
                add(v.datum)
        elif isinstance(v, Instance):
            # an instance of an earlier stage (Mesh -> Instance while a second Mesh refines that mesh) is of the part
            # as finally built, as Regions are: not a copy
            add(final(v.part))
            for s in v.steps:
                if s[0] == "align":
                    add(s[3].source)
        elif isinstance(v, Region):
            if isinstance(v.source, Part):  # in a load or contact too: on the part as it is finally built
                v = replace(v, source=final(v.source))
            add(v.source)
            if not v.name or v.combine:
                return
        elif isinstance(v, CombinedRegion):
            for region in v.regions:
                add(region.source)
        elif isinstance(v, Material):
            add(v.model)
        elif isinstance(v, Section):
            if not (v.existing or v.copy_from):  # a picked or copied section brings its own material
                add(v.material)
        elif isinstance(v, AnalysisStep):
            add(v.model)
            if v.previous is not None:
                add(v.previous)
        elif isinstance(v, Condition):
            add(v.step)
            if v.previous is not None:
                add(v.previous)
            if v.region is not None:
                add(v.region)
            if v.csys is not None:
                add(v.csys)
        elif isinstance(v, Interaction):
            if v.step is not None:
                add(v.step)
            add(v.model)
            if v.previous is not None:
                add(v.previous)
            for end in (v.master, v.slave):
                if end is not None:
                    add(end)
        elif isinstance(v, Job):
            add(v.source)
        elif isinstance(v, OutputRequests):
            add(v.step)
            if v.region is not None:
                add(v.region)
        if isinstance(v, Sketch) and not (v.name and model):
            return
        me = ident(v, model)
        if me in objs:
            if include_operations and isinstance(v, Part):
                for operation in v.operations:
                    add(operation)
                finish_after_operations(v)
            return
        kind, where, _ = me
        if isinstance(v, CombinedRegion):
            kind = "Region"  # share the CAE Set/Surface namespace with ordinary named Regions
        scope = ""
        if isinstance(v, Region):
            scope = (("part|" + v.source.name) if isinstance(v.source, Part) else "assembly") + "|" + v.form
        elif isinstance(v, CombinedRegion):
            first_source = v.regions[0].source
            scope = (("part|" + first_source.name) if isinstance(first_source, Part) else "assembly") + "|" + v.regions[0].form
        names = taken.setdefault((kind, where, scope), set())
        external = bool(getattr(v, "existing", False))
        name, k = v.name, 2
        # something chosen from CAE is a reference: it keeps its name and takes none (when the graph builds an object
        # of that name, it is that object: picking what the graph made must not rename and delete it)
        while name in names and not external:
            name, k = f"{v.name}-{k}", k + 1
        if not external:
            names.add(name)
        if isinstance(v, Model):
            key, deps, code = f"model|{name}", [], model_code(v)
        elif isinstance(v, Sketch):
            key = f"sketch|{where}|{name}"
            deps, code = [f"model|{where}"], [f"m = mdb.models[{where!r}]"] + sketch_code(v, name)
        elif isinstance(v, MergedPart):
            used = ((v.target,) if v.target is not None else ()) + v.instances
            instance_objs = [objs[ident(instance)] for instance in used]
            key = f"part|{where}|{name}"
            code = merge_code(v, name, list(objs.values()))
            if v.keep_originals:
                deps = [f"model|{where}"] + [o["key"] for o in instance_objs]
            else:
                # DELETE consumes the source instances. Build them inside the Boolean Part's own builder so they
                # are not ledger objects that appear missing and force a rebuild on every live-sync cycle.
                consumed = {o["key"] for o in instance_objs}
                deps = [f"model|{where}"] + [d for o in instance_objs for d in o["deps"] if d not in consumed]
                deps = list(dict.fromkeys(deps))
                code = [line for o in instance_objs for line in o["code"]] + code
                for instance_obj in instance_objs:
                    instance_obj["transient"] = True
        elif isinstance(v, Part):
            sk = objs[ident(v.sketch, where)] if v.sketch is not None and v.sketch.name else None
            key = f"part|{where}|{name}"
            source = objs[ident(v.source)] if v.source is not None else None
            section_objs = [objs[ident(section)] for section, _ in v.sections]
            deps = ([f"model|{where}"] + ([sk["key"]] if sk else []) + ([source["key"]] if source else []) +
                    [o["key"] for o in section_objs])
            section_names = {section: o["name"] for (section, _), o in zip(v.sections, section_objs)}
            # operations are built apart; a shelling one stays known so sections and mesh go on faces
            code = part_code(replace(v, operations=tuple(o for o in v.operations if o.kind in SHELLING)), name,
                             sk["name"] if sk else None, section_names)
        elif isinstance(v, DatumPlane):
            if isinstance(v.part, Part):
                source = objs[ident(final(v.part))]
                key, deps = f"datum|{where}|{source['name']}|{name}", [source["key"]]
            else:
                source = objs[ident(v.part)]
                key, deps = f"datum|{where}|assembly|{name}", [source["key"]]
            code = datum_code(v, name, source["name"], list(objs.values()))
        elif isinstance(v, PartOperation):
            part = objs[ident(final(v.part))]
            datum = objs[ident(v.datum)] if v.datum is not None else None
            key = f"feature|{where}|{part['name']}|{name}"
            deps = [part["key"]] + ([datum["key"]] if datum else [])
            code = operation_code(v, name, part["name"], datum["name"] if datum else "")
        elif isinstance(v, (Material, Section)) and v.existing:  # picked in CAE: referenced, never built
            key, deps, code = f"{type(v).__name__.lower()}|{where}|{name}", [], []
        elif isinstance(v, (Material, Section)) and v.copy_from:  # from a library model, by name
            kind = type(v).__name__.lower()
            key, deps = f"{kind}|{where}|{name}", [f"model|{where}"]
            code = copy_code(v, where)
        elif isinstance(v, Material):
            key, deps, code = f"material|{where}|{name}", [f"model|{where}"], material_code(v, name)
        elif isinstance(v, Section):
            material = objs[ident(v.material)]
            key, deps = f"section|{where}|{name}", [f"model|{where}", material["key"]]
            code = section_code(v, name, material["name"])
        elif isinstance(v, AnalysisStep):
            previous = objs[ident(v.previous)] if v.previous is not None else None
            key = f"step|{where}|{name}"
            deps = [f"model|{where}"] + ([previous["key"]] if previous else [])
            code = step_code(v, name, previous["name"] if previous else "Initial")
        elif isinstance(v, Condition):
            step = objs[ident(v.step)]
            previous = objs[ident(v.previous)] if v.previous is not None else None
            kind_key = ("field" if v.predefined else "film" if v.kind == "Convection" else
                        "bc" if v.kind in ("Fixed", "Displacement / rotation", "Fixed temperature") else "load")
            key = f"condition|{where}|{kind_key}|{name}"
            deps = [step["key"]] + ([previous["key"]] if previous else [])
            if isinstance(v.region, Interaction):
                deps.append(objs[ident(v.region)]["key"])
            elif v.region is not None:
                source = final(v.region.source) if isinstance(v.region.source, Part) else v.region.source
                deps.append(objs[ident(source)]["key"])
                part = source if isinstance(source, Part) else source.part
                deps += [objs[ident(op)]["key"] for op in part.operations]
            if v.csys is not None:
                deps.append(objs[ident(v.csys)]["key"])
            code = condition_code(v, name, step["name"], list(objs.values()))
        elif isinstance(v, Interaction):
            step = objs[ident(v.step)] if v.step is not None else {"key": f"model|{where}", "name": ""}
            previous = objs[ident(v.previous)] if v.previous is not None else None
            kind_key = ("tie" if v.kind == "Tie" else "coupling" if v.point_owner or v.kind == "Equation" else
                        "inertia" if v.kind == "Point mass" else "connector" if v.kind == "Connector" else "contact")
            key = f"interaction|{where}|{kind_key}|{name}"
            deps = [step["key"]] + ([previous["key"]] if previous else [])
            for region in (r for r in (v.master, v.slave) if r is not None):
                if isinstance(region, Interaction):  # a connector ending at a coupling's point
                    deps.append(objs[ident(region)]["key"])
                    continue
                source = final(region.source) if isinstance(region.source, Part) else region.source
                deps.append(objs[ident(source)]["key"])
                part = source if isinstance(source, Part) else source.part
                deps += [objs[ident(op)]["key"] for op in part.operations]
            code = interaction_code(v, name, step["name"], list(objs.values()))
        elif isinstance(v, OutputRequests):
            step = objs[ident(v.step)]
            key, deps = f"output|{where}|{step['name']}", [step["key"]]
            if isinstance(v.region, Interaction):
                deps.append(objs[ident(v.region)]["key"])
            elif v.region is not None:
                source = final(v.region.source) if isinstance(v.region.source, Part) else v.region.source
                deps.append(objs[ident(source)]["key"])
                deps += [objs[ident(op)]["key"] for op in (source if isinstance(source, Part) else source.part).operations]
            code = output_code(v, step["name"], list(objs.values()))
        elif isinstance(v, Job):
            # A job represents the complete model at this point, not only the wire feeding its input. Depending
            # on every collected object in that model makes any geometry, mesh, load or assembly edit rebuild it.
            deps = [o["key"] for o in objs.values() if o["key"].split("|")[1] == where]
            key, code = f"job|{where}|{name}", job_code(v, name)
        elif isinstance(v, CombinedRegion):
            first = v.regions[0]
            part_owner = isinstance(first.source, Part)
            owner_kind = "part" if part_owner else "assembly"
            owner_name = objs[ident(first.source)]["name"] if part_owner else "rootAssembly"
            key = f"region|{where}|{owner_kind}|{owner_name}|{first.form.lower()}|{name}"
            deps = [objs[ident(region.source)]["key"] for region in v.regions]
            for region in v.regions:
                part = region.source if isinstance(region.source, Part) else region.source.part
                deps += [objs[ident(op)]["key"] for op in part.operations]
            deps = list(dict.fromkeys(deps))
            code = combined_region_code(v, name, list(objs.values()))
        elif isinstance(v, Region):
            source = objs[ident(v.source)]
            owner_kind = "part" if isinstance(v.source, Part) else "assembly"
            owner_name = source["name"] if isinstance(v.source, Part) else "rootAssembly"
            key = f"region|{where}|{owner_kind}|{owner_name}|{v.form.lower()}|{name}"
            part = v.source if isinstance(v.source, Part) else v.source.part
            deps = [source["key"]] + [objs[ident(op)]["key"] for op in part.operations]
            code = region_code(v, name, source["name"])
        else:
            mine = final(v.part)
            part = objs[ident(mine)]
            key = f"instance|{where}|{name}"
            deps = [f"model|{where}", part["key"]] + [objs[ident(op)]["key"] for op in mine.operations]
            # aligned onto another instance: rebuilt with it (deleting it deletes the constraint, and this snaps back)
            deps += [objs[ident(s[3].source)]["key"] for s in v.steps if s[0] == "align"]
            code = instance_code(v, name, part["name"], list(objs.values()))
        label = f"{type(v).__name__} {name}"
        objs[me] = {"key": key, "fp": fingerprint((v, name, deps)), "deps": deps, "code": code, "label": label,
                    "value": v, "name": name, "renamed": name != v.name, "external": external}
        if include_operations and isinstance(v, Part):
            for operation in v.operations:
                add(operation)
            finish_after_operations(v)

    # An Instance fed into another Instance is a step of one chain: only the chain's end is built.
    types = {n["id"]: n.get("type") for n in graph.get("nodes", [])}
    chained = {e.get("source") for e in graph.get("edges", []) if types.get(e.get("source")) == "model.instance"
               and types.get(e.get("target")) == "model.instance" and (e.get("targetHandle") or "part") == "part"}
    featured = {e.get("source") for e in graph.get("edges", [])
                if types.get(e.get("target")) in ("model.feature", "model.section", "model.mesh")
                and (e.get("targetHandle") or "part") in ("part", "target")}
    for node in graph.get("nodes", []):
        if not is_model_node(node) or node.get("type") not in REGISTRY:
            continue
        status = results.get(node["id"], {}).get("status")
        if status in ("error", "blocked"):
            broken.append(node["id"])
        if status != "ok" or node["id"] not in outs:
            continue
        vals = [v for v in _flat(list(outs[node["id"]].values()))
                if isinstance(v, (Model, Sketch, Part, Instance, Region, DatumPlane, PartOperation,
                                  AnalysisStep, Condition, Interaction, Job, OutputRequests))]
        if vals:
            values[node["id"]] = vals
    # Jobs go last even if their nodes were placed earlier, so they depend on the complete collected model.
    ordered_values = list(values.items())
    ordered_values.sort(key=lambda item: any(isinstance(v, Job) for v in item[1]))
    all_parts[:] = [v for vals in values.values() for v in vals if isinstance(v, Part)]
    for nid, vals in ordered_values:
        combined = [v for v in vals if isinstance(v, Region) and v.combine and v.name]
        if combined:
            groups = {}
            for region in combined:
                owner = (("part", fingerprint(region.source)) if isinstance(region.source, Part) else
                         ("assembly", region.source.part.model.name))
                key = (region.name, region.form, region.kind, region.side, owner)
                groups.setdefault(key, []).append(region)
            for regions in groups.values():
                add(CombinedRegion(regions[0].name, tuple(regions)))
        for v in vals:
            if not isinstance(v, (Model, Sketch, Part, Instance, Region, DatumPlane, PartOperation,
                                  AnalysisStep, Condition, Interaction, Job, OutputRequests)):
                continue
            if isinstance(v, Region):
                if isinstance(v.source, Part):  # on the part as it is finally built
                    v = replace(v, source=final_part(v.source, all_parts))
                add(final(v.source.part) if isinstance(v.source, Instance) else v.source)
                add(v)
                continue
            if isinstance(v, Part):  # every stage and branch of a part builds that one part, with all its edits
                v = final_part(v, all_parts)
            if isinstance(v, Instance) and v.removed:
                add(final(v.part))  # keep its Part available, but deliberately omit this placement
            elif isinstance(v, Instance) and (nid in chained or _superseded(v, values)):
                add(final(v.part))  # a step of a chain: its part (and model) still count
            elif not isinstance(v, Sketch):
                add(v)
    # a sketch with a Model is built there too (its parts may be elsewhere); one with no Model and no part using
    # it goes into NoCode-Model
    for v in [x for vals in values.values() for x in vals if isinstance(x, Sketch) and x.name]:
        home = v.model or (None if any(o["value"] == v for o in objs.values()) else Model(DEFAULT_MODEL))
        if home is not None:
            add(home)
            add(v, home.name)
    # a load, interaction or output on a Region made on a part goes on that part's instances: it depends on them
    instance_keys = [(o["value"].part, o["key"]) for o in objs.values() if isinstance(o["value"], Instance)]
    for o in objs.values():
        v = o["value"]
        regions = ([v.region] if isinstance(v, (Condition, OutputRequests)) else
                   [v.master, v.slave] if isinstance(v, Interaction) else [])
        parts = [r.source for r in regions if isinstance(r, Region) and isinstance(r.source, Part)]
        extra = [k for p in parts for ip, k in instance_keys
                 if final_part(ip, all_parts) == final_part(p, all_parts) and k not in o["deps"]]
        stepwise_value = isinstance(v, (Condition, Interaction)) and v.stepwise
        # contact in an Abaqus/Explicit model is its Explicit type: built after the steps, rebuilt when they change
        explicit_contact = (isinstance(v, Interaction) and v.kind in CONTACTS and
                            explicit_model(v.model.name, list(objs.values())))
        stepwise_value = stepwise_value or explicit_contact
        if (isinstance(v, Condition) and v.kind == "Bolt load" and v.values[1]) or stepwise_value:
            # fixed at its length in every later step, or changing by step: built after them all (and again when
            # one changes), knowing them all (later steps aren't upstream of it)
            model = v.step.model if isinstance(v, Condition) else v.model
            extra += [x["key"] for x in objs.values() if isinstance(x["value"], AnalysisStep)
                      and x["value"].model == model and x["key"] not in o["deps"] + extra]
        if stepwise_value:
            everything = list(objs.values())
            o["code"] = (condition_code(v, o["name"], "", everything) if isinstance(v, Condition) else
                         interaction_code(v, o["name"], "", everything))
        if extra:
            o["deps"] = o["deps"] + extra
            o["fp"] = fingerprint((v, o["name"], o["deps"]))
    for nid in featured:  # an intermediate Part superseded by Part Feature is not built (shown as what it becomes)
        stage = values.pop(nid, None)
        if stage is not None and stages is not None:
            stages[nid] = stage
    # Dependency-aware order is required for assembly operations such as Instance boolean merge, whose output is
    # a Part but whose inputs are Instances. ORDER remains the stable tie-breaker for unrelated objects.
    pending, ordered, emitted = list(objs.values()), [], set()
    while pending:
        ready = [o for o in pending if all(d in emitted or not any(x["key"] == d for x in pending) for d in o["deps"])]
        if not ready:  # defensive fallback; graph validation should prevent dependency cycles
            ready = pending[:1]
        ready.sort(key=lambda o: ORDER[type(o["value"])])
        for o in ready:
            pending.remove(o)
            ordered.append(o)
            emitted.add(o["key"])
    return ordered, values, broken


def node_keys(values: dict, objects: list[dict], types: dict | None = None) -> dict[str, list[str]]:
    """The CAE objects each modelling node made itself (not its upstream ones): what to keep while it's broken.
    A Part Feature's output is the part; what it made is the part's last operation."""
    out = {}
    for nid, vals in values.items():
        if (types or {}).get(nid) == "model.feature":
            vals = [v.operations[-1] for v in vals if isinstance(v, Part) and v.operations]
        out[nid] = [o["key"] for o in objects if not o.get("external") and not o.get("transient") and
                    (any(o["value"] == v for v in vals)
                     or (isinstance(o["value"], CombinedRegion)
                         and all(region in vals for region in o["value"].regions)))]
    return out


def picked_value(graph: dict, node_inputs) -> tuple | None:
    """(node id, value) of a clicked Input that is a sketch, part or instance picked in CAE (shown as it is)."""
    focus = graph.get("focus")
    node = next((n for n in graph.get("nodes", []) if n.get("id") == focus), None)
    if node is None or node.get("type") != "input.value":
        return None
    v = getattr(node_inputs, "outputs", {}).get(focus, {}).get("value")
    return (focus, v) if isinstance(v, (Sketch, Part, Instance)) and v.existing else None
